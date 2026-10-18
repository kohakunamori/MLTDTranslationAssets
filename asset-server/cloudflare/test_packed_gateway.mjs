import assert from 'node:assert/strict';
import {createHash, webcrypto} from 'node:crypto';
import {mkdtemp, readFile, writeFile, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';
globalThis.crypto ??= webcrypto;
const telemetry=[];
const originalLog=console.log;
console.log=(...args)=>{
 if(typeof args[0]==='string' && args[0].startsWith('mltd_gateway_metrics ')) {
  telemetry.push(JSON.parse(args[0].slice('mltd_gateway_metrics '.length)));
 } else originalLog(...args);
};
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
 const env={GATEWAY_METRICS_ENABLED:'true',ASSETS:{async get(key){
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
 assert.equal(telemetry.at(-1).r2_pack_gets,1);
 assert.equal(telemetry.at(-1).r2_get_attempts,1);
 assert.equal(telemetry.at(-1).cache,'pack-miss');
 await w.settle();
 r=await w.call('b.data');
 assert.equal(r.headers.get('x-mltd-cache'),'pack-hit');
 assert.equal(await r.text(),'BRAVO!');
 assert.equal(telemetry.at(-1).r2_get_attempts,0);
 assert.equal(telemetry.at(-1).cache,'pack-hit');
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
 assert.equal(w.keys.length,1);
 // Concurrent responses share one R2 GetObject, not one for each HTTP request.
 const concurrent=telemetry.slice(-2);
 assert.equal(concurrent.reduce((n,event)=>n+event.r2_get_attempts,0),1);
 assert(concurrent.some(event=>event.shared_pack_inflight));
 await w.settle();
 w=await worker({bad:true});r=await w.call('a.data');
 assert.equal(await r.text(),'alpha');assert.equal(r.headers.get('x-mltd-cache'),'miss');
 assert.deepEqual(w.keys,['packs/sha256/'+dp,'objects/sha256/'+da]);
 assert.equal(telemetry.at(-1).r2_get_attempts,2);
 assert.equal(telemetry.at(-1).r2_pack_gets,1);
 assert.equal(telemetry.at(-1).r2_object_gets,1);
 await w.settle();
 assert(!w.cache.store.has('https://mltd-localized-assets.invalid/objects/sha256/packs/sha256/'+dp));
 w=await worker({missing:true});r=await w.call('a.data');
 assert.equal(await r.text(),'alpha');assert.equal(w.keys.length,2);
 const safe=JSON.stringify(telemetry);
 assert(!safe.includes('/1077830/'));
 assert(!safe.includes('https://'));
 assert(!safe.includes('cf-connecting-ip'));
 assert(!safe.includes(da), 'telemetry must not leak content digests');
 assert(telemetry.every(event => event.event==='mltd_gateway_cache'));
 console.log('PASS packed R2 gateway and privacy-safe GET/cache telemetry');
}finally{
 console.log=originalLog;
 await rm(dir,{recursive:true,force:true});
}
