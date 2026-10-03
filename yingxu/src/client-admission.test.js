import test from 'node:test';
import assert from 'node:assert/strict';
import {createCloudClient} from './cloud-client.js';

const tick=()=>new Promise(resolve=>setImmediate(resolve));
const busy=(headers={})=>new Response('{"detail":"busy"}',{status:429,headers:{'Retry-After':'0',...headers}});

test('media reads run two at a time with room for bounded control requests',async()=>{
  const calls=[],pending=[];let active=0,downloads=0,maxActive=0,maxDownloads=0;
  const api=createCloudClient({fetcher:(path)=>{
    const media=path.endsWith('/content');calls.push(path);active++;if(media)downloads++;
    maxActive=Math.max(maxActive,active);maxDownloads=Math.max(maxDownloads,downloads);
    return new Promise(resolve=>pending.push(()=>{active--;if(media)downloads--;resolve(new Response(media?'media':'{"projects":[]}'));}));
  }});
  const requests=Array.from({length:6},(_,i)=>api.download(`/v1/artifacts/a${i}/content`));
  requests.push(api.projects(),api.projects(),api.projects());
  assert.equal(calls.length,4);
  while(pending.length){pending.shift()();await tick();}
  await Promise.all(requests);
  assert.equal(calls.length,9);assert.equal(maxDownloads,2);assert.equal(maxActive,4);
});

test('account reset aborts active and queued reads without fetching old queued paths',async()=>{
  const calls=[];
  const api=createCloudClient({fetcher:(path,{signal})=>{
    calls.push(path);return new Promise((resolve,reject)=>signal.addEventListener('abort',()=>{const error=Error('abort');error.name='AbortError';reject(error);},{once:true}));
  }});
  api.setAccount('superdan');
  const old=Array.from({length:8},(_,i)=>api.download(`/v1/artifacts/old${i}/content`));
  const settled=Promise.allSettled(old);assert.equal(calls.length,2);
  api.setAccount('supervan');
  assert.ok((await settled).every(result=>result.status==='rejected'));
  assert.equal(calls.length,2);
});

test('429 GET retry keeps exact path and owner and stops after three attempts',async()=>{
  const calls=[];const api=createCloudClient({fetcher:async(path,options)=>{calls.push([path,options.headers['X-Expected-Account']]);return calls.length===1?busy():new Response('{"projects":[]}');}});
  api.setAccount('superdan');await api.projects();
  assert.deepEqual(calls,[['/v1/projects?limit=100&offset=0','superdan'],['/v1/projects?limit=100&offset=0','superdan']]);
  let count=0;const unavailable=createCloudClient({fetcher:async()=>{count++;return busy();}});
  await assert.rejects(unavailable.projects(),error=>error.status===429);assert.equal(count,3);
});

test('writes, network failures and non-429 responses are never automatically retried',async()=>{
  let count=0,last;
  const api=createCloudClient({fetcher:async(path,options)=>{count++;last={path,options};return busy();}});
  await assert.rejects(api.submit('plan-one','original-intent'),error=>error.status===429);
  assert.equal(count,1);assert.equal(last.options.headers['Idempotency-Key'],'original-intent');
  for(const response of [()=>new Response('{}',{status:503}),()=>{throw Error('offline');}]){
    let attempts=0;const read=createCloudClient({fetcher:async()=>{attempts++;return response();}});
    await assert.rejects(read.projects());assert.equal(attempts,1);
  }
});

test('reset interrupts a Retry-After wait and foreign-owner errors are not retried',async()=>{
  let calls=0;const api=createCloudClient({fetcher:async()=>{calls++;return busy({'Retry-After':'30'});},timeoutMs:60000});
  const pending=api.projects();await tick();api.reset();await assert.rejects(pending);assert.equal(calls,1);
  let foreignCalls=0,unauthorized=0;
  const foreign=createCloudClient({fetcher:async()=>{foreignCalls++;return busy({'X-Authenticated-Account':'supervan'});},onUnauthorized:()=>unauthorized++});
  foreign.setAccount('superdan');await assert.rejects(foreign.projects(),error=>error.status===401);
  assert.equal(foreignCalls,1);assert.equal(unauthorized,1);
});

test('oversized Retry-After is surfaced instead of retrying sooner than the server requested',async()=>{
  let attempts=0;
  const api=createCloudClient({fetcher:async()=>{attempts++;return busy({'Retry-After':'120'});}});
  await assert.rejects(api.projects(),error=>error.status===429);assert.equal(attempts,1);
});
