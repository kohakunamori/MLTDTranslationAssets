/**
 * MLTD translated-asset gateway (Cloudflare Worker).
 *
 * Serves the newest published translation release and forwards everything else
 * to the official Japanese CDN, so a client that points its asset base at this
 * host keeps working for paths the overlay does not cover.
 *
 * Where the translated bytes come from
 * -----------------------------------
 * This Worker stores nothing.  It carries a *table* (`src/index.json`, built by
 * `build_index.py` from `generated/<asset_version>/manifest.json`) that maps a
 * request path to the content-addressed object that holds its translated bytes,
 * and reads that object out of the GitHub repository at a **pinned commit**:
 *
 *     https://raw.githubusercontent.com/<owner>/<repo>/<commit>/generated/objects/sha256/<digest>
 *
 * A commit-pinned blob is immutable, so the answer is cached at Cloudflare's
 * edge for a year and the repository is read at most once per object per
 * datacentre.  Publishing a new release is therefore "rebuild the table, deploy
 * again" -- there is no copy of the release to move anywhere.
 *
 * If the repository cannot answer (rate limit, network, a blob that is gone) the
 * request falls through to the official CDN rather than failing: a client that
 * is mid-download gets the untranslated official file, never an error page.
 */

import index from './index.json';

const NAMESPACES = new Set(['assets', 'generated-assets', 'cn']);
const DEFAULT_OFFICIAL_BASE = 'https://td-assets.bn765.com';
const ASSET_PREFIX = 'production/2018/Android/';

const ASSET_VERSION = String(index.asset_version || '');
const OBJECTS = new Map(Object.entries(index.objects || {}));
const SIZES = new Map(Object.entries(index.sizes || {}));
const OBJECT_BASE = `https://raw.githubusercontent.com/${index.repository}/${index.source_commit}/` +
  `${index.object_root || 'generated/objects/sha256'}`;
const BUILD = String(index.source_commit || '').slice(0, 12);
// The object URL pins a commit, so a cached copy of it can never go stale.
const OBJECT_CACHE_SECONDS = 31536000;
// The *client-facing* URL is not pinned: the same asset version can be
// republished with new translations (that happened on 2026-10-11, 491 lyric
// bundles changed under an unchanged version number).  So a client that caches
// by URL must ask again every time.  Asking is cheap -- the ETag is the artifact
// hash, and a match is answered without touching the network or the store.
const CLIENT_CACHE_CONTROL = 'public, max-age=0, must-revalidate';

/** The Cache API is not guaranteed everywhere; a gateway without it still works. */
function objectCache() {
  try {
    return caches.default;
  } catch {
    return null;
  }
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    if (url.pathname === '/healthz') return healthz(env);

    if (request.method !== 'GET' && request.method !== 'HEAD') {
      return text(405, 'read-only asset gateway\n', { allow: 'GET, HEAD' });
    }

    if (!/^[0-9]{1,32}$/.test(ASSET_VERSION)) {
      return text(500, 'gateway bundle carries no usable asset version\n');
    }

    const route = routeFromPath(url.pathname, ASSET_VERSION);
    if (route === null) return text(404, 'not found\n');

    // Only one release is published here.  Another version belongs to the
    // official CDN: handing a client running version B a bundle built for
    // version A would corrupt its asset set.
    if (route.version !== ASSET_VERSION) {
      return forwardOfficial(request, env, route, 'official-other-version');
    }

    const digest = lookup(route);
    if (digest !== null) {
      const served = await serveFromRepository(request, ctx, route, digest);
      if (served !== null) return served;
      return forwardOfficial(request, env, route, 'official-repository-unavailable');
    }

    return forwardOfficial(request, env, route, 'official-miss');
  },
};

/* ------------------------------------------------------------------ routing */

/**
 * Turn a request path into `{version, resource}`.
 *
 * Accepted shapes (the union of the existing nginx vhost and the official CDN):
 *   /<version>/production/2018/Android/<name>     official CDN layout
 *   /assets/<version|current>/<resource>          assets_route.py layout
 *   /generated-assets/<version>/<resource>        public mirror layout
 *   /cn/<version>/<resource>                      CN namespace layout
 *   /<resource>                                   current release
 *
 * Returns null when the path is malformed; malformed input is a 404 rather than
 * a lookup, so a traversal attempt can never reach the store or the upstream.
 */
