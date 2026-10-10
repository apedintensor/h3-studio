import test from 'node:test';
import assert from 'node:assert/strict';
import {createDstackClient} from './dstack-client.js';
import {createOperatorController} from './operator-controller.js';
import {dstackGroups,dstackNodeStatus,dstackPreviewCurrent} from './dstack-presentation.js';

const memory=()=>{const values=new Map();return {getItem:key=>values.get(key)||null,setItem:(key,value)=>values.set(key,value),removeItem:key=>values.delete(key)};};
function client(overrides={}){let account;return {setAccount(value){account=value;},reset(){},state:async()=>({capacity_backend:'dstack-v1',enabled:true,operator:{account},nodes:[]}),catalog:async()=>({enabled:true,profiles:[]}),...overrides};}

test('dstack client uses authenticated same-origin endpoints and original command keys',async()=>{
  const calls=[],api=createDstackClient({fetcher:async(path,options)=>{calls.push({path,options});return new Response(JSON.stringify(path.endsWith('/state')?{capacity_backend:'dstack-v1',operator:{account:'superdan'},nodes:[{node_id:'original-node'}]}:{}),{headers:{'Content-Type':'application/json','X-Authenticated-Account':'superdan'}});}});
  api.setAccount('superdan');
  assert.equal((await api.state()).nodes[0].id,'original-node');
  await api.preview({profile_id:'p',ttl_seconds:900});
  await api.start({preview_id:'quote'},'original-start');
  await api.stop('node/one',{expected_version:1},'original-stop');
  await api.hold('node/one',{hold_seconds:300,expected_version:1},'original-hold');
  assert.deepEqual(calls.map(call=>call.path),['/v1/operator/dstack/state','/v1/operator/dstack/previews','/v1/operator/dstack/starts','/v1/operator/dstack/nodes/node%2Fone/stop','/v1/operator/dstack/nodes/node%2Fone/hold']);
  assert.equal(calls[1].options.headers['Idempotency-Key'],undefined);
  assert.deepEqual(calls.slice(2).map(call=>call.options.headers['Idempotency-Key']),['original-start','original-stop','original-hold']);
  for(const call of calls){assert.equal(call.options.credentials,'same-origin');assert.equal(call.options.headers['X-Expected-Account'],'superdan');}
});

test('dstack reads reject missing adapter identity rather than display dummy empty capacity',async()=>{
  const api=createDstackClient({fetcher:async()=>new Response(JSON.stringify({nodes:[]}),{headers:{'Content-Type':'application/json'}})});
  await assert.rejects(api.state(),/状态尚未确认/);
});

test('dstack unknown start survives reload and replays only the same original request',async()=>{
  const storage=memory(),calls=[];let fail=true;
  const api=client({start:async(body,key)=>{calls.push({body,key});if(fail)throw Error('connection lost');return {command_id:'same-command',node_id:'same-node'};}});
  const first=createOperatorController({client:api,storage,pendingNamespace:'dstack'});
  await first.setAccount('superdan');
  await assert.rejects(first.start({preview_id:'original-preview'}));
  const original=structuredClone(first.getState().pending);first.destroy();
  const resumed=createOperatorController({client:api,storage,pendingNamespace:'dstack'});
  await resumed.setAccount('superdan');await resumed.poll();
  assert.equal(calls.length,1);assert.deepEqual(resumed.getState().pending,original);
  fail=false;await resumed.recover();
  assert.deepEqual(calls[1],calls[0]);assert.equal(resumed.getState().pending,null);
});

test('dstack and legacy pending commands remain in distinct existing owner namespaces',async()=>{
  const storage=memory(),lost=async()=>{throw Error('lost');};
  const legacy=createOperatorController({client:client({start:lost}),storage});
  const dstack=createOperatorController({client:client({start:lost}),storage,pendingNamespace:'dstack'});
  await legacy.setAccount('superdan');await dstack.setAccount('superdan');
  await assert.rejects(legacy.start({preview_id:'legacy'}));await assert.rejects(dstack.start({preview_id:'dstack'}));
  assert.equal(JSON.parse(storage.getItem('sixnine:operator:pending:superdan')).body.preview_id,'legacy');
  assert.equal(JSON.parse(storage.getItem('sixnine:operator:dstack:pending:superdan')).body.preview_id,'dstack');
  await dstack.setAccount('supervan');assert.equal(dstack.getState().pending,null);
  await dstack.setAccount('superdan');assert.equal(dstack.getState().pending.body.preview_id,'dstack');
});

