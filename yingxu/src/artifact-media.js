import {safeCloudPath} from './cloud-client.js';

// Keep only authenticated application URLs. Never persist a redirected storage URL.
export function artifactMedia(artifact,job,{title='章节'}={}){
  if(!artifact||typeof artifact.id!=='string'||! /^[A-Za-z0-9_-]+$/.test(artifact.id))return null;
  const mime=artifact.mime||artifact.metadata?.mime||artifact.metadata?.content_type;
  const type=artifact.kind==='video'&&mime==='video/mp4'?'video':artifact.kind==='audio'&&['audio/flac','audio/x-flac'].includes(mime)?'audio':null;
  if(!type)return null;
  const expected=`/v1/artifacts/${artifact.id}/content`;
  try{if(safeCloudPath(artifact.content_url)!==expected)return null;}catch{return null;}
  const extension=type==='audio'?'flac':'mp4',label=type==='audio'?'独立混音 FLAC':'成片 MP4';
  const filename=`${title}-${job?.simulation?'模拟来源-':''}粗剪-${String(job?.id||'结果').slice(0,8)}.${extension}`.replace(/[\\/:*?"<>|\x00-\x1f]/g,'_');
  return {id:artifact.id,type,label,mime,filename,previewURL:expected,downloadURL:expected+'?download=1',duration:artifact.metadata?.duration};
}
