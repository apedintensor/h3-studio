'use strict';

const $ = (id) => document.getElementById(id);
const DEFAULT_LIMITS = Object.freeze({ max_images: 9, max_videos: 3, max_audios: 3, max_total_files: 12, min_clip_duration: 2, max_clip_duration: 15, max_total_video_duration: 15, max_total_audio_duration: 15 });
const GROUPS = ['images', 'videos', 'audios', 'first_frame', 'last_frame'];
const GROUP_KIND = { images: 'image', videos: 'video', audios: 'audio', first_frame: 'image', last_frame: 'image' };
const GROUP_LABEL = { images: '图片', videos: '视频', audios: '音频', first_frame: '首帧', last_frame: '尾帧' };
const STATUS_LABEL = { queued: '排队中', running: '生成中', cancel_requested: '正在取消', succeeded: '已完成', failed: '失败', cancelled: '已取消', canceled: '已取消' };
const ACTIVE_STATUSES = ['queued', 'running', 'cancel_requested'];
const LEGACY_INPUT_STORAGE_KEY = 'h3-studio:inputs:v1';
const LEGACY_PREFERENCES_STORAGE_KEY = 'h3-studio:preferences';
const TEST_ACCOUNTS = ['superdan', 'supervan'];
const CONTROL_FIELDS = ['width', 'height', 'seed', 'steps', 'sampler_name', 'scheduler', 'denoise', 'ref_image_size', 'shift_video', 'shift_audio', 'video_decode', 'audio_decode', 'video_tile_size', 'video_overlap', 'video_temporal_size', 'video_temporal_overlap', 'audio_tile_size', 'audio_overlap', 'export_crf', 'encoder_device'];
const CONTROL_DEFAULTS = { width: 1344, height: 768, seed: '', steps: 20, sampler_name: 'res_multistep', scheduler: 'auto', denoise: 1, ref_image_size: 'max', shift_video: '', shift_audio: '', video_decode: 'normal', audio_decode: 'normal', video_tile_size: 512, video_overlap: 64, video_temporal_size: 64, video_temporal_overlap: 8, audio_tile_size: 512, audio_overlap: 64, export_crf: 18, encoder_device: 'default' };
const STRING_CONTROLS = new Set(['seed', 'sampler_name', 'scheduler', 'ref_image_size', 'video_decode', 'audio_decode', 'encoder_device']);
const state = { capabilities: null, backend: null, model: null, mode: 'ref', assets: Object.fromEntries(GROUPS.map((group) => [group, []])), extraGuideAssets: [], guides: [], videoAudio: {}, jobs: [], selectedJob: null, submitting: false, capabilitiesLoading: false, polling: false, restoringAssets: false, reusingSettings: false, pendingSubmission: null, savedPreferences: null, previewURL: null };
let toastTimer;
let sessionEpoch = 0;
let authBusy = false;
let authConfig = null;
const pendingRequests = new Set();
const sessionChannel = typeof BroadcastChannel === 'function' ? new BroadcastChannel('h3-studio:account-change') : null;
state.username = null;

