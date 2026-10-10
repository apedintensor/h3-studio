const seconds=value=>typeof value==='number'&&Number.isFinite(value)?value:null;
export const dstackProvider=value=>({vast:'Vast.ai',vastai:'Vast.ai',runpod:'RunPod'}[value]||value||'尚未确认');
export const dstackMode=value=>({fl:'FL2VA 首尾帧',ref:'Ref2VA 全能参考'}[value]||'模式尚未确认');

export function dstackNodeStatus(node,now=Date.now()){
  const observed=seconds(node.observed_at),deadline=seconds(node.hard_deadline);
  if(node.state==='stopped')return {label:node.billing_state==='no_charge_confirmed'?'已结束 · 未产生费用':'运行任务已结束 · 删除与费用待核对',tone:node.billing_state==='no_charge_confirmed'?'neutral':'warn',group:node.billing_state==='no_charge_confirmed'?'history':'review'};
  if(node.desired_state==='stopped')return {label:'排空与停止中',tone:'neutral',group:'current'};
  if(!node.observation_fresh||observed===null||observed*1000>now||now-observed*1000>30000||node.state?.includes('unknown'))return {label:'状态待核对',tone:'warn',group:'review'};
  if(deadline===null||deadline*1000<=now)return {label:'期限已到 · 等待停止核对',tone:'warn',group:'review'};
  if(node.ready===true&&node.state==='ready')return {label:'运行接口就绪',tone:'good',group:'current'};
  if(node.state==='ready')return {label:'等待业务执行槽与准入',tone:'neutral',group:'current'};
  return {label:({reserved:'启动请求已保存',creating:'正在申请机器',starting:'正在准备机器',runtime_unconfirmed:'等待运行接口验收',busy:'正在处理原任务',draining:'正在排空',stopping:'停止中'}[node.state]||'等待状态核对'),tone:'neutral',group:'current'};
}

export function dstackGroups(nodes=[],now=Date.now()){
  const groups={current:[],review:[],history:[]};
  for(const node of nodes)groups[dstackNodeStatus(node,now).group].push(node);
  return groups;
}

export function dstackPreviewCurrent(preview,profileId,ttlSeconds,now=Date.now()){
  return !!preview&&preview.selection?.profile_id===profileId&&preview.selection?.ttl_seconds===ttlSeconds&&
    typeof preview.preview_id==='string'&&preview.capacity_backend==='dstack-v1'&&
    seconds(preview.expires_at)!==null&&preview.expires_at*1000>now&&
    seconds(preview.hard_deadline)!==null&&preview.hard_deadline*1000>now&&
    Number.isSafeInteger(preview.reservation_microusd)&&preview.reservation_microusd>=0&&
    Number.isSafeInteger(preview.hourly_cost_microusd)&&preview.hourly_cost_microusd>0;
}
