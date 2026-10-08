import test from 'node:test';
import assert from 'node:assert/strict';
import {createOperatorClient} from './operator-client.js';
import {createOperatorController} from './operator-controller.js';
import {allowed,profileSelection,nodeDeadline,runtimeDuration,initialOperatorSelection,operatorStartPayload} from './operator-model.js';
import {recipeFor,effectiveControlSchema,effectiveLimits,clipLimits,inputProblems} from './quick-chat-model.js';

const memory=()=>{const data=new Map();return {getItem:key=>data.get(key)||null,setItem:(key,value)=>data.set(key,value),removeItem:key=>data.delete(key)};};
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};};
function fakeClient(overrides={}){let account;return {setAccount(value){account=value;},reset(){},state:async()=>({observed_at:Date.now()/1000,operator:{account,permissions:{view:true,start:true}},nodes:[]}),catalog:async()=>({profiles:[]}),...overrides};}
test('uncertain start is retained across reload and retried only explicitly with original key and preview',async()=>{
  const storage=memory(),calls=[];let fail=true;
  const client=fakeClient({start:async(body,key)=>{calls.push({body,key});if(fail)throw Error('connection lost');return {operation:{id:'original',state:'accepted'}};}});
  const first=createOperatorController({client,storage});await first.setAccount('owner');await assert.rejects(first.start({preview_id:'p1'}));
  const saved=structuredClone(first.getState().pending);assert.ok(saved.key);await first.poll();assert.equal(calls.length,1);
  await assert.rejects(first.start({preview_id:'p2'}));assert.equal(calls.length,1);first.destroy();
  const next=createOperatorController({client,storage});await next.setAccount('owner');assert.deepEqual(next.getState().pending,saved);assert.equal(calls.length,1);
  fail=false;await next.recover();assert.deepEqual(calls[0],calls[1]);assert.equal(next.getState().pending,null);
});
test('old-account start response cannot erase its record or appear under another owner',async()=>{
  const held=deferred(),storage=memory(),client=fakeClient({start:()=>held.promise}),controller=createOperatorController({client,storage});
  await controller.setAccount('a');const call=controller.start({preview_id:'p'});await controller.setAccount('b');held.resolve({operation:{id:'a-op'}});
  await assert.rejects(call);assert.equal(controller.getState().account,'b');assert.equal(controller.getState().pending,null);assert.ok(storage.getItem('sixnine:operator:pending:a'));assert.equal(storage.getItem('sixnine:operator:pending:b'),null);
});
test('late old-account read never unlocks a newer account read or displays its nodes',async()=>{
  const a=deferred(),b=deferred();let account,reads=0;
  const client=fakeClient({setAccount(value){account=value;},state(){reads++;return account==='a'?a.promise:b.promise;}}),controller=createOperatorController({client,storage:memory()});
  const old=controller.setAccount('a'),current=controller.setAccount('b');a.resolve({operator:{account:'a'},nodes:[{id:'private-a'}]});await old;
  assert.equal(controller.getState().loading,true);await controller.refresh();assert.equal(reads,2);assert.equal(controller.getState().snapshot,null);
  b.resolve({operator:{account:'b'},nodes:[]});await current;assert.equal(controller.getState().loading,false);assert.equal(controller.getState().snapshot.operator.account,'b');
});
test('403 clears privileged stale data and cannot masquerade as empty healthy inventory',async()=>{
  let denied=false;const client=fakeClient({state:async()=>{if(denied)throw Object.assign(Error('forbidden'),{status:403});return {operator:{account:'a',permissions:{view:true}},nodes:[{id:'n'}]};}}),controller=createOperatorController({client,storage:memory()});
  await controller.setAccount('a');denied=true;await assert.rejects(controller.refresh());assert.equal(controller.getState().denied,true);assert.equal(controller.getState().snapshot,null);assert.equal(controller.getState().catalog,null);
});
test('storage failure prevents a start request and an explicit rejection releases its old intent',async()=>{
  let calls=0;const client=fakeClient({start:async()=>{calls++;throw Object.assign(Error('conflict'),{status:409});}});
  const blocked=createOperatorController({client,storage:{getItem:()=>null,setItem(){throw Error('blocked storage');}}});await blocked.setAccount('a');await assert.rejects(blocked.start({preview_id:'p'}));assert.equal(calls,0);
  const controller=createOperatorController({client,storage:memory()});await controller.setAccount('a');await assert.rejects(controller.start({preview_id:'p'}));assert.equal(calls,1);assert.equal(controller.getState().pending,null);
});
test('drain/stop bind observed node version; client uses only same-origin cookie endpoint and supplied idempotency',async()=>{
  const requests=[],client=createOperatorClient({fetcher:async(path,options)=>{requests.push({path,options});return new Response(JSON.stringify({operation:{id:'op'}}),{status:202,headers:{'Content-Type':'application/json','X-Authenticated-Account':'owner'}});}});
  client.setAccount('owner');await client.stop('node/one',{expected_version:'original-version'},'original-key');
  assert.equal(requests[0].path,'/v1/operator/capacity/nodes/node%2Fone/stop');assert.equal(requests[0].options.credentials,'same-origin');assert.equal(requests[0].options.headers['Idempotency-Key'],'original-key');assert.deepEqual(JSON.parse(requests[0].options.body),{expected_version:'original-version'});
});
test('stale, absent and unauthorized observations never enable an operator action',()=>{
  const now=Date.now(),snapshot={observed_at:now/1000,operator:{permissions:{start:true,stop:false}}};assert.equal(allowed(snapshot,'start',now),true);assert.equal(allowed(snapshot,'stop',now),false);assert.equal(allowed(snapshot,'start',now+31000),false);assert.equal(allowed(null,'start',now),false);
});
test('inventory and preview preserve explicit FL/REF mode without initiating rental',async()=>{
  const requests=[],client=createOperatorClient({fetcher:async(path,options)=>{requests.push({path,options});return new Response('{}',{headers:{'Content-Type':'application/json','X-Authenticated-Account':'owner'}});}});
  client.setAccount('owner');const selection={runtime_profile_id:'p',mode:'ref',gpu_type:'GPU exact',gpu_count:2,node_count:1,ttl_seconds:3600};await client.offers(selection);await client.preview(selection);
  const query=new URL(requests[0].path,'https://local.invalid').searchParams;assert.equal(query.get('mode'),'ref');assert.equal(query.get('gpu_count'),'2');assert.deepEqual(JSON.parse(requests[1].options.body),selection);assert.equal(requests.some(r=>r.path.endsWith('/starts')),false);
});
test('deployment profile selects only reported GPU topology, preserving price/TTL and authored node count',()=>{
  const selection={node_count:2,ttl_seconds:1800,filters:{max_price_per_gpu_hour_microusd:1500000}};
  const next=profileSelection({id:'pro-bf16',gpu_models:['PRO exact'],gpu_count_options:[2],minimum_ram_bytes:256*1024**3,minimum_disk_bytes:350*1024**3},selection);
  assert.equal(next.gpu_count,2);assert.equal(next.gpu_type,'PRO exact');assert.equal(next.filters.min_ram_gib,256);assert.equal(next.node_count,2);assert.equal(next.ttl_seconds,1800);assert.equal(next.filters.max_price_per_gpu_hour_microusd,1500000);
});
test('new operator run uses a policy-bounded 180-minute default and reports provider minimum without changing an override',()=>{
  assert.equal(initialOperatorSelection({max_ttl_seconds:14400}).ttl_seconds,10800);
  assert.equal(initialOperatorSelection({max_ttl_seconds:7200}).ttl_seconds,7200);
  const authored={ttl_seconds:3600},bounds=runtimeDuration({max_ttl_seconds:10800},3780,authored.ttl_seconds);
  assert.equal(bounds.minimumMinutes,63);assert.equal(bounds.maximumMinutes,180);assert.ok(bounds.problem);assert.equal(bounds.suggestedSeconds,10800);assert.equal(authored.ttl_seconds,3600);
  assert.equal(runtimeDuration({max_ttl_seconds:10800},3780,7200).problem,null);
  assert.equal(runtimeDuration({max_ttl_seconds:3600},3780,3600).suggestedSeconds,null);
  assert.ok(runtimeDuration({max_ttl_seconds:3600},3780,3600).problem);
  assert.equal(runtimeDuration({max_ttl_seconds:10800}).minimumMinutes,null);
});
test('default start omits filters and custom Lium filters never invent a CPU requirement',()=>{
  const selection=initialOperatorSelection({max_ttl_seconds:10800});
  assert.equal(Object.hasOwn(selection.filters,'min_cpu_cores'),false);
  assert.equal(Object.hasOwn(operatorStartPayload(selection),'filters'),false);
  selection.filters.min_ram_gib=128;selection.filters.max_price_per_gpu_hour_microusd=1200000;
  assert.equal(operatorStartPayload(selection,true),selection);
  assert.equal(operatorStartPayload(selection,true).filters.min_ram_gib,128);
  assert.equal(operatorStartPayload(selection,true).filters.max_price_per_gpu_hour_microusd,1200000);
  assert.equal(Object.hasOwn(operatorStartPayload(selection,true).filters,'min_cpu_cores'),false);
});
test('explicit deployment receives own control schema without legacy pool limits, old requests remain unchanged',()=>{
  const base={id:'fl',mode:'fl',controls:{steps:{type:'integer',maximum:100}},limits:{max_images:9},execution_support:{constraints:{max_steps:20,input_limits:{max_images:1}}},deployment_preset:{controls:{video_decode:'tiled'}}};
  const capabilities={recipes:[base],deployment_profiles:[{id:'bf16',model_id:'base-bf16',generation_support:{fl:{configured:false,enabled:false,controls:{steps:{type:'integer',maximum:50}},constraints:{input_limits:{max_images:2}}}}}]};
  const settings={recipe_id:'fl',deployment_profile_id:'bf16',controls:{steps:50}},selected=recipeFor(capabilities,settings);
  assert.equal(effectiveControlSchema(selected).steps.maximum,50);assert.equal(effectiveLimits(selected).max_images,2);assert.equal(selected.deployment_preset,null);assert.equal(selected.model_id,'base-bf16');assert.equal(settings.controls.steps,50);assert.equal(recipeFor(capabilities,{recipe_id:'fl'}),base);assert.equal(effectiveControlSchema(base).steps.maximum,20);
});

