import {createCloudClient} from './cloud-client.js';

const ROOT='/v1/operator/capacity';
const id=value=>encodeURIComponent(value);

/** Cookie-authenticated operator projection. No provider credentials in the browser. */
export function createOperatorClient(options={}){
  const api=createCloudClient(options);
  return {...api,
    state:()=>api.request(ROOT+'/state'),
    catalog:()=>api.request(ROOT+'/catalog'),
    offers:({runtime_profile_id,mode,gpu_type,gpu_count})=>api.request(ROOT+'/offers?'+new URLSearchParams({runtime_profile_id,mode,gpu_type,gpu_count})),
    preview:selection=>api.request(ROOT+'/previews',{method:'POST',body:selection}),
    start:(body,key)=>api.request(ROOT+'/starts',{method:'POST',body,key}),
    drain:(nodeId,body,key)=>api.request(ROOT+`/nodes/${id(nodeId)}/drain`,{method:'POST',body,key}),
    stop:(nodeId,body,key)=>api.request(ROOT+`/nodes/${id(nodeId)}/stop`,{method:'POST',body,key}),
    updatePolicy:body=>api.request(ROOT+'/policy',{method:'PUT',body}),
  };
}