export function routeFromPath(pathname, active) {
  const parts = decodeSegments(pathname);
  if (parts === null || parts.length === 0) return null;

  let rest = parts;
  if (NAMESPACES.has(rest[0])) rest = rest.slice(1);

  let version = active;
  if (rest.length > 0 && (rest[0] === 'current' || /^[0-9]{1,32}$/.test(rest[0]))) {
    version = rest[0] === 'current' ? active : rest[0];
    rest = rest.slice(1);
  }

  if (rest.length === 0) return null;
  return { version, resource: rest.join('/') };
}

/** Percent-decode a path into segments, refusing anything ambiguous. */
function decodeSegments(pathname) {
  let decoded;
  try {
    decoded = decodeURIComponent(pathname);
  } catch {
    return null;
  }
  if (decoded.includes('\\')) return null;
  if (/[\u0000-\u001f\u007f]/.test(decoded)) return null;

  const raw = decoded.startsWith('/') ? decoded.slice(1) : decoded;
  if (raw === '') return [];
  const parts = raw.split('/');
  for (const part of parts) {
    if (part === '' || part === '.' || part === '..') return null;
  }
  return parts;
}

/**
 * The artifact digest for a request, or null when the release does not cover it.
 *
 * A bare file name also resolves under the fixed asset prefix, so
 * `/assets/current/foo.unity3d` and `/production/2018/Android/foo.unity3d` both
 * work.  Every candidate key is fully qualified before it is compared, so a
 * request can never collide with an object name by accident.
 */
export function lookup(route) {
  const candidates = [route.resource];
  if (!route.resource.includes('/')) candidates.push(ASSET_PREFIX + route.resource);
  for (const candidate of candidates) {
    if (!candidate.startsWith(ASSET_PREFIX)) continue;
    const digest = OBJECTS.get(candidate);
    if (digest) return digest;
  }
  return null;
}

/* ------------------------------------------------- repository (GitHub raw) */

/**
 * Returns a Response when the repository answered, or null when it did not and
 * the caller should fall back to the official CDN.
 *
 * The object is cached explicitly through the Cache API rather than by relying
 * on `cf.cacheTtl`: on a `*.workers.dev` hostname the transparent cache does not
 * engage for Worker subrequests, and without this every client download would
 * re-read the object from GitHub.
 */
async function serveFromRepository(request, ctx, route, digest) {
  const etag = `"${digest}"`;

  // A HEAD is answered from the table: the size is known and no bytes, no
  // subrequest and no cache lookup are needed.
  if (request.method === 'HEAD') {
    return new Response(null, {
      status: 200,
      headers: objectHeaders(route, digest, SIZES.get(digest), { 'x-mltd-cache': 'index' }),
    });
  }

  // The digest *is* the content hash, so a matching validator is answered here
  // without spending a subrequest.
  if (request.headers.get('if-none-match') === etag) {
    return new Response(null, { status: 304, headers: objectHeaders(route, digest, SIZES.get(digest)) });
  }

  const objectUrl = `${OBJECT_BASE}/${digest}`;
  const rangeHeader = request.headers.get('range');
  const cache = rangeHeader ? null : objectCache();
  const cacheKey = cache ? new Request(objectUrl) : null;

  if (cache) {
    try {
      const hit = await cache.match(cacheKey);
      if (hit) {
        return new Response(hit.body, {
          status: 200,
          headers: objectHeaders(route, digest, hit.headers.get('content-length') || SIZES.get(digest), {
            'x-mltd-cache': 'hit',
          }),
        });
      }
    } catch (err) {
      console.log(`cache lookup failed for ${digest}: ${err && err.message}`);
    }
  }

  const headers = new Headers({ 'user-agent': 'mltd-asset-gateway/1' });
  if (rangeHeader) headers.set('range', rangeHeader);

  let upstream;
  try {
    upstream = await fetch(objectUrl, { method: 'GET', headers, redirect: 'follow' });
  } catch (err) {
    console.log(`repository fetch failed for ${digest}: ${err && err.message}`);
    return null;
  }

  if (upstream.status < 200 || upstream.status >= 300) {
    console.log(`repository answered ${upstream.status} for ${digest}`);
    return null;
  }

  if (cache && upstream.status === 200) {
    try {
      ctx.waitUntil(cache.put(cacheKey, storable(upstream.clone())));
    } catch (err) {
      console.log(`cache store failed for ${digest}: ${err && err.message}`);
    }
  }

  const out = objectHeaders(route, digest, upstream.headers.get('content-length') || SIZES.get(digest), {
    'x-mltd-cache': cache ? 'miss' : 'bypass',
  });
  const contentType = upstream.headers.get('content-type');
  if (contentType) out.set('content-type', contentType);
  if (upstream.status === 206) {
    const contentRange = upstream.headers.get('content-range');
    if (contentRange) out.set('content-range', contentRange);
  }
  return new Response(upstream.body, { status: upstream.status, headers: out });
}

