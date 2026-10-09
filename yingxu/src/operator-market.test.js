import test from 'node:test';
import assert from 'node:assert/strict';
import {createOperatorClient} from './operator-client.js';
import {createOperatorController} from './operator-controller.js';
import {operatorModels,operatorCandidateSelection,operatorCandidateCurrent,operatorCandidatePreviewMatches,initialOperatorMarket,operatorMarketReducer,operatorSlotsLabel,operatorMarketSummary,roundedSpec} from './operator-model.js';

const catalog={profiles:[
  {id:'pruned-5090',model_id:'MiniMax-H3-Pruned-Rank8-INT8',gpu_models:['RTX 5090']},
  {id:'pruned-pro',model_id:'MiniMax-H3-Pruned-Rank8-INT8',gpu_models:['RTX PRO 6000 Blackwell']},
  {id:'base-bf16',model_id:'MiniMax-H3-Base-BF16'},
  {id:'base-int8',model_id:'MiniMax-H3-Base-INT8'},
]};
const query={model_id:'MiniMax-H3-Pruned-Rank8-INT8',mode:'ref',ttl_seconds:10800};
const row={provider:'targon',offer_id:'rtx6000b-small',gpu_type:'RTX PRO 6000 Blackwell',gpu_count:1,available_count:2,observed_at:990,
  selection:{runtime_profile_id:'pruned-pro',mode:'ref',provider:'targon',gpu_type:'RTX PRO 6000 Blackwell',gpu_count:1,node_count:1,ttl_seconds:10800,filters:{},offer_id:'rtx6000b-small'}};
const result={...query,observed_at:995,fresh_seconds:120,providers:[{provider:'targon',status:'ok',observed_at:990}],candidates:[row]};

test('model-first catalog groups hardware profiles without combining precision variants',()=>{
  const models=operatorModels(catalog);
  assert.equal(models.length,3);
  assert.deepEqual(models[0].profiles.map(profile=>profile.id),['pruned-5090','pruned-pro']);
  assert.match(models[0].label,/Pruned Rank8 INT8/);
  assert.doesNotMatch(models[0].label,/5090|PRO 6000/);
  assert.notEqual(models[1].id,models[2].id);
});

test('candidate selection copies the exact server allocation and rejects model, mode, TTL or offer substitution',()=>{
  const chosen=operatorCandidateSelection(catalog,query,row);
  assert.deepEqual(chosen,row.selection);
  assert.notEqual(chosen,row.selection);
  assert.notEqual(chosen.filters,row.selection.filters);
  for(const patch of [{runtime_profile_id:'base-bf16'},{runtime_profile_id:'missing'},{mode:'fl'},{ttl_seconds:120},{offer_id:'other-host'},{node_count:2},{gpu_count:2},{provider:'lium'}]){
    assert.equal(operatorCandidateSelection(catalog,query,{...row,selection:{...row.selection,...patch}}),null);
  }
});

test('model, mode, window changes and refresh invalidate selected allocation, consent and preview; late responses stay discarded',()=>{
  const initial=initialOperatorMarket({max_ttl_seconds:10800});
  assert.deepEqual(initial.query,{model_id:'',mode:'fl',ttl_seconds:10800});
  let state=operatorMarketReducer(initial,{type:'change',patch:query});
  state=operatorMarketReducer(state,{type:'scan'});
  const readRevision=state.revision;
  state=operatorMarketReducer(state,{type:'result',revision:readRevision,result});
  state=operatorMarketReducer(state,{type:'select',row,selection:row.selection});
  state=operatorMarketReducer(state,{type:'preview',revision:state.revision,preview:{preview_id:'old',can_start:true}});
  state=operatorMarketReducer(state,{type:'confirm',value:true});
  for(const patch of [{model_id:'MiniMax-H3-Base-BF16'},{mode:'fl'},{ttl_seconds:7200}]){
    const next=operatorMarketReducer(state,{type:'change',patch});
    for(const key of ['result','selection','row','preview'])assert.equal(next[key],null);
    assert.equal(next.confirmed,false);
    assert.equal(operatorMarketReducer(next,{type:'result',revision:readRevision,result}),next);
    assert.equal(operatorMarketReducer(next,{type:'preview',revision:state.revision,preview:{preview_id:'late'}}),next);
  }
  const refreshed=operatorMarketReducer(state,{type:'scan'});
  assert.equal(refreshed.preview,null);assert.equal(refreshed.selection,null);assert.equal(refreshed.confirmed,false);
  const returned=operatorMarketReducer(state,{type:'back'});
  assert.equal(returned.result,result);assert.deepEqual(returned.query,query);
  assert.equal(returned.selection,null);assert.equal(returned.preview,null);assert.equal(returned.confirmed,false);
  assert.equal(operatorMarketReducer(returned,{type:'preview',revision:state.revision,preview:{preview_id:'late'}}),returned);
});

