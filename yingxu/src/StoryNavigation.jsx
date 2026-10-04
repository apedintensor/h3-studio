import React,{useEffect,useRef,useState} from 'react';
import {BookOpen,Check,ChevronDown,Cloud,HardDrive,Plus,RefreshCw,X} from 'lucide-react';
import {store} from './store.js';
import {cloudController as cloud} from './cloud-controller.js';
import {useCloud} from './CloudStudio.jsx';
import {createLocalStory,openLocalStory,readLocalStoryLibrary} from './story-navigation.js';
import './story-navigation.css';

export default function StoryNavigation({state,onOpenCloud,onOpenLocal}){
  const c=useCloud(),[expanded,setExpanded]=useState(()=>!window.matchMedia('(max-width:760px)').matches),[creating,setCreating]=useState(false),[title,setTitle]=useState(''),[source,setSource]=useState('local'),[query,setQuery]=useState(''),[error,setError]=useState(''),[pending,setPending]=useState(''),[library,setLibrary]=useState(()=>readLocalStoryLibrary()),inputRef=useRef();
  const refreshLocal=()=>setLibrary(readLocalStoryLibrary());
  useEffect(()=>{refreshLocal();},[state.project.id,state.project.title,state.workspace.mode]);
  useEffect(()=>{const refresh=e=>{if(!e.key||e.key==='yingxu-project-library-v1')refreshLocal();};window.addEventListener('storage',refresh);window.addEventListener('focus',refreshLocal);return()=>{window.removeEventListener('storage',refresh);window.removeEventListener('focus',refreshLocal);};},[]);
  useEffect(()=>{setQuery('');setError('');setCreating(false);setTitle('');},[c.account]);
  useEffect(()=>{if(creating)inputRef.current?.focus();},[creating]);
  const local=store.getLocalProjectSummary(),locals=[...(local?[local]:[]),...library.projects.filter(p=>p.id!==local?.id)];
  const cloudProjects=c.account?[...c.projects]:[];
  if(c.account&&state.workspace.mode==='cloud'){
    const index=cloudProjects.findIndex(p=>p.id===state.project.id),current={id:state.project.id,title:state.project.title};
    if(index<0)cloudProjects.unshift(current);else cloudProjects[index]={...cloudProjects[index],...current};
  }
  const matches=p=>p.title.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()),visibleCloud=cloudProjects.filter(matches),visibleLocal=locals.filter(matches),working=c.busy||!!pending;
  async function act(label,fn){setPending(label);setError('');try{return await fn();}catch(e){setError(e.message);return false;}finally{setPending('');}}
  const openCloud=id=>act('正在打开故事…',async()=>{const done=await cloud.open(id);if(!done)onOpenCloud();else if(window.matchMedia('(max-width:760px)').matches)setExpanded(false);return done;});
  const openLocal=id=>act('正在打开故事…',()=>{const done=openLocalStory(id,{store,cloud});if(done&&window.matchMedia('(max-width:760px)').matches)setExpanded(false);return done;});
  const begin=()=>{setTitle('');setSource(c.account?'cloud':'local');setError('');setCreating(true);setExpanded(true);};
  const create=e=>{e.preventDefault();void act('正在创建故事…',async()=>{
    const done=source==='cloud'?await cloud.create(title.trim()):createLocalStory(title,{store,cloud});
    refreshLocal();if(done){setCreating(false);setTitle('');setQuery('');store.notify('新故事已创建，原故事保留。');}else if(cloud.getState().draftOffer)onOpenCloud();return done;
  });};
  const item=(p,kind)=>{const active=state.workspace.mode===kind&&state.project.id===p.id,I=kind==='cloud'?Cloud:HardDrive;return <button key={kind+':'+p.id} type="button" className="story-nav-item" aria-current={active?'page':undefined} disabled={working||active} onClick={()=>kind==='cloud'?openCloud(p.id):openLocal(p.id)} title={p.title}><I size={14}/><span><b>{p.title}</b><small>{kind==='cloud'?'云故事':'此浏览器'}{active?' · 当前':''}</small></span>{active&&<Check size={13}/>}</button>;};
  return <section className="story-navigation" aria-label="我的故事"><div className="story-nav-heading"><button type="button" aria-expanded={expanded} aria-controls="sidebar-story-list" onClick={()=>setExpanded(!expanded)}><BookOpen size={15}/><b>我的故事</b><ChevronDown size={14} className={expanded?'expanded':''}/></button><button type="button" className="story-nav-refresh" disabled={working} aria-label="刷新故事列表" title="刷新故事列表" onClick={()=>act('正在刷新故事…',async()=>{refreshLocal();if(c.account)await cloud.list();})}><RefreshCw size={13}/></button></div>
    <button type="button" className="story-nav-create" disabled={working} onClick={begin}><Plus size={15}/>新建故事</button>
    {creating&&<form className="story-nav-form" onSubmit={create}><div><label htmlFor="new-sidebar-story-title">故事名称</label><button type="button" disabled={working} aria-label="取消新建故事" onClick={()=>setCreating(false)}><X size={14}/></button></div><input ref={inputRef} id="new-sidebar-story-title" required maxLength={80} placeholder="给新故事起个名字" value={title} disabled={working} onChange={e=>setTitle(e.target.value)}/><label htmlFor="new-sidebar-story-source">保存位置</label><select id="new-sidebar-story-source" value={source} disabled={working} onChange={e=>setSource(e.target.value)}>{c.account&&<option value="cloud">云端 · {c.account}</option>}<option value="local">仅此浏览器</option></select><small>{source==='cloud'?'从空白故事开始，已有故事分别保留。':'仅保存在此浏览器，记得下载完整备份。'}</small><button type="submit" disabled={working||!title.trim()}>创建故事</button></form>}
    <div id="sidebar-story-list" hidden={!expanded}><label className="story-nav-search"><span className="visually-hidden">搜索故事</span><input type="search" value={query} onChange={e=>setQuery(e.target.value)} placeholder="搜索故事"/></label><nav className="story-nav-list" aria-label="切换故事">{c.account&&<><p className="story-nav-group">云端 · {c.account}</p>{visibleCloud.map(p=>item(p,'cloud'))}{!c.projectsLoaded&&<p className="story-nav-note">故事列表尚未载入，可刷新重试。</p>}{c.projectsLoaded&&!cloudProjects.length&&<p className="story-nav-note">还没有云故事，点击上方新建。</p>}</>}<p className="story-nav-group">本机故事</p>{visibleLocal.map(p=>item(p,'local'))}{query&&!visibleCloud.length&&!visibleLocal.length&&<p className="story-nav-note">已载入的故事中没有匹配项。</p>}</nav>{c.moreProjects&&<button type="button" className="story-nav-more" disabled={working} onClick={()=>act('正在载入更多故事…',()=>cloud.loadMoreProjects())}>加载更多云故事</button>}{query&&c.moreProjects&&<p className="story-nav-note">搜索只包含已载入的故事。</p>}{library.error&&<p className="story-nav-error" role="alert">本机故事列表读取失败，原始数据未覆盖。<button type="button" onClick={onOpenLocal}>查看本机备份</button></p>}{!c.account&&<button type="button" className="story-nav-more" onClick={onOpenCloud}>登录，查看云故事</button>}</div>
    {state.workspace.mode==='cloud'&&state.workspace.dirty&&<p className="story-nav-note">{state.saveState.startsWith('保存失败')?'当前草稿仅暂存在本页面，请先下载备份，勿刷新。':'当前修改已留为本机草稿；切换后可恢复。'}</p>}{pending&&<p className="story-nav-note" role="status">{pending}</p>}{error&&<p className="story-nav-error" role="alert">{error}</p>}
  </section>;
}
