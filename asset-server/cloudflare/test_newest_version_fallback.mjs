#!/usr/bin/env node
/**
 * Local harness for the gateway's newest-version recovery path.
 *
 * It imports the real `src/index.js` (so the code under test is exactly the code
 * that gets deployed) with stub `fetch`, `caches` and `env` globals, drives the
 * exported `fetch` handler with fake `Request`s, and checks what the worker did:
 * which URLs it asked for, in which order, and what it sent back.
 *
 * The one thing that has to be adapted is the JSON import: `import index from
 * './index.json'` is resolved by the Worker bundler, not by Node, so the source
 * is copied into a temporary module with that single line replaced by a
 * `readFile` + `JSON.parse` of the same file.  Nothing else about the worker is
 * touched, and the copy fails loudly if that line ever changes.
 *
 * Every case runs in its own module instance, so the per-isolate memo cannot leak
 * from one case into the next.
 *
 * Usage (from the repository root, or from this directory):
 *
 *     node asset-server/cloudflare/test_newest_version_fallback.mjs
 */
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const HERE = fileURLToPath(new URL('.', import.meta.url));
const WORKER_PATH = join(HERE, 'src', 'index.js');
const INDEX_PATH = join(HERE, 'src', 'index.json');
const JSON_IMPORT = "import index from './index.json';";

const FEED = 'https://api.matsurihi.me/api/mltd/v2/version/assets';
const OFFICIAL = 'https://td-assets.bn765.com';
const PREFIX = 'production/2018/Android/';
const GATEWAY = 'https://gateway.test';

const table = JSON.parse(await readFile(INDEX_PATH, 'utf8'));
const PINNED = String(table.asset_version);
/** A newer version than the pinned one, so the retry has something to aim at. */
const NEWEST = String(Number(PINNED) + 9);
/** A file name the release cannot possibly cover (checked below). */
const UNCOVERED = `${'f'.repeat(40)}.data`;
// These assertions exercise the original *single-object* R2 path.
// New packed-object reads, including range and failure fallback, have a
// separate dedicated test; use an unpacked covered object here to avoid
// mocking a pack GET with an unrelated single-file R2 stub.
const COVERED = Object.keys(table.objects).find((key) =>
  key.startsWith(PREFIX) && !Object.hasOwn(table.packs || {}, table.objects[key]));
// These tests keep exercising the unchanged streaming path for >2 MiB
// objects; medium-sized objects now have a separate full-object Range cache.
const RANGE_COVERED = Object.keys(table.objects).find(key =>
  key.startsWith(PREFIX) && !(table.packs || {})[table.objects[key]] &&
  table.sizes[table.objects[key]] > 2 * 1024 * 1024);
if (!RANGE_COVERED) throw new Error('No large unpacked object for Range regression');
if (!COVERED) throw new Error(`${INDEX_PATH} carries no ${PREFIX} path to test with`);
if (table.objects[UNCOVERED] || table.objects[PREFIX + UNCOVERED]) {
  throw new Error(`${UNCOVERED} is covered by the release; pick another name for the uncovered case`);
}

/* ------------------------------------------------------------------- stubs */

/** A tiny stand-in for `caches.default`, good enough for both caches in use. */
class FakeCache {
  constructor() {
    this.store = new Map();
    this.matches = 0;
    this.puts = 0;
  }

  static key(request) {
    return typeof request === 'string' ? request : request.url;
  }

  async match(request) {
    this.matches += 1;
    const hit = this.store.get(FakeCache.key(request));
    if (!hit) return undefined;
    return new Response(hit.body, { status: hit.status, headers: hit.headers });
  }

  async put(request, response) {
    this.puts += 1;
    const body = await response.arrayBuffer();
    this.store.set(FakeCache.key(request), { body, status: response.status, headers: new Headers(response.headers) });
  }

  seed(url, body) {
    this.store.set(url, { body: Buffer.from(body), status: 200, headers: new Headers() });
  }
}

const ok = (body, headers = {}) => new Response(body, { status: 200, headers });
const forbidden = (body = 'AccessDenied') => new Response(body, { status: 403, headers: { 'content-type': 'application/xml' } });
const notFound = () => new Response('missing', { status: 404 });

