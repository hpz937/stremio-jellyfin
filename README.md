# Jellyfin Local for AIOStreams

An AIOStreams/Stremio addon that offers files from **chosen Jellyfin libraries**:

- **Streams:** when you open a title (IMDb `tt…`, `tmdb:…`, `tvdb:…`, or for anime `anilist:…`, `kitsu:…`, `mal:…` id), every matching file in your chosen libraries is listed as a version, e.g. `Jellyfin 2160p`.
- **Browser versions (optional, on by default):** a file a web browser can't play (MKV, HEVC, 10-bit…) also gets a `Jellyfin 1080p Browser` link. Jellyfin transcodes it to H.264 + AAC over HLS, and only repackages it when the video already fits. The addon's Jellyfin user needs *Allow video playback that requires transcoding*.
- **Catalogs (optional):** each chosen library becomes a catalog, which is a library in the AIOStreams app.

Jellyfin does the matching (its provider ids) and serves the video. This addon only looks things up and returns links to Jellyfin: direct play, plus Jellyfin-transcoded browser versions when those are on.

## Setup

1. **Create a Jellyfin user for the addon** (Dashboard → Users), not an administrator. Under *Access*, give it only the libraries you might want in AIOStreams. Its session token goes into stream links, so it should not be able to do more than play those libraries.
2. `cp .env.example .env` and fill it in:
   - `JELLYFIN_URL`: how this container reaches Jellyfin.
   - `JELLYFIN_PUBLIC_URL`: how your TV/phone reaches Jellyfin. A LAN address also works over WireGuard if the tunnel routes your LAN subnet.
3. `docker compose up -d --build`
4. Open `http://<host>:7000/configure`, pick the libraries, copy the link, and add it to AIOStreams as a custom addon. To list Jellyfin first, put it at the top of your sort order.

The library choices are read when the addon starts, so restart it after adding or renaming a Jellyfin library. Install links name libraries, and the addon remembers every name a library has had, so links made before a rename keep working. Titles inside libraries are re-indexed every `REFRESH_MINUTES` (default 15).

Titles Jellyfin couldn't match to an IMDb, TMDB, TVDB or AniList id are skipped; run with `LOG_LEVEL=debug` to list them. You can fix a match in Jellyfin with *Identify*.

### Anime ids

Anime clients often ask by Kitsu, MyAnimeList or AniList id, which name one release (a season, or one part of a split season) and count episodes from 1 within it. The addon finds the file two ways:

1. A series Jellyfin's AniList plugin tagged with that release: the episode number is used as-is, in the series' only season (or the release's TVDB season).
2. Otherwise, [Fribb's anime-lists](https://github.com/Fribb/anime-lists) give the release's TVDB season, used on the series with that TVDB id.

A split season (two releases sharing one TVDB season, like *Part 2*) has no known episode offset, so it gets no streams rather than the wrong episode. The list is downloaded once a day; set `ANIME_IDS_URL=` (empty) to turn this off.

### Saved data

The `data` volume (`DATA_DIR`) holds the Jellyfin session, past library names and the anime id list. Reusing the session across restarts keeps stream links that were already handed out working; Jellyfin revokes the old token whenever the addon signs in again. All of it can be deleted; it's rebuilt on the next start.

## Development

```sh
npm install
npm run dev        # needs a .env
npm run typecheck
```

Requires Node 22.18+ (it runs the TypeScript directly). Built for Jellyfin 12, which only accepts the `Authorization: MediaBrowser …` header for API calls and `ApiKey=` in media links.
