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

Only the Python standard library is used (shared code is in animelib.py). The two id lists are downloaded once a day
to ~/.cache/anime-nfo.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from animelib import (
    VIDEO_EXTENSIONS,
    EpisodeDetails,
    Tvdb,
    anidb_for_anilist,
    episode_from_name,
    episode_nfo,
    parse_override,
    release_for_anidb,
    to_tvdb,
    tvshow_nfo,
)


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
        details = Tvdb(args.tvdb_key, args.tvdb_pin).episodes(release.tvdb_id, args.language)
        print(f"TVDB: {len(details)} episodes")
    else:
        print("No TVDB key: writing numbers only. Jellyfin will give these files the titles of the")
        print("episodes their filenames name; set TVDB_API_KEY to write the right ones.")

    planned: list[tuple[Path, str]] = []
    if not args.no_tvshow:
        planned.append((args.folder / "tvshow.nfo", tvshow_nfo(release.tvdb_id, args.title)))

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
