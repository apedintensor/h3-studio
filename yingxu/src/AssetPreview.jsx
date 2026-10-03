import React,{useEffect,useState} from 'react';
import {getFile} from './media.js';
export default function AssetPreview({entity,alt,muted=false}){
 const [url,setUrl]=useState(''),[error,setError]=useState('');
 useEffect(()=>{let active=true,objectUrl;setUrl('');setError('');if(!entity?.data.fileId){setError('素材文件缺失，请补传。');return;}getFile(entity.data.fileId).then(blob=>{if(!active)return;if(!blob){setError('素材文件缺失，请补传。');return;}objectUrl=URL.createObjectURL(blob);setUrl(objectUrl)}).catch(()=>active&&setError('暂时无法读取文件，请重新打开或补传。'));return()=>{active=false;if(objectUrl)URL.revokeObjectURL(objectUrl)}},[entity?.data.fileId]);
 if(error)return <p role="alert">{error}</p>;if(!url)return <p>读取素材中…</p>;
 return entity.type==='video'?<video src={url} controls muted={muted} preload="metadata" aria-label={alt||entity.title}/>:entity.type==='audio'?<audio src={url} controls preload="metadata" aria-label={alt||entity.title}/>:<img src={url} alt={alt||entity.title}/>;
}
