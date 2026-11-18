import { defineEnv } from '@aiostreams/addon-sdk';
import { z } from 'zod';

const url = z
  .url()
  .transform((value) => value.replace(/\/+$/, ''));

export const env = defineEnv({
  PORT: z.coerce.number().int().default(7000),
  LOG_LEVEL: z.enum(['debug', 'info', 'warn', 'error']).default('info'),

  /** How this addon reaches Jellyfin, e.g. http://jellyfin:8096 on a shared docker network. */
  JELLYFIN_URL: url,
  /**
   * The address your playback devices use for Jellyfin. It goes into every stream
   * link and poster, so it must work from the TV/phone, not just from this container.
   * Defaults to JELLYFIN_URL.
   */
  JELLYFIN_PUBLIC_URL: url.optional(),

  /** A dedicated non-admin Jellyfin user; its token ends up in stream links. */
  JELLYFIN_USERNAME: z.string().min(1),
  JELLYFIN_PASSWORD: z.string().default(''),

  /**
   * Asked for on the configure page before an install link is made, so someone who
   * finds this addon can't get a link (and with it the Jellyfin token). Install links
   * are encrypted with a key derived from it; changing it revokes every link.
   */
  ACCESS_KEY: z.string().min(12, 'use at least 12 characters'),

  /** How often the library index is rebuilt. */
  REFRESH_MINUTES: z.coerce.number().min(1).default(15),

  /** Where the Jellyfin session, past library names and the anime id list are kept. */
  DATA_DIR: z.string().default('data'),

  /**
   * Maps Kitsu, MyAnimeList and AniList ids to TVDB seasons, so anime opened with
   * those ids still finds your files. Set to an empty value to turn this off.
   */
  ANIME_IDS_URL: z
    .union([z.url(), z.literal('')])
    .default('https://raw.githubusercontent.com/Fribb/anime-lists/master/anime-list-full.json'),
});

export const publicUrl = env.JELLYFIN_PUBLIC_URL ?? env.JELLYFIN_URL;
