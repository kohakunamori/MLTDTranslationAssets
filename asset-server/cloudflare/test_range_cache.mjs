import assert from 'node:assert/strict';
import {createHash, webcrypto} from 'node:crypto';
import {mkdtemp, readFile, writeFile, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';
globalThis.crypto ??= webcrypto;
const dir=await mkdtemp(join(tmpdir(),'mltd-range-cache-'));
const sha=x=>createHash('sha256').update(x).digest('hex');
const small=Buffer.alloc(512*1024,71);
const large=Buffer.alloc(3*1024*1024,72);
const ds=sha(small), dl=sha(large), prefix='production/2018/Android/';
const index={asset_version:'1077830',source_commit:'a'.repeat(40),
 objects:{[prefix+'medium.data']:ds,[prefix+'large.data']:dl},
 sizes:{[ds]:small.length,[dl]:large.length},packs:{},pack_sizes:{}};
const src=await readFile(new URL('./src/index.js',import.meta.url),'utf8');
const marker="import index from './index.json';";
assert(src.includes(marker));
const path=join(dir,'worker.mjs');
await writeFile(path,src.replace(marker,'const index = '+JSON.stringify(index)+';'));
const cache=new Map(),gets=[],waits=[];
globalThis.caches={default:{
 async match(key){let value=cache.get(key.url);return value?new Response(value.bytes,{headers:value.headers}):undefined;},
 async put(key,response){cache.set(key.url,{bytes:Buffer.from(await response.arrayBuffer()),headers:new Headers(response.headers)});}
}};
const env={ASSETS:{async get(key,options){
 gets.push({key,options});
 const bytes=key==='objects/sha256/'+ds?small:key==='objects/sha256/'+dl?large:null;
 if(!bytes)throw Error('unexpected key '+key);
 const body=options?.range?bytes.subarray(options.range.offset,options.range.offset+options.range.length):bytes;
 return {size:bytes.length,body:Buffer.from(body),customMetadata:{sha256:key.slice(-64)},
 async arrayBuffer(){return Buffer.from(body);}};
}}};
const ctx={waitUntil(p){waits.push(Promise.resolve(p));}};
const worker=(await import(pathToFileURL(path))).default;
const call=(file,headers={})=>worker.fetch(new Request('https://test.example/1077830/'+prefix+file,{headers}),env,ctx);
try{
 let r=await call('medium.data',{range:'bytes=2-7'});
 assert.equal(r.status,206);
 assert.equal(r.headers.get('content-range'),'bytes 2-7/'+small.length);
 assert.equal(await r.arrayBuffer().then(x=>x.byteLength),6);
 assert.equal(r.headers.get('x-mltd-cache'),'miss-range');
 assert.equal(gets.length,1);
 assert.equal(gets[0].options,undefined,'cold medium Range reads full R2 once');
 await Promise.all(waits);
 r=await call('medium.data',{range:'bytes=10-19'});
 assert.equal(r.status,206);
 assert.equal(r.headers.get('x-mltd-cache'),'hit-range');
 assert.equal(await r.arrayBuffer().then(x=>x.byteLength),10);
 assert.equal(gets.length,1,'warm medium Range did not perform another R2 GET');
 r=await call('medium.data',{'if-none-match':'"'+ds+'"'});
 assert.equal(r.status,304);assert.equal(gets.length,1);
 r=await call('medium.data',{'if-range':'"not-current"','range':'bytes=2-7'});
 assert.equal(r.status,200);
 assert.equal(r.headers.get('x-mltd-cache'),'hit');
 assert.equal(gets.length,1);

 r=await call('large.data',{range:'bytes=2-7'});
 assert.equal(r.status,206);
 assert.equal(r.headers.get('content-range'),'bytes 2-7/'+large.length);
 assert.equal(gets.length,2);
 assert.equal(gets[1].options.range.offset,2);
 assert.equal(gets[1].options.range.length,6);
 r=await call('large.data',{range:'bytes=30-35'});
 assert.equal(r.status,206);assert.equal(gets.length,3,'large ranges remain streamed from R2');
 assert(![...cache.keys()].some(x=>x.endsWith(dl)));
 console.log('PASS range-cache: medium cold full-read + warm zero-GET ranges; HEAD/ETag/If-Range; >2MiB requests remain streaming');
} finally {await rm(dir,{recursive:true,force:true});}