test('explicit REF profile keeps audio and video clip limits separate from each other and legacy limits',()=>{
  const base={id:'ref',mode:'ref',limits:{min_clip_duration:2,max_clip_duration:3,max_images:9},controls:{},custom_canvas_constraints:{maximum_pixel_area:400000},execution_support:{constraints:{input_limits:{max_audio_duration_seconds:3}}}};
  const support={controls:{},limits:{min_clip_duration:2,max_clip_duration:5.2,max_video_clip_duration:56/24,max_audio_clip_duration:5.2,max_total_video_duration:56/24,max_total_audio_duration:5.2,max_images:1,max_videos:1,max_audios:1,max_guides:0},constraints:{input_limits:{max_video_duration_seconds:56/24,max_audio_duration_seconds:5.2}}};
  const capabilities={recipes:[base],deployment_profiles:[{id:'native',generation_support:{ref:support}}]},selected=recipeFor(capabilities,{recipe_id:'ref',deployment_profile_id:'native'}),limits=effectiveLimits(selected);
  assert.deepEqual(clipLimits(limits,'audio'),{minimum:2,maximum:5.2});assert.deepEqual(clipLimits(limits,'video'),{minimum:2,maximum:56/24});assert.equal(selected.custom_canvas_constraints,null);
  const refs=[{asset_id:'audio',role:'reference'},{asset_id:'video',role:'reference',use_audio:false}],assets=[{id:'audio',kind:'audio',file_name:'voice.wav',metadata:{duration:5.2}},{id:'video',kind:'video',file_name:'motion.mp4',metadata:{duration:56/24,has_audio:false}}];
  assert.deepEqual(inputProblems(refs,assets,selected),[]);
  assert.match(inputProblems(refs,[assets[0],{...assets[1],metadata:{duration:3}}],selected).join(' '),/motion.mp4/);
  assert.match(inputProblems(refs,[{...assets[0],metadata:{duration:5.21}},assets[1]],selected).join(' '),/voice.wav/);
  assert.match(inputProblems([refs[0]],[assets[0]],base).join(' '),/2–3/);
  assert.equal(base.limits.max_clip_duration,3);assert.equal(effectiveLimits(base).max_audio_clip_duration,3);
});

