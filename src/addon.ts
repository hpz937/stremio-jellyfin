import {
  configureLogging,
  createLogger,
  defineAddon,
  notFound,
  type CatalogEntry,
  type StandardSchemaV1,
  type Stream,
} from '@aiostreams/addon-sdk';
import { serve } from '@aiostreams/addon-sdk/node';
import { z } from 'zod';
import { ANIME_SOURCES, animeRelease, refreshAnimeIds, type AnimeSource } from './anime.ts';
import { env, publicUrl } from './env.ts';
import {
  allEpisodes,
  getSession,
  libraries as listLibraries,
  movieSources,
  seasonEpisodes,
  type Item,
  type Library,
  type MediaSource,
} from './jellyfin.ts';
import { find, rebuild, titles, type Kind, type Title } from './library.ts';
import { load, save } from './store.ts';

configureLogging({ level: env.LOG_LEVEL });
const log = createLogger('addon');

// ---------------------------------------------------------------------------
// Libraries: read once at startup, since the configure page's choices are
// built from them. Restart the addon after adding or renaming a library;
// install links made before a rename keep working (see "Past library names").
// ---------------------------------------------------------------------------

/** Library kinds that hold movies or series; mixed libraries have no CollectionType. */
const KINDS_BY_COLLECTION: Record<string, Kind[]> = {
  movies: ['movie'],
  tvshows: ['series'],
  mixed: ['movie', 'series'],
};
const kindsOf = (library: Library) => KINDS_BY_COLLECTION[library.CollectionType ?? 'mixed'] ?? [];

async function loadLibraries(): Promise<Library[]> {
  for (let attempt = 1; ; attempt++) {
    try {
      return (await listLibraries()).filter((library) => kindsOf(library).length > 0);
    } catch (err) {
      const wait = Math.min(60, attempt * 5);
      log.warn({ err, attempt, retryInSeconds: wait }, 'cannot list Jellyfin libraries yet');
      await new Promise((resolve) => setTimeout(resolve, wait * 1000));
    }
  }
}

const libraries = await loadLibraries();
if (!libraries.length) {
  throw new Error(
    `Jellyfin user "${env.JELLYFIN_USERNAME}" can see no movie or TV libraries. Give it access to at least one.`
  );
}

// The configure page shows these labels; a duplicate name gets part of its id.
const labelOf = new Map<string, Library>();
for (const library of libraries) {
  const taken = libraries.filter((other) => other.Name === library.Name).length > 1;
  labelOf.set(taken ? `${library.Name} (${library.Id.slice(0, 6)})` : library.Name, library);
}
const labels = [...labelOf.keys()] as [string, ...string[]];
const libraryById = new Map(libraries.map((library) => [library.Id, library]));

const Settings = z.object({
  libraries: z
    .array(z.enum(labels))
    .min(1, 'Pick at least one library')
    .meta({
      title: 'Libraries',
      description: 'Only titles in these Jellyfin libraries are offered.',
    }),
  catalogs: z
    .boolean()
    .default(true)
    .meta({
      title: 'Show as catalogs',
      description:
        'Each chosen library becomes a catalog (a library in the AIOStreams app). Turn off to only add your files as versions of titles you open.',
    }),
  browserVersions: z
    .enum(['1080p', '720p', 'off'])
    .default('1080p')
    .meta({
      title: 'Browser versions',
      description:
        'Adds a second, transcoded link (H.264 + AAC) for files a web browser cannot play, like MKV, HEVC or 10-bit video. Jellyfin does the transcoding, so its user needs permission to transcode. Pick the maximum quality, or off.',
    }),
});
type Settings = z.output<typeof Settings>;

// ---------------------------------------------------------------------------
// Past library names. Install links hold library names (the configure page can
// only show what it stores). Every name a library has had is remembered, so a
// link made before a rename still finds the library under its new name.
// ---------------------------------------------------------------------------

const NAMES_FILE = 'library-names.json';
const pastNames = (await load<Record<string, string[]>>(NAMES_FILE)) ?? {};
const labelById = new Map([...labelOf].map(([label, library]) => [library.Id, label]));

