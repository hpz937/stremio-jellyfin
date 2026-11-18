#!/usr/bin/env python3
"""
Give anime files clean names with TVDB season/episode numbers and the tags stream
parsers (AIOStreams, Jellyfin) read, e.g.

    [DB]High School DxD_-_SP01_(10bit_BD1080p_x265).mkv
    -> High School DxD - S00E01 [BluRay 1080p HEVC 10bit][FLAC 2.0][Japanese][Eng Subs].mkv

The folder holds one release, as for anime-nfo.py. Episode numbers come from either:

    --anilist ID   AniList -> AniDB -> TVDB via the id lists, which handle split seasons
                   (Mushoku Tensei Part 2 episode 1 -> S01E12) and specials
    --tvdb ID      TVDB numbering as-is: SxxEyy from the filename, or --season/--offset

Resolution, codec, bit depth, HDR, audio and languages are measured with ffprobe when it
is installed (apt install ffmpeg); otherwise they're read from the old filename. The
source (BluRay, WEB-DL...) and Uncensored can only come from the old file or folder name.

Modes (the default only shows what would happen):
    --link DEST    hardlink into DEST/<Title> [tvdbid-N]/Season NN/ (same filesystem only).
                   The originals stay as they are, so AniList-based tools keep matching them.
    --rename       rename in place
    --undo FILE    reverse a run, from the manifest it wrote

Subtitles and .nfo files named after a video go with it.

Usage:
    export TVDB_API_KEY=...                          # for series and episode titles
    anime-rename.py FOLDER --anilist 146065
    anime-rename.py FOLDER --anilist 146065 --link /media/anime-clean
    anime-rename.py FOLDER --tvdb 371310 --season 2 --rename --group suffix
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from animelib import (
    VIDEO_EXTENSIONS,
    EpisodeDetails,
    SeriesInfo,
    Tvdb,
    anidb_for_anilist,
    anilist_series,
    episode_nfo,
    fit,
    have_ffprobe,
    name_tags,
    numbers_from_name,
    parse_override,
    probe,
    release_for_anidb,
    safe_title,
    tag_groups,
    to_tvdb,
    tvshow_nfo,
    with_group,
)

DEFAULT_TEMPLATE = "{title} - {sxe} {tags}"
MANIFEST_PREFIX = ".anime-rename-"


@dataclass
class Op:
    src: Path
    dst: Path
    label: str  # e.g. "E1 -> S01E12"
    video: bool


def sidecars(video: Path) -> list[Path]:
    """Files named after a video: 'Ep 01.en.ass', 'Ep 01.nfo'..."""
    prefix = video.stem + "."
    return sorted(
        p for p in video.parent.iterdir() if p.is_file() and p != video and p.name.startswith(prefix)
    )


def render(template: str, fields: dict) -> str:
    try:
        name = template.format(**fields)
    except (KeyError, ValueError) as err:
        raise SystemExit(f"Bad --template: {err}. Fields: {', '.join(sorted(fields))}")
    name = re.sub(r"\[\s*\]|\(\s*\)", "", name)
    name = re.sub(r"\s+", " ", name)
    name = re.sub(r"(?:\s-)+\s*(?=\[|$)", " ", name)  # a dangling " - " left by an empty field
    return name.strip(" -.")


def write_manifest(where: Path, mode: str, done: list[tuple[Path | None, Path]]) -> Path:
    """Records what a run did; a None source is a file it wrote (an .nfo)."""
    path = where / f"{MANIFEST_PREFIX}{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(
        json.dumps({"mode": mode, "ops": [[s and str(s), str(d)] for s, d in done]}, indent=1),
        encoding="utf-8",
    )
    return path


def undo(manifest: Path) -> None:
    data = json.loads(manifest.read_text())
    mode = data.get("mode")
    undone = kept = 0
    dirs: set[Path] = set()
    for src, dst in reversed(data.get("ops", [])):
        dst = Path(dst)
        if src is None:
            if dst.exists():  # an .nfo the run wrote
                dst.unlink()
                dirs.add(dst.parent)
                undone += 1
            continue
        src = Path(src)
        if mode == "rename":
            if dst.exists() and not src.exists():
                dst.rename(src)
                undone += 1
                continue
        elif mode == "link":
            if dst.exists() and src.exists() and os.path.samefile(src, dst):
                dst.unlink()
                dirs.add(dst.parent)
                undone += 1
                continue
        print(f"  left {dst} (changed since)")
        kept += 1
    # Season and series folders the run made, if now empty.
    for d in sorted(dirs | {d.parent for d in dirs}, key=lambda p: len(p.parts), reverse=True):
        try:
            d.rmdir()
        except OSError:
            pass
    print(f"Undid {undone}, left {kept}.")
    if not kept:
        manifest.unlink()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Rename or hardlink anime files to clean names with TVDB numbering and release tags.",
    )
    parser.add_argument("folder", type=Path, nargs="?", help="folder holding one release")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--anilist", type=int, help="the release's AniList id (anilist.co/anime/<id>)")
    source.add_argument("--tvdb", help="the series' TVDB id; files keep their own numbering (see --season)")
    parser.add_argument("--season", type=int, help="with --tvdb: the TVDB season of files without SxxEyy (default 1)")
    parser.add_argument("--offset", type=int, default=0, help="with --tvdb: add this to every regular episode number")
    parser.add_argument(
        "--episode",
        action="append",
        type=parse_override,
        default=[],
        metavar="FILE=N|S<n>",
        help="set a file's episode when its name can't be read; S<n> means special n (repeatable)",
    )

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--link", type=Path, metavar="DEST", help="hardlink into DEST/<Title> [tvdbid-N]/Season NN/")
    mode.add_argument("--rename", action="store_true", help="rename the files in place")
    mode.add_argument("--undo", type=Path, metavar="MANIFEST", help="reverse a run from the manifest it wrote")
    parser.add_argument("--flat", action="store_true", help="with --link: put the files straight into DEST")
    parser.add_argument("--force", action="store_true", help="replace files already at the new names")

    parser.add_argument(
        "--group",
        choices=["remove", "prefix", "suffix"],
        default="remove",
        help="release group: leave out (default), '[Group] Name', or 'Name-Group'",
    )
    parser.add_argument("--group-name", help="use this release group instead of the one in the old name")
    parser.add_argument("--title", help="series title (default: TVDB's, or AniList's without a key)")
    parser.add_argument(
        "--template",
        default=DEFAULT_TEMPLATE,
        help=(
            "new name without extension (default: %(default)r). Fields: {title} {year} {season} {episode} "
            "{sxe} {ep_title} {tags}. Episode titles are left out by default: parsers scan the whole "
            "name for tags, so a title like 'The Dub' would mark the file as dubbed."
        ),
    )
    parser.add_argument("--no-probe", action="store_true", help="don't run ffprobe; read tags from the old name only")
    parser.add_argument("--nfo", action="store_true", help="also write Jellyfin .nfo files next to the new names")

    parser.add_argument(
        "--tvdb-key",
        default=os.environ.get("TVDB_API_KEY"),
        help="TVDB v4 API key, for the series title and episode titles (default: $TVDB_API_KEY)",
    )
    parser.add_argument("--tvdb-pin", default=os.environ.get("TVDB_PIN"), help="PIN for a user-supported key (default: $TVDB_PIN)")
    parser.add_argument("--language", default="eng", help="TVDB language for titles (default: eng)")
    args = parser.parse_args()

    if args.undo:
        undo(args.undo)
        return
    if not args.folder or not args.folder.is_dir():
        parser.error("give a folder holding one release")
    if not args.anilist and not args.tvdb:
        parser.error("give --anilist or --tvdb")
    if args.anilist and (args.season is not None or args.offset):
        parser.error("--season and --offset are for --tvdb; with --anilist the id lists give the numbers")
    folder = args.folder.resolve()

    # --- Where the numbers and titles come from ------------------------------------
    release = None
    if args.anilist:
        anidb_id = anidb_for_anilist(args.anilist)
        release = release_for_anidb(anidb_id)
        tvdb_id = release.tvdb_id
        print(f"AniList {args.anilist} -> AniDB {anidb_id} ({release.name}) -> TVDB {tvdb_id or '?'}")
        if not tvdb_id:
            raise SystemExit("This release has no TVDB mapping; use --tvdb with --season/--offset instead.")
    else:
        tvdb_id = str(args.tvdb)

    tvdb = Tvdb(args.tvdb_key, args.tvdb_pin) if args.tvdb_key else None
    series: SeriesInfo | None = tvdb.series(tvdb_id, args.language) if tvdb else None
    if not series and args.anilist and not args.title:
        series = anilist_series(args.anilist)
        if series:
            print(f"No TVDB key: using AniList's title {series.name!r}; check it, or pass --title.")
    title = args.title or (series.name if series else None)
    if not title:
        raise SystemExit("No series title: set TVDB_API_KEY or pass --title.")
    title = safe_title(title)
    year = series.year if series else None
    details: dict[tuple[int, int], EpisodeDetails] = tvdb.episodes(tvdb_id, args.language) if tvdb else {}
    print(f"Series: {title}{f' ({year})' if year else ''}, TVDB {tvdb_id}" + (f", {len(details)} episodes" if tvdb else ""))

    probing = not args.no_probe and have_ffprobe()
    if not args.no_probe and not probing:
        print("ffprobe not found (apt install ffmpeg): reading resolution, codec and audio from the old names.")

    if args.link:
        dest_root = args.link.resolve()
        series_dir = dest_root if args.flat else dest_root / f"{title} [tvdbid-{tvdb_id}]"
    else:
        series_dir = folder

    # --- Plan ----------------------------------------------------------------------
    overrides = dict(args.episode)
    ops: list[Op] = []
    nfos: list[tuple[Path, str]] = []
    problems = 0
    videos = sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS)
    for video in videos:
        override = overrides.pop(video.name, None)
        named_season = None
        if override:
            anidb_season, episode = override
        elif parsed := numbers_from_name(video.name):
            named_season, anidb_season, episode = parsed
        else:
            print(f"  ?               {video.name}: can't read an episode number; use --episode")
            problems += 1
            continue
        label = f"{'SP' if anidb_season == 0 else 'E'}{episode}"

        if release:
            result = to_tvdb(release, anidb_season, episode)
            if isinstance(result, str):
                print(f"  {label:<6}          {video.name}: {result}")
                problems += 1
                continue
            season, number = result
        elif anidb_season == 0:
            season, number = 0, episode
        else:
            season = args.season if args.season is not None else (named_season or 1)
            number = episode + args.offset

        found = details.get((season, number))
        if tvdb and not found:
            print(f"  {label:<6} -> S{season:02}E{number:02}  {video.name}: not on TVDB; check the numbering")
            problems += 1

        tags = name_tags(video.name, folder.name)
        media = probe(video) if probing else None
        if probing and media is None:
            print(f"  {label:<6}          {video.name}: ffprobe failed; using the old name's tags")
        sxe = f"S{season:02}E{number:02}"
        name = render(
            args.template,
            {
                "title": title,
                "year": year or "",
                "season": season,
                "episode": number,
                "sxe": sxe,
                "ep_title": safe_title(found.name) if found and found.name else "",
                "tags": "".join(f"[{group}]" for group in tag_groups(tags, media)),
            },
        )
        name = with_group(name, args.group_name or tags.group, args.group)
        target_dir = series_dir if (args.flat or not args.link) else series_dir / f"Season {season:02}"
        new_video = target_dir / fit(name, video.suffix)
        ops.append(Op(video, new_video, f"{label} -> {sxe}", True))
        new_stem = new_video.stem
        for side in sidecars(video):
            if args.nfo and side.suffix == ".nfo":
                continue  # replaced by the one written below
            ops.append(Op(side, target_dir / (new_stem + side.name[len(video.stem):]), "", False))
        if args.nfo:
            nfos.append((new_video.with_suffix(".nfo"), episode_nfo(season, number, found)))

    for name in overrides:
        print(f"  --episode {name}: no such video in the folder")
        problems += 1
    if args.nfo and args.link and not args.flat:
        nfos.append((series_dir / "tvshow.nfo", tvshow_nfo(tvdb_id, args.title)))

    # Two files given one name (e.g. a v2 next to the v1) would overwrite each other.
    by_dst: dict[Path, list[Op]] = {}
    for op in ops:
        by_dst.setdefault(op.dst, []).append(op)
    clashes = {dst for dst, same in by_dst.items() if len(same) > 1}
    for dst in clashes:
        print(f"  CLASH {dst.name}: " + ", ".join(op.src.name for op in by_dst[dst]) + "; use --episode")
        problems += 1
    ops = [op for op in ops if op.dst not in clashes]

    # --- Show ----------------------------------------------------------------------
    action = "link" if args.link else "rename" if args.rename else None
    print()
    for op in ops:
        if op.video:
            print(f"  {op.label:<15} {op.src.name}")
        else:
            print(f"  {'':<15} {op.src.name}")
        shown = op.dst.relative_to(dest_root) if args.link else op.dst.name
        print(f"  {'':<15} => {shown}")
    for path, _ in nfos:
        shown = path.relative_to(dest_root) if args.link else path.name
        print(f"  {'nfo':<15} => {shown}")

    if not action:
        print(f"\nPreview only: {sum(op.video for op in ops)} video(s). Add --link DEST or --rename to apply.")
        if problems:
            print(f"{problems} file(s) need attention.")
        return

    # --- Apply ---------------------------------------------------------------------
    done: list[tuple[Path | None, Path]] = []
    skipped = 0
    try:
        for op in ops:
            if op.src == op.dst:
                continue
            if op.dst.exists():
                if os.path.samefile(op.src, op.dst):
                    continue  # linked by an earlier run
                if not args.force:
                    print(f"  kept existing {op.dst.name} (use --force to replace)")
                    skipped += 1
                    continue
                op.dst.unlink()
            op.dst.parent.mkdir(parents=True, exist_ok=True)
            if action == "link":
                try:
                    os.link(op.src, op.dst)
                except OSError as err:
                    if err.errno == errno.EXDEV:
                        raise SystemExit(f"{dest_root} is on another filesystem than {folder}; hardlinks can't cross.")
                    raise
            else:
                op.src.rename(op.dst)
            done.append((op.src, op.dst))
        for path, content in nfos:
            if path.exists() and not args.force:
                print(f"  kept existing {path.name} (use --force to replace)")
                skipped += 1
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            done.append((None, path))
    finally:
        if done:
            manifest = write_manifest(dest_root if action == "link" else folder, action, done)
            print(f"\n{'Linked' if action == 'link' else 'Renamed'} {len(done)} file(s), kept {skipped}.")
            print(f"Undo with: {Path(sys.argv[0]).name} --undo '{manifest}'")
    if problems:
        print(f"{problems} file(s) need attention.")


if __name__ == "__main__":
    main()
