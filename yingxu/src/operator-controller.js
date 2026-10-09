import {createOperatorClient} from './operator-client.js';

const uid=()=>crypto.randomUUID();
const keyFor=account=>'sixnine:operator:pending:'+account;
const clone=value=>structuredClone(value);
export function createOperatorController({client=createOperatorClient(),storage=globalThis.localStorage}={}){
  let epoch=0,loading=false,state={account:null,snapshot:null,catalog:null,pending:null,busy:false,loading:false,error:'',denied:false,unavailable:false};
  const listeners=new Set();
  const emit=patch=>{state={...state,...patch};listeners.forEach(fn=>fn());};
  function guard(account,version){if(state.account!==account||epoch!==version)throw Error('登录账户已改变，原账户响应未展示。');}
  function pending(value){if(!state.account||!storage)throw Error('浏览器无法保留操作标识，未发出管理操作。');if(value)storage.setItem(keyFor(state.account),JSON.stringify(value));else storage.removeItem(keyFor(state.account));emit({pending:value});}
  async function refresh(){
    if(!state.account||loading)return;loading=true;const account=state.account,current=epoch;emit({loading:true});
    try{const [snapshot,catalog]=await Promise.all([client.state(),client.catalog()]);guard(account,current);if(snapshot.operator?.account!==account)throw Error('管理身份与登录账户不一致。');emit({snapshot,catalog,denied:false,unavailable:false,error:''});return snapshot;}
    catch(error){if(current===epoch)emit({error:error.message,denied:error.status===403,unavailable:error.status===404,...([401,403].includes(error.status)?{snapshot:null,catalog:null}:{})});throw error;}
    finally{if(current===epoch){loading=false;emit({loading:false});}}
  }
  async function act(fn){if(state.busy)throw Error('原操作仍在处理中。');const account=state.account,current=epoch;emit({busy:true,error:''});try{const result=await fn();guard(account,current);return result;}catch(error){if(current===epoch)emit({error:error.message});throw error;}finally{if(current===epoch)emit({busy:false});}}
  async function send(operation){
    const account=state.account,current=epoch;
    try{const result=await client[operation.method](...operation.args,operation.body,operation.key);guard(account,current);pending(null);await refresh();return result;}
    catch(error){guard(account,current);if(error.status>=400&&error.status<500&&![408,429].includes(error.status))pending(null);throw error;}
  }
  function command(method,args,body){return act(async()=>{if(state.pending)throw Error('有原管理操作待核对，请先核对原操作。');const operation={account:state.account,method,args,body:clone(body),key:'operator-'+uid(),created_at:Date.now()};pending(operation);return send(operation);});}
  return {client,subscribe(fn){listeners.add(fn);return()=>listeners.delete(fn);},getState:()=>state,
    async setAccount(account){epoch++;client.setAccount(account);loading=false;let saved=null;try{saved=account?JSON.parse(storage?.getItem(keyFor(account))||'null'):null;}catch{saved={corrupt:true};}emit({account,snapshot:null,catalog:null,pending:saved,busy:false,loading:false,error:'',denied:false,unavailable:false});if(account)try{await refresh();}catch{}},
    refresh,poll:()=>state.busy?Promise.resolve():refresh(),
    preview:selection=>act(()=>client.preview(selection)),
    candidates:query=>act(()=>client.candidates(query)),
    offers:selection=>act(()=>client.offers(selection)),
    start:preview=>command('start',[],{preview_id:preview.preview_id}),
    drain:node=>command('drain',[node.id],{expected_version:node.version}),
    stop:node=>command('stop',[node.id],{expected_version:node.version}),
    updatePolicy:body=>act(async()=>{if(state.pending)throw Error('请先核对原管理操作。');const result=await client.updatePolicy(body);await refresh();return result;}),
    recover:()=>act(async()=>{const op=state.pending;if(!op||op.corrupt||op.account!==state.account||!['start','drain','stop'].includes(op.method)||!Array.isArray(op.args)||typeof op.key!=='string')throw Error('原操作记录无法核对，请保留记录并联系管理员。');return send(op);}),
    destroy(){epoch++;client.reset();listeners.clear();},
  };
}
