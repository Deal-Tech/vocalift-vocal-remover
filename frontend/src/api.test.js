import assert from 'node:assert/strict';
import { test } from 'node:test';
import { detectPlatform, parseMediaLink, pollingRetryDelay, pollingRetryLimit, request } from './api.js';

test('single YouTube videos are recognised in every common link shape', () => {
  for (const pasted of [
    'https://www.youtube.com/watch?v=dQw4w9WgXcQ',
    'https://youtu.be/dQw4w9WgXcQ?si=Ab12Cd34',
    'https://music.youtube.com/watch?v=dQw4w9WgXcQ&si=x',
    'https://www.youtube.com/shorts/dQw4w9WgXcQ',
    'https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=RDdQw4w9WgXcQ&start_radio=1',
    'youtu.be/dQw4w9WgXcQ',
    'www.youtube.com/watch?v=dQw4w9WgXcQ',
    'Dengerin ini deh https://youtu.be/dQw4w9WgXcQ?si=x mantap',
  ]) {
    const link = parseMediaLink(pasted);
    assert.equal(link?.platform, 'youtube', pasted);
    assert.match(link.url, /^https:\/\//, pasted);
  }
});

test('YouTube pages that are not one video are not offered for processing', () => {
  for (const pasted of [
    'https://www.youtube.com/playlist?list=PLx0sYbCqOb8TBPRdmBHs5Iftvv9TPboYG',
    'https://www.youtube.com/@RickAstleyYT',
    'https://www.youtube.com/',
    'https://www.youtube.com/watch?v=short',
    'https://example.com/watch?v=dQw4w9WgXcQ',
    'ftp://www.youtube.com/watch?v=dQw4w9WgXcQ',
    '',
  ]) assert.equal(detectPlatform(pasted), null, pasted);
});

test('TikTok videos stay accepted and profiles stay rejected', () => {
  assert.equal(detectPlatform('https://www.tiktok.com/@someone/video/7234567890123456789'), 'tiktok');
  assert.equal(detectPlatform('vm.tiktok.com/ZMabcdef/'), 'tiktok');
  assert.equal(detectPlatform('https://www.tiktok.com/@someone'), null);
});

test('HTTP errors preserve their status and server message for retry decisions', async t => {
  t.mock.method(globalThis, 'fetch', async () => new Response(
    JSON.stringify({ detail: 'Server sedang sibuk.' }),
    { status: 503, headers: { 'Content-Type': 'application/json' } },
  ));
  await assert.rejects(request('/api/jobs/example'), error => {
    assert.equal(error.status, 503);
    assert.equal(error.message, 'Server sedang sibuk.');
    assert.ok(pollingRetryDelay(error, 1) > 0);
    return true;
  });
});

test('missing jobs remain terminal and keep the backend explanation', async t => {
  t.mock.method(globalThis, 'fetch', async () => new Response(
    JSON.stringify({ detail: 'Job tidak ditemukan atau sudah kedaluwarsa.' }),
    { status: 404 },
  ));
  await assert.rejects(request('/api/jobs/missing'), error => {
    assert.equal(error.status, 404);
    assert.match(error.message, /kedaluwarsa/);
    assert.equal(pollingRetryDelay(error, 1), null);
    return true;
  });
});

test('non-JSON server outages still expose an HTTP status', async t => {
  t.mock.method(globalThis, 'fetch', async () => new Response('Gateway unavailable', { status: 502 }));
  await assert.rejects(request('/api/jobs/example'), error => {
    assert.equal(error.status, 502);
    assert.equal(error.message, 'Server merespons 502.');
    assert.ok(pollingRetryDelay(error, 1) > 0);
    return true;
  });
});

test('network interruptions remain retryable without hiding the original error', async t => {
  const interruption = new TypeError('Failed to fetch');
  t.mock.method(globalThis, 'fetch', async () => { throw interruption; });
  await assert.rejects(request('/api/jobs/example'), error => {
    assert.equal(error, interruption);
    assert.ok(pollingRetryDelay(error, 1) > 0);
    return true;
  });
});

test('polling retries timeouts, throttling, and server failures only', () => {
  for (const status of [408, 429, 500, 502, 503, 504, 599]) {
    assert.ok(pollingRetryDelay({ status }, 1) > 0, `HTTP ${status} should retry`);
  }
  for (const status of [400, 401, 403, 404, 415, 422, 600]) {
    assert.equal(pollingRetryDelay({ status }, 1), null, `HTTP ${status} should stop`);
  }
  assert.equal(pollingRetryDelay(new Error('Respons server tidak valid.'), 1), null);
  assert.equal(pollingRetryDelay(new DOMException('Aborted', 'AbortError'), 1), null);
});

test('backoff increases, stays bounded, and stops after five consecutive retries', () => {
  const interruption = new TypeError('Failed to fetch');
  let previous = 0;
  assert.equal(pollingRetryLimit, 5);
  for (let attempt = 1; attempt <= pollingRetryLimit; attempt++) {
    const delay = pollingRetryDelay(interruption, attempt);
    assert.ok(delay > previous);
    assert.ok(delay <= 10000);
    previous = delay;
  }
  for (const attempt of [0, -1, 1.5, pollingRetryLimit + 1]) {
    assert.equal(pollingRetryDelay(interruption, attempt), null);
  }
});