/** One upstream URL, answered fresh on every call. */
function url(url, make) {
  return { match: (candidate) => candidate === url, respond: make };
}

/** Everything under one origin, for URLs that carry a digest this test does not know. */
function prefix(base, make) {
  return { match: (candidate) => candidate.startsWith(base), respond: make };
}

const feedUrl = (make) => url(FEED, make);
const pinnedFetch = (resource, make) => url(`${OFFICIAL}/${PINNED}/${resource}`, make);
const newestFetch = (resource, make) => url(`${OFFICIAL}/${NEWEST}/${resource}`, make);
/** Anything the release does not cover is forwarded, so nothing else may be fetched. */
const unexpectedFetch = () => ({ match: () => false, respond: () => { throw new Error('unreachable'); } });

let instances = 0;
/** Kept before any stub is installed, for the opt-in `--live` mode. */
const realFetch = globalThis.fetch.bind(globalThis);

const recordCall = (calls, input, init) => {
  const target = typeof input === 'string' ? input : input.url;
  calls.push({ url: target, method: String((init && init.method) || 'GET').toUpperCase() });
  return target;
};

/** Build the module instance: only the globals around it differ between modes. */
async function instantiate(directory) {
  let source = await readFile(WORKER_PATH, 'utf8');
  if (!source.includes(JSON_IMPORT)) {
    throw new Error(`src/index.js no longer contains ${JSON_IMPORT}; update this harness`);
  }
  source = `import { readFile } from 'node:fs/promises';\n`
    + source.replace(JSON_IMPORT, `const index = JSON.parse(await readFile(${JSON.stringify(INDEX_PATH)}, 'utf8'));`);

  const copy = join(directory, `worker-${(instances += 1)}.mjs`);
  await writeFile(copy, source, 'utf8');
  const module = await import(pathToFileURL(copy).href);

  const cache = new FakeCache();
  globalThis.caches = { default: cache };
  const pending = [];
  const ctx = { waitUntil: (promise) => pending.push(Promise.resolve(promise).catch(() => {})) };
  return { handler: module.default.fetch, cache, ctx, settle: () => Promise.all(pending) };
}

/**
 * Import `src/index.js` into a fresh module instance that can only reach the
 * routes given here, and hand back the handler plus a record of what it fetched.
 * Any other URL is an error: that is how "must not touch the feed" is enforced.
 */
async function loadWorker(routes, directory) {
  const calls = [];
  globalThis.fetch = async (input, init = {}) => {
    const target = recordCall(calls, input, init);
    const route = routes.find((candidate) => candidate.match(target));
    if (!route) throw new Error(`the worker fetched an unexpected URL: ${target}`);
    return route.respond(target, init);
  };
  return { ...(await instantiate(directory)), calls };
}

/** The same, but every upstream request really goes out (opt-in `--live` only). */
async function loadLiveWorker(directory) {
  const calls = [];
  globalThis.fetch = async (input, init = {}) => {
    recordCall(calls, input, init);
    return realFetch(input, init);
  };
  return { ...(await instantiate(directory)), calls };
}

const call = (worker, path, init) => worker.handler(new Request(`${GATEWAY}${path}`, init), worker.env || {}, worker.ctx);
const callsTo = (worker, target) => worker.calls.filter((entry) => entry.url === target || entry.url.startsWith(target));

/* -------------------------------------------------------------- assertions */

