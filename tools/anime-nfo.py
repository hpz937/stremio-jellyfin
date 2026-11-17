#!/usr/bin/env python3
"""
Write Jellyfin .nfo sidecars that give anime files their TVDB season/episode numbers,
without renaming anything (so AniList-based tools like Seanime keep matching them).

A folder holds one AniList release (a season, or one part of a split season), with
files numbered from 1 within that release. Given its AniList id, each episode is mapped:

    AniList id --(Fribb anime-lists)--> AniDB id --(Anime-Lists XML)--> TVDB season/episode

The Anime-Lists XML carries the episode offsets for split seasons, e.g. Mushoku Tensei
Part 2 episode 1 -> S01E12, and the mappings for specials/OVAs.

With a TVDB API key (TVDB_API_KEY, or --tvdb-key), each episode's title, plot and air
date are written too, and locked. Jellyfin looks episodes up by the number in the
filename, so without this a refresh gives Part 2 episode 1 the details of S01E01.

Usage:
    export TVDB_API_KEY=...                      # optional, but recommended
    anime-nfo.py FOLDER --anilist ID             # preview what would be written
    anime-nfo.py FOLDER --anilist ID --write     # write the .nfo files
    anime-nfo.py FOLDER --anilist ID --episode 'Mushoku Tensei - OVA.mkv=S1'

Only the Python standard library is used. The two id lists are downloaded once a day
to ~/.cache/anime-nfo.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

FRIBB_URL = "https://raw.githubusercontent.com/Fribb/anime-lists/master/anime-list-full.json"
ANIME_LISTS_URL = "https://raw.githubusercontent.com/Anime-Lists/anime-lists/master/anime-list-master.xml"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "anime-nfo"
MAX_AGE = 24 * 60 * 60
VIDEO_EXTENSIONS = {".mkv", ".mp4", ".m4v", ".avi", ".webm", ".ts", ".mov", ".wmv"}


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
# TVDB episode details
# ---------------------------------------------------------------------------

TVDB_API = "https://api4.thetvdb.com/v4"


@dataclass
class EpisodeDetails:
    tvdb_id: int
    name: str | None
    overview: str | None
    aired: str | None
    runtime: int | None


def tvdb_request(path: str, token: str | None = None, body: dict | None = None) -> dict:
    headers = {"accept": "application/json", "user-agent": "anime-nfo"}
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


class TvdbError(Exception):
    def __init__(self, status: int, path: str):
        super().__init__(f"TVDB answered {status} to {path}")
        self.status = status


def tvdb_episodes(series_id: str, api_key: str, pin: str | None, language: str) -> dict[tuple[int, int], EpisodeDetails]:
    """Every episode of a series in TVDB's default (aired) order, by (season, number)."""
    login = {"apikey": api_key, **({"pin": pin} if pin else {})}
    try:
        token = tvdb_request("/login", body=login)["data"]["token"]
    except TvdbError as err:
        raise SystemExit(f"TVDB login failed ({err.status}); check the API key (and PIN, for user keys).")

    def pages(path: str):
        page = 0
        while True:
            body = tvdb_request(f"{path}?page={page}", token)
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
    return episodes


# ---------------------------------------------------------------------------
# Filenames
# ---------------------------------------------------------------------------

SPECIAL = re.compile(r"\b(?:SP|OVA|OAD|ONA|Special)s?[\s._-]*(\d{1,3})?(?:v\d)?\b", re.I)
SXXEXX = re.compile(r"\bS\d{1,2}[\s._-]*E(\d{1,4})(?:v\d)?\b", re.I)
DASH_NUMBER = re.compile(r"\s-\s+(\d{1,4})(?:v\d)?\b")
EP_WORD = re.compile(r"\b(?:E|EP|Episode)[\s._-]*(\d{1,4})(?:v\d)?\b", re.I)
BARE_NUMBER = re.compile(r"(?<![\w.])(\d{1,4})(?:v\d)?(?![\w.])")


def episode_from_name(filename: str) -> tuple[int, int] | None:
    """(AniDB season, episode) read from an anime filename; season 0 for specials."""
    stem = Path(filename).stem
    # Drop [group], [CRC], (1080p), (BD x265 ...) and similar tags.
    core = re.sub(r"\[[^\]]*\]|\([^)]*\)|\{[^}]*\}", " ", stem)
    if m := SXXEXX.search(core):
        return (1, int(m.group(1)))
    if m := SPECIAL.search(core):
        return (0, int(m.group(1) or 1))
    if m := DASH_NUMBER.search(core):
        return (1, int(m.group(1)))
    if m := EP_WORD.search(core):
        return (1, int(m.group(1)))
    numbers = [
        int(n.group(1))
        for n in BARE_NUMBER.finditer(core)
        # Skip years and resolutions.
        if not (1900 <= int(n.group(1)) <= 2100) and n.group(1) not in {"480", "576", "720", "1080", "2160"}
    ]
    return (1, numbers[-1]) if numbers else None


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