test('candidate freshness needs matching query and current provider and row observations',()=>{
  assert.equal(operatorCandidateCurrent(result,query,row,1000000),true);
  for(const changed of [{...row,observed_at:879},{...row,observed_at:1001},{...row,observed_at:null}])assert.equal(operatorCandidateCurrent(result,query,changed,1000000),false);
  assert.equal(operatorCandidateCurrent({...result,providers:[{provider:'targon',status:'error',observed_at:990}]},query,row,1000000),false);
  assert.equal(operatorCandidateCurrent(result,{...query,mode:'fl'},row,1000000),false);
  assert.equal(operatorCandidateCurrent(result,{...query,ttl_seconds:7200},row,1000000),false);
  assert.equal(operatorCandidateCurrent(result,query,row,1111000),false);
  assert.match(operatorMarketSummary({...result,providers:[]},1000000),/不能判断/);
});

test('preview must retain exact offer and deployment identity; server filters can become authoritative',()=>{
  const preview={selection:{...row.selection,filters:{min_ram_gib:96}},selected_offer:row};
  assert.equal(operatorCandidatePreviewMatches(row.selection,preview),true);
  for(const patch of [{offer_id:'other-host'},{runtime_profile_id:'pruned-5090'},{gpu_count:2},{provider:'lium'}])assert.equal(operatorCandidatePreviewMatches(row.selection,{...preview,selection:{...preview.selection,...patch}}),false);
  assert.equal(operatorCandidatePreviewMatches(row.selection,{...preview,selected_offer:{...row,offer_id:'other-host'}}),false);
  const {offer_id,...legacySelection}=row.selection;
  assert.equal(operatorCandidatePreviewMatches(row.selection,{selection:legacySelection}),false);
});

test('cards only promise per-GPU slots for an exactly qualified topology and round resource telemetry',()=>{
  assert.equal(operatorSlotsLabel({gpu_count:2,execution_slots:2,deployment_qualified:true}),'2 个执行槽 · 每卡 1 槽');
  for(const candidate of [{gpu_count:2,execution_slots:1,deployment_qualified:true},{gpu_count:2,execution_slots:2,deployment_qualified:false},{gpu_count:2,execution_slots:null}])assert.equal(operatorSlotsLabel(candidate),'执行槽数量待验收');
  assert.equal(roundedSpec(96.123456,'GiB'),'96.1 GiB');assert.equal(roundedSpec(null,'Mbps'),'尚未上报');
});

test('candidate GET uses only model/mode/window, then explicit preview sends exact selected row without a start',async()=>{
  const calls=[],client=createOperatorClient({fetcher:async(path,options)=>{calls.push({path,options});return new Response(JSON.stringify(result),{headers:{'Content-Type':'application/json','X-Authenticated-Account':'owner'}});}});
  client.setAccount('owner');
  await client.candidates({...query,provider:'lium',gpu_count:8});
  const search=new URL(calls[0].path,'https://local.invalid').searchParams;
  assert.deepEqual(Object.fromEntries(search),{model_id:query.model_id,mode:'ref',ttl_seconds:'10800'});
  assert.equal(calls[0].options.method,'GET');
  await client.preview(operatorCandidateSelection(catalog,query,row));
  assert.deepEqual(JSON.parse(calls[1].options.body),row.selection);
  assert.equal(calls.some(call=>call.path.endsWith('/starts')),false);
});

test('old-account candidate responses are rejected and never create a pending rental',async()=>{
  let finish,account;
  const client={setAccount:value=>{account=value;},reset(){},state:async()=>({operator:{account},nodes:[]}),catalog:async()=>catalog,candidates:()=>new Promise(resolve=>{finish=resolve;})};
  const controller=createOperatorController({client,storage:{getItem:()=>null}});
  await controller.setAccount('a');const read=controller.candidates(query);await controller.setAccount('b');finish(result);
  await assert.rejects(read,/账户已改变/);
  assert.equal(controller.getState().pending,null);assert.equal(controller.getState().account,'b');
});
