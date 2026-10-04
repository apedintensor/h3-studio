import {boundVideoRange} from './video-cut-model.js';
import {ancestors,validateProject} from './store.js';
import {effectiveLook} from './character-model.js';

const clone=value=>structuredClone(value);
export const jobLabels={planned:'等待批次确认',waiting_capacity:'等待计算容量',queued:'等待执行',claimed:'已分配',submitting:'正在提交',running:'生成中',collecting:'保存与核验结果',checking:'检查成片',succeeded:'已完成',failed:'失败',cancel_requested:'正在确认取消',cancelled:'已取消',blocked:'等待条件满足',submission_unknown:'提交结果待核对',recovery_hold:'恢复后待核对'};
export const activeJobStatuses=new Set(['waiting_capacity','queued','claimed','submitting','running','collecting','checking','cancel_requested']);
export function recoveryMessage(job){return job?.error_code==='cancellation_requires_reconciliation'?'已记录取消意愿，但原提交结果仍需管理员核对。原任务和费用记录保留，请勿重复提交。':'原任务等待管理员核对执行与费用；作品仍可编辑，请勿重复提交。';}
export function jobMessage(job){if(job?.status==='recovery_hold')return recoveryMessage(job);if(job?.error_code==='render_cache_capacity_exhausted')return '粗剪服务的工作空间已满，本次已停止，尚未开始编码。素材保持不变；请等管理员处理容量后，再预检并确认新的粗剪任务。';return job?.message||job?.error?.message||job?.error_code||'';}
export function estimateLabel(estimate){if(!estimate||estimate.cost_microusd==null)return '费用尚未报价，不能据此认为免费。';if(estimate.source==='mock'||estimate.kind==='simulation')return '模拟测试不调用模型，费用为 $0；真实推理价格尚未验证。';if(estimate.kind==='included')return '此项粗剪当前不单独计费；服务器资源仍有运行成本。';if(estimate.kind==='budget_reservation')return `预算预留 $${(estimate.cost_microusd/1000000).toFixed(4)} ${estimate.currency||'USD'}；这是运营配置的额度占用，实际费用待核对，不是最终账单。`;if(typeof estimate.cost_microusd==='number')return `预计 $${(estimate.cost_microusd/1000000).toFixed(4)} ${estimate.currency||'USD'}；以实际执行结算为准。`;return '费用信息暂不可用。';}
export function stableJSON(value){if(Array.isArray(value))return '['+value.map(stableJSON).join(',')+']';if(value&&typeof value==='object')return '{'+Object.keys(value).sort().map(key=>JSON.stringify(key)+':'+stableJSON(value[key])).join(',')+'}';return JSON.stringify(value);}
export async function digest(value){const bytes=new TextEncoder().encode(stableJSON(value));return [...new Uint8Array(await crypto.subtle.digest('SHA-256',bytes))].map(n=>n.toString(16).padStart(2,'0')).join('');}
export function blankCloudProject(title){return {schemaVersion:4,id:'project-'+crypto.randomUUID(),title:title.trim(),logline:'',entities:[],links:[],layout:{positions:{},viewport:{x:0,y:0,zoom:1}},jobs:[],updatedAt:new Date().toISOString(),journey:{stage:1}};}
export function freestyleProject(title){const p=blankCloudProject(title),chapter='chapter-'+crypto.randomUUID(),scene='scene-'+crypto.randomUUID(),shot='shot-'+crypto.randomUUID();p.entities=[{id:chapter,type:'chapter',title:'快速创作',parentId:null,data:{}},{id:scene,type:'scene',title:'单镜场景',parentId:chapter,data:{script:''}},{id:shot,type:'shot',title:'我的视频',parentId:scene,data:{seconds:5,prompt:''}}].map(e=>({...e,description:'',order:0,version:1,status:'draft'}));p.journey={workspace:'freestyle',stage:4,reviewShotId:shot};return p;}
export function removeFreestyleInput(project,shotId,assetId,{guide=false,roles=[]}={}){const shot=project.entities.find(e=>e.id===shotId);if(!shot)return false;let changed=false;if(guide){const before=shot.data.h3?.guides||[],after=before.filter(g=>g.media_id!==assetId);changed=before.length!==after.length;shot.data.h3={...shot.data.h3,guides:after};}else{const removed=project.links.filter(l=>l.target===shotId&&l.source===assetId&&roles.includes(l.role));changed=removed.length>0;project.links=project.links.filter(l=>!removed.includes(l));for(const link of removed)if(shot.data.referenceRanges)delete shot.data.referenceRanges[link.id];}if(!changed)return false;shot.status='review';shot.version++;return true;}
export function copyForCloud(project){const p=clone(project);p.id='project-'+crypto.randomUUID();p.jobs=[];p.updatedAt=new Date().toISOString();for(const e of p.entities){const asset=project.entities.find(a=>a.id===e.data.selectedAssetId);if(boundVideoRange(e.data.selectedVideoRange,asset)){e.data.selectedVideoRange.cloudAssetId=null;e.data.selectedVideoRange.cloudArtifactId=null;}}for(const e of p.entities){delete e.data.cloudAssetId;delete e.data.cloudArtifactId;delete e.data.cloudContentPath;}for(const tracks of Object.values(p.journey?.soundTracks||{}))if(Array.isArray(tracks))for(const track of tracks)if(track.generatedFrom)track.needsReview=true;return p;}
export function checkedProjectRecord(record){const project=record?.project,checked=validateProject(project);if(!checked.ok||!Number.isInteger(record.version)||record.version<1||record.id!==project.id)throw Error(checked.error||'云项目响应无效，未替换当前作品。');return {...record,project:checked.project};}

