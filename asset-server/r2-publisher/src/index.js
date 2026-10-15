// CI-only transport. The public gateway has no publication token or write route.
// No mutable keys or deletes: failed/stale builds cannot change a live release.
const KEY = /^(objects|build-cache)\/sha256\/([0-9a-f]{64})$/;
const json = (value, status = 200) => Response.json(value, {
  status, headers: {'cache-control': 'no-store'},
});

export default {
  async fetch(request, env) {
    if (!env.PUBLISH_TOKEN || request.headers.get('authorization') !== `Bearer ${env.PUBLISH_TOKEN}`) {
      return json({error: 'unauthorized'}, 401);
    }
    const url = new URL(request.url);
    if (url.pathname === '/batch' && request.method === 'POST') {
      const keys = await request.json().catch(() => null);
      if (!Array.isArray(keys) || !keys.length || keys.length > 32
          || keys.some(key => typeof key !== 'string' || !/^objects\/sha256\/[0-9a-f]{64}$/.test(key))) {
        return json({error: 'up to 32 immutable object keys required'}, 400);
      }
      let total = 0;
      // Drain each R2 body as soon as headers arrive. The runtime limits active
      // connections, while parallel reads avoid 32 cross-region round trips in
      // series. The shared byte budget bounds the entire batch's buffers.
      const parts = await Promise.all(keys.map(async key => {
        const object = await env.ASSETS.get(key);
        if (!object || object.customMetadata?.sha256 !== key.slice(-64)) {
          await object?.body?.cancel?.();
          return {error: 'missing object', status: 404};
        }
        total += object.size;
        if (total > 4 * 1024 * 1024) {
          await object.body?.cancel?.();
          return {error: 'batch exceeds 4 MiB', status: 413};
        }
        return {metadata: {key, size: object.size}, body: await object.arrayBuffer()};
      }));
      const failed = parts.find(part => part.error);
      if (failed) return json({error: failed.error}, failed.status);
      const metadata = parts.map(part => part.metadata), bodies = parts.map(part => part.body);
      const header = new TextEncoder().encode(JSON.stringify(metadata));
      const length = new Uint8Array(4);
      new DataView(length.buffer).setUint32(0, header.length);
      return new Response(new Blob([length, header, ...bodies]), {headers: {
        'content-type': 'application/octet-stream', 'cache-control': 'no-store',
      }});
    }
    if (url.pathname === '/inventory' && request.method === 'GET') {
      const prefix = url.searchParams.get('prefix');
      if (!['objects/sha256/', 'build-cache/sha256/'].includes(prefix)) return json({error: 'prefix'}, 400);
      const page = await env.ASSETS.list({prefix, cursor: url.searchParams.get('cursor') || undefined,
        limit: 1000, include: ['customMetadata']});
      return json({objects: page.objects.map(o => ({key: o.key, size: o.size, sha256: o.customMetadata?.sha256})),
        cursor: page.truncated ? page.cursor : null});
    }
    const key = url.pathname.slice(1);
    const match = KEY.exec(key);
    if (!match) return json({error: 'immutable content keys only'}, 400);
    const digest = match[2];
    if (request.method === 'PUT') {
      // R2 validates the stream against the name; a bad upload never becomes visible.
      try {
        const object = await env.ASSETS.put(key, request.body, {
          sha256: digest, onlyIf: {etagDoesNotMatch: '*'},
          customMetadata: {sha256: digest},
          httpMetadata: {contentType: 'application/octet-stream', cacheControl: 'public, max-age=31536000, immutable'},
        });
        if (object) return json({key, size: object.size, sha256: digest}, 201);
        const existing = await env.ASSETS.head(key);
        if (existing?.customMetadata?.sha256 !== digest) return json({error: 'unverified existing object'}, 409);
        return json({key, size: existing.size, sha256: digest});
      } catch {
        return json({error: 'upload failed or checksum mismatch'}, 422);
      }
    }
    if (request.method === 'GET' || request.method === 'HEAD') {
      const object = await (request.method === 'HEAD' ? env.ASSETS.head(key) : env.ASSETS.get(key));
      if (!object || object.customMetadata?.sha256 !== digest) return json({error: 'not found'}, 404);
      return new Response(request.method === 'HEAD' ? null : object.body, {headers: {
        'content-type': 'application/octet-stream', 'content-length': String(object.size),
        'cache-control': 'no-store', 'etag': `"${digest}"`,
      }});
    }
    return json({error: 'method'}, 405);
  },
};