const checks = [];
function assert(condition, message) {
  if (!condition) throw new Error(message);
}
function equal(actual, expected, what) {
  if (actual !== expected) throw new Error(`${what}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
}

async function test(name, body) {
  const directory = await mkdtemp(join(tmpdir(), 'mltd-gateway-harness-'));
  try {
    await body(directory);
    checks.push({ name, ok: true });
    console.log(`  PASS  ${name}`);
  } catch (error) {
    checks.push({ name, ok: false, detail: error && error.message });
    console.log(`  FAIL  ${name}\n        ${error && error.message}`);
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
}

/* ------------------------------------------------------------------- cases */

console.log(`worker src/index.js   pinned version ${PINNED}   pretend-newest ${NEWEST}`);

// (c) A request that carries a version is never re-aimed and never consults the feed.
await test('(c) version-carrying request: one forward, no feed', async (directory) => {
  const other = String(Number(PINNED) + 5);
  const worker = await loadWorker([url(`${OFFICIAL}/${other}/${COVERED}`, forbidden)], directory);
  const response = await call(worker, `/${other}/${COVERED}`);
  equal(response.status, 403, 'status');
  equal(response.headers.get('x-mltd-asset-source'), 'official-other-version', 'source marker');
  equal(response.headers.get('x-mltd-asset-version'), other, 'reported version');
  equal(worker.calls.length, 1, 'upstream calls');
  assert(callsTo(worker, FEED).length === 0, 'the feed must not be contacted for a version-carrying request');
});

// (d) A covered path is answered from the release: untouched, no upstream at all.
await test('(d) covered path: release bytes, no official forward, no feed', async (directory) => {
  const worker = await loadWorker([], directory);
  let reads = 0;
  const digest = table.objects[COVERED];
  worker.env = {ASSETS: {async get(key) {
    equal(key, `objects/sha256/${digest}`, 'R2 key');
    reads++;
    return {size: table.sizes[digest], body: 'translated-bytes', customMetadata: {sha256: digest}};
  }}};
  const response = await call(worker, `/${COVERED}`);
  equal(response.status, 200, 'status');
  equal(response.headers.get('x-mltd-asset-source'), 'generated-release', 'source marker');
  equal(await response.text(), 'translated-bytes', 'body');
  equal(reads, 1, 'R2 reads');
  equal(worker.calls.length, 0, 'HTTP upstream calls');
  assert(callsTo(worker, OFFICIAL).length === 0, 'a covered path must not reach the official CDN');
  assert(callsTo(worker, FEED).length === 0, 'a covered path must not reach the feed');
  await worker.settle();
  const again = await call(worker, `/${COVERED}`);
  equal(again.headers.get('x-mltd-cache'), 'hit', 'immutable cache hit');
  equal(reads, 1, 'cached R2 reads');
  const cachedKey = [...worker.cache.store.keys()][0];
  equal(cachedKey, `https://mltd-localized-assets.invalid/objects/sha256/${digest}`, 'cross-release cache key');
  equal(again.headers.get('cache-control'), 'public, max-age=0, must-revalidate', 'stable URL revalidation');
  const conditional = await call(worker, `/${COVERED}`, {method: 'HEAD', headers: {'if-none-match': `W/"${digest}"`}});
  equal(conditional.status, 304, 'conditional HEAD');
  equal(reads, 1, '304 does not read R2');
});

await test('R2 byte ranges, If-Range, and unavailable storage', async directory => {
  const digest = table.objects[RANGE_COVERED];
  const size = table.sizes[digest];
  const worker = await loadWorker([pinnedFetch(RANGE_COVERED, () => ok('official-fallback'))], directory);
  const ranges = [];
  worker.env = {ASSETS: {async get(key, options) {
    ranges.push(options?.range);
    return {size, body: options?.range ? '123' : 'full', customMetadata: {sha256: digest}};
  }}};
  const partial = await call(worker, `/${RANGE_COVERED}`, {headers: {range: 'bytes=1-3'}});
  equal(partial.status, 206, 'partial status');
  equal(partial.headers.get('content-range'), `bytes 1-3/${size}`, 'content range');
  equal(ranges[0].offset, 1, 'R2 range offset');
  equal(ranges[0].length, 3, 'R2 range length');
  equal(worker.cache.puts, 0, 'partial bytes are never cached');
  const invalid = await call(worker, `/${RANGE_COVERED}`, {headers: {range: `bytes=${size}-`}});
  equal(invalid.status, 416, 'unsatisfiable range');
  equal(ranges.length, 1, '416 no R2 read');
  const full = await call(worker, `/${RANGE_COVERED}`, {headers: {range: 'bytes=1-3', 'if-range': '"old"'}});
  equal(full.status, 200, 'changed If-Range gets full object');
  equal(ranges[1], undefined, 'no range on changed If-Range');
  await worker.settle();
  worker.cache.store.clear();
  worker.env = {ASSETS: {async get() {return null;}}};
  const fallback = await call(worker, `/${RANGE_COVERED}`);
  equal(await fallback.text(), 'official-fallback', 'missing R2 fallback');
  equal(fallback.headers.get('x-mltd-asset-source'), 'official-r2-unavailable', 'R2 failure marker');
  equal(worker.cache.store.size, 0, 'fallback not cached as translated content');
});

