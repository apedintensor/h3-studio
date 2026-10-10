import {createQuickChatClient} from './quick-chat-client.js';
import {stableJSON} from './cloud-model.js';
import {bindingPayload} from './quick-chat-model.js';
import {requireModeAvailable} from './generation-availability.js';
const clone=value=>structuredClone(value),uuid=()=>crypto.randomUUID();
const record=(value,field)=>value?.[field]||value;
const merge=(older,newer)=>[...new Map([...older,...newer].map(value=>[value.id||value.binding_id,value])).values()];
export async function saveMaterialSelection(controller,{original,next,proposed}){
  const account=original.account,sid=original.session?.id;
  let expectedVersion=original.session?.version;
  function checked(version=true){const live=controller.getState();if(!sid||live.account!==account||live.session?.id!==sid||version&&live.session.version!==expectedVersion)throw Error('创作已被更新或目标已改变。请核对最新材料后再调整，原素材保留。');return live;}
  const live=checked();
  // Profile and controls may change without changing FL/REF mode.
  if(stableJSON(next)!==stableJSON(live.session.next_settings)){
    const patched=await controller.patch({expected_version:expectedVersion,next_settings:next});
    expectedVersion=record(patched,'session').version;
  }
  const latest=checked(),merged=proposed.map(binding=>{const known=latest.materials.find(item=>item.asset_id===binding.asset_id);return {...bindingPayload(binding),binding_id:known?.binding_id||binding.binding_id,version:known?.version??binding.version??0};});
  await controller.saveMaterials(merged);
  checked(false);return true;
}
export function createQuickChatController({client=createQuickChatClient(),storage=globalThis.localStorage}={}){
  let epoch=0,sessionEpoch=0,readGeneration=0,refreshing=false,availabilitySequence=0,availabilityRequest=null;
  let state={account:null,schema:null,capabilities:null,availability:null,availabilityChecking:false,operatorAccess:false,sessions:[],sessionsCursor:null,session:null,materials:[],timeline:[],historyCursor:null,forwardCursor:null,cards:{},revisions:{},preflights:{},submissions:{},imports:{},busy:false,error:'',notice:'',pending:null,lastUpdated:null};
  const listeners=new Set();const emit=patch=>{state={...state,...patch};for(const fn of listeners)fn();};
  const context=()=>({epoch,sessionEpoch,account:state.account,sessionId:state.session?.id});
  function guard(ctx,{session=true}={}){if(ctx.epoch!==epoch||ctx.account!==state.account||ctx.readGeneration!==undefined&&ctx.readGeneration!==readGeneration||session&&(ctx.sessionEpoch!==sessionEpoch||ctx.sessionId!==state.session?.id))throw Error('工作账户、会话或内容已经改变；旧响应没有加入当前创作。');}
  async function refreshAvailability({fresh=false}={}){
    if(!state.account)return null;
    if(availabilityRequest&&!fresh)return availabilityRequest;
    const ctx=context(),sequence=++availabilitySequence;emit({availabilityChecking:true});
    const request=(async()=>{try{const value=await Promise.resolve().then(()=>client.generationAvailability());guard(ctx,{session:false});if(sequence!==availabilitySequence)throw Error('执行状态已被更新，请重新核对。');emit({availability:value});return value;}catch(error){if(ctx.epoch===epoch&&sequence===availabilitySequence)emit({availability:null});throw error;}finally{if(ctx.epoch===epoch&&sequence===availabilitySequence){availabilityRequest=null;emit({availabilityChecking:false});}}})();
    availabilityRequest=request;return request;
  }
  async function checkGeneration(revision){
    const ctx=context();if(state.pending)throw Error('有原操作结果待核对，请先恢复原操作，不要重复提交。');
    const snapshot=await refreshAvailability({fresh:true});guard(ctx);
    return requireModeAvailable(snapshot,state.capabilities,revision);
  }
  async function readOperatorAccess(){const ctx=context();try{const me=await client.me();guard(ctx,{session:false});emit({operatorAccess:(me.username||me.account)===state.account&&me.operator_capacity?.view===true});}catch{}}
  const pendingName=account=>`yingxu:quick-chat:operation:${account}`;
  function readPending(account){try{return account?JSON.parse(storage?.getItem(pendingName(account))||'null'):null;}catch{return {corrupt:true};}}
  function writePending(value){if(!state.account||!storage)throw Error('无法保存原操作标识，未提交。请启用浏览器存储后重试。');if(value)storage.setItem(pendingName(state.account),JSON.stringify(value));else storage.removeItem(pendingName(state.account));emit({pending:value});}
  async function list({more=false}={}){const ctx=context(),data=await client.sessions({cursor:more?state.sessionsCursor:null});guard(ctx,{session:false});emit({sessions:more?merge(state.sessions,data.sessions||[]):data.sessions||[],sessionsCursor:data.next_cursor||null});return data;}
  async function loadResources(id,{initial=false}={}){
    const ctx={...context(),readGeneration};const [sessionData,materialsData,firstPage]=await Promise.all([client.session(id),client.materials(id),client.timeline(id,{...(initial?{}:{after_cursor:state.forwardCursor})})]);guard(ctx);
    const session=record(sessionData,'session');if(Number.isInteger(state.session?.version)&&session.version<state.session.version)throw Error('旧版本读取已丢弃，请刷新最新内容。');if(session.id!==id)throw Error('会话响应不匹配。');
    let timelineData=firstPage,events=[...(firstPage.events||[])];
    // Drain forward pages without jumping to latest_seq: a busy Agent can
    // write more than one page between polls. Older history is user-paged.
    if(!initial)for(let page=0;page<5&&timelineData.has_more&&timelineData.next_cursor;page++){
      timelineData=await client.timeline(id,{after_cursor:timelineData.next_cursor});guard(ctx);events=merge(events,timelineData.events||[]);
    }
    // Automatic naming may finish without an authoring-version change. Merge
    // the current summary rather than reloading or discarding paged history.
    const sessions=state.sessions.map(item=>item.id===session.id?{...item,title:session.title??item.title,updated_at:session.updated_at??item.updated_at,version:session.version}:item);
    emit({session,sessions,materials:materialsData.materials||materialsData.bindings||[],timeline:initial?events:merge(state.timeline,events),historyCursor:initial?(timelineData.before_cursor||timelineData.next_cursor||null):state.historyCursor,forwardCursor:timelineData.after_cursor||timelineData.latest_cursor||state.forwardCursor,lastUpdated:Date.now(),error:''});
    // Timeline holds durable creation references; current job state is read
    // separately even when its sequence has not changed.
    const cardIds=new Set(events.map(event=>event.record?.card_id||event.card_id).filter(Boolean));
    for(const revisionId of new Set(events.map(event=>event.record?.revision_id).filter(Boolean))){const revision=record(await client.revision(id,revisionId),'revision');guard(ctx);emit({revisions:{...state.revisions,[revision.id]:revision}});if(revision.card_id)cardIds.add(revision.card_id);}
    const eventPreflights=Object.fromEntries(events.filter(event=>event.type==='preflight.completed'&&event.record?.id).map(event=>[event.record.id,event.record]));
    if(Object.keys(eventPreflights).length)emit({preflights:{...state.preflights,...eventPreflights}});
    for(const cardId of cardIds){await loadCard(cardId,ctx);}
    const pendingTurns=new Map(state.timeline.map(event=>event.record).filter(turn=>turn?.text!==undefined&&['pending','running'].includes(turn.status)).map(turn=>[turn.id,turn]));
    for(const turnId of pendingTurns.keys()){const turn=record(await client.getTurn(id,turnId),'turn');guard(ctx);emit({timeline:state.timeline.map(event=>event.record?.id===turnId?{...event,record:turn}:event)});}
    const submissionIds=new Set([...Object.keys(state.submissions),...events.filter(event=>(event.type.startsWith('submission.')||event.type.startsWith('item.'))).map(event=>event.record?.id).filter(Boolean)]);
    for(const submissionId of submissionIds){const value=record(await client.submission(id,submissionId),'submission');guard(ctx);emit({submissions:{...state.submissions,[value.id]:value}});}
    const importIds=new Set([...Object.keys(state.imports),...events.filter(event=>event.type.startsWith('result_import.')).map(event=>event.record?.id).filter(Boolean)]);
    for(const importId of importIds){const value=record(await client.getResultImport(id,importId),'import');guard(ctx);emit({imports:{...state.imports,[value.id]:value}});}
    return session;
  }
  async function loadCard(cardId,ctx=context()){
    const response=await client.card(ctx.sessionId,cardId);guard(ctx);const card=record(response,'card');emit({cards:{...state.cards,[card.id]:card}});
    const ids=new Set([card.current_revision_id,card.current_revision?.id,...(response.revisions||card.revisions||[]).map(item=>item.id)].filter(Boolean));
    for(const id of ids){const revision=record(await client.revision(ctx.sessionId,id),'revision');guard(ctx);emit({revisions:{...state.revisions,[revision.id]:revision}});}
    return card;
  }
  async function refresh(){if(!state.account||!state.session||refreshing)return;refreshing=true;const ctx={...context(),readGeneration};try{await loadResources(state.session.id);}catch(error){try{guard(ctx);emit({error:'云端状态暂未更新，保留原任务；稍后自动核对。'});}catch{}throw error;}finally{refreshing=false;}}
  async function act(fn,{success}={}){if(state.busy)throw Error('原操作仍在处理中，请等待。');const ctx=context();emit({busy:true,error:'',notice:''});try{const value=await fn();guard(ctx,{session:false});if(success)emit({notice:success});return value;}catch(error){if(ctx.epoch===epoch)emit({error:error.message||'操作没有完成。请核对原记录。'});throw error;}finally{if(ctx.epoch===epoch)emit({busy:false});}}
  async function command(method,args,body,{sessionId=state.session?.id}={}){
    if(state.pending)throw Error('有原操作结果待核对，请先恢复原操作，不要重复提交。');
    readGeneration++;
    const ctx=context(),operation={id:uuid(),method,args,body:clone(body),sessionId,account:state.account,key:'quick-'+uuid(),created_at:Date.now()};writePending(operation);
    try{const value=await client[method](...args,body,operation.key);guard(ctx,{session:!!sessionId});writePending(null);return value;}
    catch(error){guard(ctx,{session:!!sessionId});if(error.status>=400&&error.status<500&&![408,429].includes(error.status))writePending(null);throw error;}
  }
  return {client,subscribe(fn){listeners.add(fn);return()=>listeners.delete(fn);},getState:()=>state,
    async setAccount(account){if(account===state.account)return;epoch++;sessionEpoch++;availabilitySequence++;availabilityRequest=null;client.setAccount(account);emit({account,schema:null,capabilities:null,availability:null,availabilityChecking:false,operatorAccess:false,sessions:[],sessionsCursor:null,session:null,materials:[],timeline:[],historyCursor:null,forwardCursor:null,cards:{},revisions:{},preflights:{},submissions:{},imports:{},busy:false,error:'',notice:'',pending:readPending(account),lastUpdated:null});if(!account)return;const ctx=context();const availabilityLoad=refreshAvailability().catch(()=>null);const operatorLoad=readOperatorAccess();try{const [schema,capabilities]=await Promise.all([client.schema(),client.capabilities()]);guard(ctx,{session:false});emit({schema,capabilities});await list();await Promise.all([availabilityLoad,operatorLoad]);}catch(error){try{guard(ctx,{session:false});emit({error:error.message});}catch{}}},
    list:options=>act(()=>list(options)),
    open:(id,{revisionId,cardId,submissionId}={})=>act(async()=>{sessionEpoch++;client.reset();emit({session:{id},materials:[],timeline:[],cards:{},revisions:{},preflights:{},submissions:{},imports:{},historyCursor:null,forwardCursor:null,error:'',notice:''});const session=await loadResources(id,{initial:true}),ctx=context();if(submissionId){const submission=record(await client.submission(id,submissionId),'submission');guard(ctx);emit({submissions:{...state.submissions,[submission.id]:submission}});revisionId=revisionId||submission.revision_id;}if(revisionId){const revision=record(await client.revision(id,revisionId),'revision');guard(ctx);emit({revisions:{...state.revisions,[revision.id]:revision}});cardId=cardId||revision.card_id;}if(cardId)await loadCard(cardId,ctx);return session;}),
    create:body=>act(async()=>{const response=await command('createSession',[],body,{sessionId:null}),session=record(response,'session');sessionEpoch++;emit({session,materials:[],timeline:[],cards:{},revisions:{},preflights:{},submissions:{},imports:{},historyCursor:null,forwardCursor:null});await list();await loadResources(session.id,{initial:true});return session;}),
    patch:patch=>act(async()=>{const result=await command('patchSession',[state.session.id],{expected_version:state.session.version,...patch}),session=record(result,'session');emit({session,sessions:state.sessions.map(item=>item.id===session.id?{...item,title:session.title,updated_at:session.updated_at,version:session.version}:item)});return result;},{success:'创作设置已保存；历史卡片保持原版。'}),
    saveMaterials:bindings=>act(async()=>{const ready=bindings.filter(binding=>{const previous=state.materials.find(item=>item.binding_id===binding.binding_id),asset=previous?.asset;if(asset?.status&&asset.status!=='ready')return false;return !(previous?.version===0&&binding.enabled===false&&stableJSON(binding)===stableJSON(bindingPayload(previous)));});const result=await command('saveMaterials',[state.session.id],{expected_version:state.session.version,bindings:ready});await loadResources(state.session.id);return result;},{success:'本轮材料选择已保存；历史原件保留。'}),
    upload:(file,assetId=uuid())=>act(async()=>{if(state.pending)throw Error('先核对原操作。');readGeneration++;const ctx=context(),key='quick-upload-'+assetId;writePending({id:assetId,method:'uploadMaterial',args:[ctx.sessionId],body:{client_asset_id:assetId,file_name:file.name},sessionId:ctx.sessionId,account:ctx.account,key,created_at:Date.now()});try{const value=await client.uploadMaterial(ctx.sessionId,file,assetId,key);guard(ctx);writePending(null);await loadResources(ctx.sessionId);return value;}catch(error){guard(ctx);if(error.status>=400&&error.status<500&&![408,429].includes(error.status))writePending(null);throw error;}},{success:'文件接收结果已记录；是否可生成以素材校验与预检为准。'}),
    resumeMaterial:assetId=>act(async()=>{const result=await command('resumeMaterial',[state.session.id,assetId],{});await loadResources(state.session.id);return result;},{success:'已核对原素材处理收据，没有重新上传原文件。'}),
    refreshAvailability,
    refresh:()=>act(async()=>{const ctx=context(),[schema,capabilities]=await Promise.all([client.schema(),client.capabilities()]);guard(ctx,{session:false});emit({schema,capabilities});await refreshAvailability().catch(()=>null);await list();return refresh();}),poll:()=>state.busy?Promise.resolve():refresh(),
    history:()=>act(async()=>{const ctx=context(),data=await client.timeline(ctx.sessionId,{before_cursor:state.historyCursor});guard(ctx);emit({timeline:merge(state.timeline,data.events||[]).sort((a,b)=>a.seq-b.seq),historyCursor:data.before_cursor||data.next_cursor||null});const ids=new Set((data.events||[]).map(e=>e.record?.card_id||e.card_id).filter(Boolean));for(const id of ids)await loadCard(id,ctx);}),
    turn:(text,model_id,assistant_mode='assist',{createCard=false}={})=>act(async()=>{if(createCard&&assistant_mode!=='none')throw Error('直接出卡只用于原输入模式，不替代助手回复。');const result=await command('turn',[state.session.id],{expected_version:state.session.version,text,model_id,assistant_mode,...(createCard?{create_card:true}:{})});await loadResources(state.session.id);return result;},{success:'这一轮已保存；只有确认任务卡才会生成视频。'}),
    createCard:body=>act(async()=>{const result=await command('createCard',[state.session.id],body);await loadResources(state.session.id);const card=record(result,'card');if(card.id)await loadCard(card.id);return result;},{success:'任务卡已保存，尚未提交视频生成。'}),
    reviseCard:(card,body)=>act(async()=>{const result=await command('reviseCard',[state.session.id,card.id],{expected_card_version:card.version,...body});await loadCard(card.id);await loadResources(state.session.id);return result;},{success:'新版本已保存；旧版本及已提交输入保留。'}),
    preflight:(revision,scope={})=>act(async()=>{await checkGeneration(revision);const response=await command('preflight',[state.session.id,revision.id],{capabilities_version:state.capabilities?.capabilities_version,revision_hash:revision.input_hash,...scope});const value=record(response,'preflight');emit({preflights:{...state.preflights,[value.id]:value}});return value;},{success:'输入与执行条件已检查。'}),
    submit:(revision,preflight)=>act(async()=>{await checkGeneration(revision);const response=await command('submit',[state.session.id,revision.id],{preflight_id:preflight.id,revision_hash:revision.input_hash,confirmed:true});const value=record(response,'submission');emit({submissions:{...state.submissions,[value.id]:value}});await loadResources(state.session.id);return value;},{success:'生成确认已接受，查看原任务进度，无需重复提交。'}),
    cancel:(submission,item_ids)=>act(async()=>{const value=await command('cancelSubmission',[state.session.id,submission.id],item_ids?{item_ids}:{});await refresh();return value;},{success:'取消意愿已记录；以最终停止状态为准，已执行部分可能计费。'}),
    retry:(submission,item,preflight)=>act(async()=>{await checkGeneration(state.revisions[submission.revision_id]||{});const result=await command('retryItem',[state.session.id,submission.id,item.id],{retry_of_execution_id:item.current_execution_id||item.execution?.id,fresh_preflight_id:preflight.id,confirmed:true});await refresh();return result;},{success:'原失败项的重试已记录，成功份保持不变。'}),
    resume:(submission,item,preflight)=>act(async()=>{await checkGeneration(state.revisions[submission.revision_id]||{});const result=await command('resumeAdmission',[state.session.id,submission.id],{item_ids:[item.id],fresh_preflight_id:preflight.id,confirmed:true});await refresh();return result;},{success:'原未准入项已恢复；未创建第二批。'}),
    importResult:(artifact,purpose='reference')=>act(async()=>{const response=await command('importResult',[state.session.id],{source_artifact_id:artifact.id,purpose});const value=record(response,'import');emit({imports:{...state.imports,[value.id]:value}});await loadResources(state.session.id);return value;},{success:'已记录结果导入；校验完成后才能作为参考。'}),
    acknowledge:turn=>act(async()=>{const result=await command('acknowledgeUnknown',[state.session.id,turn.id],{acknowledged:true});await loadResources(state.session.id);return result;}),
    async recover(){return act(async()=>{const operation=state.pending;if(!operation||operation.corrupt)throw Error('原操作记录无法读取，请管理员核对，不要重新创建任务。');if(operation.account!==state.account)throw Error('请登录原操作账户。');if(operation.sessionId&&operation.sessionId!==state.session?.id)throw Error('请先打开原操作会话。');readGeneration++;const ctx=context();if(operation.sessionId)await loadResources(operation.sessionId);if(operation.method==='uploadMaterial'){const catalog=await client.sessionAssets(operation.sessionId,operation.body.client_asset_id);guard(ctx);if(!(catalog.assets||[]).length)throw Error('原上传收据暂未查到，保持原标识；未自动重新上传。请管理员核对后恢复。');writePending(null);await loadResources(operation.sessionId);return catalog;}const result=await client[operation.method](...operation.args,operation.body,operation.key);guard(ctx,{session:!!operation.sessionId});writePending(null);if(operation.sessionId)await loadResources(operation.sessionId);else await list();return result;},{success:'已使用原标识核对操作；没有建立新的确认。'});},
    destroy(){epoch++;sessionEpoch++;availabilitySequence++;availabilityRequest=null;client.reset();listeners.clear();},
  };
}