def tvshow_nfo(release: Release, title: str | None) -> str:
    # Only the TVDB id: an AniList id here makes Jellyfin's AniList plugin take over the
    # series (its title and per-release numbering), which is what this script undoes.
    lines = [
        '<?xml version="1.0" encoding="utf-8" standalone="yes"?>',
        "<tvshow>",
    ]
    if title:
        lines.append(f"  <title>{escape(title)}</title>")
    lines += [
        f'  <uniqueid type="Tvdb" default="true">{release.tvdb_id}</uniqueid>',
        f"  <tvdbid>{release.tvdb_id}</tvdbid>",
        "</tvshow>",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Write Jellyfin .nfo files mapping an AniList release's files to TVDB numbering.",
    )
    parser.add_argument("folder", type=Path, help="folder holding one AniList release")
    parser.add_argument("--anilist", type=int, required=True, help="the release's AniList id (anilist.co/anime/<id>)")
    parser.add_argument("--write", action="store_true", help="write the files (default: only show them)")
    parser.add_argument("--force", action="store_true", help="replace .nfo files that already exist")
    parser.add_argument("--no-tvshow", action="store_true", help="don't write tvshow.nfo")
    parser.add_argument("--title", help="series title for tvshow.nfo (default: let Jellyfin fetch it)")
    parser.add_argument(
        "--tvdb-key",
        default=os.environ.get("TVDB_API_KEY"),
        help="TVDB v4 API key, to write episode titles and plots (default: $TVDB_API_KEY)",
    )
    parser.add_argument("--tvdb-pin", default=os.environ.get("TVDB_PIN"), help="PIN for a user-supported key (default: $TVDB_PIN)")
    parser.add_argument("--language", default="eng", help="TVDB language for titles and plots (default: eng)")
    parser.add_argument(
        "--episode",
        action="append",
        type=parse_override,
        default=[],
        metavar="FILE=N|S<n>",
        help="set a file's episode when its name can't be read; S<n> means special n (repeatable)",
    )
    args = parser.parse_args()

    if not args.folder.is_dir():
        raise SystemExit(f"{args.folder} is not a folder")
    overrides = dict(args.episode)

    anidb_id = anidb_for_anilist(args.anilist)
    release = release_for_anidb(anidb_id)
    print(f"AniList {args.anilist} -> AniDB {anidb_id} ({release.name}) -> TVDB {release.tvdb_id or '?'}")
    if not release.tvdb_id:
        raise SystemExit("This release has no TVDB mapping; nothing to write.")

    details: dict[tuple[int, int], EpisodeDetails] = {}
    if args.tvdb_key:
        details = tvdb_episodes(release.tvdb_id, args.tvdb_key, args.tvdb_pin, args.language)
        print(f"TVDB: {len(details)} episodes")
    else:
        print("No TVDB key: writing numbers only. Jellyfin will give these files the titles of the")
        print("episodes their filenames name; set TVDB_API_KEY to write the right ones.")

    planned: list[tuple[Path, str]] = []
    if not args.no_tvshow:
        planned.append((args.folder / "tvshow.nfo", tvshow_nfo(release, args.title)))

    problems = 0
    seen: dict[tuple[int, int], str] = {}
    videos = sorted(p for p in args.folder.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS)
    for video in videos:
        parsed = overrides.pop(video.name, None) or episode_from_name(video.name)
        if parsed is None:
            print(f"  ?        {video.name}: can't read an episode number; use --episode")
            problems += 1
            continue
        anidb_season, episode = parsed
        result = to_tvdb(release, anidb_season, episode)
        label = f"{'SP' if anidb_season == 0 else 'E'}{episode}"
        if isinstance(result, str):
            print(f"  {label:<8} {video.name}: {result}")
            problems += 1
            continue
        season, tvdb_episode = result
        target = f"S{season:02}E{tvdb_episode:02}"
        if target in seen.values():
            print(f"  {label:<8} {video.name}: also maps to {target}; check the filenames or use --episode")
            problems += 1
        seen[parsed] = target
        found = details.get((season, tvdb_episode))
        if args.tvdb_key and not found:
            print(f"  {label:<8} -> {target}  {video.name}: not on TVDB; writing the number only")
            problems += 1
        else:
            title = f'  "{found.name}"' if found and found.name else ""
            print(f"  {label:<8} -> {target}  {video.name}{title}")
        planned.append((video.with_suffix(".nfo"), episode_nfo(season, tvdb_episode, found)))

    for name in overrides:
        print(f"  --episode {name}: no such video in the folder")
        problems += 1

    if not args.write:
        print(f"\nPreview only; {len(planned)} file(s) would be written. Add --write to write them.")
        return
    written = skipped = 0
    for path, content in planned:
        if path.exists() and not args.force:
            print(f"  kept existing {path.name} (use --force to replace)")
            skipped += 1
            continue
        path.write_text(content, encoding="utf-8")
        written += 1
    print(f"\nWrote {written} file(s), kept {skipped}. {problems} file(s) need attention.")
    print("In Jellyfin: keep Nfo enabled under the library's metadata readers and turn the Nfo")
    print("metadata saver off (it overwrites these files), then Refresh metadata ->")
    print("Replace all metadata on the series.")


if __name__ == "__main__":
    main()