// (a) Version-less, pinned pairing refused, newest version answers: the retry is adopted.
await test('(a) version-less 403 then newest-version 200: retry body + marker', async (directory) => {
  const covered = `${PREFIX}${UNCOVERED}`;
  const worker = await loadWorker(
    [
      pinnedFetch(covered, forbidden),
      newestFetch(covered, () => ok('newest-version-bytes')),
      feedUrl(() => ok(JSON.stringify([{ version: 1 }, { version: PINNED }, { version: NEWEST }]))),
    ],
    directory,
  );
  const response = await call(worker, `/${covered}`);
  equal(response.status, 200, 'status');
  equal(await response.text(), 'newest-version-bytes', 'body');
  equal(response.headers.get('x-mltd-asset-source'), 'official-newest-version', 'source marker');
  equal(response.headers.get('x-mltd-asset-version'), NEWEST, 'reported version');
  equal(response.headers.get('x-mltd-upstream-status'), '200', 'upstream status');
  equal(callsTo(worker, FEED).length, 1, 'feed reads');
  equal(worker.calls[0].url, `${OFFICIAL}/${PINNED}/${covered}`, 'the pinned forward still comes first');
  assert(worker.calls.findIndex((entry) => entry.url === FEED) > 0, 'the feed is only read after the refusal');
  await worker.settle();
});

// (b) The retry is refused too: the client gets exactly the response it gets today.
await test('(b) version-less 403 and the retry also 403: original response unchanged', async (directory) => {
  const covered = `${PREFIX}${UNCOVERED}`;
  const worker = await loadWorker(
    [
      pinnedFetch(covered, () => forbidden('pinned-refusal')),
      newestFetch(covered, () => forbidden('retry-refusal')),
      feedUrl(() => ok(JSON.stringify([{ version: NEWEST }]))),
    ],
    directory,
  );
  const response = await call(worker, `/${covered}`);
  equal(response.status, 403, 'status');
  equal(await response.text(), 'pinned-refusal', 'body comes from the original refusal');
  equal(response.headers.get('x-mltd-asset-source'), 'official-miss', 'source marker');
  equal(response.headers.get('x-mltd-asset-version'), PINNED, 'reported version');
  equal(response.headers.get('x-mltd-upstream-status'), '403', 'upstream status');
  await worker.settle();
});

// A bare index name is the shape the client actually asks for: the CDN has no
// object at a version root, so only the prefixed shape can answer.
await test('(e) bare index name: pinned 403, bare retry 403, prefixed retry 200', async (directory) => {
  const worker = await loadWorker(
    [
      pinnedFetch(UNCOVERED, forbidden),
      newestFetch(UNCOVERED, () => notFound()),
      newestFetch(PREFIX + UNCOVERED, () => ok('index-bytes')),
      feedUrl(() => ok(JSON.stringify([{ version: NEWEST }]))),
    ],
    directory,
  );
  const response = await call(worker, `/${UNCOVERED}`);
  equal(response.status, 200, 'status');
  equal(await response.text(), 'index-bytes', 'body');
  equal(response.headers.get('x-mltd-asset-source'), 'official-newest-version', 'source marker');
  equal(response.headers.get('x-mltd-asset-version'), NEWEST, 'reported version');
  equal(worker.calls[worker.calls.length - 1].url, `${OFFICIAL}/${NEWEST}/${PREFIX}${UNCOVERED}`, 'last attempt');
  await worker.settle();
});

// A successful forward is not second-guessed: no retry, no feed.
await test('(f) version-less 200: no retry and no feed', async (directory) => {
  const covered = `${PREFIX}${UNCOVERED}`;
  const worker = await loadWorker([pinnedFetch(covered, () => ok('official-bytes'))], directory);
  const response = await call(worker, `/${covered}`);
  equal(response.status, 200, 'status');
  equal(await response.text(), 'official-bytes', 'body');
  equal(response.headers.get('x-mltd-asset-source'), 'official-miss', 'source marker');
  equal(worker.calls.length, 1, 'upstream calls');
  assert(callsTo(worker, FEED).length === 0, 'the feed is not read when the pinned version answers');
});

