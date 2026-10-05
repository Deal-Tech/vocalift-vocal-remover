const youtubeHosts = new Set(['youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com', 'youtu.be', 'youtube-nocookie.com', 'www.youtube-nocookie.com']);
const tiktokHosts = new Set(['tiktok.com', 'www.tiktok.com', 'm.tiktok.com', 'vm.tiktok.com', 'vt.tiktok.com']);
export const audioExtensions = ['mp3', 'wav', 'flac', 'm4a', 'aac', 'ogg', 'opus'];

const youtubeVideoId = /^[A-Za-z0-9_-]{11}$/;
const youtubeIdPaths = new Set(['shorts', 'live', 'embed', 'v']);

function hostOf(url) {
  return url.hostname.toLowerCase().replace(/\.$/, '');
}

// Share sheets paste "Judul lagu https://youtu.be/…" and some apps copy links
// without a scheme; pick the token on a supported host. Mirrors the backend.
function extractUrl(value) {
  for (const raw of value.split(/\s+/)) {
    const token = raw.replace(/^[<>()[\]"'.,;]+|[<>()[\]"'.,;]+$/g, '');
    if (!token) continue;
    try {
      const url = new URL(token.includes('://') ? token : `https://${token}`);
      const host = hostOf(url);
      if (youtubeHosts.has(host) || tiktokHosts.has(host)) return url;
    } catch { /* not a link, keep looking */ }
  }
  return null;
}

function youtubeId(url, host) {
  const parts = url.pathname.split('/').filter(Boolean);
  let candidate = null;
  if (host === 'youtu.be') candidate = parts[0];
  else if (parts.length === 1 && parts[0] === 'watch') candidate = url.searchParams.get('v');
  else if (parts.length >= 2 && youtubeIdPaths.has(parts[0])) candidate = parts[1];
  return candidate && youtubeVideoId.test(candidate) ? candidate : null;
}

// Returns the platform and the link to send, or null when the text is not one
// public video. Playlists, channels and TikTok profiles are not single videos.
export function parseMediaLink(value) {
  const url = extractUrl(value ?? '');
  if (!url || !['http:', 'https:'].includes(url.protocol) || url.username || url.password || !['', '80', '443'].includes(url.port)) return null;
  const host = hostOf(url);
  if (youtubeHosts.has(host)) return youtubeId(url, host) ? { platform: 'youtube', url: url.href } : null;
  if (['vm.tiktok.com', 'vt.tiktok.com'].includes(host)) return { platform: 'tiktok', url: url.href };
  const parts = url.pathname.split('/').filter(Boolean);
  return parts[0] === 't' || parts.includes('video') || parts.includes('photo') ? { platform: 'tiktok', url: url.href } : null;
}

export function detectPlatform(value) {
  return parseMediaLink(value)?.platform ?? null;
}

export function formatBytes(bytes) {
  if (bytes == null) return '';
  return bytes < 1024 * 1024 ? `${(bytes / 1024).toFixed(1)} KB` : `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export const pollingRetryLimit = 5;

export function pollingRetryDelay(error, attempt) {
  if (!Number.isInteger(attempt) || attempt < 1 || attempt > pollingRetryLimit || error?.name === 'AbortError') return null;
  const status = error?.status;
  const transient = status === 408 || status === 429 || (status >= 500 && status < 600)
    || (status == null && error instanceof TypeError);
  return transient ? Math.min(1200 * 2 ** (attempt - 1), 10000) : null;
}

export async function request(url, options = {}) {
  const response = await fetch(url, { cache: 'no-store', ...options });
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = body?.detail || body?.message;
    const error = new Error(typeof detail === 'string' ? detail : Array.isArray(detail) ? detail.map(item => item.msg).join('; ') : `Server merespons ${response.status}.`);
    error.status = response.status;
    throw error;
  }
  if (!body) throw new Error('Respons server tidak valid.');
  return body;
}
