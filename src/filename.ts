import type { Item, MediaSource, MediaStream } from './jellyfin.ts';
import type { Title } from './library.ts';

/**
 * A clean filename for AIOStreams to parse (it reads `behaviorHints.filename` first),
 * built from what Jellyfin knows instead of what the file happens to be called:
 *
 *   [DB]High School DxD_-_SP01_(10bit_BD1080p_x265).mkv
 *   -> High School DxD - S00E01 [BluRay 1080p HEVC 10bit][FLAC 2.0][Japanese][Eng Subs].mkv
 *
 * Title and SxxEyy come from Jellyfin's series and episode numbers; resolution, codec,
 * bit depth, HDR, audio and languages from its media streams. Only the source (BluRay,
 * WEB-DL...), streaming service, Uncensored and the release group are read from the old
 * file and folder names. Spellings are the ones AIOStreams' parser recognises.
 * Episode titles are left out: the parser scans the whole name for tags, so a title
 * like "The Dub" would mark the file as dubbed.
 */

export type GroupPlacement = 'remove' | 'prefix' | 'suffix';

/** A tag must start and end at a separator (or the ends of the name), as in AIOStreams. */
const tag = (pattern: string) => new RegExp(`(?<![^\\s\\[(_\\-.,])(?:${pattern})(?=[\\s\\[()\\]_.\\-,+]|$)`, 'i');

const SOURCES: [string, RegExp][] = [
  ['BluRay REMUX', tag('(?:bd|br|blu[ .\\-_]?ray|uhd)?[ .\\-_]?remux')],
  ['BluRay', tag('(?:bd|blu[ .\\-_]?ray)(?:[ .\\-_]?rip)?(?:\\d{3,4}p)?|br[ .\\-_]?rip|bdmv')],
  ['WEBRip', tag('web[ .\\-_]?rip')],
  ['WEB-DL', tag('web(?:[ .\\-_]?dl)?(?:\\d{3,4}p)?')],
  ['DVDRip', tag('dvd(?:[ .\\-_]?rip)?|dvd5|dvd9')],
  ['HDTV', tag('hdtv|tv[ .\\-_]?rip')],
];
const NETWORKS: [string, RegExp][] = [
  ['CR', tag('cr|crunchyroll')],
  ['AMZN', tag('amzn|amazon')],
  ['NF', tag('nf|netflix')],
  ['DSNP', tag('dsnp|disney\\+?')],
  ['HIDIVE', tag('hidi(?:ve)?')],
  ['ADN', tag('adn')],
  ['B-Global', tag('b[ .\\-_]?global')],
];
const UNCENSORED = tag('uncen(?:sored)?');
/** A leading [tag] that is a release tag, not a group: [1080p], [CRC32], [BD]... */
const NOT_GROUP = /^(?:\d{3,4}[pi]|[0-9A-F]{8}|BD|WEB|BluRay|HEVC|x26[45]|10bit|Batch|Dual Audio)$/i;
const SUFFIX_GROUP = /(?<=[\w\])])-([A-Za-z0-9][A-Za-z0-9_&]*)$/;

const first = (table: [string, RegExp][], text: string) => table.find(([, pattern]) => pattern.test(text))?.[0];

const VIDEO_EXTENSION = /\.(?:mkv|mp4|m4v|avi|webm|ts|mov|wmv)$/i;

function releaseGroup(name: string): string | undefined {
  const leading = name.match(/^\s*\[([^\]]+)\]/);
  if (leading?.[1] && !NOT_GROUP.test(leading[1].trim())) return leading[1].trim();
  // Scene style, "...x264-GROUP": only once trailing bracketed tags are dropped.
  const bare = name.replace(/\s*(?:\[[^\]]*\]|\([^)]*\))\s*$/, '').trim();
  const suffix = bare.match(SUFFIX_GROUP)?.[1];
  if (suffix && !/^\d{3,4}p$/i.test(suffix) && !first(SOURCES, suffix)) return suffix;
  return undefined;
}

interface NameTags {
  group?: string;
  source?: string;
  network?: string;
  uncensored: boolean;
}

/** What the old file and folder names say; the first name with a tag wins. */
function nameTags(names: string[]): NameTags {
  const tags: NameTags = { uncensored: false };
  for (const raw of names) {
    const name = raw.replace(VIDEO_EXTENSION, '');
    const text = name.replaceAll('_', ' ');
    tags.group ??= releaseGroup(name);
    tags.source ??= first(SOURCES, text);
    tags.network ??= first(NETWORKS, text);
    tags.uncensored ||= UNCENSORED.test(text);
  }
  // The network patterns are short ("CR", "NF"); only trust them for web releases.
  if (tags.source !== 'WEB-DL' && tags.source !== 'WEBRip') tags.network = undefined;
  return tags;
}