/** Old name → current label, for libraries that still exist under another name. */
const renamed = new Map<string, string>();
for (const [libraryId, names] of Object.entries(pastNames)) {
  const current = labelById.get(libraryId);
  if (!current) continue;
  // A name now used by another library belongs to that library.
  for (const name of names) if (!labelOf.has(name)) renamed.set(name, current);
}

let namesChanged = false;
for (const [label, library] of labelOf) {
  const names = (pastNames[library.Id] ??= []);
  if (!names.includes(label)) {
    names.push(label);
    namesChanged = true;
  }
}
if (namesChanged) await save(NAMES_FILE, pastNames);
if (renamed.size) log.info({ renamed: Object.fromEntries(renamed) }, 'old library names still accepted');

function withCurrentNames(value: unknown): unknown {
  if (!renamed.size || typeof value !== 'object' || value === null) return value;
  const chosen = (value as { libraries?: unknown }).libraries;
  if (!Array.isArray(chosen)) return value;
  const current = chosen.map((name) => (typeof name === 'string' ? (renamed.get(name) ?? name) : name));
  return { ...value, libraries: [...new Set(current)] };
}

/** Settings, reading old library names as the current ones. The form is built from Settings unchanged. */
const settingsSchema: StandardSchemaV1<unknown, Settings> = {
  '~standard': {
    ...Settings['~standard'],
    validate: (value) => Settings['~standard'].validate(withCurrentNames(value)),
  },
};

const chosenIds = (config: Settings) =>
  new Set(config.libraries.flatMap((label) => labelOf.get(label)?.Id ?? []));

// ---------------------------------------------------------------------------
// Streams
// ---------------------------------------------------------------------------

function resolution(source: MediaSource): string | undefined {
  const video = source.MediaStreams?.find((stream) => stream.Type === 'Video');
  if (!video?.Width && !video?.Height) return undefined;
  // Width first: scope films are cropped, so 3840x1600 is still 2160p.
  const width = video.Width ?? 0;
  const height = video.Height ?? 0;
  if (width >= 3200 || height >= 2000) return '2160p';
  if (width >= 1800 || height >= 1000) return '1080p';
  if (width >= 1200 || height >= 700) return '720p';
  return `${height}p`;
}

function size(bytes: number | undefined): string | undefined {
  if (!bytes) return undefined;
  const gb = bytes / 1024 ** 3;
  return gb >= 1 ? `${gb.toFixed(2)} GB` : `${(bytes / 1024 ** 2).toFixed(0)} MB`;
}

const basename = (path: string | undefined) => path?.split(/[\\/]/).pop();

function toStream(item: Item, source: MediaSource, title: Title, token: string): Stream {
  const res = resolution(source);
  const video = source.MediaStreams?.find((stream) => stream.Type === 'Video');
  const audio = source.MediaStreams?.find((stream) => stream.Type === 'Audio');
  const filename = basename(source.Path);
  const url = new URL(`${publicUrl}/Videos/${item.Id}/stream`);
  url.searchParams.set('static', 'true');
  url.searchParams.set('mediaSourceId', source.Id);
  url.searchParams.set('ApiKey', token);
  const ticks = source.RunTimeTicks ?? item.RunTimeTicks;
  return {
    url: url.toString(),
    name: ['Jellyfin', res].filter(Boolean).join('\n'),
    description: [
      filename ?? source.Name,
      [size(source.Size), video?.Codec?.toUpperCase(), video?.VideoRangeType !== 'SDR' && video?.VideoRangeType, audio?.DisplayTitle]
        .filter(Boolean)
        .join(' · '),
      `📁 ${libraryById.get(title.libraryId)?.Name ?? 'Jellyfin'}`,
    ]
      .filter(Boolean)
      .join('\n'),
    behaviorHints: {
      filename,
      videoSize: source.Size,
      duration: ticks ? ticks / 10_000_000 : undefined,
      notWebReady: source.Container?.toLowerCase() !== 'mp4',
      bingeGroup: `jellyfin-local|${title.libraryId}|${res ?? 'unknown'}`,
    },
  };
}