export function referenceSpecs(project,shotId){
  const shot=project.entities.find(e=>e.id===shotId);if(!shot||!['shot','generation'].includes(shot.type))return {references:[],issues:['镜头不存在。']};
  const find=id=>project.entities.find(e=>e.id===id),references=[],issues=[];
  const add=(asset,purpose,range=null,linkId=null)=>{
    if(!asset||!['image','video','audio'].includes(asset.type)){issues.push('参考必须指向实际的图片、视频或音频素材。');return;}
    if(!asset.data.fileId||asset.data.missingFile){issues.push(`「${asset.title}」缺少文件，请补传。`);return;}
    if(range&&(range.fileId!==asset.data.fileId||!Number.isFinite(range.start)||!Number.isFinite(range.end)||range.start<0||range.end<=range.start)){issues.push(`「${asset.title}」的选段已过期或无效，请重新确认。`);return;}
    references.push({entity:asset,purpose,range,linkId,key:[asset.id,asset.data.fileId,purpose,range?.start??'',range?.end??''].join('|')});
  };
  for(const link of project.links.filter(l=>l.target===shotId)){
    const asset=find(link.source);
    if(['image','video','audio'].includes(asset?.type))add(asset,link.role,shot.data.referenceRanges?.[link.id],link.id);
    else if(link.role==='dependency')issues.push(`前置步骤「${asset?.title||'未知'}」尚未被明确选为实际参考素材。`);
  }
  const scene=find(shot.parentId),characters=new Set([...(scene?.data.cast||[]),...(shot.data.cast||[])].map(c=>c.characterId));
  for(const link of project.links.filter(l=>l.target===shotId&&l.role==='identity'&&find(l.source)?.type==='character'))characters.add(link.source);
  for(const id of characters){const character=find(id),binding=effectiveLook(project,shot,id),look=character?.data.looks?.find(l=>l.id===binding?.lookId);
    if(!look){issues.push(`角色「${character?.title||'未知'}」尚未选择可用造型和参考图。`);continue;}
    const gallery=Object.values(look.gallery||{}).filter(Boolean);if(!gallery.length)issues.push(`造型「${look.name}」尚未放入参考图。`);
    for(const assetId of gallery){const asset=find(assetId);if(!references.some(r=>r.entity.id===assetId))add(asset,'identity');}
  }
  return {references,issues:[...new Set(issues)]};
}
export function shotSnapshot(project,shotId){
  const shot=project.entities.find(e=>e.id===shotId);if(!shot)return null;
  const scene=project.entities.find(e=>e.id===shot.parentId),{references}=referenceSpecs(project,shotId);
  return {id:shot.id,version:shot.version,description:shot.description,prompt:shot.data.prompt||'',seconds:shot.data.seconds??5,h3:shot.data.h3||{},cast:shot.data.cast||[],scene:{id:scene?.id||null,script:scene?.data.script||'',cast:scene?.data.cast||[]},references:references.map(r=>({key:r.key,assetId:r.entity.id,fileId:r.entity.data.fileId,cloudAssetId:r.entity.data.cloudAssetId||null,version:r.entity.version})),guides:(shot.data.h3?.guides||[]).map(g=>{const e=project.entities.find(e=>e.id===g.media_id);return {id:g.media_id,fileId:e?.data.fileId||null,cloudAssetId:e?.data.cloudAssetId||null,version:e?.version||null};}),links:project.links.filter(l=>l.target===shot.id)};
}
export function controlsForRecipe(recipe,saved={},seconds=5){
  const result={};for(const [field,schema]of Object.entries(recipe?.controls||{})){
    if(schema.available===false||['guides','video_audio'].includes(field))continue;
    if(Object.hasOwn(saved,field)&&saved[field]!==''&&saved[field]!==null)result[field]=saved[field];
    else if(field==='duration')result[field]=seconds;
    else if(Object.hasOwn(schema,'default'))result[field]=schema.default;
  }return result;
}
export function validateControlValues(recipe,controls){
  const issues=[];for(const [field,value]of Object.entries(controls)){
    const schema=recipe?.controls?.[field];if(!schema){issues.push(`当前配方不支持 ${field}。`);continue;}
    if(schema.enum&&!schema.enum.includes(value)){issues.push(`${schema.title||field} 不在支持的选项内。`);continue;}
    if(value===null&&Array.isArray(schema.type)&&schema.type.includes('null'))continue;
    if(field==='seed'){if(typeof value!=='string'||!/^\d{1,20}$/.test(value)||BigInt(value)>18446744073709551615n)issues.push('种子须为 uint64 十进制字符串。');continue;}
    if(schema.type==='boolean'&&typeof value!=='boolean')issues.push(`${schema.title||field} 必须为开关值。`);
    if(['integer','number'].includes(schema.type)&&(typeof value!=='number'||!Number.isFinite(value)||schema.type==='integer'&&!Number.isInteger(value)||schema.minimum!==undefined&&value<schema.minimum||schema.maximum!==undefined&&value>schema.maximum))issues.push(`${schema.title||field} 超出支持范围。`);
  }return issues;
}
export function buildPlanPayload(project,shotId,{recipe,capabilitiesVersion,assetMap={},sourceHash}={}){
  const shot=project.entities.find(e=>e.id===shotId);if(!shot)throw Error('镜头已被删除。');
  if(!recipe)throw Error('先选择有明确能力说明的配方。');
  const {references,issues}=referenceSpecs(project,shotId),controls=controlsForRecipe(recipe,shot.data.h3?.controls,shot.data.seconds);
  issues.push(...validateControlValues(recipe,controls));
  const prompt=String(shot.data.prompt??shot.description??'').trim();if(!prompt)issues.push('先写清这个镜头的画面与动作。');
  const inputs={images:[],videos:[],audios:[],guides:[]},seen=new Set();
  for(const ref of references){
    const assetId=assetMap[ref.key]||(!ref.range?ref.entity.data.cloudAssetId:null);
    if(!assetId){issues.push(`「${ref.entity.title}」还未同步或生成实际选段。`);continue;}
    const kind=ref.entity.type,role=ref.purpose;
    if(['firstFrame','lastFrame'].includes(role)){
      const key=role==='firstFrame'?'first_frame':'last_frame';if(inputs[key])issues.push('同一个镜头只能选择一张首帧和一张尾帧。');inputs[key]={asset_id:assetId};continue;
    }
    if(seen.has(assetId))continue;seen.add(assetId);
    const entry={asset_id:assetId,purpose:role};if(kind==='video')entry.include_audio=shot.data.h3?.video_audio?.[ref.entity.id]!==false;
    inputs[{image:'images',video:'videos',audio:'audios'}[kind]].push(entry);
  }
  for(const [guideIndex,guide] of (shot.data.h3?.guides||[]).entries()){const entity=project.entities.find(e=>e.id===guide.media_id),assetId=assetMap['guide:'+guideIndex]||(!guide.source_range?entity?.data.cloudAssetId:null);if(!assetId){issues.push('有时间锚点的素材还未同步。');continue;}inputs.guides.push({media_id:assetId,time_seconds:guide.time_seconds,use_audio:!!guide.use_audio});}
  if(recipe.mode==='fl'&&(inputs.images.length||inputs.videos.length||inputs.audios.length))issues.push('首尾帧配方不能混用全能参考；请切换配方或解除这些关联。');
  if(recipe.mode==='ref'&&(inputs.first_frame||inputs.last_frame))issues.push('全能参考配方不接受首尾帧约束；请切换配方或更改参考用途。');
  if(recipe.mode==='ref'&&!inputs.images.length&&!inputs.videos.length&&!inputs.audios.length)issues.push('全能参考至少需要一份参考素材；纯文字请选择文生/首尾帧配方。');
  if(issues.length)throw Error([...new Set(issues)].join(' '));
  const parents=ancestors(project,shot.id);
  return {client_ref:{project_id:project.id,chapter_id:parents.find(e=>e.type==='chapter')?.id||null,scene_id:parents.find(e=>e.type==='scene')?.id||null,shot_id:shot.id,shot_version:shot.version,...(sourceHash?{source_hash:sourceHash}:{})},recipe_id:recipe.id,capabilities_version:capabilitiesVersion,prompt,inputs,controls,client_edit:{edit_duration_s:shot.data.seconds}};
}

