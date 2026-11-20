# Jellyfin Local for AIOStreams

An AIOStreams/Stremio addon that offers files from **chosen Jellyfin libraries**:

- **Streams:** when you open a title (IMDb `tt…`, `tmdb:…`, `tvdb:…`, or for anime `anilist:…`, `kitsu:…`, `mal:…` id), every matching file in your chosen libraries is listed as a version, e.g. `Jellyfin 2160p`.
- **Browser versions (optional, on by default):** a file a web browser can't play (MKV, HEVC, 10-bit…) also gets a `Jellyfin 1080p Browser` link. Jellyfin transcodes it to H.264 + AAC over HLS, and only repackages it when the video already fits. The addon's Jellyfin user needs *Allow video playback that requires transcoding*.
- **Catalogs (optional):** each chosen library becomes a catalog, which is a library in the AIOStreams app.
- **Watched status (optional):** what you finish, stop or mark watched in AIOStreams is marked in Jellyfin for a user you pick. See [Watched status](#watched-status).

Jellyfin does the matching (its provider ids) and serves the video. This addon only looks things up and returns links to Jellyfin: direct play, plus Jellyfin-transcoded browser versions when those are on.

## Setup

1. **Create a Jellyfin user for the addon** (Dashboard → Users), not an administrator. Under *Access*, give it only the libraries you might want in AIOStreams. Its session token goes into stream links, so it should not be able to do more than play those libraries.
2. `cp .env.example .env` and fill it in:
   - `JELLYFIN_URL`: how this container reaches Jellyfin.
   - `JELLYFIN_PUBLIC_URL`: how your TV/phone reaches Jellyfin. A LAN address also works over WireGuard if the tunnel routes your LAN subnet.
   - `ACCESS_KEY`: a password for the configure page (`openssl rand -base64 24`). See [Access key](#access-key).
3. `docker compose up -d --build`
4. Open `http://<host>:7000/configure`, enter the access key, pick the libraries, copy the link, and add it to AIOStreams as a custom addon. To list Jellyfin first, put it at the top of your sort order.

### Access key

Every install link uses the addon's one Jellyfin user, and its stream links carry that user's token. So the configure page only makes a link for someone who enters `ACCESS_KEY`, and install links are encrypted, so neither the key nor your settings can be read out of one.

An install link is still all it takes to use the addon, so share it like a password. To revoke every link, change `ACCESS_KEY` and restart; then make new links. Jellyfin tokens already sent in stream links stay valid until Jellyfin revokes them (sign the addon's user out under Dashboard → Devices, or delete the `data` volume).

Anyone who can open `/configure` still sees your library names. Keep the addon off the public internet unless it's behind a reverse proxy you trust.

The library choices are read when the addon starts, so restart it after adding or renaming a Jellyfin library. Install links name libraries, and the addon remembers every name a library has had, so links made before a rename keep working. Titles inside libraries are re-indexed every `REFRESH_MINUTES` (default 15).

Titles Jellyfin couldn't match to an IMDb, TMDB, TVDB or AniList id are skipped; run with `LOG_LEVEL=debug` to list them. You can fix a match in Jellyfin with *Identify*.

### Watched status

Pick a Jellyfin user under *Sync watched status to* on the configure page, and reinstall the link in AIOStreams. AIOStreams then tells the addon (through its *Watch State* resource) whenever a movie or episode is:

- finished (past AIOStreams' played threshold, 90% by default): it's marked watched in Jellyfin;
- stopped part way (past 5%): Jellyfin gets its resume point;
- marked watched or unwatched by hand, including a whole season or show at once.

This works whichever addon's stream you watched, as long as the title is in one of the link's libraries. Every version of it there is marked. Episodes are found the same way as their streams, so anime ids work too.

Out of the box only the addon's own Jellyfin user can be picked. To sync to your own account, create an API key in Jellyfin (Dashboard → API Keys) and set `JELLYFIN_API_KEY`; every Jellyfin user can then be picked. The key can change any user's data, but it stays on this server and never goes into a link. With it set, the configure page lists Jellyfin user names, like it lists library names. The user list is read when the addon starts, so restart it after adding a user.

Each install link syncs to one Jellyfin user, so give each person their own link. To turn syncing off, pick *off*, or untick *Watch State* in the addon's Resources in AIOStreams.

### Anime ids

Anime clients often ask by Kitsu, MyAnimeList or AniList id, which name one release (a season, or one part of a split season) and count episodes from 1 within it. The addon finds the file two ways:

1. A series Jellyfin's AniList plugin tagged with that release: the episode number is used as-is, in the series' only season (or the release's TVDB season).
2. Otherwise, [Fribb's anime-lists](https://github.com/Fribb/anime-lists) give the release's TVDB season, used on the series with that TVDB id.

A split season (two releases sharing one TVDB season, like *Part 2*) has no known episode offset, so it gets no streams rather than the wrong episode. The list is downloaded once a day; set `ANIME_IDS_URL=` (empty) to turn this off.

### Filenames

AIOStreams reads a stream's resolution, codec, audio and languages from its filename, which anime releases rarely spell out (`[DB]High School DxD_-_SP01_(10bit_BD1080p_x265).mkv`). With *Filenames for AIOStreams* on `clean` (the default), the addon sends a name built from Jellyfin's series title, season/episode numbers and media streams instead:

```
High School DxD - S00E01 [BluRay 1080p HEVC 10bit][FLAC 2.0][Japanese][Eng Subs].mkv
```

Only the source (BluRay, WEB-DL...), streaming service, *Uncensored* and the release group come from the real file and folder names. *Release group* leaves the group out (default), or puts it first (`[Group] ...`) or last (`...-Group`). The stream description still shows the real filename.

### Anime tools

`tools/` has two scripts (Python 3, standard library only) for anime folders, each holding one AniList release. Both preview by default.

- `anime-nfo.py FOLDER --anilist ID [--write]` writes Jellyfin `.nfo` files that give the files their TVDB season/episode without renaming them.
- `anime-rename.py FOLDER --anilist ID | --tvdb ID` gives the files clean names like the one above. `--link DEST` hardlinks them into `DEST/<Title> [tvdbid-N]/Season NN/`, leaving the originals for AniList-based tools (same filesystem only). `--rename` renames in place. Either writes a manifest for `--undo`. With ffprobe installed (`apt install ffmpeg`) the media info is measured; otherwise it's read from the old names. See `--help` for `--group`, `--template` and `--nfo`.

Set `TVDB_API_KEY` for series and episode titles.

### Saved data

The `data` volume (`DATA_DIR`) holds the Jellyfin session, past library names and the anime id list. Reusing the session across restarts keeps stream links that were already handed out working; Jellyfin revokes the old token whenever the addon signs in again. All of it can be deleted; it's rebuilt on the next start.

## Development

```sh
npm install
npm run dev        # needs a .env
npm run typecheck
```

Requires Node 22.18+ (it runs the TypeScript directly). Built for Jellyfin 12, which only accepts the `Authorization: MediaBrowser …` header for API calls and `ApiKey=` in media links.
