// Share links locate existing content; they never authorize a read or submit work.
const validId=value=>typeof value==='string'&&value.length>0&&value.length<=160&&!/[\x00-\x20\x7f\\]/.test(value);
export function readAgentLink(search){
  const params=new URLSearchParams(search);
  if(!params.has('project'))return null;
  if(params.getAll('project').length!==1||!validId(params.get('project')))return null;
  if(params.getAll('entity').length>1||params.has('entity')&&!validId(params.get('entity')))return null;
  return {projectId:params.get('project'),entityId:params.get('entity')||null,activity:params.get('panel')==='activity'};
}
export function locateEntity(project,id){
  const entity=project.entities.find(e=>e.id===id);
  if(!entity)return null;
  let node=entity,chapterId=null;const seen=new Set();
  while(node&&!seen.has(node.id)){seen.add(node.id);if(node.type==='chapter'){chapterId=node.id;break;}node=project.entities.find(e=>e.id===node.parentId);}
  const section=entity.type==='character'?'characters':entity.type==='location'?'locations':['image','audio','video'].includes(entity.type)?'assets':'story';
  return {entityId:entity.id,chapterId,section};
}
export function mergeActivity(previous,incoming){
  const events=new Map(previous.map(item=>[item.id,item]));
  for(const item of incoming)events.set(item.id,item);
  return [...events.values()].sort((a,b)=>b.project_version-a.project_version);
}