test('missing explicit profile limits never inherit unrelated legacy media or custom canvas limits',()=>{
  const base={id:'ref',mode:'ref',limits:{max_images:9,max_audios:3,max_clip_duration:3},custom_canvas_constraints:{maximum_pixel_area:1},controls:{}};
  const selected=recipeFor({recipes:[base],deployment_profiles:[{id:'draft',generation_support:{ref:{controls:{}}}}]},{recipe_id:'ref',deployment_profile_id:'draft'});
  assert.deepEqual(selected.limits,{});assert.equal(effectiveLimits(selected).max_audio_clip_duration,null);assert.equal(selected.custom_canvas_constraints,null);assert.equal(recipeFor({recipes:[base]},{recipe_id:'ref'}),base);
});


test('safe stop deadline requires fresh verified evidence and never exceeds durable cap',()=>{
  const now=1000000,node={provider_safe_deadline:2000,hard_deadline:1800,provider_lifetime_state:'verified',provider_lifetime_observed_at:990};
  assert.deepEqual(nodeDeadline(node,now),{verified:true,label:'安全停止期限',deadline:1800});
  assert.equal(nodeDeadline({...node,provider_safe_deadline:1700},now).deadline,1700);
  for(const patch of [{provider_lifetime_observed_at:969},{provider_lifetime_observed_at:1001},{provider_lifetime_state:'unverified'},{provider_safe_deadline:null}]){
    const result=nodeDeadline({...node,...patch},now);assert.equal(result.verified,false);assert.equal(result.deadline,1800);assert.match(result.label,/待核对/);
  }
  assert.equal(nodeDeadline({},now).deadline,null);
});
