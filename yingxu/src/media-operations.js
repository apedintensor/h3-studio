import {validateLink} from './store.js';
import {resolveCanvasPositions} from './canvas-layout.js';
import {editLook,gallerySlots} from './character-model.js';
import {captureWorkspace,assertWorkspace} from './workspace-context.js';

const mediaIdentity=entity=>JSON.stringify([entity?.type,entity?.data.fileId??null,entity?.data.cloudAssetId??null,entity?.data.cloudArtifactId??null]);
const find=(store,id)=>store.getState().project.entities.find(e=>e.id===id);
const requireCommit=(store,ok)=>{if(!ok)throw Error(store.getState().notice||'这次素材关联未保存，请核对当前作品后重试。');};
async function guardedUpload(store,context,unchanged,message,upload){
 // Latch a relevant change, even if the user later undoes it back to the old
 // ID. This subscription lasts only for this upload; no entity stays locked.
 let changed=false;const unsubscribe=store.subscribe(()=>{if(!unchanged())changed=true;});
 try{const result=await upload();assertWorkspace(store,context);if(changed||!unchanged())throw Error(message);return result;}finally{unsubscribe();}
}

export async function replaceEntityMedia({store,entityId,file,upload}){
 const context=captureWorkspace(store),target=find(store,entityId);
 if(!target||!['image','video','audio'].includes(target.type))throw Error('原素材已删除，请重新选择。');
 const identity=mediaIdentity(target),result=await guardedUpload(store,context,()=>{const live=find(store,entityId);return !!live&&identity===mediaIdentity(live);},'上传期间这份素材已被替换或删除；保留你刚才的选择，请重新选择文件。',()=>upload(file,target.type));
 // Merge only newly uploaded media fields. Keep description, notes and any
 // unrelated metadata edited while the file was being transferred.
 requireCommit(store,store.updateEntity(entityId,{data:{cloudAssetId:null,cloudArtifactId:null,cloudContentPath:null,sourceJobId:null,sourceShotId:null,sourceShotVersion:null,sourceHash:null,oldVersion:false,simulation:false,metadata:{},...result.data}}));return result;
}

export async function uploadLookImage({store,characterId,lookId,slot,file,upload}){
 const context=captureWorkspace(store),character=find(store,characterId),look=character?.data.looks?.find(l=>l.id===lookId);
 if(!look||!Object.hasOwn(gallerySlots,slot))throw Error('造型已删除，请重新选择后上传。');
 const previous=look.gallery?.[slot]||'',result=await guardedUpload(store,context,()=>{const live=find(store,characterId)?.data.looks?.find(l=>l.id===lookId);return !!live&&(live.gallery?.[slot]||'')===previous;},'上传期间这张造型参考已另选或删除；保留你的新选择，没有覆盖。',()=>upload(file,'image'));
 const owner=find(store,characterId),current=owner?.data.looks?.find(l=>l.id===lookId);
 if(!current)throw Error('造型已删除，请重新选择后上传。');
 if((current.gallery?.[slot]||'')!==previous)throw Error('上传期间这张造型参考已另选；保留你的新选择，没有覆盖。');
 const imageId=`image-${crypto.randomUUID()}`;
 requireCommit(store,store.editProject(p=>{
  p.entities.push({id:imageId,type:'image',title:`${owner.title} · ${current.name} · ${gallerySlots[slot]}`.slice(0,160),description:'人物造型参考',parentId:null,order:Math.max(-1,...p.entities.filter(e=>e.parentId===null).map(e=>e.order))+1,version:1,status:'draft',data:result.data});
  p.layout.positions[imageId]=resolveCanvasPositions(p.entities,p.layout.positions)[imageId];
  const edited=editLook(p,characterId,lookId,{gallery:{[slot]:imageId}});if(!edited.ok)throw Error(edited.error);
 }));return imageId;
}

export const freestyleRoles=(kind,role)=>kind==='image'&&role==='reference'?['reference','identity']:kind==='video'?['motion','reference']:kind==='audio'?['audio','reference']:[role];
export function freestyleInputIds(project,shotId,{kind,role,guide=false}){const shot=project.entities.find(e=>e.id===shotId);return guide?(shot?.data.h3?.guides||[]).map(g=>g.media_id):[...new Set(project.links.filter(l=>l.target===shotId&&freestyleRoles(kind,role).includes(l.role)&&project.entities.some(e=>e.id===l.source&&e.type===kind)).map(l=>l.source))];}

export async function uploadFreestyleFiles({store,shotId,files,kind,role,guide=false,maximum,upload}){
 const context=captureWorkspace(store),shot=find(store,shotId),recipe=shot?.data.h3?.recipeId??null;
 if(context.mode!=='cloud'||shot?.type!=='shot')throw Error('请先打开要添加参考的云镜头。');
 const check=count=>{assertWorkspace(store,context);const s=find(store,shotId);if(s?.type!=='shot'||(s.data.h3?.recipeId??null)!==recipe)throw Error('镜头或生成方式已改变；没有把迟到的素材接入新设置。');if(!Number.isInteger(maximum)||maximum<1||freestyleInputIds(store.getState().project,shotId,{kind,role,guide}).length+count>maximum)throw Error(`此区域最多 ${maximum} 份，请先移除一份关联再替换。原文件会保留。`);};
 check(files.length);
 for(const file of files){
  check(1);const result=await guardedUpload(store,context,()=>{const live=find(store,shotId);return live?.type==='shot'&&(live.data.h3?.recipeId??null)===recipe;},'镜头或生成方式已改变；没有把迟到的素材接入新设置。',()=>upload(file,kind||undefined));check(1);
  const id=`${result.type}-${crypto.randomUUID()}`;
  requireCommit(store,store.editProject(p=>{
   const live=p.entities.find(e=>e.id===shotId);
   p.entities.push({id,type:result.type,title:file.name.slice(0,160),description:'',parentId:null,order:Math.max(-1,...p.entities.filter(e=>e.parentId===null).map(e=>e.order))+1,version:1,status:'draft',data:result.data});
   p.layout.positions[id]=resolveCanvasPositions(p.entities,p.layout.positions)[id];
   if(guide)live.data.h3={...live.data.h3,guides:[...(live.data.h3?.guides||[]),{media_id:id,time_seconds:0,use_audio:result.type==='audio'}]};
   else{const checked=validateLink(p,id,shotId,role);if(!checked.ok)throw Error(checked.error);p.links.push({id:`link-${crypto.randomUUID()}`,source:id,target:shotId,role});}
   live.version++;live.status='review';
  }));
 }
}
