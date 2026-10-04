import test from 'node:test';
import assert from 'node:assert/strict';
import {createStore} from './store.js';
import {blankCloudProject} from './cloud-model.js';
import {LOCAL_STORIES_KEY,archiveLocalStory,createLocalStory,openLocalStory,readLocalStoryLibrary} from './story-navigation.js';

function fixture(){const data=new Map();let fail=false;const storage={getItem:key=>data.get(key)||null,setItem(key,value){if(fail)throw Error('storage full');data.set(key,value);}},store=createStore({storage});store.newProject('故事 A');const cloud={getState:()=>({busy:false}),leave:()=>store.leaveCloudProject()};return {store,cloud,storage,data,setFail:value=>fail=value};}

test('sidebar local creation archives the old story and switches between independent stories',()=>{
  const h=fixture(),a=h.store.getState().project.id;h.store.addEntity('chapter',null,{title:'A 第一章'});
  createLocalStory('故事 B',h);const b=h.store.getState().project.id;assert.notEqual(a,b);assert.equal(h.store.getState().project.entities.length,0);
  h.store.addEntity('chapter',null,{title:'B 第一章'});openLocalStory(a,h);assert.equal(h.store.getState().project.entities[0].title,'A 第一章');
  openLocalStory(b,h);assert.equal(h.store.getState().project.entities[0].title,'B 第一章');assert.equal(readLocalStoryLibrary(h.storage).projects.length,2);
});

test('return from a dirty cloud story restores the original local story and keeps cloud draft private',()=>{
  const h=fixture(),local=h.store.exportProject();archiveLocalStory(h.store,h.storage);
  const remote=blankCloudProject('云故事');h.store.enterCloudProject(remote,{account:'supervan',version:1});h.store.updateProject({logline:'未同步的修改'});
  assert.deepEqual(h.store.getLocalProjectSummary(),{id:local.id,title:local.title});openLocalStory(local.id,h);
  assert.deepEqual(h.store.exportProject(),local);assert.equal(h.store.getCloudDraft('supervan',remote.id).project.logline,'未同步的修改');assert.equal(readLocalStoryLibrary(h.storage).projects.some(p=>p.id===remote.id),false);
});

test('failed local archive prevents replacing the current story',()=>{
  const h=fixture(),before=h.store.exportProject();h.setFail(true);
  assert.throws(()=>createLocalStory('不会覆盖',h),/storage full/);assert.deepEqual(h.store.exportProject(),before);
});

test('corrupt local story is rejected before leaving a cloud story',()=>{
  const h=fixture(),remote=blankCloudProject('云故事');h.store.enterCloudProject(remote,{account:'supervan',version:1});
  h.data.set(LOCAL_STORIES_KEY,JSON.stringify([{id:'broken',title:'损坏故事',entities:[]}]))
  assert.throws(()=>openLocalStory('broken',h));assert.equal(h.store.getState().workspace.mode,'cloud');assert.equal(h.store.getState().project.id,remote.id);
});