/** Caps for each browser-version quality. */
const TRANSCODE = {
  '1080p': { maxWidth: 1920, videoBitrate: 8_000_000 },
  '720p': { maxWidth: 1280, videoBitrate: 4_000_000 },
} as const;

/**
 * The same file as an HLS stream Jellyfin transcodes to H.264 (8-bit) + stereo AAC,
 * which every browser plays. Jellyfin copies the video instead of re-encoding when it
 * already fits, so an 8-bit H.264 MKV is only repackaged.
 */
function toTranscodedStream(
  item: Item,
  source: MediaSource,
  title: Title,
  token: string,
  quality: keyof typeof TRANSCODE
): Stream {
  const { maxWidth, videoBitrate } = TRANSCODE[quality];
  const url = new URL(`${publicUrl}/Videos/${item.Id}/master.m3u8`);
  const params: Record<string, string | number> = {
    MediaSourceId: source.Id,
    ApiKey: token,
    DeviceId: 'aiostreams-jellyfin-local-browser',
    // One id per link, so two viewers never share (and kill) a transcode.
    PlaySessionId: crypto.randomUUID().replaceAll('-', ''),
    VideoCodec: 'h264',
    AudioCodec: 'aac',
    'h264-profile': 'high,main,baseline,constrainedbaseline',
    'h264-videobitdepth': 8,
    MaxWidth: maxWidth,
    VideoBitrate: videoBitrate,
    AudioBitrate: 192_000,
    TranscodingMaxAudioChannels: 2,
    SegmentContainer: 'ts',
  };
  for (const [key, value] of Object.entries(params)) url.searchParams.set(key, String(value));

  const sourceRes = resolution(source);
  const res = sourceRes && parseInt(sourceRes) < parseInt(quality) ? sourceRes : quality;
  const ticks = source.RunTimeTicks ?? item.RunTimeTicks;
  return {
    url: url.toString(),
    name: ['Jellyfin', res, 'Browser'].join('\n'),
    description: [
      basename(source.Path) ?? source.Name,
      '🔄 Transcoded for browsers · H.264 · AAC 2.0',
      `📁 ${libraryById.get(title.libraryId)?.Name ?? 'Jellyfin'}`,
    ]
      .filter(Boolean)
      .join('\n'),
    behaviorHints: {
      duration: ticks ? ticks / 10_000_000 : undefined,
      notWebReady: false,
      bingeGroup: `jellyfin-local|${title.libraryId}|browser-${res}`,
    },
  };
}

/** What a stream request asks for: a title, and for a series the episode in Jellyfin's numbering. */
interface Target {
  title: Title;
  season?: number;
  episode?: number;
}

const isAnimeSource = (prefix: string): prefix is AnimeSource =>
  (ANIME_SOURCES as readonly string[]).includes(prefix);

/**
 * `tt123[:1:5]` and `tmdb:|tvdb:123[:1:5]` name a title and its season/episode.
 * `anilist:|kitsu:|mal:123[:5]` name one anime release (a season or part) and the
 * episode within it, which is translated to Jellyfin's seasons.
 */
async function targetsFor(kind: Kind, id: string, libraryIds: Set<string>): Promise<Target[]> {
  const parts = id.split(':');
  const prefix = parts[0] ?? '';
  if (isAnimeSource(prefix)) {
    const episode = parts[2] === undefined ? undefined : Number(parts[2]);
    return animeTargets(kind, prefix, Number(parts[1]), episode, libraryIds);
  }
  const titleParts = prefix === 'tmdb' || prefix === 'tvdb' ? 2 : 1;
  const [season, episode] = parts.slice(titleParts).map(Number);
  return find(kind, parts.slice(0, titleParts).join(':'), libraryIds).map((title) => ({
    title,
    season,
    episode,
  }));
}

