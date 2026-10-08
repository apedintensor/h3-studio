export const epochMs=value=>typeof value==='number'&&Number.isFinite(value)?value*1000:null;
export function dollars(value,{digits=2}={}){return typeof value==='number'&&Number.isFinite(value)?'$'+(value/1e6).toFixed(digits):'尚未核对';}
export function age(value,now=Date.now()){
  const time=epochMs(value);if(time===null)return '尚未观测';
  const seconds=Math.max(0,Math.floor((now-time)/1000));
  return seconds<60?`${seconds} 秒前`:seconds<3600?`${Math.floor(seconds/60)} 分钟前`:`${Math.floor(seconds/3600)} 小时前`;
}
export function duration(value){
  if(typeof value!=='number'||!Number.isFinite(value)||value<0)return '暂无实测';
  return value<60?`${Math.round(value)} 秒`:`${Math.floor(value/60)} 分 ${Math.round(value%60)} 秒`;
}
export function deadlineLabel(value,now=Date.now()){
  const time=epochMs(value);if(time===null)return '期限未知';
  if(time<=now)return '期限已到，等待状态核对';
  return '剩余 '+duration((time-now)/1000);
}
export function nodeDeadline(node,now=Date.now()){
  const observed=epochMs(node.provider_lifetime_observed_at),safe=node.provider_safe_deadline,cap=node.hard_deadline;
  const verified=node.provider_lifetime_state==='verified'&&Number.isFinite(safe)&&observed!==null&&observed<=now&&now-observed<=30000;
  return {verified,label:verified?'安全停止期限':'停止上限 · 供应商待核对',deadline:verified?(Number.isFinite(cap)?Math.min(cap,safe):safe):(Number.isFinite(cap)?cap:null)};
}
const states={reserved:'已预留预算',creating:'正在申请机器',creation_unknown:'租赁结果待核对',waiting_provider:'等待供应商启动',awaiting_qualified_workers:'等待执行槽验收',bootstrap_unconfigured:'启动配置尚未就绪',provider_execution_unverified:'供应商执行尚未验收',observation_failed:'观测失败，待核对',ready:'可接单',running:'运行中',starting:'正在启动',provisioning:'正在租机',preparing:'正在准备',loading:'正在加载模型',loading_model:'正在加载模型',downloading:'正在下载权重',bootstrapping:'正在准备环境',checking:'正在检查',busy:'正在生成',idle:'空闲',draining:'正在排空',stopping:'正在停止',destroying:'正在销毁',stopped:'已停止',destroyed:'已销毁',disabled:'未启用',unconfigured:'未配置',unavailable:'暂不可用',unknown:'状态待核对',reconcile_required:'需要核对原操作',waiting:'等待核对',partial:'部分完成',pending:'已接受，等待处理',queued:'等待处理',accepted:'已接受',complete:'已完成',completed:'已完成',succeeded:'已完成',failed:'失败',blocked:'条件未满足',offline:'离线',healthy:'正常',stale:'状态已过期'};
export const stateLabel=value=>states[value]||'状态待核对';
export const stateTone=value=>['ready','healthy','complete','completed','succeeded'].includes(value)?'good':['failed','blocked','unknown','reconcile_required','stale','unavailable'].includes(value)?'warn':'neutral';
const reasons={bootstrap_reconciliation_required:'启动结果需要核对，尚不能确认已准备好。',operator_bootstrap_failed:'启动检查未通过，请先处理已记录的原因。',wangp_configuration_permissions:'运行配置文件权限不符合要求，需要修复后核对原启动。',runtime_process_exited:'模型服务进程提前退出，需要核对原启动。',runtime_readiness_timeout:'模型服务未在规定时间内就绪，需要核对原启动。',wangp_runtime_dependency_missing:'运行环境缺少依赖，需要修复后核对原启动。',UnclassifiedBootstrapFailure:'启动检查异常，具体原因尚未确认。',operator_inventory_stale:'库存信息尚未更新，请等待控制器检查后刷新。',provider_inventory_unavailable:'目前无法核对供应商库存，请稍后刷新。',provider_inventory_unconfirmed:'供应商库存尚未确认，请等待控制器核对。',operator_ttl_below_provider_minimum:'所选期限短于供应商允许的最短期限，请调整本次运行时间后重新预览。',operator_deployment_not_configured:'此配方、模式与硬件尚无匹配的部署配置。',operator_binding_selection_mismatch:'所选条件与已配置部署不匹配。',operator_deployment_not_qualified:'此部署尚未完成执行验收。',operator_inventory_not_configured:'当前服务尚未配置供应商库存查询。',operator_ttl_limit:'所选运行期限超过策略上限。',operator_instance_limit:'将超过机器数量上限。',operator_gpu_limit:'将超过物理 GPU 数量上限。',operator_hourly_cost_limit:'将超过每小时费用上限。',operator_unpriced_existing_capacity:'已有容量的费用尚未核对，先保留原记录。',operator_authority_expiring:'已授权租赁期限不足，请核对原授权。',operator_reservation_insufficient:'预算预留不足以覆盖本次期限。',operator_policy_changed:'启动策略已改变，请核对当前操作。',operator_policy_version_conflict:'策略已被更新，请刷新后重新修改。',operator_binding_changed:'部署配置已改变，请核对原操作。',operator_bootstrap_unconfigured:'工作机启动流程尚未配置。',operator_provider_disabled:'供应商执行尚未启用。',operator_node_observation_stale:'机器观测已过期，请先核对。',operator_active_obligations:'仍有任务或租赁义务需要完成。',operator_capacity_disabled:'手动容量服务未启用。',operator_capacity_unconfigured:'手动容量服务尚未配置。',controller_unavailable:'控制器暂不可用。',controller_stale:'控制器状态已过期。',policy_disabled:'启动策略未启用。',provider_unconfigured:'供应商尚未连接。',no_matching_offers:'没有符合条件的机器。',inventory_unavailable:'目前无法核对库存。',max_instances_exceeded:'将超过机器数量上限。',max_physical_gpus_exceeded:'将超过 GPU 数量上限。',max_hourly_cost_exceeded:'将超过每小时费用上限。',budget_exceeded:'可用预算不足。',active_jobs:'仍有任务正在执行或收集。',pending_obligations:'仍有任务、租赁或费用需要核对。',unknown_rental:'原租赁结果未知，不能重复开机。',node_stale:'机器状态已过期，需先核对。',version_conflict:'机器或策略已改变，请刷新后重新预览。',preview_expired:'启动预览已过期，请重新预览。',profile_unavailable:'此部署配方尚未开放启动。',runtime_profile_unavailable:'此部署配方尚未开放启动。',capacity_not_configured:'容量控制尚未配置。'};
export function reasonText(value){const code=typeof value==='string'?value:value?.code;return reasons[code]||'条件尚未满足，请核对状态后重试。';}
export function staleSnapshot(snapshot,now=Date.now()){
  const observed=epochMs(snapshot?.observed_at);
  return !snapshot||observed===null||now-observed>30000;
}
export function allowed(snapshot,permission,now=Date.now()){
  return !staleSnapshot(snapshot,now)&&snapshot.operator?.permissions?.[permission]===true;
}
export function profileSelection(profile,selection){
  return {...selection,runtime_profile_id:profile?.id||'',gpu_type:profile?.gpu_models?.[0]||'',gpu_count:profile?.gpu_count_options?.[0]||1,filters:{...selection.filters,min_ram_gib:Math.ceil((profile?.minimum_ram_bytes||0)/1024**3),min_disk_gib:Math.ceil((profile?.minimum_disk_bytes||0)/1024**3)}};
}
export function runtimeDuration(policy,reportedMinimum=null,value=null){
  const maximumMinutes=Number.isFinite(policy?.max_ttl_seconds)?Math.floor(policy.max_ttl_seconds/60):null;
  const minimumMinutes=Number.isFinite(reportedMinimum)&&reportedMinimum>0?Math.ceil(reportedMinimum/60):null;
  const feasible=maximumMinutes!==null&&maximumMinutes>=Math.max(2,minimumMinutes??2);
  const suggestedSeconds=feasible?Math.min(maximumMinutes,Math.max(180,minimumMinutes??2))*60:null;
  const problem=!feasible?'当前策略上限无法容纳供应商期限，请先核对运行上限。':value!==null&&(!Number.isFinite(value)||value%60!==0||value<120||value>maximumMinutes*60||minimumMinutes!==null&&value<minimumMinutes*60)?'本次运行时间不在已核对的范围内，请调整后重新预览。':null;
  return {minimumMinutes,maximumMinutes,suggestedSeconds,problem};
}
export function initialOperatorSelection(policy){
  return {runtime_profile_id:'',mode:'fl',gpu_type:'',node_count:1,gpu_count:1,ttl_seconds:runtimeDuration(policy).suggestedSeconds??0,filters:{min_ram_gib:0,min_disk_gib:0,min_download_mbps:500,max_price_per_gpu_hour_microusd:2000000,allowed_countries:[]}};
}
export function operatorStartPayload(selection,customFilters=false){
  if(customFilters)return selection;
  const {filters,...body}=selection;return body;
}
