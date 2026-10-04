import {validateProject} from './store.js';

export const LOCAL_STORIES_KEY='yingxu-project-library-v1';
export function readLocalStoryLibrary(storage){
  let raw='';
  try{
    if(storage===undefined)storage=globalThis.localStorage;
    raw=storage?.getItem(LOCAL_STORIES_KEY)||'[]';
    const projects=JSON.parse(raw);
    if(!Array.isArray(projects)||projects.some(p=>!p||typeof p.id!=='string'||typeof p.title!=='string'||!Array.isArray(p.entities)))throw Error('invalid library');
    return {raw,projects,error:false};
  }catch{return {raw,projects:[],error:true};}
}

export function archiveLocalStory(store,storage=globalThis.localStorage){
  if(store.getState().workspace.mode!=='local')throw Error('请先返回本机作品，再操作本机故事列表。');
  const saved=readLocalStoryLibrary(storage);
  if(saved.error)throw Error('本机故事列表无法读取，请先下载列表原始数据和当前作品备份。');
  const project=store.exportProject();
  storage.setItem(LOCAL_STORIES_KEY,JSON.stringify([project,...saved.projects.filter(p=>p.id!==project.id)]));
}

// Preserve the current local work before any replacement; never copy cloud
// project bodies into the browser's shared local-project library.
export function openLocalStory(id,{store,cloud,storage=globalThis.localStorage}){
  if(cloud.getState().busy)throw Error('上一项云操作还在处理中，请稍后切换故事。');
  const original=store.getLocalProjectSummary();
  let target;
  if(id!==original?.id){
    const saved=readLocalStoryLibrary(storage);
    if(saved.error)throw Error('本机故事列表无法读取，请先下载备份。');
    target=saved.projects.find(p=>p.id===id);
    if(!target)throw Error('这个本机故事已不在列表中，请刷新故事列表。');
    const checked=validateProject(target);
    if(!checked.ok)throw Error(checked.error);
    target=checked.project;
  }
  if(store.getState().workspace.mode==='cloud')cloud.leave();
  if(store.getState().workspace.mode!=='local')throw Error('原本机作品暂时无法恢复，请先备份当前作品。');
  if(target){archiveLocalStory(store,storage);if(store.replaceProject(target)===false)throw Error(store.getState().notice);}
  store.setSection('journey');store.select(null);store.setScope(null);
  return true;
}

export function createLocalStory(title,{store,cloud,storage=globalThis.localStorage}){
  const name=title.trim();if(!name)throw Error('请填写故事名称。');
  if(cloud.getState().busy)throw Error('上一项云操作还在处理中，请稍后新建故事。');
  if(store.getState().workspace.mode==='cloud')cloud.leave();
  if(store.getState().workspace.mode!=='local')throw Error('原本机作品暂时无法恢复，请先备份当前作品。');
  archiveLocalStory(store,storage);
  if(store.newProject(name)===false)throw Error(store.getState().notice);
  store.setSection('journey');store.setMode('guide');
  return true;
}
