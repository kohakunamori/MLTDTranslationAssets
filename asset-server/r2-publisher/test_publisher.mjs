import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {mkdtemp, readFile, writeFile, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';

const directory = await mkdtemp(join(tmpdir(), 'mltd-r2-publisher-'));
try {
  const copy = join(directory, 'publisher.mjs');
  await writeFile(copy, await readFile(new URL('./src/index.js', import.meta.url)));
  const {default: worker} = await import(pathToFileURL(copy));
  const objects = new Map();
  let writes = 0;
  const env = {PUBLISH_TOKEN: 'test-secret', ASSETS: {
    async put(key, body, options) {
      writes++;
      assert.equal(options.onlyIf.etagDoesNotMatch, '*');
      if (objects.has(key)) return null;
      const bytes = Buffer.from(await new Response(body).arrayBuffer());
      if (createHash('sha256').update(bytes).digest('hex') !== options.sha256) throw Error('checksum');
      const object = {key, size: bytes.length, body: bytes, customMetadata: options.customMetadata};
      object.arrayBuffer = async () => bytes;
      objects.set(key, object);
      return object;
    },
    async get(key) {return objects.get(key);},
    async head(key) {return objects.get(key);},
    async delete(keys) {keys.forEach(key => objects.delete(key));},
    async list() {return {objects: [...objects.values()], truncated: false};},
  }};
  const call = (path, method = 'GET', body, authorized = true) => worker.fetch(new Request('https://publisher.test/' + path, {
    method, body, headers: authorized ? {authorization: 'Bearer test-secret'} : {},
  }), env);
  assert.equal((await call('inventory?prefix=objects%2Fsha256%2F', 'GET', undefined, false)).status, 401);
  assert.equal((await call('active.json', 'PUT', 'oops')).status, 400);
  const digest = createHash('sha256').update('translated').digest('hex');
  const key = 'objects/sha256/' + digest;
  assert.equal((await call(key, 'PUT', 'translated')).status, 201);
  assert.equal((await call(key, 'PUT', 'different')).status, 200);
  assert.equal(await (await call(key)).text(), 'translated');
  assert.equal((await call(key, 'DELETE')).status, 405);
  const wrong = 'objects/sha256/' + '0'.repeat(64);
  assert.equal((await call(wrong, 'PUT', 'bad checksum')).status, 422);
  assert.equal((await call(wrong)).status, 404);
  assert.equal(objects.size, 1);
  assert.equal(writes, 3);
  const inventory = await (await call('inventory?prefix=objects%2Fsha256%2F')).json();
  assert.equal(inventory.objects[0].sha256, digest);
  const batch = await call('batch', 'POST', JSON.stringify([key]));
  assert.equal(batch.status, 200);
  const frame = Buffer.from(await batch.arrayBuffer());
  const length = frame.readUInt32BE(0);
  assert.deepEqual(JSON.parse(frame.subarray(4, 4 + length).toString()), [{key, size: 10}]);
  assert.equal(frame.subarray(4 + length).toString(), 'translated');
  assert.equal((await call('batch', 'POST', JSON.stringify(Array(33).fill(key)))).status, 400);
  assert.equal((await call('batch', 'POST', JSON.stringify(['active.json']))).status, 400);
  assert.equal((await call('batch', 'POST', JSON.stringify([wrong]))).status, 404);
  const secondDigest = createHash('sha256').update('two').digest('hex');
  const secondKey = 'objects/sha256/' + secondDigest;
  await call(secondKey, 'PUT', 'two');
  const get = env.ASSETS.get;
  let cancelled = 0;
  const huge = 'objects/sha256/' + 'c'.repeat(64);
  env.ASSETS.get = async key => {
    if (key === huge) return {size: 5 * 1024**2, customMetadata: {sha256: key.slice(-64)},
      body: {async cancel() {cancelled++;}}, async arrayBuffer() {throw Error('must not buffer oversized object');}};
    if (key !== secondKey) await new Promise(resolve => setTimeout(resolve, 15));
    return get(key);
  };
  const ordered = Buffer.from(await (await call('batch', 'POST', JSON.stringify([key, secondKey]))).arrayBuffer());
  const orderedLength = ordered.readUInt32BE(0);
  assert.deepEqual(JSON.parse(ordered.subarray(4, 4 + orderedLength).toString()), [{key, size: 10}, {key: secondKey, size: 3}]);
  assert.equal(ordered.subarray(4 + orderedLength).toString(), 'translatedtwo');
  assert.equal((await call('batch', 'POST', JSON.stringify([huge]))).status, 413);
  assert.equal(cancelled, 1);
  assert.equal((await call('delete-batch', 'POST', JSON.stringify([key]), false)).status, 401);
  assert.equal((await call('delete-batch', 'POST', JSON.stringify(['source/file.png']))).status, 400);
  assert.equal((await call('delete-batch', 'POST', JSON.stringify(Array(101).fill(key)))).status, 400);
  assert.equal((await call('delete-batch', 'POST', JSON.stringify([key]))).status, 200);
  assert.equal((await call(key)).status, 404);
  assert.equal((await call(secondKey)).status, 200); // shared retained bytes survive
  const packBody = 'packed-content';
  const packDigest = createHash('sha256').update(packBody).digest('hex');
  const packKey = 'packs/sha256/' + packDigest;
  assert.equal((await call(packKey, 'PUT', packBody)).status, 201);
  assert.equal(await (await call(packKey)).text(), packBody);
  const packs = await (await call('inventory?prefix=packs%2Fsha256%2F')).json();
  assert(packs.objects.some(row => row.key === packKey && row.sha256 === packDigest));
  assert.equal((await call('delete-batch', 'POST', JSON.stringify([packKey]))).status, 200);
  assert.equal((await call(packKey)).status, 404);
  console.log('PASS publisher authentication, immutable writes, checksum rejection, inventory, and bounded batch reads');
} finally {
  await rm(directory, {recursive: true});
}
