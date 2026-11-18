"""
Shared by anime-nfo.py and anime-rename.py: AniList/AniDB/TVDB id mapping, TVDB and
AniList lookups, reading episode numbers and release tags out of anime filenames, and
probing files with ffprobe.

Only the Python standard library is used. The two id lists are downloaded once a day
to ~/.cache/anime-nfo.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape

FRIBB_URL = "https://raw.githubusercontent.com/Fribb/anime-lists/master/anime-list-full.json"
ANIME_LISTS_URL = "https://raw.githubusercontent.com/Anime-Lists/anime-lists/master/anime-list-master.xml"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "anime-nfo"
MAX_AGE = 24 * 60 * 60
VIDEO_EXTENSIONS = {".mkv", ".mp4", ".m4v", ".avi", ".webm", ".ts", ".mov", ".wmv"}
USER_AGENT = "anime-tools"


# ---------------------------------------------------------------------------
# Id lists
# ---------------------------------------------------------------------------


def cached_download(url: str, name: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / name
    if path.exists() and time.time() - path.stat().st_mtime < MAX_AGE:
        return path
    print(f"Downloading {url}", file=sys.stderr)
    try:
        with urllib.request.urlopen(url, timeout=120) as res:
            data = res.read()
    except OSError as err:
        if path.exists():
            print(f"  failed ({err}); using the saved copy", file=sys.stderr)
            return path
        raise SystemExit(f"Cannot download {url}: {err}")
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)
    return path


def anidb_for_anilist(anilist_id: int) -> int:
    entries = json.loads(cached_download(FRIBB_URL, "anime-list-full.json").read_text())
    for entry in entries:
        if entry.get("anilist_id") == anilist_id:
            if entry.get("anidb_id"):
                return int(entry["anidb_id"])
            raise SystemExit(f"AniList {anilist_id} has no AniDB id in Fribb's anime-lists.")
    raise SystemExit(f"AniList {anilist_id} isn't in Fribb's anime-lists.")


@dataclass
class Mapping:
    """One <mapping> element: explicit pairs and/or a start-end range shifted by offset."""

    anidb_season: int
    tvdb_season: int
    pairs: dict[int, int]
    start: int | None
    end: int | None
    offset: int


@dataclass
class Release:
    anidb_id: int
    name: str
    tvdb_id: str
    default_season: str  # a number, or "a" for absolute numbering
    episode_offset: int
    mappings: list[Mapping]


def parse_pairs(text: str) -> dict[int, int]:
    """';1-12;2-13;' -> {1: 12, 2: 13}. '1-5+6' (one file covering two) keeps the first."""
    pairs: dict[int, int] = {}
    for part in (text or "").split(";"):
        if "-" not in part:
            continue
        anidb, tvdb = part.split("-", 1)
        try:
            pairs[int(anidb)] = int(tvdb.split("+")[0])
        except ValueError:
            continue
    return pairs


def release_for_anidb(anidb_id: int) -> Release:
    root = ET.parse(cached_download(ANIME_LISTS_URL, "anime-list-master.xml")).getroot()
    for anime in root.iter("anime"):
        if anime.get("anidbid") != str(anidb_id):
            continue
        mappings = []
        for m in anime.iter("mapping"):
            mappings.append(
                Mapping(
                    anidb_season=int(m.get("anidbseason", "1")),
                    tvdb_season=int(m.get("tvdbseason", "0")),
                    pairs=parse_pairs(m.text or ""),
                    start=int(m.get("start")) if m.get("start") else None,
                    end=int(m.get("end")) if m.get("end") else None,
                    offset=int(m.get("offset") or 0),
                )
            )
        return Release(
            anidb_id=anidb_id,
            name=(anime.findtext("name") or "").strip(),
            tvdb_id=anime.get("tvdbid") or "",
            default_season=anime.get("defaulttvdbseason") or "",
            episode_offset=int(anime.get("episodeoffset") or 0),
            mappings=mappings,
        )
    raise SystemExit(f"AniDB {anidb_id} isn't in the Anime-Lists XML.")


def to_tvdb(release: Release, anidb_season: int, episode: int) -> tuple[int, int] | str:
    """AniDB (season 1 = regular, 0 = specials) episode -> (TVDB season, episode), or why not."""
    for m in release.mappings:
        if m.anidb_season != anidb_season:
            continue
        if episode in m.pairs:
            target = m.pairs[episode]
            return (m.tvdb_season, target) if target > 0 else "listed as having no TVDB episode"
    for m in release.mappings:
        if m.anidb_season == anidb_season and m.start is not None and m.end is not None:
            if m.start <= episode <= m.end:
                target = episode + m.offset
                return (m.tvdb_season, target) if target > 0 else "listed as having no TVDB episode"
    if anidb_season == 0:
        return "special with no TVDB mapping; pass --episode FILE=S<n> or set it in Jellyfin"
    if release.default_season == "a":
        return "TVDB uses absolute numbering for this show; not supported"
    if not release.default_season.isdigit():
        return "no TVDB season known"
    return (int(release.default_season), episode + release.episode_offset)


# ---------------------------------------------------------------------------
# TVDB
# ---------------------------------------------------------------------------

TVDB_API = "https://api4.thetvdb.com/v4"


@dataclass
class EpisodeDetails:
    tvdb_id: int
    name: str | None
    overview: str | None
    aired: str | None
    runtime: int | None


@dataclass
class SeriesInfo:
    name: str
    year: str | None


class TvdbError(Exception):
    def __init__(self, status: int, path: str):
        super().__init__(f"TVDB answered {status} to {path}")
        self.status = status


def tvdb_request(path: str, token: str | None = None, body: dict | None = None) -> dict:
    headers = {"accept": "application/json", "user-agent": USER_AGENT}
    data = None
    if token:
        headers["authorization"] = f"Bearer {token}"
    if body is not None:
        headers["content-type"] = "application/json"
        data = json.dumps(body).encode()
    req = urllib.request.Request(f"{TVDB_API}{path}", data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as res:
            return json.load(res)
    except urllib.error.HTTPError as err:
        raise TvdbError(err.code, path) from None
    except OSError as err:
        raise SystemExit(f"TVDB is unreachable: {err}")


class Tvdb:
    """A signed-in TVDB v4 client."""

    def __init__(self, api_key: str, pin: str | None = None):
        login = {"apikey": api_key, **({"pin": pin} if pin else {})}
        try:
            self.token = tvdb_request("/login", body=login)["data"]["token"]
        except TvdbError as err:
            raise SystemExit(f"TVDB login failed ({err.status}); check the API key (and PIN, for user keys).")

    def series(self, series_id: str, language: str) -> SeriesInfo:
        """The series' name (translated when TVDB has one) and first-aired year."""
        quoted = urllib.parse.quote(series_id)
        try:
            data = tvdb_request(f"/series/{quoted}", self.token).get("data") or {}
        except TvdbError as err:
            raise SystemExit(str(err))
        name = data.get("name") or ""
        try:
            translated = tvdb_request(f"/series/{quoted}/translations/{urllib.parse.quote(language)}", self.token)
            name = (translated.get("data") or {}).get("name") or name
        except TvdbError as err:
            if err.status != 404:
                raise SystemExit(str(err))
        year = data.get("year") or (data.get("firstAired") or "")[:4] or None
        return SeriesInfo(name=name, year=str(year) if year else None)

    def episodes(self, series_id: str, language: str) -> dict[tuple[int, int], EpisodeDetails]:
        """Every episode of a series in TVDB's default (aired) order, by (season, number)."""

        def pages(path: str):
            page = 0
            while True:
                body = tvdb_request(f"{path}?page={page}", self.token)
                data = body.get("data") or {}
                yield from data.get("episodes", []) if isinstance(data, dict) else data
                if not (body.get("links") or {}).get("next"):
                    return
                page += 1

        base = f"/series/{urllib.parse.quote(series_id)}/episodes/default"
        try:
            # Translated names and overviews; falls back to the original language below.
            translated = {ep["id"]: ep for ep in pages(f"{base}/{urllib.parse.quote(language)}")}
        except TvdbError as err:
            if err.status != 404:
                raise SystemExit(str(err))
            translated = {}
        episodes: dict[tuple[int, int], EpisodeDetails] = {}
        try:
            for ep in pages(base):
                if ep.get("seasonNumber") is None or ep.get("number") is None:
                    continue
                local = translated.get(ep["id"], {})
                episodes[(int(ep["seasonNumber"]), int(ep["number"]))] = EpisodeDetails(
                    tvdb_id=int(ep["id"]),
                    name=local.get("name") or ep.get("name"),
                    overview=local.get("overview") or ep.get("overview"),
                    aired=ep.get("aired"),
                    runtime=ep.get("runtime"),
                )
        except TvdbError as err:
            raise SystemExit(str(err))
        return episodes