export function mergeJobCandidate(project,job,{snapshotHash,now=new Date().toISOString()}={}){
  if(!job?.id||job.client_ref?.project_id!==project.id)return {changed:false,reason:'foreign-project'};
  const before=project.jobs.find(j=>j.id===job.id),{clientImportedAudioIds:importedAudio=[],...previous}=before||{},same=!!before&&stableJSON(previous)===stableJSON(job),audioSeen=new Set(importedAudio);
  project.jobs=[...project.jobs.filter(j=>j.id!==job.id),clone(job)].slice(-10000);
  if(job.recipe_id==='chapter-roughcut-v1')return {changed:!same,added:[],reason:'chapter-deliverable'};
  if(job.status!=='succeeded')return {changed:!same,added:[]};
  const shot=project.entities.find(e=>e.id===job.client_ref.shot_id),old=!!(shot&&(shot.version!==job.client_ref.shot_version||job.client_ref.source_hash&&snapshotHash&&job.client_ref.source_hash!==snapshotHash)),added=[];
  for(const artifact of job.artifacts||[]){const isAudio=artifact.kind==='audio'&&['audio/flac','audio/x-flac'].includes(artifact.mime||artifact.content_type||artifact.metadata?.mime);if(!artifact.id||!['video','image'].includes(artifact.kind)&&!isAudio)continue;const id='result-'+artifact.id;
    if(isAudio&&audioSeen.has(artifact.id))continue;
    if(same&&!isAudio)continue;
    if(isAudio)audioSeen.add(artifact.id);
    if(project.entities.some(e=>e.id===id))continue;
    project.entities.push({id,type:artifact.kind,title:`${(shot?.title||'历史镜头').slice(0,120)} · ${job.simulation?'模拟 ':''}${old?'旧版 ':''}v${job.client_ref.shot_version} ${isAudio?'独立声音':'候选'}`,description:job.simulation?'模拟任务产物，不代表 H3 真实生成；未自动采用。':'实际任务结果；未自动采用。',parentId:null,order:project.entities.filter(e=>e.parentId===null).length,version:1,status:'draft',data:{fileId:'cloud_artifact_'+artifact.id,cloudArtifactId:artifact.id,cloudContentPath:artifact.content_url||null,fileName:artifact.filename||`${artifact.id}.${artifact.kind==='image'?'png':isAudio?'flac':'mp4'}`,mime:artifact.mime||artifact.content_type||(artifact.kind==='image'?'image/png':isAudio?'audio/flac':'video/mp4'),bytes:artifact.size_bytes||artifact.size||0,metadata:artifact.metadata||{},simulation:!!job.simulation,source:'generation',sourceJobId:job.id,sourceShotId:job.client_ref.shot_id,sourceShotVersion:job.client_ref.shot_version,sourceHash:job.client_ref.source_hash||null,oldVersion:old,missingFile:false,createdAt:now}});
    added.push(id);
    if(shot&&!isAudio){shot.data.candidateIds=[...new Set([...(shot.data.candidateIds||[]),id])];if(old)shot.status='review';}
  }
  const markerChanged=audioSeen.size!==importedAudio.length;if(audioSeen.size)project.jobs.find(j=>j.id===job.id).clientImportedAudioIds=[...audioSeen];
  return {changed:!same||added.length>0||markerChanged,added,oldVersion:old,...(same&&!added.length&&!markerChanged?{reason:'unchanged'}:{})};
}
