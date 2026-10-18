import assert from 'node:assert/strict';
import {createHash, webcrypto} from 'node:crypto';
import {mkdtemp, readFile, writeFile, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';
globalThis.crypto ??= webcrypto;
const sha=x=>createHash('sha256').update(x).digest('hex');
const dir=await mkdtemp(join(tmpdir(),'mltd-pack-'));
const a=Buffer.from('alpha'),b=Buffer.from('BRAVO!'),pack=Buffer.concat([a,b]);
const da=sha(a),db=sha(b),dp=sha(pack),prefix='production/2018/Android/';
const index={asset_version:'1077830',source_commit:'a'.repeat(40),
 objects:{[prefix+'a.data']:da,[prefix+'b.data']:db},
 sizes:{[da]:a.length,[db]:b.length},
 packs:{[da]:[dp,0],[db]:[dp,a.length]},pack_sizes:{[dp]:pack.length}};
const source=await readFile(new URL('./src/index.js',import.meta.url),'utf8');
const replace="import index from './index.json';";
assert(source.includes(replace));
let serial=0;
class Cache{
 store=new Map();
 async match(req){const bytes=this.store.get(req.url);return bytes?new Response(bytes):undefined;}
 async put(req,response){this.store.set(req.url,Buffer.from(await response.arrayBuffer()));}
}
async function worker({bad=false,missing=false}={}){
 const path=join(dir,'worker-'+(++serial)+'.mjs');
 await writeFile(path,source.replace(replace,'const index = '+JSON.stringify(index)+';'));
 const module=await import(pathToFileURL(path));
 const cache=new Cache(),waits=[],keys=[];
 globalThis.caches={default:cache};
 const env={ASSETS:{async get(key){
  keys.push(key);
  if(key==='packs/sha256/'+dp){
   if(missing)return null;
   const bytes=bad?Buffer.alloc(pack.length,0):pack;
   return {size:bytes.length,customMetadata:{sha256:dp},async arrayBuffer(){return bytes;}};
  }
  if(key==='objects/sha256/'+da)return {size:a.length,body:a,customMetadata:{sha256:da}};
  throw Error('unexpected R2 key '+key);
 }}};
 const ctx={waitUntil(p){waits.push(Promise.resolve(p));}};
 const call=(name,init={})=>module.default.fetch(new Request('https://local.test/1077830/'+prefix+name,init),env,ctx);
 return{call,keys,cache,settle:()=>Promise.all(waits)};
}
try{
 let w=await worker();
 let r=await w.call('a.data');
 assert.equal(r.status,200);assert.equal(r.headers.get('etag'),'"'+da+'"');
 assert.equal(r.headers.get('x-mltd-cache'),'pack-miss');assert.equal(await r.text(),'alpha');
 await w.settle();
 r=await w.call('b.data');
 assert.equal(r.headers.get('x-mltd-cache'),'pack-hit');
 assert.equal(await r.text(),'BRAVO!');
 assert.deepEqual(w.keys,['packs/sha256/'+dp]);
 r=await w.call('b.data',{method:'HEAD'});
 assert.equal(r.status,200);assert.equal(r.headers.get('content-length'),'6');
 r=await w.call('b.data',{headers:{range:'bytes=1-2'}});
 assert.equal(r.status,206);assert.equal(r.headers.get('content-range'),'bytes 1-2/6');
 assert.equal(await r.text(),'RA');assert.equal(w.keys.length,1);
 r=await w.call('b.data',{headers:{'if-none-match':'"'+db+'"'}});
 assert.equal(r.status,304);assert.equal(w.keys.length,1);
 w=await worker();const pair=await Promise.all([w.call('a.data'),w.call('b.data')]);
 assert.equal(await pair[0].text(),'alpha');assert.equal(await pair[1].text(),'BRAVO!');
 assert.equal(w.keys.length,1);await w.settle();
 w=await worker({bad:true});r=await w.call('a.data');
 assert.equal(await r.text(),'alpha');assert.equal(r.headers.get('x-mltd-cache'),'miss');
 assert.deepEqual(w.keys,['packs/sha256/'+dp,'objects/sha256/'+da]);
 await w.settle();
 assert(!w.cache.store.has('https://mltd-localized-assets.invalid/objects/sha256/packs/sha256/'+dp));
 w=await worker({missing:true});r=await w.call('a.data');
 assert.equal(await r.text(),'alpha');assert.equal(w.keys.length,2);
 console.log('PASS packed R2 gateway: two URLs/one GET; HEAD, Range, ETag 304, concurrent cold requests, corrupt/unavailable fallback');
}finally{await rm(dir,{recursive:true,force:true});}