test('hold and stop retain original version and key after unknown responses',async()=>{
  const storage=memory(),calls=[];let fail=true;
  const method=name=>async(id,body,key)=>{calls.push({name,id,body,key});if(fail)throw Error('lost');return {node_id:id};};
  const controller=createOperatorController({client:client({hold:method('hold'),stop:method('stop')}),storage,pendingNamespace:'dstack'});
  await controller.setAccount('superdan');
  await assert.rejects(controller.hold({id:'node',version:1000},300));
  assert.deepEqual(calls[0].body,{expected_version:1000,hold_seconds:300});
  fail=false;await controller.recover();assert.deepEqual(calls[1],calls[0]);
  await controller.stop({id:'node',version:1010});
  assert.deepEqual(calls[2].body,{expected_version:1010});
});

test('authorization loss clears modal data without forgetting an unknown original operation',async()=>{
  for(const status of [401,403]){
    let denied=false;
    const storage=memory(),calls=[];
    const controller=createOperatorController({storage,pendingNamespace:'dstack',client:client({
      state:async()=>{if(denied)throw Object.assign(Error('authorization lost'),{status});return {operator:{account:'superdan'},nodes:[{id:'owned-node'}]};},
      catalog:async()=>({enabled:true,profiles:[{id:'owned-profile'}]}),
      start:async(body,key)=>{calls.push({body,key});throw Error('unknown response');},
    })});
    await controller.setAccount('superdan');
    assert.equal(controller.getState().catalog.profiles[0].id,'owned-profile');
    await assert.rejects(controller.start({preview_id:'owned-preview'}));
    const original=structuredClone(controller.getState().pending);
    denied=true;
    await assert.rejects(controller.refresh());
    assert.equal(controller.getState().snapshot,null);
    assert.equal(controller.getState().catalog,null);
    assert.deepEqual(controller.getState().pending,original);
    assert.deepEqual(JSON.parse(storage.getItem('sixnine:operator:dstack:pending:superdan')),original);
    assert.equal(calls.length,1);
    controller.destroy();
  }
});

test('acknowledged command remains accepted when the subsequent read fails',async()=>{
  let acknowledged=false;
  const controller=createOperatorController({storage:memory(),pendingNamespace:'dstack',client:client({
    state:async()=>{if(acknowledged)throw Error('read temporarily unavailable');return {operator:{account:'superdan'},nodes:[]};},
    start:async()=>{acknowledged=true;return {command_id:'accepted'};}})});
  await controller.setAccount('superdan');
  assert.deepEqual(await controller.start({preview_id:'p'}),{command_id:'accepted'});
  assert.equal(controller.getState().pending,null);
  assert.match(controller.getState().error,/read temporarily unavailable/);
});

test('running is never presented as ready without exact fresh native state',()=>{
  const now=1000000,node={id:'n',state:'runtime_unconfirmed',ready:false,desired_state:'running',observation_fresh:true,observed_at:1000,hard_deadline:2000};
  assert.equal(dstackNodeStatus(node,now).label,'等待运行接口验收');
  assert.equal(dstackNodeStatus({...node,state:'ready',ready:true},now).label,'运行接口就绪');
  assert.equal(dstackNodeStatus({...node,state:'ready',native_ready:true,ready:false},now).label,'等待业务执行槽与准入');
  assert.equal(dstackNodeStatus({...node,state:'ready',ready:true},now+31000).label,'状态待核对');
  assert.equal(dstackNodeStatus({...node,state:'ready',ready:true,observation_fresh:false},now).label,'状态待核对');
});

test('unknown deletion and unsettled costs remain visible; only proven unpaid end becomes history',()=>{
  const groups=dstackGroups([{id:'unknown',state:'removal_unknown',desired_state:'stopped'},
    {id:'terminal',state:'stopped',billing_state:'unsettled'},
    {id:'cancelled',state:'stopped',billing_state:'no_charge_confirmed'}],1000000);
  assert.deepEqual(groups.current.map(node=>node.id),['unknown']);
  assert.deepEqual(groups.review.map(node=>node.id),['terminal']);
  assert.deepEqual(groups.history.map(node=>node.id),['cancelled']);
});

test('expired or changed profile/mode selection cannot confirm another preview',()=>{
  const preview={capacity_backend:'dstack-v1',preview_id:'p',selection:{profile_id:'fl-profile',ttl_seconds:900},
    expires_at:1100,hard_deadline:1900,reservation_microusd:250000,hourly_cost_microusd:750000};
  assert.equal(dstackPreviewCurrent(preview,'fl-profile',900,1000000),true);
  assert.equal(dstackPreviewCurrent(preview,'ref-profile',900,1000000),false);
  assert.equal(dstackPreviewCurrent(preview,'fl-profile',1800,1000000),false);
  assert.equal(dstackPreviewCurrent(preview,'fl-profile',900,1100000),false);
});