// An unreachable feed must cost nothing but the retry.
await test('(g) feed unreachable: original refusal', async (directory) => {
  const covered = `${PREFIX}${UNCOVERED}`;
  const worker = await loadWorker(
    [
      pinnedFetch(covered, () => forbidden('pinned-refusal')),
      feedUrl(() => { throw new Error('network down'); }),
    ],
    directory,
  );
  const response = await call(worker, `/${covered}`);
  equal(response.status, 403, 'status');
  equal(await response.text(), 'pinned-refusal', 'body');
  equal(response.headers.get('x-mltd-asset-source'), 'official-miss', 'source marker');
  equal(worker.calls.length, 2, 'upstream calls');
});

// A malformed feed must cost nothing but the retry.
await test('(h) malformed feed: original refusal', async (directory) => {
  const covered = `${PREFIX}${UNCOVERED}`;
  const worker = await loadWorker(
    [
      pinnedFetch(covered, () => forbidden('pinned-refusal')),
      feedUrl(() => ok('{"not":"an array"}')),
      newestFetch(covered, () => ok('must-not-be-used')),
    ],
    directory,
  );
  const response = await call(worker, `/${covered}`);
  equal(response.status, 403, 'status');
  equal(await response.text(), 'pinned-refusal', 'body');
  equal(response.headers.get('x-mltd-asset-source'), 'official-miss', 'source marker');
  assert(callsTo(worker, `${OFFICIAL}/${NEWEST}`).length === 0, 'a feed without a usable version means no retry');
});

// If the feed agrees with the pinned version there is nothing to retry.
await test('(i) feed says the pinned version is newest: no retry', async (directory) => {
  const covered = `${PREFIX}${UNCOVERED}`;
  const worker = await loadWorker(
    [
      pinnedFetch(covered, () => forbidden('pinned-refusal')),
      feedUrl(() => ok(JSON.stringify([{ version: PINNED }]))),
      newestFetch(covered, () => ok('must-not-be-used')),
    ],
    directory,
  );
  const response = await call(worker, `/${covered}`);
  equal(response.status, 403, 'status');
  equal(response.headers.get('x-mltd-asset-source'), 'official-miss', 'source marker');
  assert(callsTo(worker, `${OFFICIAL}/${NEWEST}`).length === 0, 'no retry against the version already refused');
});

// The feed is read once per isolate, not once per request, and the answer is parked
// in the Cache API for the others.
await test('(j) feed is read once for repeated recoveries, then cached', async (directory) => {
  const covered = `${PREFIX}${UNCOVERED}`;
  const worker = await loadWorker(
    [
      pinnedFetch(covered, () => forbidden('pinned-refusal')),
      newestFetch(covered, () => ok('newest-version-bytes')),
      feedUrl(() => ok(JSON.stringify([{ version: NEWEST }]))),
    ],
    directory,
  );
  for (let attempt = 0; attempt < 3; attempt += 1) {
    const response = await call(worker, `/${covered}`);
    equal(response.status, 200, `attempt ${attempt + 1} status`);
    equal(await response.text(), 'newest-version-bytes', `attempt ${attempt + 1} body`);
  }
  equal(callsTo(worker, FEED).length, 1, 'feed reads');
  equal(worker.cache.puts, 1, 'cache stores of the newest version');
  await worker.settle();
});

// A fresh isolate with a warm Cache API and a dead feed still recovers.
await test('(k) warm cache, dead feed: still recovers', async (directory) => {
  const covered = `${PREFIX}${UNCOVERED}`;
  const worker = await loadWorker(
    [
      pinnedFetch(covered, () => forbidden('pinned-refusal')),
      newestFetch(covered, () => ok('newest-version-bytes')),
      feedUrl(() => { throw new Error('must not be needed'); }),
    ],
    directory,
  );
  worker.cache.seed('https://mltd-asset-gateway.invalid/newest-official-version', NEWEST);
  const response = await call(worker, `/${covered}`);
  equal(response.status, 200, 'status');
  equal(await response.text(), 'newest-version-bytes', 'body');
  equal(response.headers.get('x-mltd-asset-source'), 'official-newest-version', 'source marker');
  assert(callsTo(worker, FEED).length === 0, 'a warm cache means no feed read');
  await worker.settle();
});

