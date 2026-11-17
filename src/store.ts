import { mkdir, readFile, rename, stat, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { createLogger } from '@aiostreams/addon-sdk';
import { env } from './env.ts';

const log = createLogger('store');

/**
 * Small JSON files kept in DATA_DIR across restarts. Everything stored here can be
 * rebuilt, so a missing or unwritable directory only costs a warning.
 */
export async function load<T>(name: string): Promise<T | undefined> {
  try {
    return JSON.parse(await readFile(join(env.DATA_DIR, name), 'utf8')) as T;
  } catch (err) {
    if ((err as NodeJS.ErrnoException).code !== 'ENOENT') log.warn({ err, name }, 'cannot read saved file');
    return undefined;
  }
}

/** How old a saved file is, in milliseconds; undefined when there is none. */
export async function age(name: string): Promise<number | undefined> {
  try {
    return Date.now() - (await stat(join(env.DATA_DIR, name))).mtimeMs;
  } catch {
    return undefined;
  }
}

export async function save(name: string, value: unknown): Promise<void> {
  const path = join(env.DATA_DIR, name);
  try {
    await mkdir(env.DATA_DIR, { recursive: true });
    // Write then rename, so a crash mid-write never leaves a half file behind.
    await writeFile(`${path}.tmp`, JSON.stringify(value), { mode: 0o600 });
    await rename(`${path}.tmp`, path);
  } catch (err) {
    log.warn({ err, name, dir: env.DATA_DIR }, 'cannot save file; it will be rebuilt after a restart');
  }
}