async function animeTargets(
  kind: Kind,
  source: AnimeSource,
  id: number,
  episode: number | undefined,
  libraryIds: Set<string>
): Promise<Target[]> {
  if (!Number.isInteger(id)) return [];
  const release = animeRelease(source, id);
  const anilist = source === 'anilist' ? id : release?.anilist;
  // Jellyfin's AniList plugin tags the series (or movie) it matched to one release.
  const direct = anilist ? find(kind, `anilist:${anilist}`, libraryIds) : [];
  if (kind === 'movie') return direct.map((title) => ({ title }));
  if (episode === undefined || !Number.isInteger(episode)) return [];

  const targets: Target[] = [];
  for (const title of direct) {
    const season = await releaseSeason(title, release);
    if (season !== undefined) targets.push({ title, season, episode });
  }
  if (targets.length) return targets;

  // Otherwise place the release by its TVDB season, as most libraries are numbered.
  // A split cour (two releases in one TVDB season) has no known episode offset, so
  // it's left out rather than risk playing the wrong episode.
  if (release?.tvdb && release.tvdbSeason && !release.sharesSeason) {
    for (const title of find(kind, `tvdb:${release.tvdb}`, libraryIds)) {
      targets.push({ title, season: release.tvdbSeason, episode });
    }
  }
  return targets;
}

/** The Jellyfin season holding a release matched by AniList id, when it's clear which. */
async function releaseSeason(title: Title, release: ReturnType<typeof animeRelease>): Promise<number | undefined> {
  const seasons = new Set(
    (await cachedEpisodes(title.jellyfinId)).flatMap((item) =>
      item.ParentIndexNumber ? [item.ParentIndexNumber] : []
    )
  );
  // A series holding just this release, whatever its season is numbered.
  if (seasons.size === 1) return [...seasons][0];
  const season = release?.tvdbSeason;
  return season && !release.sharesSeason && seasons.has(season) ? season : undefined;
}

// Episode lists are kept briefly: opening one episode usually means the next one
// is asked for soon.
const EPISODE_TTL = 5 * 60_000;
const episodeCache = new Map<string, { at: number; episodes: Promise<Item[]> }>();
/** One season with media sources, or every season (no season) without them. */
function cachedEpisodes(seriesId: string, season?: number): Promise<Item[]> {
  const key = `${seriesId}:${season ?? 'all'}`;
  const now = Date.now();
  const hit = episodeCache.get(key);
  if (hit && now - hit.at < EPISODE_TTL) return hit.episodes;
  // Drop expired entries so the cache doesn't grow with every series ever opened.
  for (const [other, entry] of episodeCache) if (now - entry.at >= EPISODE_TTL) episodeCache.delete(other);
  const episodes = season === undefined ? allEpisodes(seriesId) : seasonEpisodes(seriesId, season);
  episodes.catch(() => episodeCache.delete(key));
  episodeCache.set(key, { at: now, episodes });
  return episodes;
}

async function itemsFor(title: Title, season?: number, episode?: number): Promise<Item[]> {
  if (title.kind === 'movie') {
    const movie = await movieSources(title.jellyfinId);
    return movie ? [movie] : [];
  }
  if (season === undefined || episode === undefined || Number.isNaN(season) || Number.isNaN(episode)) {
    return [];
  }
  const episodes = await cachedEpisodes(title.jellyfinId, season);
  return episodes.filter(
    (item) =>
      item.ParentIndexNumber === season &&
      item.IndexNumber !== undefined &&
      // A double episode file (S01E01-E02) covers both numbers.
      item.IndexNumber <= episode &&
      episode <= (item.IndexNumberEnd ?? item.IndexNumber)
  );
}

// ---------------------------------------------------------------------------
// Catalogs
// ---------------------------------------------------------------------------

const PAGE = 100;
const image = (title: Title, kind: 'Primary' | 'Backdrop', tag: string | undefined, width: number) =>
  tag ? `${publicUrl}/Items/${title.jellyfinId}/Images/${kind}?tag=${tag}&maxWidth=${width}` : undefined;

