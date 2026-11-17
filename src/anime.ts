import { createLogger } from '@aiostreams/addon-sdk';
import { env } from './env.ts';
import { age, load, save } from './store.ts';

const log = createLogger('anime-ids');

/** The id kinds anime clients use, each naming one release (a season or a part of one). */
export const ANIME_SOURCES = ['anilist', 'kitsu', 'mal'] as const;
export type AnimeSource = (typeof ANIME_SOURCES)[number];

/** Where one anime release sits in TVDB numbering. */
export interface AnimeRelease {
  anilist?: number;
  tvdb?: number;
  tvdbSeason?: number;
  /**
   * Another release shares this TVDB season (a split cour, e.g. "Part II"). The list
   * gives no episode offset, so the release's episode 1 can't be placed in the season.
   */
  sharesSeason: boolean;
}

/** One entry of Fribb's anime-lists, only the fields used here. */
interface Entry {
  anilist_id?: number;
  kitsu_id?: number;
  mal_id?: number;
  tvdb_id?: number;
  season?: { tvdb?: number };
}

const CACHE_FILE = 'anime-ids.json';
const MAX_AGE = 24 * 60 * 60_000;

let bySource: Record<AnimeSource, Map<number, AnimeRelease>> | undefined;

function build(entries: Entry[]): void {
  // Count releases per TVDB season to spot split cours.
  const perSeason = new Map<string, number>();
  for (const entry of entries) {
    if (!entry.tvdb_id || entry.season?.tvdb === undefined) continue;
    const key = `${entry.tvdb_id}:${entry.season.tvdb}`;
    perSeason.set(key, (perSeason.get(key) ?? 0) + 1);
  }
  const next = { anilist: new Map(), kitsu: new Map(), mal: new Map() } satisfies typeof bySource;
  for (const entry of entries) {
    const season = entry.season?.tvdb;
    const release: AnimeRelease = {
      anilist: entry.anilist_id,
      tvdb: entry.tvdb_id,
      tvdbSeason: season,
      sharesSeason: (perSeason.get(`${entry.tvdb_id}:${season}`) ?? 0) > 1,
    };
    if (entry.anilist_id) next.anilist.set(entry.anilist_id, release);
    if (entry.kitsu_id) next.kitsu.set(entry.kitsu_id, release);
    if (entry.mal_id) next.mal.set(entry.mal_id, release);
  }
  bySource = next;
}

async function download(): Promise<Entry[]> {
  const res = await fetch(env.ANIME_IDS_URL, { signal: AbortSignal.timeout(60_000) });
  if (!res.ok) throw new Error(`anime id list answered ${res.status}`);
  const all = (await res.json()) as Entry[];
  // Keep only what's used: the full list is ~7.5 MB, this is a fraction of it.
  return all.map(({ anilist_id, kitsu_id, mal_id, tvdb_id, season }) => ({
    anilist_id,
    kitsu_id,
    mal_id,
    tvdb_id,
    season: season?.tvdb === undefined ? undefined : { tvdb: season.tvdb },
  }));
}

/** Load the list from the saved copy, downloading a new one once a day. */
export async function refreshAnimeIds(): Promise<void> {
  if (!env.ANIME_IDS_URL) return;
  const savedAge = await age(CACHE_FILE);
  if (!bySource && savedAge !== undefined) {
    const saved = await load<Entry[]>(CACHE_FILE);
    if (saved) build(saved);
  }
  if (savedAge !== undefined && savedAge < MAX_AGE && bySource) return;
  try {
    const entries = await download();
    build(entries);
    await save(CACHE_FILE, entries);
    log.info({ entries: entries.length }, 'anime id list updated');
  } catch (err) {
    log.warn({ err, usingSaved: Boolean(bySource) }, 'cannot download the anime id list');
  }
}

/** The release an anime id names, or undefined when it's unknown or the list is off. */
export function animeRelease(source: AnimeSource, id: number): AnimeRelease | undefined {
  return bySource?.[source].get(id);
}