// healthz is untouched and reaches nothing.
await test('(l) healthz: unchanged, no upstream', async (directory) => {
  const worker = await loadWorker([unexpectedFetch()], directory);
  const response = await call(worker, '/healthz');
  equal(response.status, 200, 'status');
  const payload = await response.json();
  equal(payload.ok, true, 'ok flag');
  equal(payload.kind, 'mltd-asset-gateway', 'kind');
  equal(String(payload.asset_version), PINNED, 'asset version');
  equal(worker.calls.length, 0, 'upstream calls');
});

/* --------------------------------------------------------------- live mode */

/**
 * `--live` runs the same worker against the *real* feed and the *real* official
 * CDN: no stubs, no deployment, just `src/index.js` executing in Node.  It is
 * opt-in so the default run stays offline and deterministic.
 */
if (process.argv.includes('--live')) {
  console.log('\nlive mode: real feed + real official CDN (nothing is deployed)');

  const feedEntries = await (await realFetch(FEED, { headers: { 'user-agent': 'mltd-asset-gateway/1' } })).json();
  const newestEntry = feedEntries.reduce((best, entry) => (Number(entry.version) > Number(best.version) ? entry : best));
  const liveNewest = String(newestEntry.version);
  console.log(`  feed says newest = ${liveNewest}   pinned here = ${PINNED}   index ${newestEntry.indexName}`);

  if (liveNewest === PINNED) {
    console.log('  SKIP  the pinned version is already the newest one; no recovery to observe');
  } else {
    // The exact request the client makes when the game has published a new
    // version: the new index file, on a path that names no version.
    await test('(live-1) new index file, version-less: answered by the newest version', async (directory) => {
      const worker = await loadLiveWorker(directory);
      const response = await call(worker, `/${PREFIX}${newestEntry.indexName}`, { headers: { range: 'bytes=0-99' } });
      assert(response.ok, `status ${response.status}`);
      equal(response.headers.get('x-mltd-asset-source'), 'official-newest-version', 'source marker');
      equal(response.headers.get('x-mltd-asset-version'), liveNewest, 'reported version');
      equal(callsTo(worker, `${OFFICIAL}/${PINNED}/`).length, 1, 'the pinned forward was attempted first');
      equal(callsTo(worker, FEED).length, 1, 'feed reads');
      const body = new Uint8Array(await response.arrayBuffer());
      equal(body.length, 100, 'ranged body length');
      console.log(`        ${response.status} ${response.headers.get('content-range')}`);
      await worker.settle();
    });

    // Something no version has ever had: the refusal must be the one the client
    // would have got before this change, byte for byte.
    await test('(live-2) unknown name, version-less: original refusal, not a new error', async (directory) => {
      const name = `${PREFIX}definitely-not-a-real-bundle-${Date.now()}.unity3d`;
      const worker = await loadLiveWorker(directory);
      const response = await call(worker, `/${name}`);
      const control = await realFetch(`${OFFICIAL}/${PINNED}/${name}`, { headers: { 'user-agent': 'mltd-asset-gateway/1' } });
      equal(response.status, control.status, 'status matches the old behaviour');
      equal(response.headers.get('x-mltd-asset-source'), 'official-miss', 'source marker');
      equal(response.headers.get('x-mltd-asset-version'), PINNED, 'reported version');
      console.log(`        HTTP ${response.status} (control ${control.status})`);
      await worker.settle();
    });
  }
}

/* ----------------------------------------------------------------- summary */

const failed = checks.filter((check) => !check.ok);
console.log(`\n${checks.length - failed.length}/${checks.length} checks passed`);
for (const check of failed) console.log(`  FAILED ${check.name}: ${check.detail}`);
process.exitCode = failed.length === 0 ? 0 : 1;