/** Rewrite a response into the shape the Cache API is allowed to keep. */
function storable(response) {
  const headers = new Headers(response.headers);
  headers.set('cache-control', `public, max-age=${OBJECT_CACHE_SECONDS}, immutable`);
  headers.delete('set-cookie');
  headers.delete('vary');
  return new Response(response.body, { status: response.status, headers });
}

function objectHeaders(route, digest, contentLength, extra) {
  const headers = new Headers({
    'content-type': 'application/octet-stream',
    'cache-control': CLIENT_CACHE_CONTROL,
    'etag': `"${digest}"`,
    'x-mltd-asset-source': 'generated-release',
    'x-mltd-asset-version': route.version,
    'x-mltd-build': BUILD,
    'x-content-type-options': 'nosniff',
    'access-control-allow-origin': '*',
  });
  if (contentLength) headers.set('content-length', String(contentLength));
  for (const [name, value] of Object.entries(extra || {})) headers.set(name, String(value));
  return headers;
}

/* --------------------------------------------------------- official mirror */

/**
 * Pure pass-through.  The official bytes are not stored anywhere on the way
 * through: this gateway only decides *which* origin answers a request.
 */
async function forwardOfficial(request, env, route, reason) {
  const base = String((env && env.OFFICIAL_BASE) || DEFAULT_OFFICIAL_BASE).replace(/\/+$/, '');
  const target = `${base}/${encodeURIComponent(route.version)}/${encodePath(route.resource)}`;

  const headers = new Headers({ 'user-agent': 'mltd-asset-gateway/1' });
  for (const name of ['range', 'if-none-match', 'if-modified-since', 'if-match']) {
    const value = request.headers.get(name);
    if (value) headers.set(name, value);
  }

  let upstream;
  try {
    upstream = await fetch(target, {
      method: request.method,
      headers,
      redirect: 'follow',
    });
  } catch (err) {
    return text(502, `official upstream unreachable: ${err && err.message}\n`, {
      'x-mltd-asset-source': 'official-error',
    });
  }

  const out = new Headers({
    'cache-control': CLIENT_CACHE_CONTROL,
    'x-mltd-asset-source': reason,
    'x-mltd-asset-version': route.version,
    'x-mltd-build': BUILD,
    'x-mltd-upstream-status': String(upstream.status),
    'x-content-type-options': 'nosniff',
    'access-control-allow-origin': '*',
  });
  for (const name of ['content-type', 'content-length', 'accept-ranges', 'last-modified', 'etag']) {
    const value = upstream.headers.get(name);
    if (value) out.set(name, value);
  }
  if (upstream.status === 206) {
    const value = upstream.headers.get('content-range');
    if (value) out.set('content-range', value);
  }
  if (!out.has('content-type')) out.set('content-type', 'application/octet-stream');

  return new Response(upstream.body, { status: upstream.status, headers: out });
}

function encodePath(resource) {
  return resource.split('/').map((part) => encodeURIComponent(part)).join('/');
}

/* -------------------------------------------------------------------- misc */

function healthz(env) {
  return new Response(
    `${JSON.stringify(
      {
        ok: true,
        kind: 'mltd-asset-gateway',
        asset_version: ASSET_VERSION,
        source_commit: index.source_commit,
        repository: index.repository,
        indexed_paths: OBJECTS.size,
        runtime_keys: index.runtime_keys,
        logical_keys: index.logical_keys,
        release_bytes: index.total_bytes,
        built_at: index.built_at,
        official_base: String((env && env.OFFICIAL_BASE) || DEFAULT_OFFICIAL_BASE),
        object_base: OBJECT_BASE,
        object_cache_seconds: OBJECT_CACHE_SECONDS,
      },
      null,
      2,
    )}\n`,
    { status: 200, headers: { 'content-type': 'application/json; charset=utf-8' } },
  );
}

function text(status, body, extra) {
  const headers = new Headers({ 'content-type': 'text/plain; charset=utf-8' });
  for (const [name, value] of Object.entries(extra || {})) headers.set(name, value);
  return new Response(body, { status, headers });
}
