import test from 'node:test';
import assert from 'node:assert/strict';
import {artifactMedia} from './artifact-media.js';

test('video and FLAC get separate private preview/download routes and correct names',()=>{
  const job={id:'12345678-aaaa',simulation:true};
  for(const [kind,mime,extension] of [['video','video/mp4','mp4'],['audio','audio/flac','flac']]){
    const value=artifactMedia({id:kind,kind,mime,content_url:`/v1/artifacts/${kind}/content`,metadata:{duration:12}},job,{title:'雨夜/信箱'});
    assert.equal(value.type,kind);assert.equal(value.previewURL,`/v1/artifacts/${kind}/content`);assert.equal(value.downloadURL,`/v1/artifacts/${kind}/content?download=1`);
    assert.equal(value.filename,`雨夜_信箱-模拟来源-粗剪-12345678.${extension}`);assert.equal(value.duration,12);
  }
});
test('artifact controls refuse external, signed, mismatched or unsupported resource links',()=>{
  const good={id:'a',kind:'audio',mime:'audio/flac',content_url:'/v1/artifacts/a/content'};
  for(const patch of [{content_url:'https://store.invalid/file?signature=hidden'},{content_url:'/v1/artifacts/b/content'},{content_url:'/v1/artifacts/a/content?token=hidden'},{content_url:'/v1/assets/a/content'},{id:'../a'},{mime:'application/octet-stream'},{kind:'image'}])assert.equal(artifactMedia({...good,...patch},{}),null);
});