const LANGUAGES: Record<string, string> = {
  jpn: 'Japanese', eng: 'English', chi: 'Chinese', zho: 'Chinese', kor: 'Korean',
  spa: 'Spanish', por: 'Portuguese', fre: 'French', fra: 'French', ger: 'German',
  deu: 'German', ita: 'Italian', rus: 'Russian', ara: 'Arabic', hin: 'Hindi',
  tha: 'Thai', vie: 'Vietnamese', ind: 'Indonesian', may: 'Malay', msa: 'Malay',
  pol: 'Polish', tur: 'Turkish', dut: 'Dutch', nld: 'Dutch',
};
const AUDIO_CODECS: Record<string, string> = {
  aac: 'AAC', ac3: 'DD', eac3: 'DDP', truehd: 'TrueHD', dts: 'DTS',
  flac: 'FLAC', opus: 'OPUS', mp3: 'MP3', vorbis: 'Vorbis',
};
const VIDEO_CODECS: Record<string, string> = { hevc: 'HEVC', h265: 'HEVC', h264: 'AVC', av1: 'AV1', vp9: 'VP9' };
const CHANNELS: Record<number, string> = { 1: '1.0', 2: '2.0', 6: '5.1', 7: '6.1', 8: '7.1' };
/** Jellyfin's VideoRangeType -> parser tags. */
const RANGES: Record<string, string[]> = {
  HDR10: ['HDR'],
  HDR10Plus: ['HDR10+'],
  HLG: ['HLG'],
  DOVI: ['DV'],
  DOVIWithHDR10: ['DV', 'HDR'],
  DOVIWithHDR10Plus: ['DV', 'HDR10+'],
  DOVIWithHLG: ['DV', 'HLG'],
  DOVIWithSDR: ['DV'],
};

function resolution(video: MediaStream | undefined): string | undefined {
  if (!video?.Width && !video?.Height) return undefined;
  // Width first: scope films are cropped, so 3840x1600 is still 2160p.
  const width = video.Width ?? 0;
  const height = video.Height ?? 0;
  if (width >= 3200 || height >= 2000) return '2160p';
  if (width >= 1800 || height >= 1000) return '1080p';
  if (width >= 1200 || height >= 700) return '720p';
  if (width >= 900 || height >= 540) return '576p';
  return '480p';
}

const languagesOf = (streams: MediaStream[]) => [
  ...new Set(streams.flatMap((stream) => LANGUAGES[stream.Language?.toLowerCase() ?? ''] ?? [])),
];

function tagGroups(source: MediaSource, tags: NameTags): string[] {
  const streams = source.MediaStreams ?? [];
  const video = streams.find((stream) => stream.Type === 'Video');
  const audios = streams.filter((stream) => stream.Type === 'Audio');
  const audio = audios.find((stream) => stream.IsDefault) ?? audios[0];

  let codec = audio?.Codec ? AUDIO_CODECS[audio.Codec.toLowerCase()] : undefined;
  if (codec === 'DTS' && /\bMA\b/.test(audio?.Profile ?? '')) codec = 'DTS-HD MA';
  if (codec && /atmos/i.test(`${audio?.Profile ?? ''} ${audio?.DisplayTitle ?? ''}`)) codec += ' Atmos';

  const audioLanguages = languagesOf(audios);
  const subtitleLanguages = languagesOf(streams.filter((stream) => stream.Type === 'Subtitle'));

  const groups = [
    [
      tags.network,
      tags.source,
      resolution(video),
      video?.Codec ? VIDEO_CODECS[video.Codec.toLowerCase()] : undefined,
      (video?.BitDepth ?? 8) >= 10 ? '10bit' : undefined,
      ...(RANGES[video?.VideoRangeType ?? ''] ?? []),
      tags.uncensored ? 'Uncensored' : undefined,
    ],
    [codec, audio?.Channels ? CHANNELS[audio.Channels] : undefined],
    // One language is still named, so a Japanese-only file isn't taken for a dub.
    audioLanguages.length >= 3
      ? ['Multi Audio', ...audioLanguages]
      : audioLanguages.length === 2
        ? ['Dual Audio', ...audioLanguages]
        : audioLanguages,
    subtitleLanguages.length >= 2
      ? ['Multi Subs']
      : subtitleLanguages[0]
        ? [`${subtitleLanguages[0].slice(0, 3)} Subs`]
        : [],
  ];
  return groups.map((group) => group.filter(Boolean).join(' ')).filter(Boolean);
}

/** A title fit for a filename: 'Re:Zero' -> 'Re Zero', 'A: B' -> 'A - B'. */
function safeTitle(text: string): string {
  return text
    .replace(/\s*:\s+/g, ' - ')
    .replaceAll(':', ' ')
    .replace(/[<>"/\\|?*\u0000-\u001f]/g, '')
    .replace(/\s+/g, ' ')
    .trim();
}

const pad = (n: number) => String(n).padStart(2, '0');

/** The clean name, or undefined when Jellyfin hasn't numbered the episode. */
export function cleanFilename(item: Item, source: MediaSource, title: Title, group: GroupPlacement): string | undefined {
  const parts = (source.Path ?? '').split(/[\\/]/);
  const original = parts.at(-1) ?? '';
  const folder = parts.at(-2) ?? '';
  const ext = original.match(/\.[A-Za-z0-9]{2,4}$/)?.[0] ?? (source.Container ? `.${source.Container.split(',')[0]}` : '');

  let name: string;
  if (title.kind === 'movie') {
    name = `${safeTitle(title.name)}${title.year ? ` (${title.year})` : ''}`;
  } else {
    if (item.ParentIndexNumber === undefined || item.IndexNumber === undefined) return undefined;
    const end = item.IndexNumberEnd && item.IndexNumberEnd > item.IndexNumber ? `-E${pad(item.IndexNumberEnd)}` : '';
    name = `${safeTitle(title.name)} - S${pad(item.ParentIndexNumber)}E${pad(item.IndexNumber)}${end}`;
  }

  const tags = nameTags([original, folder]);
  name += tagGroups(source, tags)
    .map((group) => ` [${group}]`)
    .join('')
    .replace(/\] \[/g, '][');

  if (tags.group && group === 'prefix') name = `[${tags.group}] ${name}`;
  if (tags.group && group === 'suffix') name = `${name}-${tags.group.replace(/[^A-Za-z0-9_&]+/g, '')}`;
  return name + ext;
}