function storageKey(name) { return state.username ? `h3-studio:user:${state.username}:${name}` : null; }
function sessionCurrent(epoch) { return epoch === sessionEpoch && Boolean(state.username); }
function staleSessionError() { const error = new Error('登录账户已改变，请在当前账户继续操作'); error.name = 'SessionChangedError'; return error; }
function invalidateSession() {
  sessionEpoch += 1;
  for (const controller of pendingRequests) controller.abort();
  pendingRequests.clear();
}
function migrateLegacyDraft(username) {
  if (username !== 'superdan') return;
  // The pre-account workbench and its historical server assets belong to superdan.
  // Never copy these references into a newly created test account.
  try {
    for (const [legacy, name] of [[LEGACY_INPUT_STORAGE_KEY, 'inputs:v1'], [LEGACY_PREFERENCES_STORAGE_KEY, 'preferences']]) {
      const current = storageKey(name), value = localStorage.getItem(legacy);
      if (value !== null && localStorage.getItem(current) === null) localStorage.setItem(current, value);
      if (value !== null) localStorage.removeItem(legacy);
    }
  } catch { /* Editing remains available without browser storage. */ }
}
function clearPrivateWorkbench() {
  for (const asset of allAssets()) if (asset.localURL && asset.previewURL) URL.revokeObjectURL(asset.previewURL);
  if (state.previewURL) URL.revokeObjectURL(state.previewURL);
  Object.assign(state, { capabilities: null, backend: null, model: null, mode: 'ref', assets: Object.fromEntries(GROUPS.map((group) => [group, []])), extraGuideAssets: [], guides: [], videoAudio: {}, jobs: [], selectedJob: null, submitting: false, capabilitiesLoading: false, polling: false, restoringAssets: false, reusingSettings: false, pendingSubmission: null, savedPreferences: null, previewURL: null, previewRequestBody: null });
  $('prompt').value = ''; $('duration').value = '5'; $('resolution').replaceChildren(); $('aspect-ratio').value = '16:9'; $('generate-audio').checked = true;
  applyControlPreferences(CONTROL_DEFAULTS);
  $('advanced-controls').open = false;
  for (const group of GROUPS) { $(`assets-${group}`).replaceChildren(); $(`upload-${group}`).value = ''; }
  $('guides-list').replaceChildren(); $('history-list').replaceChildren(); $('workflow-preview').textContent = ''; $('api-example').textContent = '';
  for (const id of ['form-error', 'result-error', 'workflow-preview-status', 'service-notice']) notice(id, '');
  for (const id of ['download-workflow', 'download-video', 'download-audio']) { $(id).removeAttribute('href'); $(id).removeAttribute('download'); }
  $('download-workflow').download = 'h3-api-workflow.json';
  for (const id of ['result-info', 'result-time', 'progress-title', 'progress-description', 'progress-value']) $(id).textContent = '';
  for (const id of ['result-video', 'result-audio']) { const media = $(id); media.pause(); media.removeAttribute('src'); delete media.dataset.jobId; media.load(); }
  show($('workflow-preview'), false); show($('download-workflow'), false); show($('lease-info'), false);
  show($('native-comfy'), false); show($('download-native-workflow'), false); show($('native-admin-note'), false);
  show($('toast'), false); clearTimeout(toastTimer);
  setAPIPanel(false); renderAssets(); renderHistory(); renderJob(); updateMode();
}
function showLogin(message = '') {
  $('password').value = '';
  invalidateSession(); state.username = null;
  show($('workbench'), false); $('workbench').inert = true;
  show($('workbench-nav'), false); show($('account-menu'), false); $('account-name').textContent = '';
  clearPrivateWorkbench();
  show($('login-panel'), true); $('connection-label').textContent = '请登录'; $('connection').className = 'connection';
  $('login-submit').disabled = authBusy || !authConfig?.auth_ready; $('login-submit').textContent = '进入工作台';
  notice('login-error', message);
}
async function enterAccount(account) {
  if (!TEST_ACCOUNTS.includes(account?.username) || account.authentication !== authConfig?.authentication) throw new Error('服务返回的账户信息无效');
  invalidateSession(); state.username = null; clearPrivateWorkbench();
  state.username = account.username;
  const epoch = sessionEpoch;
  migrateLegacyDraft(state.username); restorePreferences();
  $('account-name').textContent = state.username;
  const admin = state.username === 'superdan';
  show($('native-comfy'), admin); show($('download-native-workflow'), admin); show($('native-admin-note'), admin);
  show($('login-panel'), false); show($('workbench'), true); $('workbench').inert = false;
  show($('workbench-nav'), true); show($('account-menu'), true);
  state.restoringAssets = true;
  updateMode();
  await Promise.allSettled([loadCapabilities(), loadJobs(true), restoreAssetReferences().then(async () => { if (sessionCurrent(epoch)) await restoreExtraGuideAssets(state.guides.map((guide) => guide.media_id)); })]);
  if (sessionCurrent(epoch)) { state.restoringAssets = false; renderAssets(); updateForm(); }
}
async function restoreSession() {
  if (authBusy) return;
  authBusy = true; showLogin(); $('login-submit').disabled = true; $('login-submit').textContent = '正在检查登录状态…';
  const epoch = sessionEpoch;
  try {
    const config = await api('/api/auth/config', { authRequest: true });
    if (epoch !== sessionEpoch) return;
    if (!['username-only-test', 'password'].includes(config.authentication) || typeof config.auth_ready !== 'boolean') throw new Error('登录配置无效');
    authConfig = config;
    const requiresPassword = config.authentication === 'password';
    show($('password-field'), requiresPassword); $('password').disabled = !requiresPassword; $('password').required = requiresPassword;
    show($('account-shortcuts'), !requiresPassword);
    $('login-eyebrow').textContent = requiresPassword ? 'YOUR PRIVATE STUDIO' : 'TWO-PERSON TEST STUDIO';
    $('username').placeholder = requiresPassword ? '输入用户名' : 'superdan 或 supervan';
    $('login-description').textContent = requiresPassword ? '使用用户名和密码登录。你的素材、任务和成片按账户分别保存。' : '输入用户名即可登录。你的素材、任务和成片按账户分别保存。';
    $('login-mode-note').textContent = requiresPassword ? '仅限受邀账户。忘记密码请联系管理员；密码更改后，原会话会退出。' : '本机双人测试模式 · 无密码，知道用户名就能进入对应账户，不能保护账户身份。';
    $('api-auth-instructions').textContent = requiresPassword ? '先 POST /api/auth/login，在 JSON 中发送 username 和 password，并让客户端保存返回的 HttpOnly 会话 Cookie。后续素材、任务和下载使用同一会话。不要把密码或 Cookie 写进代码、URL、日志或请求示例；可在程序运行时交互输入。下面是工作台 API。' : '先 POST /api/auth/login，发送 {"username":"superdan"} 或 {"username":"supervan"}，并让客户端保存会话 Cookie。当前是本机免密码测试方式，不能证明身份。后续素材、任务和下载使用同一会话。';
    if (!config.auth_ready) { notice('login-error', '登录服务尚未完成初始化，请联系管理员'); return; }
    const account = await api('/api/auth/me', { authRequest: true }); if (epoch === sessionEpoch) await enterAccount(account);
  }
  catch (error) { if (epoch === sessionEpoch) notice('login-error', error.status === 401 ? '' : `无法核验登录状态：${errorMessage(error)}`); }
  finally { authBusy = false; $('login-submit').disabled = !authConfig?.auth_ready; $('login-submit').textContent = '进入工作台'; }
}
async function login(event) {
  event?.preventDefault(); if (authBusy) return;
  if (!authConfig?.auth_ready) { $('password').value = ''; notice('login-error', '登录服务尚未就绪，请刷新后重试'); return; }
  const username = $('username').value.trim();
  const requiresPassword = authConfig.authentication === 'password';
  if (!username || (!requiresPassword && !TEST_ACCOUNTS.includes(username))) { $('password').value = ''; notice('login-error', requiresPassword ? '请输入用户名' : '当前仅开放 superdan 和 supervan 两个测试账户'); $('username').focus(); return; }
  const payload = requiresPassword ? { username, password: $('password').value } : { username };
  $('password').value = '';
  authBusy = true; $('login-submit').disabled = true; $('login-submit').textContent = '正在登录…'; notice('login-error', '');
  const epoch = sessionEpoch;
  try {
    const account = await api('/api/auth/login', { authRequest: true, method: 'POST', body: JSON.stringify(payload) });
    if (epoch !== sessionEpoch) return;
    sessionChannel?.postMessage({ type: 'account-changed' });
    await enterAccount(account);
  } catch (error) { if (epoch === sessionEpoch) notice('login-error', errorMessage(error)); }
  finally { delete payload.password; authBusy = false; $('password').value = ''; $('login-submit').disabled = !authConfig?.auth_ready; $('login-submit').textContent = '进入工作台'; }
}
async function logout() {
  if (authBusy) return;
  authBusy = true; showLogin(); $('username').value = ''; $('login-submit').disabled = true; $('login-submit').textContent = '正在退出…';
  sessionChannel?.postMessage({ type: 'account-invalidated' });
  try { await api('/api/auth/logout', { authRequest: true, method: 'POST' }); sessionChannel?.postMessage({ type: 'account-changed' }); }
  catch (error) { notice('login-error', `退出未获服务确认：${errorMessage(error)}。请重试登录前先刷新。`); }
  finally { authBusy = false; $('login-submit').disabled = !authConfig?.auth_ready; $('login-submit').textContent = '进入工作台'; $('username').focus(); }
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function show(node, visible) { node.hidden = !visible; }
function notice(id, message) { const node = $(id); node.textContent = message || ''; show(node, Boolean(message)); }
function toast(message) { $('toast').textContent = message; show($('toast'), true); clearTimeout(toastTimer); toastTimer = setTimeout(() => show($('toast'), false), 4000); }
function limit(name) { const value = state.backend?.limits?.[name]; return Number.isFinite(Number(value)) && Number(value) >= 0 ? Number(value) : DEFAULT_LIMITS[name]; }
function feature(name, fallback = false) {
  const sources = [state.model?.features, state.backend?.features, state.capabilities?.features];
  for (const source of sources) {
    if (source && typeof source[name] === 'boolean') return source[name];
    if (source?.controls && typeof source.controls[name] === 'boolean') return source.controls[name];
  }
  return fallback;
}
function totalDuration(group) { return state.assets[group].reduce((total, asset) => total + (Number(asset.duration) || 0), 0); }
function seconds(value) { const amount = Number(value); return Number.isFinite(amount) ? Number(amount.toFixed(2)).toString() : '未知'; }
function fileSize(value) { return `${(Number(value) / 1048576).toFixed(1)} MB`; }
function activeGroups() { return state.mode === 'ref' ? ['images', 'videos', 'audios'] : ['first_frame', 'last_frame']; }
function controlNode(name) { return $(name.replaceAll('_', '-')); }
function allAssets() { return [...GROUPS.flatMap((group) => state.assets[group]), ...state.extraGuideAssets]; }
function videoIncludesAudio(asset) { return asset.hasAudio === true && state.videoAudio[asset.id] !== false; }
function controlsMetadata() { return state.capabilities?.controls || state.backend?.controls || state.model?.controls || {}; }
function modeSupported(mode) { return Array.isArray(state.backend?.modes) && state.backend.modes.includes(mode); }
function errorMessage(error) { return error instanceof Error ? error.message : '操作失败，请稍后重试'; }
function safeMediaURL(value) {
  if (typeof value !== 'string') return null;
  try { const url = new URL(value, location.origin); return ['http:', 'https:'].includes(url.protocol) ? url.href : null; } catch { return null; }
}
async function api(path, options = {}) {
  const { authRequest = false, ...requestOptions } = options;
  if (!authRequest && !state.username) throw staleSessionError();
  const epoch = sessionEpoch, controller = new AbortController(); pendingRequests.add(controller);
  try {
    const response = await fetch(path, { ...requestOptions, credentials: 'same-origin', signal: controller.signal, headers: { ...(options.body && !(options.body instanceof FormData) ? { 'Content-Type': 'application/json' } : {}), ...options.headers } });
    if (epoch !== sessionEpoch) throw staleSessionError();
    let data;
    try { data = await response.json(); } catch { throw new Error(`服务返回了无法读取的响应（HTTP ${response.status}）`); }
    if (epoch !== sessionEpoch) throw staleSessionError();
    if (!response.ok) {
      if (response.status === 401 && !authRequest) { showLogin('登录已失效，请重新登录'); throw staleSessionError(); }
      const detail = typeof data?.error === 'string' ? data.error : typeof data?.detail === 'string' ? data.detail : typeof data?.message === 'string' ? data.message : `请求失败（HTTP ${response.status}）`;
      const error = new Error(detail); error.status = response.status; throw error;
    }
    return data;
  } finally { pendingRequests.delete(controller); }
}

function normalizedModels(backend) {
  return (Array.isArray(backend?.models) ? backend.models : []).map((model) => typeof model === 'string' ? { id: model, label: model } : model).filter((model) => model && typeof model.id === 'string' && model.id);
}
function populateSelect(node, items, value, emptyText) {
  node.replaceChildren();
  if (!items.length) { node.add(new Option(emptyText, '')); node.disabled = true; return; }
  for (const item of items) node.add(new Option(item.label || item.id, item.id));
  node.disabled = false;
  node.value = items.some((item) => item.id === value) ? value : items[0].id;
}
async function loadCapabilities() {
  if (!state.username || state.capabilitiesLoading) return;
  const epoch = sessionEpoch;
  state.capabilitiesLoading = true;
  $('refresh-capabilities').disabled = true;
  try {
    const capabilities = await api('/api/capabilities');
    if (!sessionCurrent(epoch)) return;
    if (!Array.isArray(capabilities.backends)) throw new Error('服务没有返回有效的后端能力配置');
    const previous = $('backend').value;
    state.capabilities = capabilities;
    populateSelect($('backend'), capabilities.backends, previous || capabilities.default_backend, '没有已配置后端');
    state.backend = capabilities.backends.find((backend) => backend.id === $('backend').value) || null;
    renderLease();
    updateBackend();
  } catch (error) {
    if (!sessionCurrent(epoch)) return;
    state.capabilities = null;
    state.backend = null;
    state.model = null;
    renderLease();
    $('backend').disabled = true;
    $('model').disabled = true;
    $('resolution').disabled = true;
    $('connection').className = 'connection unavailable';
    $('connection-label').textContent = '服务连接失败';
    notice('service-notice', `${errorMessage(error)}。可继续编辑，但暂时不能上传与生成。`);
    updateForm();
  } finally { if (sessionCurrent(epoch)) { state.capabilitiesLoading = false; $('refresh-capabilities').disabled = false; } }
}
function updateBackend() {
  const backend = state.backend;
  $('connection').className = `connection ${backend?.available ? 'ready' : 'unavailable'}`;
  $('connection-label').textContent = backend?.available ? '服务已就绪' : '尚未就绪';
  $('backend-kind').textContent = backend?.label || '没有已配置后端';
  $('backend-description').textContent = backend?.description || (backend?.available ? '此后端可接收任务 · 最终能力以服务器为准' : backend?.reason || '需在服务端完成配置');
  notice('service-notice', backend?.available ? '' : `${backend?.reason || '当前后端尚未完成配置'}。你仍可查看所有输入与参数；生成暂不可用。`);
  $('backend-footnote').textContent = backend?.id?.includes('comfy') || backend?.id?.includes('local') || backend?.id?.includes('self') ? '自部署 H3 Base 使用完整 BF16 主模型，支持小尺寸与自定义画布，推荐上限 768P，无 Turbo。官方 2K 再生成与 H3ContextIR 未开源。新增控制组合尚未逐项 GPU 实测。' : '当前选择的后端由服务器明确配置。官方 API 与自部署权重的能力可能不同，以此处的后端状态为准。';
  const models = normalizedModels(backend);
  populateSelect($('model'), models, state.model?.id, '此后端没有可用模型配置');
  state.model = models.find((model) => model.id === $('model').value) || null;
  if (!modeSupported(state.mode)) {
    const supported = ['ref', 'fl'].find(modeSupported);
    if (supported) state.mode = supported;
  }
  updateModel();
  updateMode();
}
function leaseDate(value) {
  if (!value) return null;
  const date = new Date(typeof value === 'number' ? value < 1e12 ? value * 1000 : value : value);
  return Number.isNaN(date.getTime()) ? null : date;
}
function renderLease() {
  const lease = state.capabilities?.lease;
  show($('lease-info'), Boolean(lease && (lease.gpu || lease.hourly_usd !== undefined || lease.created_at)));
  if (!lease) return;
  $('lease-hardware').textContent = lease.gpu ? `GPU · ${lease.gpu}` : 'GPU 信息未提供';
  if (lease.phase === 'destroyed' || lease.destroyed_at) {
    $('lease-price').textContent = '租赁已结束';
    $('lease-expiry').textContent = 'GPU 已停止';
    $('lease-remaining').textContent = '生成暂停 · 历史作品可下载';
    $('lease-info').classList.remove('expiring');
    return;
  }
  const price = Number(lease.hourly_usd);
  $('lease-price').textContent = lease.hourly_usd !== null && lease.hourly_usd !== undefined && Number.isFinite(price) ? `$${price.toFixed(2)} / 小时` : '小时费用未提供';
  const created = leaseDate(lease.created_at);
  const expiry = leaseDate(lease.removal_scheduled_at) || (created && Number(lease.ttl_hours) > 0 ? new Date(created.getTime() + Number(lease.ttl_hours) * 3600000) : null);
  const noTermination = Object.hasOwn(lease, 'removal_scheduled_at') && lease.removal_scheduled_at === null && lease.ttl_hours === null;
  $('lease-expiry').textContent = expiry ? `预计到期 · ${expiry.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })}` : noTermination ? '未设置自动销毁' : '到期时间未提供';
  const remaining = expiry ? expiry.getTime() - Date.now() : null;
  $('lease-info').classList.toggle('expiring', remaining !== null && remaining < 3600000);
  if (remaining === null) $('lease-remaining').textContent = noTermination ? '持续运行 · 按小时计费' : '';
  else if (remaining <= 0) $('lease-remaining').textContent = '计划期限已到，请刷新服务状态';
  else { const minutes = Math.ceil(remaining / 60000); $('lease-remaining').textContent = `剩余 ${minutes >= 60 ? `${Math.floor(minutes / 60)} 小时 ` : ''}${minutes % 60} 分钟`; }
}
function updateModel() {
  const model = state.model;
  const labels = { '480P': '480P · 较省资源', '576P': '576P · 均衡', '768P': '768P · 原生推荐', custom: '自定义宽高 · 专业' };
  const resolutions = (Array.isArray(model?.resolutions) ? model.resolutions : []).map((resolution) => typeof resolution === 'string' ? { id: resolution, label: labels[resolution] || resolution } : resolution);
  const previousResolution = resolutions.some((item) => item.id === $('resolution').value) ? $('resolution').value : '768P';
  populateSelect($('resolution'), resolutions, state.savedPreferences?.resolution || previousResolution, '该模型未声明分辨率');
  const min = Number(model?.min_duration) || 4;
  const max = Number(model?.max_duration) || 15;
  $('duration').min = min;
  $('duration').max = max;
  $('duration').value = Math.min(max, Math.max(min, Number($('duration').value)));
  $('duration-min').textContent = `${min} 秒`;
  $('duration-max').textContent = `${max} 秒`;
  const audio = feature('generate_audio', true);
  $('generate-audio').disabled = !audio;
  if (!audio) $('generate-audio').checked = false;
  $('audio-capability').textContent = audio ? (state.backend?.id === 'comfy-local' ? '关闭仅移除输出音轨，模型仍联生' : '生成与画面同步的声音') : '当前后端不支持此开关';
  for (const name of ['seed', 'steps']) {
    const enabled = feature(name);
    $(name).disabled = !enabled;
    $(`${name}-help`).textContent = enabled ? (name === 'seed' ? '0–18446744073709551615；按原始数字传递。跨版本与硬件不保证完全一致。' : '默认 20；官方建议可试 25 步。低步数不等于 Turbo，当前未加载 Turbo LoRA。') : (name === 'steps' ? '当前后端未开放采样步数' : '当前后端未开放随机种子');
  }
  const controls = controlsMetadata();
  const options = (items) => (Array.isArray(items) ? items : []).map((item) => typeof item === 'string' ? { id: item, label: item } : item);
  const samplers = options(controls.samplers);
  populateSelect($('sampler-name'), samplers, state.savedPreferences?.sampler_name || $('sampler-name').value || 'res_multistep', '节点未声明采样器');
  populateSelect($('scheduler'), [{ id: 'auto', label: '按模式自动 · 推荐' }, ...options(controls.schedulers).filter((item) => item.id !== 'auto')], state.savedPreferences?.scheduler || $('scheduler').value || 'auto', '按模式自动');
  for (const name of CONTROL_FIELDS.filter((field) => !STRING_CONTROLS.has(field))) { const range = controls.ranges?.[name] || controls[name]; if (range && typeof range === 'object') for (const key of ['min', 'max', 'step']) if (range[key] !== undefined) controlNode(name)[key] = range[key]; }
  if (state.savedPreferences) { applyControlPreferences(state.savedPreferences); state.savedPreferences = null; }
  updateForm();
}
function updateMode() {
  for (const mode of ['ref', 'fl']) {
    const button = $(`mode-${mode}`);
    button.classList.toggle('active', mode === state.mode);
    button.setAttribute('aria-selected', mode === state.mode ? 'true' : 'false');
    button.disabled = false;
    button.title = modeSupported(mode) ? '' : '当前后端不支持此模式，可查看输入但不能生成';
  }
  show($('reference-panel'), state.mode === 'ref');
  show($('frames-panel'), state.mode === 'fl');
  notice('mode-notice', modeSupported(state.mode) ? '' : '当前后端未配置此模式。你可以查看与准备输入，切换到支持的后端后再生成。');
  $('prompt-help-text').textContent = state.mode === 'ref' ? '使用 @image1、@video1、@audio1 指定参考素材，点击卡片即可插入标记。' : '直接描述镜头运动、人物动作与变化，无需参考标记。首尾帧作为位置约束；都不上传时就是纯文生视频。';
  $('prompt').placeholder = state.mode === 'ref' ? '先上传素材，再点击卡片的“插入标记”。例如：以 @image1 的人物为主角，参考 @video1 的动作，使用 @audio1 的环境声。请只引用已上传的素材。' : '例如：雨后的城市街道，主角缓慢转身望向镜头，镜头从中景推近到面部。自然光影、动作连贯、有电影感。首尾帧都可留空。';
  renderAssets();
  updateForm();
}

function kindMatches(file, kind) {
  const extension = file.name.split('.').pop().toLowerCase();
  const allowed = { image: ['jpg', 'jpeg', 'png', 'webp'], video: ['mp4', 'mov'], audio: ['wav', 'mp3'] };
  return allowed[kind].includes(extension) && (!file.type || file.type.startsWith(`${kind}/`) || file.type === 'application/octet-stream');
}
async function readDuration(file, previewURL, kind) {
  if (kind === 'image') return null;
  return new Promise((resolve) => {
    const media = document.createElement(kind === 'video' ? 'video' : 'audio');
    let completed = false;
    const finish = (duration) => { if (completed) return; completed = true; clearTimeout(timer); media.removeAttribute('src'); media.load(); resolve(duration); };
    const timer = setTimeout(() => finish(null), 6000);
    media.preload = 'metadata';
    media.onloadedmetadata = () => finish(Number.isFinite(media.duration) ? media.duration : null);
    media.onerror = () => finish(null);
    media.src = previewURL;
  });
}
function countLimit(group) { return group.endsWith('_frame') ? 1 : limit(`max_${group}`); }
function validateDuration(group, duration, excluded) {
  if (!['videos', 'audios'].includes(group) || !Number.isFinite(duration)) return;
  if (duration < limit('min_clip_duration') - .05 || duration > limit('max_clip_duration') + .05) throw new Error(`${GROUP_LABEL[group]}单段时长应为 ${limit('min_clip_duration')}–${limit('max_clip_duration')} 秒；当前为 ${seconds(duration)} 秒`);
  const total = state.assets[group].reduce((sum, asset) => sum + (asset === excluded ? 0 : Number(asset.duration) || 0), 0) + duration;
  const max = limit(group === 'videos' ? 'max_total_video_duration' : 'max_total_audio_duration');
  if (total > max + .05) throw new Error(`${GROUP_LABEL[group]}参考总时长不能超过 ${max} 秒；加入后为 ${seconds(total)} 秒`);
}
async function addFiles(group, files) {
  const epoch = sessionEpoch;
  if (!sessionCurrent(epoch)) return;
  notice('form-error', '');
  if (!state.capabilities || !state.backend) { notice('form-error', '工作台服务尚未连接，请先检查服务配置再上传。'); return; }
  for (const file of Array.from(files)) {
    if (!sessionCurrent(epoch)) return;
    try {
      const kind = GROUP_KIND[group];
      if (!kindMatches(file, kind)) throw new Error(`“${file.name}”的格式不支持，请上传${kind === 'image' ? ' JPG、PNG 或 WebP 图片（HEIC 请先转换）' : kind === 'video' ? ' MP4 或 MOV 视频' : ' WAV 或 MP3 音频'}`);
      if (state.assets[group].length >= countLimit(group)) throw new Error(`${GROUP_LABEL[group]}最多上传 ${countLimit(group)} ${kind === 'image' ? '张' : '段'}，请先移除现有素材`);
      if (['images', 'videos', 'audios'].includes(group) && ['images', 'videos', 'audios'].reduce((count, name) => count + state.assets[name].length, 0) >= limit('max_total_files')) throw new Error(`全能参考素材合计最多 ${limit('max_total_files')} 个文件`);
      const sizeLimits = { image: Number(state.backend.limits?.max_image_size_mb) || 30, video: Number(state.backend.limits?.max_video_size_mb) || 50, audio: Number(state.backend.limits?.max_audio_size_mb) || 15 };
      if (file.size > sizeLimits[kind] * 1048576) throw new Error(`${GROUP_LABEL[group]}单文件不能超过 ${sizeLimits[kind]} MB`);
      const asset = { localId: crypto.randomUUID(), id: null, name: file.name, kind, size: file.size, previewURL: URL.createObjectURL(file), localURL: true, status: 'uploading', duration: null, modelDuration: null, hasAudio: null, notes: [], error: '' };
      state.assets[group].push(asset);
      renderAssets(); updateForm();
      try {
        asset.duration = await readDuration(file, asset.previewURL, kind);
        if (!sessionCurrent(epoch)) return;
        if (!state.assets[group].includes(asset)) continue;
        validateDuration(group, asset.duration, asset);
        const body = new FormData(); body.append('file', file);
        const uploaded = await api('/api/uploads', { method: 'POST', body });
        if (!sessionCurrent(epoch)) return;
        if (!state.assets[group].includes(asset)) continue;
        if (!uploaded?.id || uploaded.kind !== kind) throw new Error('上传响应缺少有效的素材 ID，或素材类型与输入区不匹配');
        if (uploaded.duration !== undefined && uploaded.duration !== null) asset.modelDuration = Number(uploaded.duration);
        if (uploaded.source_duration !== undefined && uploaded.source_duration !== null) asset.duration = Number(uploaded.source_duration);
        else if (asset.duration === null && Number.isFinite(asset.modelDuration)) asset.duration = asset.modelDuration;
        asset.hasAudio = typeof uploaded.has_audio === 'boolean' ? uploaded.has_audio : null;
        asset.notes = Array.isArray(uploaded.notes) ? uploaded.notes.filter((note) => typeof note === 'string') : typeof uploaded.notes === 'string' ? [uploaded.notes] : [];
        validateDuration(group, asset.duration, asset);
        asset.id = uploaded.id;
        asset.status = 'ready';
        const url = safeMediaURL(uploaded.preview_url);
        if (url) { if (asset.localURL) URL.revokeObjectURL(asset.previewURL); asset.previewURL = url; asset.localURL = false; }
      } catch (error) { if (!sessionCurrent(epoch)) return; asset.status = 'failed'; asset.error = errorMessage(error); }
      renderAssets(); updateForm();
    } catch (error) { if (!sessionCurrent(epoch)) return; notice('form-error', errorMessage(error)); }
  }
}
function removeAsset(group, asset) {
  if (asset.localURL) URL.revokeObjectURL(asset.previewURL);
  state.assets[group] = state.assets[group].filter((item) => item !== asset);
  delete state.videoAudio[asset.id];
  renderAssets(); updateForm();
}
function persistAssetReferences() {
  if (!state.username || state.restoringAssets) return;
  const groups = Object.fromEntries(GROUPS.map((group) => [group, state.assets[group].filter((asset) => typeof asset.id === 'string' && asset.id).map((asset) => ({ id: asset.id, name: asset.name, kind: asset.kind, size: asset.size }))]));
  try { localStorage.setItem(storageKey('inputs:v1'), JSON.stringify({ version: 1, mode: state.mode, groups })); } catch { /* Uploads remain on the server if browser storage is unavailable. */ }
}
function savedAsset(group, reference) {
  return { localId: crypto.randomUUID(), id: reference.id, name: typeof reference.name === 'string' ? reference.name : reference.id, kind: GROUP_KIND[group], size: Number(reference.size) || 0, previewURL: null, localURL: false, status: 'restoring', duration: null, modelDuration: null, hasAudio: null, notes: [], error: '' };
}
async function restoreAssetMetadata(group, asset) {
  const epoch = sessionEpoch;
  try {
    const uploaded = await api(`/api/uploads/${encodeURIComponent(asset.id)}`);
    if (!sessionCurrent(epoch)) return;
    if (!state.assets[group].includes(asset)) return;
    if (uploaded.id !== asset.id || uploaded.kind !== GROUP_KIND[group]) throw new Error('服务器素材与保存的输入引用不匹配');
    const previewURL = safeMediaURL(uploaded.preview_url);
    if (!previewURL) throw new Error('素材没有有效的预览地址');
    asset.name = typeof uploaded.name === 'string' ? uploaded.name : asset.name;
    asset.size = Number(uploaded.size) || asset.size;
    asset.previewURL = previewURL;
    asset.modelDuration = uploaded.duration === null || uploaded.duration === undefined ? null : Number(uploaded.duration);
    asset.duration = uploaded.source_duration === null || uploaded.source_duration === undefined ? asset.modelDuration : Number(uploaded.source_duration);
    asset.hasAudio = typeof uploaded.has_audio === 'boolean' ? uploaded.has_audio : null;
    asset.notes = Array.isArray(uploaded.notes) ? uploaded.notes.filter((note) => typeof note === 'string') : typeof uploaded.notes === 'string' ? [uploaded.notes] : [];
    asset.status = 'ready';
  } catch (error) { if (!sessionCurrent(epoch)) return; asset.status = 'failed'; asset.error = `恢复失败：${errorMessage(error)}。可移除后重新上传。`; }
  if (!sessionCurrent(epoch)) return;
  renderAssets(); updateForm();
}
async function restoreAssetReferences() {
  const epoch = sessionEpoch;
  if (!sessionCurrent(epoch)) return;
  let saved;
  try { saved = JSON.parse(localStorage.getItem(storageKey('inputs:v1')) || 'null'); } catch { return; }
  if (!saved || saved.version !== 1 || !saved.groups || typeof saved.groups !== 'object') return;
  state.restoringAssets = true;
  if (['ref', 'fl'].includes(saved.mode)) state.mode = saved.mode;
  const pending = [];
  for (const group of GROUPS) {
    const references = Array.isArray(saved.groups[group]) ? saved.groups[group] : [];
    for (const reference of references) {
      if (!reference || typeof reference.id !== 'string' || !reference.id || reference.kind !== GROUP_KIND[group]) continue;
      const asset = savedAsset(group, reference);
      state.assets[group].push(asset);
      pending.push(restoreAssetMetadata(group, asset));
    }
  }
  renderAssets(); updateForm();
  await Promise.allSettled(pending);
  if (!sessionCurrent(epoch)) return;
  state.restoringAssets = false;
  renderAssets(); updateForm();
}
function insertPromptToken(token) {
  const prompt = $('prompt');
  const start = prompt.selectionStart ?? prompt.value.length;
  const end = prompt.selectionEnd ?? start;
  const prefix = start > 0 && !/\s$/.test(prompt.value.slice(0, start)) ? ' ' : '';
  const suffix = end < prompt.value.length && !/^\s/.test(prompt.value.slice(end)) ? ' ' : '';
  const insertion = `${prefix}${token}${suffix}`;
  prompt.setRangeText(insertion, start, end, 'end');
  prompt.focus();
  invalidateChangedSubmission();
  updateForm();
}
function tokenButton(token, label = '插入标记') {
  const button = element('button', 'insert-token', `${label} ${token}`);
  button.type = 'button';
  button.addEventListener('click', () => insertPromptToken(token));
  return button;
}
function renderAssets() {
  const videoAudioCount = state.assets.videos.filter(videoIncludesAudio).length;
  let videoAudioNumber = 0;
  for (const group of GROUPS) {
    const container = $(`assets-${group}`);
    container.replaceChildren();
    state.assets[group].forEach((asset, index) => {
      const card = element('article', `asset-card ${asset.kind === 'audio' ? 'audio-card' : ''}`);
      const preview = asset.previewURL ? element(asset.kind === 'image' ? 'img' : asset.kind) : element('div', 'asset-preview asset-placeholder', asset.status === 'restoring' ? '正在恢复素材…' : '素材暂不可用');
      if (asset.previewURL) {
        if (asset.kind === 'image') { preview.alt = `${GROUP_LABEL[group]} ${index + 1}：${asset.name}`; preview.loading = 'lazy'; }
        else { preview.controls = true; preview.preload = 'metadata'; if (asset.kind === 'video') preview.playsInline = true; }
        preview.className = 'asset-preview';
        preview.src = asset.previewURL;
      }
      const label = element('div', 'asset-label');
      const number = index + 1;
      const token = group.endsWith('_frame') ? null : `@${asset.kind}${number}`;
      label.append(element('span', '', group.endsWith('_frame') ? GROUP_LABEL[group] : `${GROUP_LABEL[group]} ${number} · ${token}`));
      if (asset.duration) label.append(element('span', '', `${seconds(asset.duration)} 秒`));
      const remove = element('button', 'remove-asset', '×'); remove.type = 'button'; remove.setAttribute('aria-label', `移除${GROUP_LABEL[group]} ${index + 1} ${asset.name}`); remove.addEventListener('click', () => removeAsset(group, asset));
      const statusText = asset.status === 'ready' ? `已上传 · ${fileSize(asset.size)}` : asset.status === 'failed' ? asset.error : asset.status === 'restoring' ? '从服务器恢复素材引用…' : '正在检查并上传…';
      if (asset.kind === 'audio') card.append(label, preview); else card.append(preview, label);
      card.append(element('div', 'asset-name', asset.name), element('div', `asset-state ${asset.status === 'ready' ? 'ready' : asset.status === 'failed' ? 'error' : ''}`, statusText));
      if (token) card.append(tokenButton(token));
      else card.append(element('div', 'asset-state', group === 'first_frame' ? '镜头起点约束 · 无需参考标记' : '镜头终点约束 · 无需参考标记'));
      if (group === 'videos' && asset.hasAudio === true) {
        const toggleLabel = element('label', 'asset-audio-toggle');
        const toggle = element('input'); toggle.type = 'checkbox'; toggle.checked = videoIncludesAudio(asset); toggle.setAttribute('aria-label', `参考视频 ${number} 的原音`);
        toggleLabel.append(toggle, element('span', '', '同时参考这段视频的原音'));
        toggle.addEventListener('change', () => { state.videoAudio[asset.id] = toggle.checked; renderAssets(); invalidateChangedSubmission(); updateForm(); });
        card.append(toggleLabel);
        if (videoIncludesAudio(asset)) { videoAudioNumber += 1; card.append(element('div', 'asset-state ready', `启用视频原音 · <Audio ${videoAudioNumber}>`), tokenButton(`<Audio ${videoAudioNumber}>`, '插入音轨')); }
        else card.append(element('div', 'asset-state', '仅参考画面，不发送原音'));
      }
      for (const note of asset.notes) card.append(element('div', 'asset-state', note));
      card.append(remove);
      container.append(card);
    });
  }
  $('images-count').textContent = `${state.assets.images.length} / ${limit('max_images')} 张`;
  for (const group of ['videos', 'audios']) $('' + `${group}-count`).textContent = `${state.assets[group].length} / ${limit(`max_${group}`)} 段 · ${seconds(totalDuration(group))} / ${limit(group === 'videos' ? 'max_total_video_duration' : 'max_total_audio_duration')} 秒`;
  const total = ['images', 'videos', 'audios'].reduce((count, group) => count + state.assets[group].length, 0);
  $('total-count').textContent = `${total} / ${limit('max_total_files')} 个文件`;
  if (state.mode === 'ref') $('prompt-help-text').textContent = `点击卡片插入 @image1、@video1、@audio1。独立音频始终从 @audio1 起算，后端会跳过已启用的视频原音。${videoAudioCount ? '切换视频原音会改变 <Audio N> 编号，请重新检查这类原生标记。' : '只引用当前已上传的素材。'}`;
  renderGuides();
  persistAssetReferences();
}

function renderGuides() {
  const list = $('guides-list'); list.replaceChildren();
  const assets = allAssets().filter((asset, index, items) => asset.id && items.findIndex((item) => item.id === asset.id) === index);
  state.guides.forEach((guide, index) => {
    const row = element('div', 'guide-row');
    const mediaField = element('label', 'guide-field', `锚点 ${index + 1} · 素材`);
    const media = element('select'); media.setAttribute('aria-label', `锚点 ${index + 1} 素材`); media.add(new Option('选择已上传素材', ''));
    for (const asset of assets) media.add(new Option(`${asset.kind === 'image' ? '图片' : asset.kind === 'video' ? '视频' : '音频'} · ${asset.name}`, asset.id));
    if (guide.media_id && !assets.some((asset) => asset.id === guide.media_id)) media.add(new Option(`素材待恢复 / 已移除 · ${guide.media_id}`, guide.media_id));
    media.value = guide.media_id || ''; mediaField.append(media);
    const timeField = element('label', 'guide-field', '放在第几秒');
    const time = element('input'); time.type = 'number'; time.min = '0'; time.max = String(Math.max(0, Number($('duration').value) - 1 / 24)); time.step = String(1 / 24); time.value = guide.time_seconds ?? 0; time.setAttribute('aria-label', `锚点 ${index + 1} 秒数`); timeField.append(time);
    const snapped = element('small', 'guide-position', `取第 ${Math.round(Number(guide.time_seconds) * 24)} 帧`); timeField.append(snapped);
    const soundField = element('label', 'guide-audio'); const sound = element('input'); sound.type = 'checkbox'; sound.checked = guide.use_audio === true; sound.disabled = !assets.some((asset) => asset.id === guide.media_id && asset.kind === 'video' && asset.hasAudio === true); sound.setAttribute('aria-label', `锚点 ${index + 1} 包含视频原音`); soundField.append(sound, element('span', '', '包含视频原音'));
    const remove = element('button', 'text-button', '移除 ×'); remove.type = 'button'; remove.setAttribute('aria-label', `移除锚点 ${index + 1}`);
    media.addEventListener('change', () => { guide.media_id = media.value; guide.use_audio = false; renderGuides(); invalidateChangedSubmission(); updateForm(); });
    time.addEventListener('input', () => { guide.time_seconds = time.value === '' ? '' : Number(time.value); snapped.textContent = `取第 ${Math.round(Number(guide.time_seconds) * 24)} 帧`; invalidateChangedSubmission(); updateForm(); });
    sound.addEventListener('change', () => { guide.use_audio = sound.checked; invalidateChangedSubmission(); updateForm(); });
    remove.addEventListener('click', () => { state.guides.splice(index, 1); renderGuides(); invalidateChangedSubmission(); updateForm(); });
    row.append(mediaField, timeField, soundField, remove); list.append(row);
    const selected = assets.find((asset) => asset.id === guide.media_id);
    if (selected?.previewURL) { const view = element('a', 'text-link guide-source', `查看锚点素材 ↗${selected.kind !== 'image' ? ` · ${seconds(selected.modelDuration ?? selected.duration)} 秒` : ''}`); view.href = selected.previewURL; view.target = '_blank'; view.rel = 'noopener'; row.append(view); }
  });
  $('add-guide').disabled = state.guides.length >= 8 || !assets.some((asset) => asset.status === 'ready');
  show($('guides-empty'), !state.guides.length);
}

function localOutputSpec() {
  const duration = Number($('duration').value), resolution = $('resolution').value;
  let width, height;
  if (resolution === 'custom') { width = Number($('width').value); height = Number($('height').value); }
  else {
    const shortEdge = Number(resolution.replace(/P$/i, ''));
    const [numerator, denominator] = $('aspect-ratio').value.split(':').map(Number), ratio = numerator / denominator;
    width = ratio >= 1 ? shortEdge * ratio : shortEdge; height = ratio >= 1 ? shortEdge : shortEdge / ratio;
    const maxArea = shortEdge * shortEdge * 1.75;
    if (width * height > maxArea) { const factor = Math.sqrt(maxArea / (width * height)); width *= factor; height *= factor; }
    width = Math.max(32, Math.round(width / 32) * 32); height = Math.max(32, Math.round(height / 32) * 32);
  }
  const frames = Math.ceil((Math.round(duration * 24) - 5) / 17) * 17 + 5;
  return { width, height, frames, actual_duration: frames / 24, export_frames: Math.round(duration * 24) };
}

function applyControlPreferences(preferences) {
  for (const name of CONTROL_FIELDS) {
    const node = controlNode(name), value = preferences[name] ?? CONTROL_DEFAULTS[name];
    if (node.tagName === 'SELECT') { if (Array.from(node.options).some((option) => option.value === String(value))) node.value = value; }
    else node.value = value;
  }
  state.videoAudio = preferences.video_audio && typeof preferences.video_audio === 'object' ? { ...preferences.video_audio } : {};
  state.guides = Array.isArray(preferences.guides) ? preferences.guides.slice(0, 8).map((guide) => ({ media_id: guide.media_id || '', time_seconds: guide.time_seconds ?? 0, use_audio: guide.use_audio === true })) : [];
  renderGuides();
}

function requestPayload() {
  const ref = state.mode === 'ref';
  const payload = { backend: state.backend?.id || '', model: state.model?.id || '', mode: state.mode, prompt: $('prompt').value.trim(), duration: Number($('duration').value), resolution: $('resolution').value, aspect_ratio: $('aspect-ratio').value, generate_audio: !$('generate-audio').disabled && $('generate-audio').checked, inputs: { first_frame: ref ? null : state.assets.first_frame[0]?.id || null, last_frame: ref ? null : state.assets.last_frame[0]?.id || null, images: ref ? state.assets.images.map((asset) => asset.id) : [], videos: ref ? state.assets.videos.map((asset) => asset.id) : [], audios: ref ? state.assets.audios.map((asset) => asset.id) : [] } };
  for (const name of CONTROL_FIELDS) {
    const node = controlNode(name);
    if (node.disabled || node.value === '') continue;
    if (['width', 'height'].includes(name) && payload.resolution !== 'custom') continue;
    payload[name] = STRING_CONTROLS.has(name) ? node.value.trim() : Number(node.value);
  }
  payload.video_audio = ref ? Object.fromEntries(state.assets.videos.filter((asset) => asset.id).map((asset) => [asset.id, videoIncludesAudio(asset)])) : {};
  payload.guides = state.guides.map((guide) => ({ media_id: guide.media_id, time_seconds: Number(guide.time_seconds), use_audio: guide.use_audio === true }));
  return payload;
}
function invalidateChangedSubmission() {
  if (state.pendingSubmission && state.pendingSubmission.body !== JSON.stringify(requestPayload())) state.pendingSubmission = null;
}
function validationProblem(requireOnline = true) {
  if (!state.username) return '登录后即可上传素材与生成';
  if (!state.backend) return '先连接推理服务';
  if (requireOnline && !state.backend.available) return state.backend.reason || '当前后端尚未就绪';
  if (!modeSupported(state.mode)) return '当前后端不支持此创作模式';
  if (!state.model) return '当前后端没有模型配置';
  if (!$('resolution').value) return '该模型未声明可用分辨率';
  if (!$('prompt').value.trim()) return '写下镜头描述后即可生成';
  const assets = activeGroups().flatMap((group) => state.assets[group]);
  if (state.restoringAssets || assets.some((asset) => asset.status === 'restoring')) return '正在从服务器恢复参考素材，请稍候';
  if (state.mode === 'ref' && !assets.length) return '全能参考需至少 1 个素材；纯文字生成请选择“首尾帧 / 文生视频”';
  for (const group of activeGroups()) if (state.assets[group].length > countLimit(group)) return `${GROUP_LABEL[group]}数量超过当前后端上限 ${countLimit(group)}`;
  if (state.mode === 'ref' && assets.length > limit('max_total_files')) return `参考素材合计不能超过 ${limit('max_total_files')} 个文件`;
  if (assets.some((asset) => asset.status === 'uploading')) return '等待参考素材上传完成';
  if (assets.some((asset) => asset.status === 'failed' || !asset.id)) return '请移除上传失败的素材，重新上传';
  const availableTokens = state.mode === 'ref' ? { image: state.assets.images.length, picture: state.assets.images.length, video: state.assets.videos.length, audio: state.assets.audios.length } : { image: 0, picture: 0, video: 0, audio: 0 };
  for (const match of $('prompt').value.matchAll(/@(image|picture|video|audio)\s*(\d+)\b/gi)) {
    const count = availableTokens[match[1].toLowerCase()];
    if (Number(match[2]) < 1 || Number(match[2]) > count) return `提示词中的 ${match[0]} 没有对应素材。${state.mode === 'fl' ? '首尾帧模式不使用参考标记。' : '请移除该标记或上传对应素材。'}`;
  }
  if (!$('seed').disabled && $('seed').value.trim()) { const seed = $('seed').value.trim(); if (!/^\d+$/.test(seed) || BigInt(seed) > 18446744073709551615n) return '随机种子需为 0–18446744073709551615 的整数，不可用小数或科学计数法'; }
  for (const name of CONTROL_FIELDS.filter((field) => !STRING_CONTROLS.has(field))) {
    const node = controlNode(name);
    if (node.disabled || node.value === '' || (['width', 'height'].includes(name) && $('resolution').value !== 'custom')) continue;
    const value = Number(node.value), integer = !['denoise', 'shift_video', 'shift_audio'].includes(name);
    if (!Number.isFinite(value) || (integer && !Number.isInteger(value)) || (node.min !== '' && value < Number(node.min)) || (node.max !== '' && value > Number(node.max))) return `${node.labels?.[0]?.textContent || name}超出范围${integer ? '，需使用整数' : ''}`;
    if (integer && Number(node.step) > 1 && value % Number(node.step)) return `${node.labels?.[0]?.textContent || name}需为 ${node.step} 的倍数`;
  }
  if ($('resolution').value === 'custom') { const width = Number($('width').value), height = Number($('height').value); if (width % 32 || height % 32 || width * height > 768 * 1344 || width / height < .4 || width / height > 2.5) return '自定义宽高需为 32 的倍数、面积不超过 1,032,192，比例在 0.4–2.5 之间'; }
  if (!$('sampler-name').value || !$('scheduler').value) return '当前节点未声明可用采样器或调度器，请刷新服务能力';
  if ($('audio-decode').value !== 'normal') return '当前 H3 音频 VAE 实测不兼容音频分块，请主动选择“完整解码”后再生成';
  for (const [decode, size, overlap] of [['video_decode', 'video_tile_size', 'video_overlap'], ['video_decode', 'video_temporal_size', 'video_temporal_overlap'], ['audio_decode', 'audio_tile_size', 'audio_overlap']]) if (controlNode(decode).value === 'tiled' && Number(controlNode(overlap).value) >= Number(controlNode(size).value)) return '分块重叠必须小于对应分块大小';
  const all = allAssets();
  for (const [index, guide] of state.guides.entries()) {
    const asset = all.find((item) => item.id === guide.media_id);
    if (!asset || asset.status !== 'ready') return `锚点 ${index + 1} 的素材不可用，请选择已上传素材或移除此锚点`;
    const startFrame = Math.round(Number(guide.time_seconds) * 24), endFrame = Math.round(Number($('duration').value) * 24);
    if (guide.time_seconds === '' || !Number.isFinite(Number(guide.time_seconds)) || Number(guide.time_seconds) < 0 || startFrame >= endFrame) return `锚点 ${index + 1} 的秒数必须落在生成时长内`;
    if (asset.kind !== 'image' && startFrame / 24 + Number(asset.modelDuration ?? asset.duration) > Number($('duration').value) + .001) return `锚点 ${index + 1} 的${asset.kind === 'video' ? '视频（含模型补帧）' : '音频'}放不进剩余时长，请提前锚点或延长镜头`;
    if (guide.use_audio && (asset.kind !== 'video' || asset.hasAudio !== true)) return `锚点 ${index + 1} 只有带音轨的视频才能启用原音`;
  }
  if (state.mode === 'ref') for (const asset of state.assets.videos) if (Number(asset.modelDuration ?? asset.duration) > Number($('duration').value) + .001) return `参考视频 ${asset.name} 含模型补帧后超过镜头时长，请延长生成时长或换短素材`;
  try { for (const group of ['videos', 'audios']) if (state.mode === 'ref') for (const asset of state.assets[group]) validateDuration(group, asset.duration, asset); } catch (error) { return errorMessage(error); }
  return '';
}
function updateForm() {
  $('duration-label').textContent = `${$('duration').value} 秒`;
  $('prompt-count').textContent = `${$('prompt').value.length} 字`;
  const problem = validationProblem();
  $('generate').disabled = state.submitting || Boolean(problem);
  $('generate').firstElementChild.textContent = state.submitting ? '提交中…' : '生成视频';
  $('preview-workflow').disabled = state.submitting || state.restoringAssets || Boolean(validationProblem(false));
  show($('custom-dimensions'), $('resolution').value === 'custom');
  show($('aspect-field'), $('resolution').value !== 'custom');
  show($('video-tile-controls'), $('video-decode').value === 'tiled');
  show($('audio-tile-controls'), false);
  show($('audio-decoder-warning'), $('audio-decode').value !== 'normal');
  $('ref-image-size').disabled = state.mode !== 'ref';
  const spec = localOutputSpec();
  $('output-spec').textContent = Number.isFinite(spec.width) && Number.isFinite(spec.height) ? `${spec.width} × ${spec.height} · 原生 24 fps · 模型采样 ${spec.frames} 帧（${seconds(spec.actual_duration)} 秒），导出 ${$('duration').value} 秒` : '选择生成尺寸后查看实际画布';
  $('reuse-job').disabled = reuseBusy();
  $('reuse-job').textContent = state.reusingSettings ? '正在恢复…' : '复用设置 ↶';
  $('submission-summary').textContent = `${$('duration').value} 秒 · ${$('resolution').value || '等待模型'}${$('resolution').value === 'custom' ? ` · ${$('width').value} × ${$('height').value}` : ` · ${$('aspect-ratio').value}`}`;
  $('submission-detail').textContent = state.submitting ? '正在创建任务，请稍候' : problem || `${state.backend.label} · 提交后进入任务队列`;
  const currentRequest = requestPayload();
  $('api-example').textContent = JSON.stringify(currentRequest, null, 2);
  if (state.previewRequestBody && state.previewRequestBody !== JSON.stringify(currentRequest)) { state.previewRequestBody = null; show($('download-workflow'), false); show($('workflow-preview'), false); notice('workflow-preview-status', '设置已改变，请重新检查工作流后再下载。'); }
  const preferences = { prompt: $('prompt').value, duration: $('duration').value, resolution: $('resolution').value, aspect_ratio: $('aspect-ratio').value, generate_audio: $('generate-audio').checked, ...Object.fromEntries(CONTROL_FIELDS.map((name) => [name, controlNode(name).value])), guides: state.guides, video_audio: state.videoAudio, extra_guide_assets: state.extraGuideAssets.filter((asset) => asset.id).map((asset) => ({ id: asset.id, kind: asset.kind })) };
  if (state.username && !state.savedPreferences) try { localStorage.setItem(storageKey('preferences'), JSON.stringify(preferences)); } catch { /* Private browsing and storage restrictions must not stop editing. */ }
}

async function previewWorkflow() {
  const epoch = sessionEpoch;
  const problem = validationProblem(false);
  if (problem) { notice('workflow-preview-status', problem); return; }
  $('preview-workflow').disabled = true;
  notice('workflow-preview-status', '正在核验工作流结构，不创建生成任务…');
  try {
    const body = JSON.stringify(requestPayload());
    const preview = await api('/api/workflow-preview', { method: 'POST', body });
    if (!sessionCurrent(epoch)) return;
    if (body !== JSON.stringify(requestPayload())) throw new Error('检查期间设置已改变，请重新检查当前工作流。');
    const graph = preview.graph || preview.workflow;
    if (!graph || typeof graph !== 'object') throw new Error('服务未返回可导出的工作流');
    const spec = preview.native_spec || preview.nativespec || preview.spec || preview.output_spec;
    const warnings = Array.isArray(preview.warnings) ? preview.warnings : [];
    notice('workflow-preview-status', `工作流已通过配置检查${spec?.width && spec?.height ? ` · ${spec.width} × ${spec.height}` : ''}。没有 GPU 推理。${warnings.length ? ` ${warnings.join('；')}` : ''}`);
    $('workflow-preview').textContent = JSON.stringify({ spec, warnings, graph }, null, 2); show($('workflow-preview'), true);
    if (state.previewURL) URL.revokeObjectURL(state.previewURL);
    state.previewURL = URL.createObjectURL(new Blob([JSON.stringify(graph, null, 2)], { type: 'application/json' }));
    state.previewRequestBody = body;
    $('download-workflow').href = state.previewURL; show($('download-workflow'), true);
  } catch (error) { if (!sessionCurrent(epoch)) return; notice('workflow-preview-status', errorMessage(error)); show($('download-workflow'), false); }
  finally { if (sessionCurrent(epoch)) updateForm(); }
}
async function submitJob() {
  if (!state.username || state.submitting) return;
  const epoch = sessionEpoch;
  const problem = validationProblem();
  if (problem) { notice('form-error', problem); return; }
  const body = JSON.stringify(requestPayload());
  if (!state.pendingSubmission || state.pendingSubmission.body !== body) state.pendingSubmission = { body, key: crypto.randomUUID() };
  const submission = state.pendingSubmission;
  state.submitting = true; notice('form-error', ''); updateForm();
  try {
    const job = await api('/api/jobs', { method: 'POST', headers: { 'Idempotency-Key': submission.key }, body: submission.body });
    if (!sessionCurrent(epoch)) return;
    if (!job?.id) throw new Error('服务没有返回任务 ID，请检查任务记录后再重试');
    if (state.pendingSubmission === submission) state.pendingSubmission = null;
    state.selectedJob = job;
    state.jobs = [job, ...state.jobs.filter((item) => item.id !== job.id)];
    renderJob(); renderHistory(); toast('任务已提交，可在右侧查看进度');
    if (window.innerWidth < 801) $('preview-stage').scrollIntoView({ behavior: 'smooth', block: 'center' });
  } catch (error) { if (!sessionCurrent(epoch)) return; notice('form-error', errorMessage(error)); }
  finally { if (sessionCurrent(epoch)) { state.submitting = false; updateForm(); } }
}
function dateLabel(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '时间未知' : date.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
}
function renderHistory() {
  const list = $('history-list'); list.replaceChildren();
  if (!state.jobs.length) { list.append(element('p', 'history-empty', '还没有生成任务。你的每一次尝试都会保留在这里。')); return; }
  for (const job of state.jobs) {
    const button = element('button', `history-item ${state.selectedJob?.id === job.id ? 'selected' : ''}`); button.type = 'button'; button.dataset.status = job.status;
    button.append(element('span', 'history-icon', job.status === 'succeeded' ? '▷' : job.status === 'failed' ? '!' : '◷'));
    const content = element('span', 'history-item-content'); content.append(element('div', 'history-name', job.prompt || job.id), element('div', 'history-meta', `${dateLabel(job.created_at)} · ${job.model || '未知模型'}`));
    button.append(content, element('span', 'history-status', STATUS_LABEL[job.status] || job.status || '未知'));
    button.addEventListener('click', async () => {
      const epoch = sessionEpoch;
      state.selectedJob = job; renderJob(); renderHistory();
      try { const latest = await api(`/api/jobs/${encodeURIComponent(job.id)}`); if (sessionCurrent(epoch) && state.selectedJob?.id === job.id) { state.selectedJob = latest; renderJob(); } } catch (error) { if (sessionCurrent(epoch)) toast(errorMessage(error)); }
    }); list.append(button);
  }
}
function renderJob() {
  const job = state.selectedJob;
  const active = job && ACTIVE_STATUSES.includes(job.status);
  const outputURL = job?.status === 'succeeded' ? safeMediaURL(job.output_url) : null;
  const audioURL = job?.status === 'succeeded' ? safeMediaURL(job.audio_output_url) : null;
  show($('empty-preview'), !job || (!active && !outputURL));
  show($('job-progress'), Boolean(active));
  show($('result-video'), Boolean(outputURL));
  show($('result-actions'), Boolean(outputURL));
  show($('audio-result'), Boolean(audioURL));
  show($('reuse-settings'), Boolean(job?.request && typeof job.request === 'object'));
  $('reuse-job').disabled = reuseBusy();
  $('result-status').textContent = job ? STATUS_LABEL[job.status] || job.status : '等待创作';
  if (active) {
    $('progress-title').textContent = job.status === 'queued' ? '任务已排队' : job.status === 'cancel_requested' ? '正在取消任务' : '正在生成你的镜头';
    $('progress-description').textContent = job.status === 'queued' ? '等待推理资源，请稍候' : job.status === 'cancel_requested' ? '等待推理服务确认停止，状态会继续更新' : '模型正在处理参考素材与镜头描述';
    const hasProgress = Number.isFinite(job.progress);
    const progress = hasProgress ? Math.max(0, Math.min(100, job.progress)) : 0;
    $('progress-fill').style.width = `${progress}%`;
    $('progress-fill').parentElement.classList.toggle('indeterminate', !hasProgress);
    $('progress-value').textContent = hasProgress ? `${Math.round(progress)}%` : '后端未提供百分比进度';
  }
  show($('cancel-job'), Boolean(active && job.status !== 'cancel_requested' && feature('cancel')));
  if (outputURL) {
    if ($('result-video').dataset.jobId !== job.id) { $('result-video').src = outputURL; $('result-video').dataset.jobId = job.id; }
    $('download-video').href = outputURL;
    $('download-video').download = `h3-${job.id}.mp4`;
    const duration = job.duration ?? job.request?.duration;
    const resolution = job.resolution ?? job.request?.resolution;
    $('result-info').textContent = `${job.model || 'H3'}${duration ? ` · ${duration} 秒` : ''}${resolution ? ` · ${resolution}` : ''}`;
    $('result-time').textContent = `完成于 ${dateLabel(job.finished_at || job.completed_at || job.updated_at || job.created_at)}${Number.isFinite(job.elapsed_seconds) ? ` · 耗时 ${seconds(job.elapsed_seconds)} 秒` : ''}`;
  } else if ($('result-video').src) { $('result-video').pause(); $('result-video').removeAttribute('src'); delete $('result-video').dataset.jobId; $('result-video').load(); }
  if (audioURL) {
    if ($('result-audio').dataset.jobId !== job.id) { $('result-audio').src = audioURL; $('result-audio').dataset.jobId = job.id; }
    $('download-audio').href = audioURL;
    $('download-audio').download = `h3-${job.id}-audio.flac`;
  } else if ($('result-audio').src) { $('result-audio').pause(); $('result-audio').removeAttribute('src'); delete $('result-audio').dataset.jobId; $('result-audio').load(); }
  notice('result-error', job?.status === 'failed' ? job.error || '生成失败，服务未提供具体原因' : job?.status === 'succeeded' && !outputURL ? '任务已完成，但没有可播放的输出地址，请检查后端结果文件' : '');
  if (job && ['failed', 'cancelled', 'canceled'].includes(job.status)) { $('empty-preview').querySelector('h3').textContent = job.status === 'failed' ? '这次生成未完成' : '任务已取消'; $('empty-preview').querySelector('p').textContent = '素材与设置仍在左侧，可调整后再次提交。'; }
  else { $('empty-preview').querySelector('h3').textContent = '第一个镜头，从这里开始'; $('empty-preview').querySelector('p').textContent = '上传参考素材、写下想法，生成结果会出现在这里。'; }
}
async function loadJobs(quiet = false) {
  const epoch = sessionEpoch;
  if (!sessionCurrent(epoch)) return;
  try {
    const response = await api('/api/jobs');
    if (!sessionCurrent(epoch)) return;
    if (!Array.isArray(response.jobs)) throw new Error('任务列表格式无效');
    state.jobs = response.jobs;
    if (state.selectedJob) { const latest = state.jobs.find((job) => job.id === state.selectedJob.id); if (latest) state.selectedJob = latest; }
    else if (state.jobs.length) state.selectedJob = state.jobs[0];
    renderHistory(); renderJob();
  } catch (error) {
    if (!sessionCurrent(epoch)) return;
    if (!state.jobs.length) { $('history-list').replaceChildren(element('p', 'history-empty', `暂时无法读取任务：${errorMessage(error)}`)); }
    if (!quiet) toast(errorMessage(error));
  }
}
async function pollJob() {
  if (!state.username || state.polling || document.hidden || !state.selectedJob || !ACTIVE_STATUSES.includes(state.selectedJob.status)) return;
  const epoch = sessionEpoch;
  state.polling = true;
  const id = state.selectedJob.id;
  try {
    const job = await api(`/api/jobs/${encodeURIComponent(id)}`);
    if (!sessionCurrent(epoch)) return;
    if (state.selectedJob?.id === id) { state.selectedJob = job; state.jobs = state.jobs.map((item) => item.id === id ? job : item); renderJob(); renderHistory(); }
  } catch { /* Keep the last known job visible through a transient connection failure. */ }
  finally { if (sessionCurrent(epoch)) state.polling = false; }
}
async function cancelJob() {
  const epoch = sessionEpoch;
  const job = state.selectedJob; if (!job || !feature('cancel')) return;
  $('cancel-job').disabled = true;
  try { const cancelled = await api(`/api/jobs/${encodeURIComponent(job.id)}/cancel`, { method: 'POST' }); if (!sessionCurrent(epoch)) return; state.selectedJob = cancelled; state.jobs = state.jobs.map((item) => item.id === cancelled.id ? cancelled : item); renderJob(); renderHistory(); }
  catch (error) { if (sessionCurrent(epoch)) toast(errorMessage(error)); }
  finally { if (sessionCurrent(epoch)) $('cancel-job').disabled = false; }
}
function reuseBusy() {
  return state.submitting || state.restoringAssets || state.reusingSettings || GROUPS.some((group) => state.assets[group].some((asset) => ['uploading', 'restoring'].includes(asset.status)));
}
async function reuseJobSettings() {
  const epoch = sessionEpoch;
  if (!sessionCurrent(epoch)) return;
  if (reuseBusy()) { toast('素材或任务正在提交，请完成后再复用设置'); return; }
  const job = state.selectedJob;
  const request = job?.request;
  const refuse = (message) => { notice('form-error', message); toast(message); };
  if (!request || typeof request !== 'object') { refuse('这条历史任务没有完整的可复用设置'); return; }
  const backendId = request.backend || job.backend;
  const modelId = request.model || job.model;
  const backend = state.capabilities?.backends.find((item) => item.id === backendId);
  const model = normalizedModels(backend).find((item) => item.id === modelId);
  if (!backend || !model) { refuse('历史任务的后端或模型与当前配置不匹配，保留当前设置。请先恢复对应服务配置。'); return; }
  if (!Array.isArray(backend.modes) || !backend.modes.includes(request.mode)) { refuse('当前后端不支持这条历史任务的模式，保留当前设置'); return; }
  const resolutions = (model.resolutions || []).map((item) => typeof item === 'string' ? item : item.id);
  const ratios = Array.from($('aspect-ratio').options, (option) => option.value);
  const seed = request.seed ?? job.seed;
  const steps = request.steps ?? 20;
  const audio = request.generate_audio ?? true;
  const validSeed = seed === null || seed === undefined || (/^\d+$/.test(String(seed)) && BigInt(String(seed)) <= 18446744073709551615n && (typeof seed !== 'number' || Number.isSafeInteger(seed)));
  if (typeof request.prompt !== 'string' || !request.prompt.trim() || request.prompt.length > 12000 || !Number.isInteger(request.duration) || request.duration < (model.min_duration || 4) || request.duration > (model.max_duration || 15) || !resolutions.includes(request.resolution) || !ratios.includes(request.aspect_ratio) || !Number.isInteger(steps) || steps < 1 || steps > 100 || !validSeed || typeof audio !== 'boolean') { refuse('历史参数与当前可用范围不匹配，保留当前设置。请核对时长、比例、分辨率、步数和种子。'); return; }
  const inputs = request.inputs;
  if (!inputs || typeof inputs !== 'object') { refuse('历史任务缺少输入素材记录，保留当前设置'); return; }
  const ids = Object.fromEntries(GROUPS.map((group) => [group, []]));
  if (request.mode === 'ref') {
    for (const group of ['images', 'videos', 'audios']) {
      if (!Array.isArray(inputs[group]) || inputs[group].some((id) => typeof id !== 'string' || !id)) { refuse('历史任务的参考素材记录无效，保留当前设置'); return; }
      ids[group] = inputs[group];
    }
    if (!ids.images.length && !ids.videos.length && !ids.audios.length) { refuse('历史全能参考任务没有任何素材记录，保留当前设置'); return; }
  } else {
    for (const group of ['first_frame', 'last_frame']) {
      if (inputs[group] !== null && inputs[group] !== undefined) {
        if (typeof inputs[group] !== 'string' || !inputs[group]) { refuse('历史首尾帧记录无效，保留当前设置'); return; }
        ids[group] = [inputs[group]];
      }
    }
  }
  state.reusingSettings = true;
  state.restoringAssets = true;
  state.pendingSubmission = null;
  notice('form-error', '');
  for (const group of GROUPS) for (const asset of state.assets[group]) if (asset.localURL) URL.revokeObjectURL(asset.previewURL);
  state.assets = Object.fromEntries(GROUPS.map((group) => [group, ids[group].map((id) => savedAsset(group, { id }))]));
  state.extraGuideAssets = [];
  state.backend = backend;
  state.model = model;
  state.mode = request.mode;
  $('backend').value = backend.id;
  updateBackend();
  $('model').value = model.id;
  $('prompt').value = request.prompt;
  $('duration').value = request.duration;
  $('resolution').value = request.resolution;
  $('aspect-ratio').value = request.aspect_ratio;
  $('generate-audio').checked = audio;
  applyControlPreferences({ ...CONTROL_DEFAULTS, ...request, steps, seed: seed === null || seed === undefined ? '' : String(seed) });
  renderAssets(); updateForm(); renderJob();
  await Promise.allSettled(GROUPS.flatMap((group) => state.assets[group].map((asset) => restoreAssetMetadata(group, asset))));
  if (!sessionCurrent(epoch)) return;
  await restoreExtraGuideAssets(state.guides.map((guide) => guide.media_id));
  if (!sessionCurrent(epoch)) return;
  state.restoringAssets = false;
  state.reusingSettings = false;
  renderAssets(); updateForm(); renderJob();
  const failed = GROUPS.flatMap((group) => state.assets[group]).filter((asset) => asset.status === 'failed');
  if (failed.length) { notice('form-error', `设置已复用，但 ${failed.length} 个历史素材无法恢复。错误卡片已保留，请移除或补充素材后再生成。`); toast('历史设置已复用，有素材需要修复'); }
  else if ($('audio-decode').value !== 'normal') { notice('form-error', '历史设置已保留，但其中的音频分块已实测不兼容。请主动改为完整解码，才能再次生成。'); toast('历史音频分块不可用，需要主动改为完整解码'); }
  else toast('历史描述、参数与素材已复用，可修改后生成新镜头');
  $('prompt').scrollIntoView({ behavior: 'smooth', block: 'center' });
}

function setAPIPanel(open) { show($('api-panel'), open); $('api-tab').classList.toggle('active', open); $('studio-tab').classList.toggle('active', !open); $('api-tab').setAttribute('aria-expanded', String(open)); if (open) $('api-panel').scrollIntoView({ behavior: 'smooth', block: 'start' }); }
function restorePreferences() {
  if (!state.username) return;
  try { const preferences = JSON.parse(localStorage.getItem(storageKey('preferences')) || '{}'); if (typeof preferences.prompt === 'string') $('prompt').value = preferences.prompt; if (Number(preferences.duration) >= 4 && Number(preferences.duration) <= 15) $('duration').value = preferences.duration; if (['16:9', '9:16', '1:1', '4:3', '3:4', '21:9'].includes(preferences.aspect_ratio)) $('aspect-ratio').value = preferences.aspect_ratio; if (typeof preferences.generate_audio === 'boolean') $('generate-audio').checked = preferences.generate_audio; state.savedPreferences = preferences; applyControlPreferences(preferences); } catch { /* An invalid saved preference is nonessential. */ }
}
async function restoreExtraGuideAssets(ids) {
  const epoch = sessionEpoch;
  if (!sessionCurrent(epoch)) return;
  const missing = [...new Set(ids)].filter((id) => typeof id === 'string' && id && !allAssets().some((asset) => asset.id === id));
  await Promise.allSettled(missing.map(async (id) => {
    const asset = { localId: crypto.randomUUID(), id, name: id, kind: 'image', size: 0, previewURL: null, localURL: false, status: 'restoring', duration: null, modelDuration: null, hasAudio: null, notes: [], error: '' };
    state.extraGuideAssets.push(asset);
    try { const uploaded = await api(`/api/uploads/${encodeURIComponent(id)}`); if (!sessionCurrent(epoch)) return; if (uploaded.id !== id || !['image', 'video', 'audio'].includes(uploaded.kind)) throw new Error('锚点素材元信息无效'); Object.assign(asset, { kind: uploaded.kind, name: uploaded.name || id, size: Number(uploaded.size) || 0, previewURL: safeMediaURL(uploaded.preview_url), modelDuration: uploaded.duration, duration: uploaded.source_duration ?? uploaded.duration, hasAudio: uploaded.has_audio, status: 'ready' }); }
    catch (error) { if (!sessionCurrent(epoch)) return; asset.status = 'failed'; asset.error = errorMessage(error); }
  }));
  if (!sessionCurrent(epoch)) return;
  renderGuides(); updateForm();
}
function initialize() {
  $('login-form').addEventListener('submit', login);
  $('logout').addEventListener('click', logout);
  for (const button of document.querySelectorAll('[data-username]')) button.addEventListener('click', () => { $('username').value = button.dataset.username; $('username').focus(); notice('login-error', ''); });
  sessionChannel?.addEventListener('message', (event) => { if (event.data?.type === 'account-invalidated') showLogin('另一个页面正在切换账户'); else if (event.data?.type === 'account-changed') { showLogin(); restoreSession(); } });
  for (const zone of document.querySelectorAll('.drop-zone')) {
    const group = zone.dataset.group;
    const input = zone.querySelector('input');
    input.addEventListener('change', () => { addFiles(group, input.files); input.value = ''; });
    zone.addEventListener('keydown', (event) => { if (event.target === zone && ['Enter', ' '].includes(event.key)) { event.preventDefault(); input.click(); } });
    zone.addEventListener('dragover', (event) => { event.preventDefault(); zone.classList.add('dragging'); });
    zone.addEventListener('dragleave', (event) => { if (!zone.contains(event.relatedTarget)) zone.classList.remove('dragging'); });
    zone.addEventListener('drop', (event) => { event.preventDefault(); zone.classList.remove('dragging'); addFiles(group, event.dataTransfer.files); });
  }
  document.addEventListener('dragover', (event) => event.preventDefault());
  document.addEventListener('drop', (event) => event.preventDefault());
  $('backend').addEventListener('change', () => { state.backend = state.capabilities?.backends.find((backend) => backend.id === $('backend').value) || null; updateBackend(); invalidateChangedSubmission(); });
  $('model').addEventListener('change', () => { state.model = normalizedModels(state.backend).find((model) => model.id === $('model').value) || null; updateModel(); invalidateChangedSubmission(); });
  for (const button of document.querySelectorAll('[data-mode]')) button.addEventListener('click', () => { state.mode = button.dataset.mode; updateMode(); invalidateChangedSubmission(); });
  for (const id of [...new Set(['prompt', 'duration', 'resolution', 'aspect-ratio', 'generate-audio', ...CONTROL_FIELDS.map((name) => name.replaceAll('_', '-'))])]) $(id).addEventListener('input', () => { if (id === 'duration') renderGuides(); invalidateChangedSubmission(); updateForm(); });
  $('add-guide').addEventListener('click', () => { if (state.guides.length >= 8) return; const asset = allAssets().find((item) => item.status === 'ready'); state.guides.push({ media_id: asset?.id || '', time_seconds: 0, use_audio: false }); renderGuides(); invalidateChangedSubmission(); updateForm(); });
  $('preview-workflow').addEventListener('click', previewWorkflow);
  const prompts = { cinematic: '夜色中的城市街头，镜头缓慢推近主角。保持人物外观一致，浅景深、自然光影，情绪克制，带电影感。', character: '人物先看向画面外，停顿片刻后转身微笑。动作连贯，表情自然，保持人物身份、服装和场景一致。', product: '产品放在简洁的展示台上，镜头缓慢环绕，突出材质、细节与轮廓。柔和棚拍光线，背景干净，保持产品外观准确。' };
  Object.assign(prompts, { camera: '镜头描述：从中景缓慢推近到面部，摄影机移动平稳，主体始终保持在画面内。可直接改写为你想要的机位与运动。', dialogue: '对白示例：主角望向镜头，用自然、平静的语气说：“终于见到你了。”请把引号内对白、说话人和语气改成你的需求。', retain: '保留参考中的人物身份与服装，动作参考视频；改变为傍晚的场景与柔和光线。请明确写出哪些细节必须保留、哪些可以改变。', sound: '声音描述：保留自然环境声，人物对白清晰可辨，背景音乐轻柔，不盖过对白。请按你的镜头改写音乐、环境声与音效。' });
  for (const button of document.querySelectorAll('[data-prompt]')) button.addEventListener('click', () => { $('prompt').value = `${$('prompt').value.trim()}${$('prompt').value.trim() ? '\n\n' : ''}${prompts[button.dataset.prompt]}`; $('prompt').focus(); invalidateChangedSubmission(); updateForm(); });
  $('generate').addEventListener('click', submitJob);
  $('refresh-capabilities').addEventListener('click', loadCapabilities);
  $('refresh-jobs').addEventListener('click', () => loadJobs());
  $('cancel-job').addEventListener('click', cancelJob);
  $('reuse-job').addEventListener('click', reuseJobSettings);
  $('api-tab').addEventListener('click', () => setAPIPanel($('api-panel').hidden));
  $('studio-tab').addEventListener('click', () => setAPIPanel(false));
  $('close-api').addEventListener('click', () => setAPIPanel(false));
  $('copy-api').addEventListener('click', async () => { const epoch = sessionEpoch; try { await navigator.clipboard.writeText($('api-example').textContent); if (sessionCurrent(epoch)) toast('请求 JSON 已复制'); } catch { if (sessionCurrent(epoch)) toast('浏览器禁止剪贴板操作，请选中示例手动复制'); } });
  restoreSession();
  setInterval(pollJob, 3000);
  setInterval(renderLease, 30000);
  setInterval(() => { if (state.username && !document.hidden && !state.submitting) loadCapabilities(); }, 60000);
  setInterval(() => { if (state.username && !document.hidden && !state.submitting) loadJobs(true); }, 20000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden && state.username) { pollJob(); loadJobs(true); loadCapabilities(); } });
}
initialize();
