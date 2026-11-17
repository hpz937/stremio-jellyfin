import { AddonError, createLogger } from '@aiostreams/addon-sdk';
import { env } from './env.ts';
import { load, save } from './store.ts';

const log = createLogger('jellyfin');

// Jellyfin 12 only accepts the `Authorization: MediaBrowser ...` header for API calls
// (X-Emby-Token and ?api_key= were removed), and `ApiKey=` in media URLs.
const CLIENT = 'Client="AIOStreams Local", Device="addon", DeviceId="aiostreams-jellyfin-local", Version="0.1.0"';

export interface Library {
  Id: string;
  Name: string;
  /** movies, tvshows, ...; absent for mixed libraries. */
  CollectionType?: string;
}

export interface MediaStream {
  Type: 'Video' | 'Audio' | 'Subtitle' | string;
  Codec?: string;
  Width?: number;
  Height?: number;
  Language?: string;
  VideoRangeType?: string;
  DisplayTitle?: string;
}

export interface MediaSource {
  Id: string;
  Name?: string;
  Path?: string;
  Container?: string;
  Size?: number;
  Bitrate?: number;
  RunTimeTicks?: number;
  MediaStreams?: MediaStream[];
}

export interface Item {
  Id: string;
  Name: string;
  Type: 'Movie' | 'Series' | 'Episode' | string;
  ProductionYear?: number;
  PremiereDate?: string;
  DateCreated?: string;
  ProviderIds?: Record<string, string>;
  ImageTags?: Record<string, string>;
  ParentIndexNumber?: number;
  IndexNumber?: number;
  IndexNumberEnd?: number;
  RunTimeTicks?: number;
  MediaSources?: MediaSource[];
}

interface Session {
  token: string;
  userId: string;
}

interface SavedSession extends Session {
  url: string;
  username: string;
}

const SESSION_FILE = 'session.json';

let session: Session | undefined;
let signingIn: Promise<Session> | undefined;
/** Whether the saved session has been tried, so a revoked one is only checked once. */
let restoreTried = false;

async function signIn(): Promise<Session> {
  const res = await fetch(`${env.JELLYFIN_URL}/Users/AuthenticateByName`, {
    method: 'POST',
    headers: {
      authorization: `MediaBrowser ${CLIENT}`,
      'content-type': 'application/json',
    },
    body: JSON.stringify({ Username: env.JELLYFIN_USERNAME, Pw: env.JELLYFIN_PASSWORD }),
    signal: AbortSignal.timeout(10_000),
  });
  if (!res.ok) {
    throw new Error(`Jellyfin sign-in as "${env.JELLYFIN_USERNAME}" failed with ${res.status}`);
  }
  const body = (await res.json()) as { AccessToken: string; User: { Id: string } };
  log.info({ user: env.JELLYFIN_USERNAME }, 'signed in to Jellyfin');
  return { token: body.AccessToken, userId: body.User.Id };
}

/**
 * The session saved by an earlier run, if Jellyfin still accepts it. Jellyfin revokes a
 * device's old token whenever it signs in again, so reusing it keeps stream links that
 * were handed out before a restart working.
 */
async function restore(): Promise<Session | undefined> {
  const saved = await load<SavedSession>(SESSION_FILE);
  if (!saved || saved.url !== env.JELLYFIN_URL || saved.username !== env.JELLYFIN_USERNAME) {
    restoreTried = true;
    return undefined;
  }
  // A network error throws, leaving restoreTried false so the next attempt checks again.
  const res = await fetch(`${env.JELLYFIN_URL}/Users/Me`, {
    headers: { authorization: `MediaBrowser ${CLIENT}, Token="${saved.token}"` },
    signal: AbortSignal.timeout(10_000),
  });
  restoreTried = true;
  if (!res.ok) {
    log.info({ status: res.status }, 'saved Jellyfin session no longer valid; signing in');
    return undefined;
  }
  log.info({ user: env.JELLYFIN_USERNAME }, 'reusing saved Jellyfin session');
  return { token: saved.token, userId: saved.userId };
}

async function startSession(): Promise<Session> {
  const restored = restoreTried ? undefined : await restore();
  if (restored) return restored;
  const fresh = await signIn();
  await save(SESSION_FILE, {
    ...fresh,
    url: env.JELLYFIN_URL,
    username: env.JELLYFIN_USERNAME,
  } satisfies SavedSession);
  return fresh;
}

/** The current session, signing in once if needed (concurrent callers share one sign-in). */
export async function getSession(): Promise<Session> {
  if (session) return session;
  signingIn ??= startSession().finally(() => (signingIn = undefined));
  session = await signingIn;
  return session;
}

/** GET a Jellyfin API path as the addon's user, signing in again once if the token was revoked. */
export async function api<T>(
  path: string,
  params: Record<string, string | number | boolean | undefined> = {},
  retried = false
): Promise<T> {
  const { token, userId } = await getSession();
  const url = new URL(`${env.JELLYFIN_URL}${path}`);
  url.searchParams.set('userId', userId);
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined) url.searchParams.set(key, String(value));
  }
  let res: Response;
  try {
    res = await fetch(url, {
      headers: { authorization: `MediaBrowser ${CLIENT}, Token="${token}"` },
      signal: AbortSignal.timeout(15_000),
    });
  } catch (err) {
    log.error({ err, path }, 'Jellyfin unreachable');
    throw new AddonError(502, 'Jellyfin is unreachable');
  }
  if (res.status === 401 && !retried) {
    session = undefined;
    return api<T>(path, params, true);
  }
  if (!res.ok) throw new Error(`Jellyfin answered ${res.status} to ${path}`);
  return (await res.json()) as T;
}

/** The libraries the addon's user can see. */
export async function libraries(): Promise<Library[]> {
  const { Items } = await api<{ Items: Library[] }>('/UserViews');
  return Items;
}

/** Every movie and series in a library, without media details. */
export async function titlesIn(libraryId: string): Promise<Item[]> {
  const { Items } = await api<{ Items: Item[] }>('/Items', {
    parentId: libraryId,
    recursive: true,
    includeItemTypes: 'Movie,Series',
    fields: 'ProviderIds,DateCreated,PremiereDate',
    enableUserData: false,
    enableTotalRecordCount: false,
  });
  return Items;
}

/** A movie with its media sources (files/versions). */
export async function movieSources(itemId: string): Promise<Item | undefined> {
  const { Items } = await api<{ Items: Item[] }>('/Items', {
    ids: itemId,
    fields: 'MediaSources,Path',
    enableUserData: false,
  });
  return Items[0];
}

/** Every episode of a series in every season, without media details. */
export async function allEpisodes(seriesId: string): Promise<Item[]> {
  const { Items } = await api<{ Items: Item[] }>(`/Shows/${seriesId}/Episodes`, {
    enableUserData: false,
    enableImages: false,
  });
  return Items;
}

/** The episodes of one season, with their media sources. */
export async function seasonEpisodes(seriesId: string, season: number): Promise<Item[]> {
  const { Items } = await api<{ Items: Item[] }>(`/Shows/${seriesId}/Episodes`, {
    season,
    fields: 'MediaSources,Path',
    enableUserData: false,
  });
  return Items;
}