function toEntry(title: Title): CatalogEntry {
  return {
    id: title.addonId,
    type: title.kind,
    name: title.name,
    poster: image(title, 'Primary', title.posterTag, 500),
    background: image(title, 'Backdrop', title.backdropTag, 1920),
    releaseInfo: title.year,
    ids: {
      imdb: title.imdb || undefined,
      tmdb: title.tmdb ? Number(title.tmdb) || title.tmdb : undefined,
      tvdb: title.tvdb ? Number(title.tvdb) || title.tvdb : undefined,
      anilist: title.anilist ? Number(title.anilist) || title.anilist : undefined,
    },
  };
}

const catalogIdOf = (library: Library) => `jellyfin-${library.Id}`;

// ---------------------------------------------------------------------------
// The addon
// ---------------------------------------------------------------------------

const addon = defineAddon({
  manifest: (config) => {
    const chosen = config ? libraries.filter((library) => chosenIds(config).has(library.Id)) : libraries;
    const showCatalogs = config?.catalogs ?? true;
    return {
      id: 'local.jellyfin.aiostreams',
      version: '0.1.0',
      name: 'Jellyfin Local',
      description:
        'Plays files from your chosen Jellyfin libraries: as versions of the titles you open, and optionally as catalogs.',
      types: ['movie', 'series'],
      idPrefixes: ['tt', 'tmdb:', 'tvdb:', ...ANIME_SOURCES.map((source) => `${source}:`)],
      resources: showCatalogs ? ['catalog', 'stream'] : ['stream'],
      catalogs: showCatalogs
        ? chosen.flatMap((library) =>
            kindsOf(library).map((type) => ({
              type,
              id: catalogIdOf(library),
              name: library.Name,
              extra: [{ name: 'skip' }, { name: 'search' }],
              poster: `${publicUrl}/Items/${library.Id}/Images/Primary?maxWidth=600`,
            }))
          )
        : [],
      behaviorHints: { configurable: true, configurationRequired: true },
    };
  },

  config: settingsSchema,
  theme: { accent: '#aa5cc3' },

  catalog: ({ type, id, extra, config }) => {
    const library = libraries.find((candidate) => catalogIdOf(candidate) === id);
    if (!library || !chosenIds(config).has(library.Id)) notFound();
    const search = extra.search?.trim().toLowerCase();
    const matching = titles(library.Id).filter(
      (title) => title.kind === type && (!search || title.name.toLowerCase().includes(search))
    );
    const skip = extra.skip ?? 0;
    return {
      metas: matching.slice(skip, skip + PAGE).map(toEntry),
      cacheMaxAge: 300,
    };
  },

  stream: async ({ type, id, config, log }) => {
    if (type !== 'movie' && type !== 'series') return { streams: [] };
    const targets = await targetsFor(type as Kind, id, chosenIds(config));
    if (!targets.length) return { streams: [] };

    const { token } = await getSession();
    const streams: Stream[] = [];
    for (const { title, season, episode } of targets) {
      for (const item of await itemsFor(title, season, episode)) {
        for (const source of item.MediaSources ?? []) {
          const direct = toStream(item, source, title, token);
          streams.push(direct);
          if (config.browserVersions !== 'off' && direct.behaviorHints?.notWebReady) {
            streams.push(toTranscodedStream(item, source, title, token, config.browserVersions));
          }
        }
      }
    }
    log.debug({ matches: targets.length, streams: streams.length }, 'streams found');
    // Short cache: stream links carry the session token.
    return { streams, cacheMaxAge: 300 };
  },
});

// ---------------------------------------------------------------------------
// Start
// ---------------------------------------------------------------------------

async function refresh() {
  try {
    await rebuild(libraries);
  } catch (err) {
    log.error({ err }, 'index rebuild failed; keeping the previous index');
  }
}

await Promise.all([refresh(), refreshAnimeIds()]);
setInterval(refresh, env.REFRESH_MINUTES * 60_000).unref();
setInterval(refreshAnimeIds, 60 * 60_000).unref(); // downloads only once the saved list is a day old
log.info({ libraries: labels }, 'libraries available');
await serve(addon, { port: env.PORT });
