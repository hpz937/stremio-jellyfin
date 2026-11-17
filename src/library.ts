import { createLogger } from '@aiostreams/addon-sdk';
import { titlesIn, type Item, type Library } from './jellyfin.ts';

const log = createLogger('index');

export type Kind = 'movie' | 'series';

export interface Title {
  jellyfinId: string;
  libraryId: string;
  kind: Kind;
  name: string;
  year?: number;
  added: number;
  imdb?: string;
  tmdb?: string;
  tvdb?: string;
  anilist?: string;
  posterTag?: string;
  backdropTag?: string;
  /** The id clients use: the IMDb id when Jellyfin has one, else tmdb:, tvdb: or anilist:<id>. */
  addonId: string;
}

/** Provider ids as Jellyfin stores them, with keys of any case. */
function providerId(item: Item, provider: string): string | undefined {
  const entry = Object.entries(item.ProviderIds ?? {}).find(
    ([key]) => key.toLowerCase() === provider
  );
  return entry?.[1] || undefined;
}

function toTitle(item: Item, libraryId: string): Title | undefined {
  const kind: Kind | undefined =
    item.Type === 'Movie' ? 'movie' : item.Type === 'Series' ? 'series' : undefined;
  if (!kind) return undefined;
  const imdb = providerId(item, 'imdb');
  const tmdb = providerId(item, 'tmdb');
  const tvdb = providerId(item, 'tvdb');
  const anilist = providerId(item, 'anilist');
  const addonId = imdb?.startsWith('tt')
    ? imdb
    : tmdb
      ? `tmdb:${tmdb}`
      : tvdb
        ? `tvdb:${tvdb}`
        : anilist
          ? `anilist:${anilist}`
          : undefined;
  if (!addonId) return undefined;
  return {
    jellyfinId: item.Id,
    libraryId,
    kind,
    name: item.Name,
    year: item.ProductionYear,
    added: Date.parse(item.DateCreated ?? '') || 0,
    imdb,
    tmdb,
    tvdb,
    anilist,
    posterTag: item.ImageTags?.Primary,
    backdropTag: item.ImageTags?.Backdrop,
    addonId,
  };
}

interface Index {
  byLibrary: Map<string, Title[]>;
  /** `${kind}:${imdb id}` and `${kind}:tmdb|tvdb|anilist:${id}` → titles, possibly in several libraries. */
  byId: Map<string, Title[]>;
}

let index: Index = { byLibrary: new Map(), byId: new Map() };

/** Rebuild the whole index from Jellyfin. Keeps the old one if this fails. */
export async function rebuild(libraries: Library[]): Promise<void> {
  const next: Index = { byLibrary: new Map(), byId: new Map() };
  let unmatched = 0;
  for (const library of libraries) {
    const titles: Title[] = [];
    for (const item of await titlesIn(library.Id)) {
      const title = toTitle(item, library.Id);
      if (!title) {
        unmatched++;
        log.debug({ library: library.Name, name: item.Name }, 'no IMDb, TMDB, TVDB or AniList id, skipped');
        continue;
      }
      titles.push(title);
      const keys = [
        title.imdb && `${title.kind}:${title.imdb}`,
        title.tmdb && `${title.kind}:tmdb:${title.tmdb}`,
        title.tvdb && `${title.kind}:tvdb:${title.tvdb}`,
        title.anilist && `${title.kind}:anilist:${title.anilist}`,
      ];
      for (const key of keys) {
        if (!key) continue;
        const list = next.byId.get(key) ?? [];
        list.push(title);
        next.byId.set(key, list);
      }
    }
    titles.sort((a, b) => b.added - a.added);
    next.byLibrary.set(library.Id, titles);
  }
  index = next;
  const total = [...next.byLibrary.values()].reduce((n, list) => n + list.length, 0);
  log.info({ libraries: libraries.length, titles: total, unmatched }, 'index rebuilt');
}

/** Titles matching a client id (tt…, tmdb:…, tvdb:…, anilist:…) within the chosen libraries. */
export function find(kind: Kind, id: string, libraryIds: Set<string>): Title[] {
  return (index.byId.get(`${kind}:${id}`) ?? []).filter((title) => libraryIds.has(title.libraryId));
}

/** A library's titles, newest first. */
export function titles(libraryId: string): Title[] {
  return index.byLibrary.get(libraryId) ?? [];
}