# ---------------------------------------------------------------------------
# AniList (no key needed)
# ---------------------------------------------------------------------------

ANILIST_API = "https://graphql.anilist.co"
ANILIST_QUERY = "query($id:Int){Media(id:$id,type:ANIME){title{english romaji}startDate{year}}}"
# "Part 2", "Season 3", "2nd Season", "Cour 2"... so a release's title names its series.
RELEASE_SUFFIX = re.compile(
    r"[\s:.-]*\b(?:(?:Season|Part|Cour)\s*\d+|\d+(?:st|nd|rd|th)\s+(?:Season|Cour)|(?:Final|Second|Third)\s+Season)\b.*$",
    re.I,
)


def anilist_series(anilist_id: int) -> SeriesInfo | None:
    """The release's English (or romaji) title without its season/part suffix, and its year."""
    body = json.dumps({"query": ANILIST_QUERY, "variables": {"id": anilist_id}}).encode()
    req = urllib.request.Request(
        ANILIST_API,
        data=body,
        headers={"content-type": "application/json", "accept": "application/json", "user-agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            media = (json.load(res).get("data") or {}).get("Media") or {}
    except OSError as err:
        print(f"AniList lookup failed: {err}", file=sys.stderr)
        return None
    title = (media.get("title") or {}).get("english") or (media.get("title") or {}).get("romaji")
    if not title:
        return None
    year = (media.get("startDate") or {}).get("year")
    return SeriesInfo(name=RELEASE_SUFFIX.sub("", title).strip() or title, year=str(year) if year else None)


# ---------------------------------------------------------------------------
# Episode numbers in filenames
# ---------------------------------------------------------------------------

SPECIAL = re.compile(r"\b(?:SP|OVA|OAD|ONA|Special)s?[\s._-]*(\d{1,3})?(?:v\d)?\b", re.I)
SXXEXX = re.compile(r"\bS(\d{1,2})[\s._-]*E(\d{1,4})(?:v\d)?\b", re.I)
SEASON_DASH = re.compile(r"\bS(\d{1,2})\s+-\s+(\d{1,4})(?:v\d)?\b", re.I)  # "Vinland Saga S2 - 05"
DASH_NUMBER = re.compile(r"\s-\s+(\d{1,4})(?:v\d)?\b")
EP_WORD = re.compile(r"\b(?:E|EP|Episode)[\s._-]*(\d{1,4})(?:v\d)?\b", re.I)
BARE_NUMBER = re.compile(r"(?<![\w.])(\d{1,4})(?:v\d)?(?![\w.])")


def strip_tags(stem: str) -> str:
    """Drop [group], [CRC], (1080p), (BD x265 ...) and similar bracketed tags."""
    return re.sub(r"\[[^\]]*\]|\([^)]*\)|\{[^}]*\}", " ", stem)


def numbers_from_name(filename: str) -> tuple[int | None, int, int] | None:
    """(season named in the file or None, AniDB season: 1 regular / 0 special, episode)."""
    # Underscores as spaces, so "DxD_-_SP01_" reads like "DxD - SP01".
    core = strip_tags(Path(filename).stem).replace("_", " ")
    if m := SXXEXX.search(core):
        season = int(m.group(1))
        return (season, 0 if season == 0 else 1, int(m.group(2)))
    if m := SEASON_DASH.search(core):
        return (int(m.group(1)), 1, int(m.group(2)))
    if m := SPECIAL.search(core):
        return (None, 0, int(m.group(1) or 1))
    if m := DASH_NUMBER.search(core):
        return (None, 1, int(m.group(1)))
    if m := EP_WORD.search(core):
        return (None, 1, int(m.group(1)))
    numbers = [
        int(n.group(1))
        for n in BARE_NUMBER.finditer(core)
        # Skip years and resolutions.
        if not (1900 <= int(n.group(1)) <= 2100) and n.group(1) not in {"480", "576", "720", "1080", "2160"}
    ]
    return (None, 1, numbers[-1]) if numbers else None


def episode_from_name(filename: str) -> tuple[int, int] | None:
    """(AniDB season, episode) read from an anime filename; season 0 for specials.

    A file's SxxEyy is read as the episode within the release, whatever season it names.
    """
    parsed = numbers_from_name(filename)
    if parsed is None:
        return None
    _, anidb_season, episode = parsed
    return (anidb_season, episode)


def parse_override(text: str) -> tuple[str, tuple[int, int]]:
    """'file.mkv=S1' (special 1) or 'file.mkv=5' (episode 5) -> (filename, (AniDB season, episode))."""
    name, _, value = text.rpartition("=")
    if not name:
        raise argparse.ArgumentTypeError(f"expected FILE=EPISODE, got {text!r}")
    special = value.upper().startswith("S")
    try:
        number = int(value[1:] if special else value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"bad episode {value!r}; use a number, or S<n> for a special")
    return name, (0 if special else 1, number)


# ---------------------------------------------------------------------------
# NFO files
# ---------------------------------------------------------------------------


def episode_nfo(season: int, episode: int, details: EpisodeDetails | None) -> str:
    lines = [
        '<?xml version="1.0" encoding="utf-8" standalone="yes"?>',
        "<episodedetails>",
    ]
    if details:
        if details.name:
            lines.append(f"  <title>{escape(details.name)}</title>")
        if details.overview:
            lines.append(f"  <plot>{escape(details.overview)}</plot>")
        if details.aired:
            lines.append(f"  <aired>{escape(details.aired)}</aired>")
        if details.runtime:
            lines.append(f"  <runtime>{details.runtime}</runtime>")
        lines.append(f'  <uniqueid type="Tvdb" default="true">{details.tvdb_id}</uniqueid>')
        # Jellyfin looks episodes up by the filename's number, so a "Replace all metadata"
        # refresh would put another episode's title and plot here; locked, these stay.
        lines.append("  <lockedfields>Name|Overview</lockedfields>")
    lines += [
        f"  <season>{season}</season>",
        f"  <episode>{episode}</episode>",
        "</episodedetails>",
        "",
    ]
    return "\n".join(lines)


def tvshow_nfo(tvdb_id: str, title: str | None) -> str:
    # Only the TVDB id: an AniList id here makes Jellyfin's AniList plugin take over the
    # series (its title and per-release numbering), which these tools undo.
    lines = [
        '<?xml version="1.0" encoding="utf-8" standalone="yes"?>',
        "<tvshow>",
    ]
    if title:
        lines.append(f"  <title>{escape(title)}</title>")
    lines += [
        f'  <uniqueid type="Tvdb" default="true">{tvdb_id}</uniqueid>',
        f"  <tvdbid>{tvdb_id}</tvdbid>",
        "</tvshow>",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Release tags: what a filename (or its folder's name) says about the release
# ---------------------------------------------------------------------------
# Spellings are the ones AIOStreams' parser recognises (BD -> BluRay, x265 -> HEVC...).

SEP = r"(?<![^\s\[(_\-.,])"  # a tag starts at the start or after a separator...
END = r"(?=[\s\[()\]_.\-,+]|$)"  # ...and ends at one


def tag(pattern: str) -> re.Pattern:
    return re.compile(f"{SEP}(?:{pattern}){END}", re.I)


SOURCES = [
    ("BluRay REMUX", tag(r"(?:bd|br|blu[ .\-_]?ray|uhd)?[ .\-_]?remux")),
    ("BluRay", tag(r"(?:bd|blu[ .\-_]?ray)(?:[ .\-_]?rip)?(?:\d{3,4}p)?|br[ .\-_]?rip|bdmv")),
    ("WEBRip", tag(r"web[ .\-_]?rip")),
    ("WEB-DL", tag(r"web(?:[ .\-_]?dl)?(?:\d{3,4}p)?")),
    ("DVDRip", tag(r"dvd(?:[ .\-_]?rip)?|dvd5|dvd9")),
    ("HDTV", tag(r"hdtv|tv[ .\-_]?rip")),
]
NETWORKS = [
    ("CR", tag(r"cr|crunchyroll")),
    ("AMZN", tag(r"amzn|amazon")),
    ("NF", tag(r"nf|netflix")),
    ("DSNP", tag(r"dsnp|disney\+?")),
    ("HIDIVE", tag(r"hidi(?:ve)?")),
    ("ADN", tag(r"adn")),
    ("B-Global", tag(r"b[ .\-_]?global")),
]
RESOLUTION = re.compile(r"(?<!\d)(2160|1080|720|576|480)[pi](?!\w)|(?<!\w)(4k|uhd)(?!\w)", re.I)
ENCODES = [
    ("HEVC", tag(r"hevc(?:[ .\-_]?10)?|[xh][ .\-_]?265")),
    ("AVC", tag(r"avc|[xh][ .\-_]?264")),
    ("AV1", tag(r"av1")),
]
TEN_BIT = tag(r"10[ .\-_]?bits?|hi10p?|yuv420p10")
HDR = [
    ("DV", tag(r"dv|dovi|dolby[ .\-_]?vision")),
    ("HDR10+", tag(r"hdr[ .\-_]?10(?:\+|p|plus)")),
    ("HDR", tag(r"hdr(?:10)?")),
    ("HLG", tag(r"hlg")),
]
AUDIO = [
    ("TrueHD", tag(r"true[ .\-_]?hd")),
    ("DTS-HD MA", tag(r"dts[ .\-_]?hd[ .\-_]?ma")),
    ("DTS", tag(r"dts(?:[ .\-_]?hd)?")),
    ("DDP", tag(r"ddp(?:[ .\-_]?[257][ .\-_]?[01])?|e[ .\-_]?ac[ .\-_]?3|dd\+")),
    ("DD", tag(r"dd(?:[ .\-_]?[257][ .\-_]?[01])?|ac[ .\-_]?3")),
    ("FLAC", tag(r"flac(?:[ .\-_]?(?:x[2-4]|2\.0))?")),
    ("OPUS", tag(r"opus")),
    ("AAC", tag(r"aac(?:[ .\-_]?(?:2\.0|5\.1))?")),
]
CHANNELS = re.compile(r"(?<!\d)([257])[ .]([01])(?:ch)?(?!\d)", re.I)
UNCENSORED = tag(r"uncen(?:sored)?")
DUAL_AUDIO = tag(r"dual[ .\-_]?audio")
MULTI_AUDIO = tag(r"multi[ .\-_]?audio")
MULTI_SUBS = tag(r"multi[ .\-_]?subs?")
ENG_SUBS = tag(r"eng(?:lish)?[ .\-_]?subs?")
# A leading [tag] that is a release tag, not a group: [1080p], [CRC32], [BD]...
NOT_GROUP = re.compile(r"^(?:\d{3,4}[pi]|[0-9A-F]{8}|BD|WEB|BluRay|HEVC|x26[45]|10bit|Batch|Dual Audio)$", re.I)
SUFFIX_GROUP = re.compile(r"(?<=[\w\])])-([A-Za-z0-9][A-Za-z0-9_&]*)$")


def first(table: list[tuple[str, re.Pattern]], text: str) -> str | None:
    return next((name for name, pattern in table if pattern.search(text)), None)


@dataclass
class NameTags:
    group: str | None = None
    source: str | None = None
    network: str | None = None
    resolution: str | None = None
    encode: str | None = None
    ten_bit: bool = False
    hdr: list[str] = field(default_factory=list)
    audio: str | None = None
    channels: str | None = None
    uncensored: bool = False
    dual_audio: bool = False
    multi_audio: bool = False
    eng_subs: bool = False
    multi_subs: bool = False


def release_group(stem: str) -> str | None:
    if m := re.match(r"\s*\[([^\]]+)\]", stem):
        if not NOT_GROUP.match(m.group(1).strip()):
            return m.group(1).strip()
    # Scene style, "...x264-GROUP": only after the tags in brackets are dropped.
    bare = re.sub(r"\s*(?:\[[^\]]*\]|\([^)]*\))\s*$", "", stem).strip()
    if m := SUFFIX_GROUP.search(bare):
        if not RESOLUTION.fullmatch(m.group(1)) and not first(SOURCES + ENCODES, m.group(1)):
            return m.group(1)
    return None


def name_tags(*names: str) -> NameTags:
    """Tags read from filenames or folder names; the first name that has a tag wins."""
    tags = NameTags()
    for name in names:
        if Path(name).suffix.lower() in VIDEO_EXTENSIONS:
            name = Path(name).stem
        text = name.replace("_", " ")
        tags.group = tags.group or release_group(name)
        tags.source = tags.source or first(SOURCES, text)
        tags.network = tags.network or first(NETWORKS, text)
        if not tags.resolution and (m := RESOLUTION.search(text)):
            tags.resolution = f"{m.group(1)}p" if m.group(1) else "2160p"
        tags.encode = tags.encode or first(ENCODES, text)
        tags.ten_bit = tags.ten_bit or bool(TEN_BIT.search(text))
        if not tags.hdr:
            tags.hdr = [name for name, pattern in HDR if pattern.search(text)][:1]
        tags.audio = tags.audio or first(AUDIO, text)
        if not tags.channels and (m := CHANNELS.search(text)):
            tags.channels = f"{m.group(1)}.{m.group(2)}"
        tags.uncensored = tags.uncensored or bool(UNCENSORED.search(text))
        tags.dual_audio = tags.dual_audio or bool(DUAL_AUDIO.search(text))
        tags.multi_audio = tags.multi_audio or bool(MULTI_AUDIO.search(text))
        tags.eng_subs = tags.eng_subs or bool(ENG_SUBS.search(text))
        tags.multi_subs = tags.multi_subs or bool(MULTI_SUBS.search(text))
    # The network regexes are short ("CR", "NF"); only trust them for web releases.
    if tags.source not in {"WEB-DL", "WEBRip"}:
        tags.network = None
    return tags


# ---------------------------------------------------------------------------
# ffprobe
# ---------------------------------------------------------------------------

LANGUAGES = {
    "jpn": "Japanese", "eng": "English", "chi": "Chinese", "zho": "Chinese", "kor": "Korean",
    "spa": "Spanish", "por": "Portuguese", "fre": "French", "fra": "French", "ger": "German",
    "deu": "German", "ita": "Italian", "rus": "Russian", "ara": "Arabic", "hin": "Hindi",
    "tha": "Thai", "vie": "Vietnamese", "ind": "Indonesian", "may": "Malay", "msa": "Malay",
    "pol": "Polish", "tur": "Turkish", "dut": "Dutch", "nld": "Dutch",
}
AUDIO_CODECS = {
    "aac": "AAC", "ac3": "DD", "eac3": "DDP", "truehd": "TrueHD", "dts": "DTS",
    "flac": "FLAC", "opus": "OPUS", "mp3": "MP3", "vorbis": "Vorbis",
}
VIDEO_CODECS = {"hevc": "HEVC", "h264": "AVC", "av1": "AV1", "vp9": "VP9", "mpeg4": "XviD"}


@dataclass
class MediaInfo:
    resolution: str | None = None
    encode: str | None = None
    ten_bit: bool = False
    hdr: list[str] = field(default_factory=list)
    audio: str | None = None
    channels: str | None = None
    audio_languages: list[str] = field(default_factory=list)
    subtitle_languages: list[str] = field(default_factory=list)


def resolution_of(width: int, height: int) -> str | None:
    # Width first: scope films are cropped, so 3840x1600 is still 2160p.
    if not width and not height:
        return None
    if width >= 3200 or height >= 2000:
        return "2160p"
    if width >= 1800 or height >= 1000:
        return "1080p"
    if width >= 1200 or height >= 700:
        return "720p"
    if width >= 900 or height >= 540:
        return "576p"
    return "480p"


def channels_of(count: int | None) -> str | None:
    return {1: "1.0", 2: "2.0", 6: "5.1", 7: "6.1", 8: "7.1"}.get(count or 0)


def language_of(stream: dict) -> str | None:
    code = ((stream.get("tags") or {}).get("language") or "").lower()
    return LANGUAGES.get(code)


def have_ffprobe() -> bool:
    return shutil.which("ffprobe") is not None


def probe(path: Path) -> MediaInfo | None:
    """What ffprobe says about a file, or None when ffprobe is missing or fails."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", str(path)],
            capture_output=True,
            check=True,
            timeout=60,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    streams = json.loads(out).get("streams") or []
    info = MediaInfo()
    video = next(
        (s for s in streams if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")),
        None,
    )
    if video:
        info.resolution = resolution_of(int(video.get("width") or 0), int(video.get("height") or 0))
        info.encode = VIDEO_CODECS.get(video.get("codec_name", ""))
        bits = video.get("bits_per_raw_sample") or ""
        info.ten_bit = "10" in (video.get("pix_fmt") or "") or bits == "10"
        side = " ".join(d.get("side_data_type", "") for d in video.get("side_data_list") or [])
        if "DOVI" in side or "Dolby Vision" in side:
            info.hdr.append("DV")
        transfer = video.get("color_transfer")
        if transfer == "smpte2084":
            info.hdr.append("HDR")
        elif transfer == "arib-std-b67":
            info.hdr.append("HLG")

    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
    if audio_streams:
        main = next((s for s in audio_streams if (s.get("disposition") or {}).get("default")), audio_streams[0])
        codec = AUDIO_CODECS.get(main.get("codec_name", ""))
        profile = main.get("profile") or ""
        if codec == "DTS" and "MA" in profile:
            codec = "DTS-HD MA"
        if "Atmos" in profile:
            codec = f"{codec} Atmos"
        info.audio = codec
        info.channels = channels_of(main.get("channels"))
        for s in audio_streams:
            lang = language_of(s)
            if lang and lang not in info.audio_languages:
                info.audio_languages.append(lang)
    for s in streams:
        if s.get("codec_type") == "subtitle":
            lang = language_of(s)
            if lang and lang not in info.subtitle_languages:
                info.subtitle_languages.append(lang)
    return info


# ---------------------------------------------------------------------------
# Clean names
# ---------------------------------------------------------------------------


def tag_groups(tags: NameTags, media: MediaInfo | None) -> list[str]:
    """Bracketed tag groups for a clean name: what ffprobe measured, else what the old name said."""
    m = media or MediaInfo()
    video = [
        tags.network,
        tags.source,
        m.resolution or tags.resolution,
        m.encode or tags.encode,
        "10bit" if (m.ten_bit or (not media and tags.ten_bit)) else None,
        *(m.hdr or tags.hdr),
        "Uncensored" if tags.uncensored else None,
    ]
    audio = [m.audio or tags.audio, m.channels or tags.channels]
    langs = m.audio_languages
    if len(langs) >= 3 or (not media and tags.multi_audio):
        languages = ["Multi Audio", *langs]
    elif len(langs) == 2 or (not media and tags.dual_audio):
        languages = ["Dual Audio", *langs]
    else:
        languages = langs  # one language: still name it, so a Japanese-only file isn't taken for a dub
    subs = m.subtitle_languages
    if len(subs) >= 2 or (not media and tags.multi_subs):
        subtitle = ["Multi Subs"]
    elif subs:
        subtitle = [f"{subs[0][:3]} Subs"]
    elif not media and tags.eng_subs:
        subtitle = ["Eng Subs"]
    else:
        subtitle = []
    groups = [video, audio, languages, subtitle]
    return [" ".join(x for x in group if x) for group in groups if any(group)]


UNSAFE = re.compile(r'[<>"/\\|?*\x00-\x1f]')


def safe_title(text: str) -> str:
    """A title fit for a filename: 'Re:Zero' -> 'Re Zero', 'A: B' -> 'A - B'."""
    text = re.sub(r"\s*:\s+", " - ", text)
    text = text.replace(":", " ")
    text = UNSAFE.sub("", text)
    return re.sub(r"\s+", " ", text).strip(" .")


def with_group(name: str, group: str | None, mode: str) -> str:
    """Puts the release group before ([Group] name) or after (name-Group) the name, or leaves it out."""
    if not group or mode == "remove":
        return name
    if mode == "prefix":
        return f"[{group}] {name}"
    return f"{name}-{re.sub(r'[^A-Za-z0-9_&]+', '', group)}"


def fit(name: str, ext: str, limit: int = 250) -> str:
    """Trim a name so name+ext fits a filesystem's 255-byte limit."""
    while len((name + ext).encode()) > limit:
        name = name[:-1]
    return name.rstrip(" .-") + ext
