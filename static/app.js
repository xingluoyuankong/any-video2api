/* ============================================================================
 * any-video2api 控制台 · 原生 JS 单页应用（零构建）
 * 路由：hash  鉴权：X-API-Key（localStorage['u2a.key']）
 * ========================================================================== */
'use strict';

/* ---------------------------------------------------------------- 基础工具 */
const $  = (sel, root) => (root || document).querySelector(sel);
const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

const LS_KEY = 'u2a.key';
const LS_THEME = 'u2a.theme';

function esc(v) {
  return String(v == null ? '' : v).replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

function fmtNum(n) {
  if (n == null || n === '') return '—';
  const x = Number(n);
  if (!isFinite(x)) return '—';
  return x.toLocaleString('en-US');
}

function fmtAmt(n) {
  if (n == null || n === '') return '—';
  const x = Number(n);
  if (!isFinite(x)) return '—';
  return x.toLocaleString('en-US', { maximumFractionDigits: 2 });
}

function relTime(ts) {
  if (ts == null || ts === '') return '—';
  const t = Number(ts);
  if (!isFinite(t) || t <= 0) return '—';
  const d = Date.now() / 1000 - t;
  const a = Math.abs(d);
  let s;
  if (a < 45) return d >= 0 ? '刚刚' : '即将';
  if (a < 3600) s = Math.round(a / 60) + '分钟';
  else if (a < 86400) s = Math.round(a / 3600) + '小时';
  else if (a < 2592000) s = Math.round(a / 86400) + '天';
  else return new Date(t * 1000).toLocaleDateString('zh-CN');
  return d >= 0 ? s + '前' : s + '后';
}

function fmtTime(ts) {
  if (ts == null || ts === '') return '—';
  const t = Number(ts);
  if (!isFinite(t) || t <= 0) return '—';
  return new Date(t * 1000).toLocaleString('zh-CN', { hour12: false });
}

function pct(v) {
  const x = Number(v);
  if (!isFinite(x)) return '—';
  return (x * 100).toFixed(1) + '%';
}

/* 成功率：无样本返回 null（渲染为 —），避免 0/0 显示 0% */
function rateOf(ok, total) {
  const t = Number(total) || 0;
  if (t <= 0) return null;
  return (Number(ok) || 0) / t;
}

function shortDate(s) {
  const m = String(s || '').match(/^(\d{4})-(\d{2})-(\d{2})/);
  return m ? (m[2] + '-' + m[3]) : String(s || '—');
}

/* 统计卡：value 为字符串时原样展示（如百分比），否则千分位 */
function statCard(label, value, cls) {
  const v = (typeof value === 'string') ? esc(value) : fmtNum(value);
  return '<div class="stat ' + (cls || '') + '">' +
    '<div class="s-val">' + v + '</div>' +
    '<div class="s-lbl">' + esc(label) + '</div></div>';
}

/* 横向柱状图：rows = [{label,value,title?,rate?,cls?}]，纯 div 宽度百分比 */
function barList(rows, opts) {
  opts = opts || {};
  if (!rows || !rows.length) return emptyHtml(opts.emptyTitle || '暂无数据', opts.emptySub || '');
  const max = Math.max.apply(null, rows.map(r => Number(r.value) || 0).concat([1]));
  const cls = opts.showPct ? ' has-pct' : '';
  return '<div class="bar-list">' + rows.map(r => {
    const v = Number(r.value) || 0;
    const w = Math.max(v > 0 ? 2 : 0, Math.min(100, v / max * 100));
    const pctHtml = (opts.showPct)
      ? '<div class="bar-pct">' + (r.rate == null ? '—' : pct(r.rate)) + '</div>' : '';
    return '<div class="bar-row' + cls + '">' +
      '<div class="bar-name" title="' + esc(r.title || r.label) + '">' + esc(r.label) + '</div>' +
      '<div class="bar-track"><span class="bar-fill ' + (r.cls || '') + '" style="width:' + w.toFixed(2) + '%"></span></div>' +
      '<div class="bar-val">' + fmtNum(v) + '</div>' + pctHtml + '</div>';
  }).join('') + '</div>';
}

/* ---------------------------------------------------------------- 常量表 */
const CH_META = {
  jimeng: { label: '即梦', glyph: '即' },
  tiktok: { label: 'TikTok', glyph: 'TT' },
  coze:   { label: '扣子', glyph: '扣' },
  doubao: { label: '豆包', glyph: '豆' },
  hailuo: { label: '海螺', glyph: '海' },
  kling:  { label: '可灵', glyph: '可' },
  vidu:   { label: 'Vidu', glyph: 'V' },
};
/* 渠道专属色（key 与 CH_META 对应；未登记渠道走 other 灰） */
const CH_COLOR = {
  jimeng: '#5b7cfa', tiktok: '#25d3d3', coze: '#a06bff', doubao: '#4c8bf5',
  hailuo: '#ff6b9d', kling: '#00c48c', vidu: '#ff9f43',
};
function chColor(name) { return CH_COLOR[name] || 'var(--ink-3)'; }
function chGlyph(name) {
  return (CH_META[name] && CH_META[name].glyph) ||
    (name || '?').slice(0, 2).toUpperCase();
}
const CAP_LABELS = {
  video: '视频', image: '图片', upload: '上传', checkin: '签到',
  oauth: 'OAuth', credits: '额度', models: '模型', verify: '验证',
};
const STATUS_META = {
  active:    { cls: 'st-ok',   label: '活跃' },
  unknown:   { cls: 'st-idle', label: '未验证' },
  invalid:   { cls: 'st-bad',  label: '失效' },
  expired:   { cls: 'st-bad',  label: '已过期' },
  risk:      { cls: 'st-warn', label: '风控' },
  cooldown:  { cls: 'st-warn', label: '冷却' },
  healthy:   { cls: 'st-ok',   label: '健康' },
  expiring:  { cls: 'st-warn', label: '临近过期' },
  reuse:     { cls: 'st-ok',   label: '可复用' },
  refresh:   { cls: 'st-warn', label: '需刷新' },
  relogin:   { cls: 'st-bad',  label: '需重登' },
  queued:    { cls: 'st-idle', label: '排队' },
  submitted: { cls: 'st-warn', label: '已提交' },
  processing:{ cls: 'st-warn', label: '处理中' },
  success:   { cls: 'st-ok',   label: '成功' },
  failed:    { cls: 'st-bad',  label: '失败' },
  cancelled: { cls: 'st-idle', label: '已取消' },
  ok:        { cls: 'st-ok',   label: '可用' },
  cooling:   { cls: 'st-warn', label: '冷却' },
  dead:      { cls: 'st-bad',  label: '不可用' },
  unbound:   { cls: 'st-idle', label: '未绑定' },
  enabled:   { cls: 'st-ok',   label: '启用' },
  disabled:  { cls: 'st-idle', label: '禁用' },
};

function badge(status, overrideLabel) {
  const m = STATUS_META[status] || { cls: 'st-idle', label: status || '—' };
  return '<span class="st-badge ' + m.cls + '">' + esc(overrideLabel || m.label) + '</span>';
}
function markCls(status) {
  const m = STATUS_META[status];
  return m ? m.cls.replace('st-', '') : 'idle';
}
function chLabel(name) {
  const c = state.channels.find(x => x.name === name);
  return (c && c.display_name) || (CH_META[name] && CH_META[name].label) || name || '—';
}
function chChip(name) {
  const key = CH_META[name] ? name : 'other';
  return '<span class="chip ch-' + key + '"><span class="cdot"></span>' + esc(chLabel(name)) + '</span>';
}

/* ---------------------------------------------------------------- 全局状态 */
const ROUTES = ['overview', 'studio', 'accounts', 'proxies', 'jobs', 'keys', 'logs', 'usage', 'models', 'config'];
const TITLES = {
  overview: ['概览', 'overview'], studio: ['工作台', 'studio'],
  accounts: ['账号管理', 'accounts'],
  proxies: ['代理出口', 'proxies'], jobs: ['任务', 'jobs'],
  keys: ['密钥', 'keys'], logs: ['日志', 'logs'],
  usage: ['用量统计', 'usage'], models: ['模型与档位', 'models'], config: ['配置', 'config'],
};

const state = {
  key: '',            // 已弃用：控制台改用账号密码会话（保留占位以兼容旧代码）
  user: null,         // 当前登录用户 {username, role}
  theme: document.documentElement.getAttribute('data-theme') || 'light',
  route: '',
  channels: [],
  loadErrors: {},
  accounts: [],
  sessionMap: {},
  proxyMap: {},
  acctChannel: '',
  collapsed: {},
  proxy: { rows: [], stats: null, pools: [], available: true, error: '' },
  pxChannel: '',
  jobs: [],
  jobStatus: '',
  jobChannel: '',
  keys: [],
  logs: [],
  logChannel: '',
  logLimit: 200,
  logTab: 'all',
  logRuntime: null,
  usage: { days: 7, channel: '' },
  models: { search: '', probeChannel: '', data: null, specs: {}, liveHtml: '' },
  config: { data: null, channels: null, channelsError: '', openChannel: '' },
};

/* ---------------------------------------------------------------- 连接状态 */
function setConn(ok, text) {
  const el = $('#conn');
  el.classList.toggle('ok', ok === true);
  el.classList.toggle('bad', ok === false);
  $('#connText').textContent = text || (ok === true ? '已连接' : ok === false ? '连接失败' : '未连接');
}

/* ---------------------------------------------------------------- Toast */
function toast(msg, kind) {
  kind = kind || 'info';
  const ico = { ok: '✓', bad: '✕', warn: '!', info: 'i' }[kind] || 'i';
  const node = document.createElement('div');
  node.className = 'toast ' + kind;
  node.innerHTML = '<span class="t-ico">' + ico + '</span><span class="t-msg">' + esc(msg) + '</span>' +
    '<button class="t-x" aria-label="close">×</button>';
  const close = () => { node.style.opacity = '0'; setTimeout(() => node.remove(), 160); };
  node.querySelector('.t-x').addEventListener('click', close);
  $('#toasts').appendChild(node);
  setTimeout(close, kind === 'bad' ? 6500 : 3800);
}

/* ---------------------------------------------------------------- 弹窗 */
function openModal(id) {
  const m = document.getElementById(id);
  if (!m) return;
  m.hidden = false;
  requestAnimationFrame(() => m.classList.add('open'));
}
function closeModal(id) {
  const m = document.getElementById(id);
  if (!m) return;
  m.classList.remove('open');
  setTimeout(() => { m.hidden = true; }, 160);
  // 确认框被直接关闭（遮罩/Esc/×）时视为取消，避免 Promise 悬挂
  if (id === 'm-confirm' && confirmResolve) {
    const fn = confirmResolve; confirmResolve = null; fn(false);
  }
}

let confirmResolve = null;
function confirmDialog(msg, opts) {
  opts = opts || {};
  return new Promise(resolve => {
    confirmResolve = resolve;
    $('#confirmMsg').innerHTML = msg;
    $('#confirmYes').textContent = opts.yes || '确认删除';
    $('#confirmNo').textContent = opts.no || '取消';
    openModal('m-confirm');
  });
}
function resolveConfirm(v) {
  const fn = confirmResolve; confirmResolve = null;
  closeModal('m-confirm');
  if (fn) fn(v);
}

/* ---------------------------------------------------------------- API */
async function api(path, opts) {
  opts = opts || {};
  const headers = {};
  let body;
  if (opts.form) {
    body = opts.form;
  } else if (opts.body !== undefined) {
    headers['Content-Type'] = 'application/json';
    body = JSON.stringify(opts.body);
  }
  let res;
  try {
    res = await fetch(path, { method: opts.method || 'GET', headers, body, credentials: 'same-origin' });
  } catch (e) {
    setConn(false, '网络错误');
    const err = new Error('网络错误：' + e.message);
    err.status = 0;
    throw err;
  }
  const text = await res.text();
  let data = null;
  if (text) { try { data = JSON.parse(text); } catch (_) { data = text; } }

  if (res.status === 401) {
    setConn(false, '未登录');
    if (!/^\/api\/auth\//.test(path)) showLogin('登录已过期，请重新登录。');
    const err = new Error('未登录（401）');
    err.status = 401;
    throw err;
  }
  if (!res.ok) {
    let detail = res.status + ' ' + res.statusText;
    if (data && data.detail) {
      detail = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail);
    }
    const err = new Error(detail);
    err.status = res.status;
    throw err;
  }
  setConn(true, '已连接');
  return data;
}

/* 容错请求：不抛异常，返回 {ok,data,error,status} */
async function safe(promise) {
  try { return { ok: true, data: await promise }; }
  catch (e) { return { ok: false, error: e.message, status: e.status }; }
}

/* ---------------------------------------------------------------- 空态/加载 */
function loadingHtml(text) {
  return '<div class="loading"><div class="spinner"></div>' + esc(text || '加载中…') + '</div>';
}
function emptyHtml(title, sub, actionHtml) {
  return '<div class="empty"><div class="e-ico">◌</div><div class="e-t">' + esc(title) + '</div>' +
    (sub ? '<div class="e-s">' + esc(sub) + '</div>' : '') + (actionHtml || '') + '</div>';
}
function errorHtml(msg) {
  return '<div class="notice bad">加载失败：' + esc(msg) + '</div>';
}
function needKeyHtml() {
  return emptyHtml('尚未配置 API Key', '请点击右上角「密钥」输入后再试。',
    '<button class="btn primary" data-act="open-key">输入密钥</button>');
}

/* ---------------------------------------------------------------- 路由 */
function currentRoute() {
  const h = location.hash.replace(/^#\/?/, '').split('?')[0];
  return ROUTES.includes(h) ? h : 'overview';
}

function navigate() {
  const r = currentRoute();
  state.route = r;
  $$('.nav a').forEach(a => a.classList.toggle('active', a.dataset.nav === r));
  $$('.view').forEach(v => v.classList.toggle('active', v.dataset.view === r));
  const t = TITLES[r];
  $('#pageTitle').textContent = t[0];
  $('#pageCrumbs').textContent = 'any-video2api / ' + t[1];
  renderRoute();
}

function renderRoute() {
  switch (state.route) {
    case 'overview': loadOverview(); break;
    case 'studio': loadStudio(); break;
    case 'accounts': loadAccounts(); break;
    case 'proxies':  loadProxies(); break;
    case 'jobs':     loadJobs(); break;
    case 'keys':     loadKeys(); break;
    case 'logs':     loadLogs(); break;
    case 'usage':    loadUsage(); break;
    case 'models':   loadModels(); break;
    case 'config':   loadConfig(); break;
  }
}

/* ---------------------------------------------------------------- 渠道元信息 */
async function loadChannels() {
  const r = await safe(api('/api/channels'));
  if (r.ok && r.data) {
    state.channels = r.data.channels || [];
    state.loadErrors = r.data.load_errors || {};
    fillChannelSelects();
  }
}

function fillChannelSelects() {
  const opts = state.channels
    .filter(c => !c.error)
    .map(c => '<option value="' + esc(c.name) + '">' + esc(c.display_name || c.name) + '</option>')
    .join('');
  // 每个下拉的占位项：'全部渠道' 表示过滤用，'选择渠道' 表示必须显式选择，'' 表示无占位
  const meta = {
    '#acctChannel': '全部渠道', '#pxChannelFilter': '全部渠道', '#jobChannel': '全部渠道',
    '#logChannel': '全部渠道', '#usageChannel': '全部渠道',
    '#impChannel': '', '#modelProbeChannel': '选择渠道',
  };
  Object.keys(meta).forEach(sel => {
    const el = $(sel);
    if (!el) return;
    const keep = el.value;
    const first = meta[sel] ? '<option value="">' + meta[sel] + '</option>' : '';
    el.innerHTML = first + opts;
    if (keep && el.querySelector('option[value="' + keep + '"]')) el.value = keep;
  });
}

/* ============================================================================
 * 视图 1 · 概览
 * ========================================================================== */
async function loadOverview() {
  const view = $('#body-overview');
  view.innerHTML = loadingHtml();
  // 逐项容错：新增的 /api/stats/overview 未实现时不应拖垮概览
  const [hR, sR, stR] = await Promise.all([
    safe(api('/health')),
    safe(api('/api/summary')),
    safe(api('/api/stats/overview')),
  ]);
  if (!hR.ok && !sR.ok) { view.innerHTML = errorHtml(hR.error || sR.error); toast('概览加载失败：' + (hR.error || sR.error), 'bad'); return; }
  const health = hR.ok ? (hR.data || {}) : {};
  const summary = sR.ok ? (sR.data || {}) : {};
  const stats = stR.ok ? (stR.data || {}) : null;
  if (summary.channels && summary.channels.length) {
    state.channels = summary.channels;
    fillChannelSelects();
  }
  view.innerHTML = overviewHtml(health, summary, stats);
  updateNavCounts();
}

function overviewHtml(health, summary, stats) {
  const channels = summary.channels || state.channels || [];
  const acctMap = health.accounts || summary.accounts || {};
  const jobsByStatus = summary.jobs_by_status || {};

  // /api/stats/overview 提供按渠道的账号/任务/模型聚合，缺失时退回 summary 计算
  const stChans = {};
  ((stats && stats.channels) || []).forEach(c => { stChans[c.name] = c; });
  const totals = (stats && stats.totals) || null;

  let total = 0, active = 0, cooling = 0, bad = 0;
  Object.keys(acctMap).forEach(ch => {
    Object.keys(acctMap[ch] || {}).forEach(st => {
      const n = Number(acctMap[ch][st]) || 0;
      total += n;
      if (st === 'active') active += n;
      else if (st === 'cooldown') cooling += n;
      else if (st === 'invalid' || st === 'expired' || st === 'risk') bad += n;
    });
  });
  if (totals) {
    if (totals.accounts != null) total = Number(totals.accounts) || 0;
    if (totals.active != null) active = Number(totals.active) || 0;
    if (totals.cooling != null) cooling = Number(totals.cooling) || 0;
  }

  const chCount = channels.filter(c => !c.error).length;
  const jobsTotal = totals ? totals.jobs_total : summary.jobs_total;
  const jobsOk = totals ? totals.jobs_success : (Number(jobsByStatus.success) || 0);
  const jobsBad = totals ? totals.jobs_failed : (Number(jobsByStatus.failed) || 0);
  const pending = health.pending_jobs != null ? health.pending_jobs : (totals ? totals.jobs_running : null);

  const chCards = channels.map(c => {
    if (c.error) {
      return '<div class="card ch-card"><div class="ch-top"><div class="ch-glyph" style="background:var(--bad)">!</div>' +
        '<div><div class="ch-name">' + esc(c.name) + '</div><div class="ch-key">加载失败</div></div></div>' +
        '<div class="hint">' + esc(c.error) + '</div></div>';
    }
    const meta = CH_META[c.name] || { glyph: (c.name || '?').slice(0, 2).toUpperCase() };
    const caps = Object.keys(c.capabilities || {}).filter(k => c.capabilities[k])
      .map(k => '<span class="chip">' + esc(CAP_LABELS[k] || k) + '</span>').join('');
    const st = stChans[c.name] || {};
    return '<div class="card ch-card">' +
      '<div class="ch-top"><div class="ch-glyph">' + esc(meta.glyph) + '</div>' +
      '<div><div class="ch-name">' + esc(c.display_name || c.name) + '</div>' +
      '<div class="ch-key">' + esc(c.name) + ' · ' + esc(c.kind || '') + '</div></div></div>' +
      '<div class="ch-metrics">' +
      '<div class="metric"><div class="m-val">' + fmtNum(st.accounts != null ? st.accounts : c.account_count) + '</div><div class="m-lbl">账号数</div></div>' +
      '<div class="metric"><div class="m-val">' + fmtNum(st.jobs_total) + '</div><div class="m-lbl">任务数</div></div>' +
      '<div class="metric"><div class="m-val">' + fmtNum(st.models != null ? st.models : c.model_count) + '</div><div class="m-lbl">模型数</div></div>' +
      '</div><div class="caps">' + (caps || '<span class="muted" style="font-size:12px">无能力声明</span>') + '</div></div>';
  }).join('');

  return '' +
    '<div class="section-title">总览</div>' +
    '<div class="stat-row">' +
      statCard('渠道数', chCount) +
      statCard('账号总数', total) +
      statCard('活跃', active, 'ok') +
      statCard('冷却', cooling, 'warn') +
      statCard('任务总数', jobsTotal) +
      statCard('成功', jobsOk, 'ok') +
      statCard('失败', jobsBad, 'bad') +
      statCard('待处理', pending) +
    '</div>' +

    '<div class="section-title">渠道</div>' +
    '<div class="grid cols-3">' + (chCards || emptyHtml('无已注册渠道', '检查 config/channels/*.json')) + '</div>' +

    '<div class="section-title">运行状态</div>' +
    '<div class="card"><div class="kv">' +
      '<dt>服务状态</dt><dd>' + esc(health.status || '—') + '</dd>' +
      '<dt>渠道</dt><dd>' + esc((health.channels || []).join(', ') || '—') + '</dd>' +
      '<dt>渠道错误</dt><dd>' + esc(Object.keys(health.channel_errors || {}).join(', ') || '无') + '</dd>' +
      '<dt>生成时间</dt><dd>' + esc(stats && stats.generated_at ? fmtTime(stats.generated_at) : '—') + '</dd>' +
    '</div></div>';
}

/* ============================================================================
 * 视图 2 · 账号管理
 * ========================================================================== */
/* ==========================================================================
 * 视图 1b · 工作台（模型测试与创作）
 * ========================================================================== */
async function loadStudio() {
  const view = $('#body-studio');
  if (!state.channels.length) {
    const r = await safe(api('/api/channels'));
    if (r.ok) { state.channels = r.data.channels || []; fillChannelSelects(); }
  }
  view.innerHTML = studioHtml();
  bindStudio();
  await studioLoadModels();
  studioLoadAccounts();
  studioLoadJobs();
}

function studioHtml() {
  const chOpts = state.channels.filter(c => !c.error)
    .map(c => '<option value="' + esc(c.name) + '">' + esc(c.display_name || c.name) + '</option>').join('');
  return '' +
    '<div class="section-title">工作台 · 模型测试与创作</div>' +
    '<div class="card" style="padding:16px">' +
      '<div class="inline" style="gap:12px;flex-wrap:wrap">' +
        '<label class="field" style="flex:1;min-width:150px;margin:0"><span>渠道</span>' +
          '<select id="stChannel">' + chOpts + '</select></label>' +
        '<label class="field" style="flex:1;min-width:150px;margin:0"><span>模型</span>' +
          '<select id="stModel"><option value="">加载中…</option></select></label>' +
        '<label class="field" style="width:104px;margin:0"><span>类型</span>' +
          '<select id="stKind"><option value="video">视频</option><option value="image">图片</option></select></label>' +
      '</div>' +
      '<label class="field"><span>提示词 (prompt)</span>' +
        '<textarea id="stPrompt" rows="4" placeholder="描述你想要的画面…"></textarea></label>' +
      '<div class="inline" style="gap:12px;flex-wrap:wrap">' +
        '<label class="field" style="width:104px;margin:0"><span>时长(秒)</span>' +
          '<input type="number" id="stDuration" value="5" min="1" max="60"></label>' +
        '<label class="field" style="width:110px;margin:0"><span>比例</span>' +
          '<select id="stRatio"><option>16:9</option><option>9:16</option><option>1:1</option><option>4:3</option><option>3:4</option></select></label>' +
        '<label class="field" style="width:110px;margin:0"><span>分辨率</span>' +
          '<select id="stRes"><option>720p</option><option>1080p</option><option>480p</option></select></label>' +
        '<label class="field" style="flex:1;min-width:170px;margin:0"><span>账号（留空自动选择）</span>' +
          '<select id="stAccount"><option value="">自动选择</option></select></label>' +
      '</div>' +
      '<div class="inline" style="gap:10px;margin-top:4px;align-items:center">' +
        '<button class="btn primary" id="stSubmit">提交任务</button>' +
        '<button class="btn ghost" id="stRefresh">刷新结果</button>' +
        '<span class="muted" id="stHint" style="font-size:12px"></span>' +
      '</div>' +
    '</div>' +
    '<div class="section-title">最近任务</div>' +
    '<div id="stJobs"><div class="loading"><div class="spinner"></div>加载中…</div></div>';
}

function bindStudio() {
  $('#stChannel').addEventListener('change', () => { studioLoadModels(); studioLoadAccounts(); });
  $('#stKind').addEventListener('change', () => studioLoadModels());
  $('#stSubmit').addEventListener('click', studioSubmit);
  $('#stRefresh').addEventListener('click', studioLoadJobs);
}

async function studioLoadModels() {
  const ch = $('#stChannel').value;
  const kind = $('#stKind').value;
  const sel = $('#stModel');
  if (!ch) { sel.innerHTML = '<option value="">无渠道</option>'; return; }
  sel.innerHTML = '<option value="">加载中…</option>';
  const r = await safe(api('/api/models?channel=' + encodeURIComponent(ch)));
  if (!r.ok) { sel.innerHTML = '<option value="">加载失败</option>'; return; }
  const d = r.data || {};
  let models = d.models || (d.by_channel && d.by_channel[ch]) || [];
  if (kind) models = models.filter(m => !m.kind || m.kind === kind || kind === 'video');
  if (!models.length) { sel.innerHTML = '<option value="">该渠道无模型</option>'; return; }
  sel.innerHTML = models.map(m => {
    const id = m.alias || m.id || m.upstream_id || '';
    const label = (m.display_name || id) + (m.price ? ' · ' + m.price : '');
    return '<option value="' + esc(id) + '">' + esc(label) + '</option>';
  }).join('');
}

async function studioLoadAccounts() {
  const ch = $('#stChannel').value;
  const sel = $('#stAccount');
  if (!sel) return;
  const r = await safe(api('/api/accounts?channel=' + encodeURIComponent(ch)));
  const list = (r.ok && r.data.accounts) ? r.data.accounts : [];
  sel.innerHTML = '<option value="">自动选择</option>' + list.map(a =>
    '<option value="' + esc(a.id) + '">' + esc(a.name || a.id) + ' · ' + esc(a.status || '') + '</option>').join('');
}

async function studioSubmit() {
  const ch = $('#stChannel').value;
  const model = $('#stModel').value;
  const prompt = $('#stPrompt').value.trim();
  if (!ch) { toast('请选择渠道', 'warn'); return; }
  if (!prompt) { toast('请填写提示词', 'warn'); return; }
  const btn = $('#stSubmit');
  btn.disabled = true; btn.textContent = '提交中…';
  const body = {
    channel: ch, kind: $('#stKind').value, model: model, prompt: prompt,
    duration: Number($('#stDuration').value) || 5,
    ratio: $('#stRatio').value, resolution: $('#stRes').value,
    account_id: $('#stAccount').value || null,
  };
  const r = await safe(api('/api/jobs', { method: 'POST', body }));
  btn.disabled = false; btn.textContent = '提交任务';
  if (r.ok) {
    toast('已提交：' + (r.data.id || ''), 'ok');
    $('#stHint').textContent = '任务 ' + (r.data.id || '') + ' 已入队，正在轮询…';
    studioLoadJobs();
  } else {
    toast('提交失败：' + r.error, 'bad');
  }
}

async function studioLoadJobs() {
  const box = $('#stJobs');
  if (!box) return;
  const r = await safe(api('/api/jobs?limit=20'));
  if (!r.ok) { box.innerHTML = errorHtml(r.error); return; }
  const jobs = (r.data && r.data.jobs) || [];
  if (!jobs.length) { box.innerHTML = emptyHtml('暂无任务', '在上方提交一个试试'); return; }
  const rows = jobs.map(j => {
    const st = j.status || '';
    const cls = /success|done/.test(st) ? 'ok' : /fail|error/.test(st) ? 'bad' : 'warn';
    const url = j.result_url || j.video_url || j.url || '';
    return '<tr>' +
      '<td><span class="cell-main">' + esc(j.id || '') + '</span></td>' +
      '<td>' + esc(j.channel || '') + '</td>' +
      '<td>' + esc(j.model || '') + '</td>' +
      '<td><span class="badge ' + cls + '">' + esc(st) + '</span></td>' +
      '<td>' + (url ? '<a href="' + esc(url) + '" target="_blank" rel="noopener">打开</a>' : '<span class="muted">—</span>') + '</td>' +
      '<td class="muted" style="font-size:12px">' + esc(fmtTime(j.created_at)) + '</td>' +
      '</tr>';
  }).join('');
  box.innerHTML = '<div class="table-wrap"><div class="table-scroll"><table class="acc">' +
    '<thead><tr><th>任务 ID</th><th>渠道</th><th>模型</th><th>状态</th><th>结果</th><th>创建时间</th></tr></thead>' +
    '<tbody>' + rows + '</tbody></table></div></div>';
}

async function loadAccounts() {
  const box = $('#body-accounts');
  const tabs = $('#acctTabs');
  box.innerHTML = loadingHtml();
  // 始终拉全部账号，前端按渠道标签过滤（保证各标签计数准确）
  const [accR, sessR, pxR] = await Promise.all([
    safe(api('/api/accounts')),
    safe(api('/api/sessions')),
    safe(api('/api/proxy/accounts')),
  ]);
  if (!accR.ok) { box.innerHTML = errorHtml(accR.error); toast('账号加载失败：' + accR.error, 'bad'); return; }
  state.accounts = (accR.data && accR.data.accounts) || [];
  state.sessionMap = {};
  const sess = sessR.ok ? (sessR.data || {}) : {};
  (sess.accounts || []).forEach(h => { state.sessionMap[h.account_id] = h; });
  state.proxyMap = {};
  const px = pxR.ok ? (pxR.data || {}) : {};
  (px.accounts || []).forEach(p => { state.proxyMap[p.account_id] = p; });
  if (tabs) tabs.innerHTML = acctTabsHtml();
  renderAcctBody();
  updateNavCounts();
}

/* 渠道标签页：全部 / 各渠道，带账号数与冷却提示 */
function acctTabsHtml() {
  const counts = {}, cooling = {};
  state.accounts.forEach(a => {
    counts[a.channel] = (counts[a.channel] || 0) + 1;
    if (a.cooling) cooling[a.channel] = (cooling[a.channel] || 0) + 1;
  });
  const names = state.channels.filter(c => !c.error).map(c => c.name);
  Object.keys(counts).forEach(ch => { if (names.indexOf(ch) < 0) names.push(ch); });
  const cur = state.acctChannel || '';
  const mk = (ch, label, n, cool, key) =>
    '<button class="ch-tab' + (key ? ' ch-' + key : '') + (cur === ch ? ' active' : '') + '"' +
      ' data-act="acct-tab" data-ch="' + esc(ch) + '">' +
      '<span class="cdot"></span>' + esc(label) +
      '<span class="t-n">' + n + '</span>' +
      (cool ? '<span class="t-warn" title="冷却中">●' + cool + '</span>' : '') +
    '</button>';
  let html = mk('', '全部', state.accounts.length, 0, '');
  names.forEach(name => {
    const key = CH_META[name] ? name : 'other';
    html += mk(name, chLabel(name), counts[name] || 0, cooling[name] || 0, key);
  });
  return html;
}

/* 按当前标签渲染账号表 */
function renderAcctBody() {
  const box = $('#body-accounts');
  const cur = state.acctChannel || '';
  const list = cur ? state.accounts.filter(a => a.channel === cur) : state.accounts;
  const scope = $('#acctScope');
  if (scope) scope.textContent = (cur ? chLabel(cur) : '全部渠道') + ' · ' + list.length + ' 个账号';
  box.innerHTML = accountsHtml(list);
}

function accountsHtml(accounts) {
  const cur = state.acctChannel || '';
  if (!accounts.length) {
    return '<div class="table-wrap">' + emptyHtml(
      cur ? (chLabel(cur) + ' 暂无账号') : '暂无账号',
      '点击「导入账号」添加 Cookie 或上传凭据文件。',
      '<button class="btn primary" data-act="import-open">导入账号</button>') + '</div>';
  }
  return '<div class="table-wrap">' + accountTable(accounts) + '</div>';
}

function accountTable(list) {
  const rows = list.map(a => {
    const sess = state.sessionMap[a.id];
    const credit = a.credit || {};
    const hasCredit = a.credit && (a.credit.total != null);
    const creditCell = hasCredit
      ? '<span class="num">' + fmtAmt(credit.total) + '</span>' +
        '<span class="muted" style="font-size:11px"> / ' + fmtAmt(credit.allocatable) + '</span>'
      : '<span class="muted">—</span>';
    const sessCell = sess
      ? badge(sess.action, (STATUS_META[sess.action] && STATUS_META[sess.action].label) || sess.action)
      : '<span class="muted">—</span>';
    const sessTitle = sess
      ? ('最近成功 ' + relTime(sess.last_ok_at) + '，连续失败 ' + (sess.consecutive_failures || 0) +
         '，成功/失败 ' + (sess.total_ok || 0) + '/' + (sess.total_fail || 0))
      : '无会话记录';
    const sfCell = sess
      ? '<span class="num" style="color:var(--ok)">' + fmtNum(sess.total_ok || 0) + '</span>' +
        '<span class="muted"> / </span>' +
        '<span class="num" style="color:var(--bad)">' + fmtNum(sess.total_fail || 0) + '</span>'
      : '<span class="muted">—</span>';

    // 出口 IP：来自 /api/proxy/accounts 的实测/声明 IP
    const px = state.proxyMap[a.id] || {};
    const actual = px.actual_ip || '';
    const expected = px.expected_ip || '';
    const mismatch = actual && expected && actual !== expected;
    let ipCell;
    if (actual) {
      ipCell = '<span class="mono"' + (mismatch ? ' style="color:var(--warn)"' : '') + '>' + esc(actual) +
        (mismatch ? ' ⚠' : '') + '</span>' +
        (expected ? '<div class="cell-sub">期望 ' + esc(expected) + '</div>' : '');
    } else if (expected) {
      ipCell = '<span class="mono muted">' + esc(expected) + '</span><div class="cell-sub">未测</div>';
    } else {
      ipCell = '<span class="muted">直连</span>';
    }

    const proxy = a.proxy || '';
    const at = (a.account_type || '').toLowerCase();
    const atLabel = at === 'vip' ? '会员' : at === 'member' ? '会员' : at === 'free' ? 'Free'
      : at ? at : '—';
    const atCls = at === 'vip' || at === 'member' ? 'ok' : at === 'free' ? 'warn' : 'idle';
    return '<tr data-id="' + esc(a.id) + '">' +
      '<td class="mark ' + markCls(a.status) + '"></td>' +
      '<td><div class="cell-main">' + esc(a.name || a.id) + '</div>' +
        '<div class="cell-sub">' + esc(a.id) + '</div></td>' +
      '<td>' + chChip(a.channel) + '</td>' +
      '<td>' + badge(a.status) + (a.cooling ? ' ' + badge('cooling', '冷却中') : '') + '</td>' +
      '<td><span class="badge ' + atCls + '">' + esc(atLabel) + '</span></td>' +
      '<td class="nowrap">' + creditCell + '</td>' +
      '<td title="' + esc(sessTitle) + '">' + sessCell + '</td>' +
      '<td class="nowrap">' + sfCell + '</td>' +
      '<td class="nowrap">' + ipCell + '</td>' +
      '<td class="mono ellipsis" title="' + esc(proxy) + '">' + (proxy ? esc(proxy) : '<span class="muted">直连</span>') + '</td>' +
      '<td class="row-actions"><div class="ops">' +
        '<button class="btn xs" data-act="acct-verify" data-id="' + esc(a.id) + '">检查</button>' +
        '<button class="btn xs" data-act="acct-checkin" data-id="' + esc(a.id) + '">签到</button>' +
        '<button class="btn xs" data-act="acct-edit" data-id="' + esc(a.id) + '">编辑</button>' +
        '<button class="btn xs" data-act="acct-backup" data-id="' + esc(a.id) + '">备份</button>' +
        '<button class="btn xs danger" data-act="acct-del" data-id="' + esc(a.id) + '">删除</button>' +
      '</div></td></tr>';
  }).join('');

  return '<div class="table-scroll"><table class="acc"><thead><tr>' +
    '<th class="mark"></th><th>账号</th><th>渠道</th><th>状态</th><th>类型</th><th>额度</th>' +
    '<th>会话健康</th><th>成功/失败</th><th>出口 IP</th><th>代理</th><th class="row-actions">操作</th>' +
    '</tr></thead><tbody>' + rows + '</tbody></table></div>';
}

function accountById(id) { return state.accounts.find(a => a.id === id); }

async function doVerify(id) {
  toast('正在检查账号…', 'info');
  const r = await safe(api('/api/accounts/' + encodeURIComponent(id) + '/check', { method: 'POST' }));
  if (r.ok) toast('检查完成：' + (r.data.status || 'ok') + (r.data.message ? ' · ' + r.data.message : ''), r.data.ok ? 'ok' : 'warn');
  else toast('检查失败：' + r.error, 'bad');
  loadAccounts();
}

async function doCheckin(id) {
  toast('正在签到…', 'info');
  const r = await safe(api('/api/accounts/' + encodeURIComponent(id) + '/checkin', { method: 'POST' }));
  if (r.ok) toast('签到结果：' + (r.data.status || 'ok'), 'ok');
  else toast('签到失败：' + r.error, 'bad');
  loadAccounts();
}

/* ---- 批量运维（/api/ops/*）---- */
async function opsVerifyAll() {
  toast('正在批量检查…', 'info');
  const q = state.acctChannel ? '?channel=' + encodeURIComponent(state.acctChannel) : '';
  const r = await safe(api('/api/ops/verify-all' + q, { method: 'POST' }));
  if (r.ok) {
    const d = r.data || {};
    const total = d.total != null ? d.total : (d.results || []).length;
    toast('检查完成：' + fmtNum(d.ok) + '/' + fmtNum(total) + ' 正常', Number(d.failed) ? 'warn' : 'ok');
  } else toast('批量检查失败：' + r.error, 'bad');
  loadAccounts();
}

async function opsCheckinAll() {
  toast('正在批量签到…', 'info');
  const q = state.acctChannel ? '?channel=' + encodeURIComponent(state.acctChannel) : '';
  const r = await safe(api('/api/ops/checkin-all' + q, { method: 'POST' }));
  if (r.ok) {
    const d = r.data || {};
    const total = d.total != null ? d.total : (d.results || []).length;
    toast('签到完成：' + fmtNum(d.ok) + '/' + fmtNum(total), Number(d.failed) ? 'warn' : 'ok');
  } else toast('批量签到失败：' + r.error, 'bad');
  loadAccounts();
}

async function opsKeepaliveAll() {
  toast('正在批量保活…', 'info');
  const r = await safe(api('/api/ops/keepalive-all', { method: 'POST' }));
  if (r.ok) {
    const d = r.data || {};
    toast('保活完成：检查 ' + fmtNum(d.checked) + '，成功 ' + fmtNum(d.ok) +
      (d.skipped ? '，跳过 ' + fmtNum(d.skipped) : ''), Number(d.failed) ? 'warn' : 'ok');
  } else toast('批量保活失败：' + r.error, 'bad');
  loadAccounts();
}

async function opsBalanceAll() {
  toast('正在刷新额度…', 'info');
  const q = state.acctChannel ? '?channel=' + encodeURIComponent(state.acctChannel) : '';
  const r = await safe(api('/api/ops/balance-all' + q, { method: 'POST' }));
  if (r.ok) {
    const d = r.data || {};
    const total = d.total != null ? d.total : (d.results || []).length;
    toast('额度刷新完成：' + fmtNum(d.ok) + '/' + fmtNum(total), Number(d.failed) ? 'warn' : 'ok');
  } else toast('额度刷新失败：' + r.error, 'bad');
  loadAccounts();
}

function openEdit(id) {
  const a = accountById(id);
  if (!a) return;
  $('#editId').value = a.id;
  $('#editName').value = a.name || '';
  $('#editStatus').value = '';
  $('#editProxy').value = a.proxy || '';
  $('#editNote').value = '';
  $('#editMeta').textContent = 'ID ' + a.id + ' · 渠道 ' + a.channel +
    ' · 凭据 cookie=' + (a.has_cookies ? '有' : '无') + ' oauth=' + (a.has_oauth ? '有' : '无') +
    ' profile=' + (a.has_profile ? '有' : '无');
  openModal('m-edit');
}

async function saveEdit() {
  const id = $('#editId').value;
  const body = {};
  const name = $('#editName').value.trim();
  const status = $('#editStatus').value;
  const proxy = $('#editProxy').value.trim();
  const note = $('#editNote').value.trim();
  if (name) body.name = name;
  if (status) body.status = status;
  if (proxy) body.proxy = proxy;
  if (note) body.note = note;
  if (!Object.keys(body).length) { toast('没有需要修改的字段', 'warn'); return; }
  const r = await safe(api('/api/accounts/' + encodeURIComponent(id), { method: 'PATCH', body }));
  if (r.ok) { toast('已保存', 'ok'); closeModal('m-edit'); loadAccounts(); }
  else toast('保存失败：' + r.error, 'bad');
}

async function openBackups(id) {
  const a = accountById(id);
  $('#bkAccountId').value = id;
  $('#bkTag').value = 'manual';
  $('#bkList').innerHTML = loadingHtml();
  openModal('m-backups');
  await refreshBackups(id);
}

async function refreshBackups(id) {
  const r = await safe(api('/api/accounts/' + encodeURIComponent(id) + '/backups'));
  if (!r.ok) { $('#bkList').innerHTML = errorHtml(r.error); return; }
  const list = (r.data && r.data.backups) || [];
  if (!list.length) { $('#bkList').innerHTML = emptyHtml('暂无备份', '点击上方「新建备份」创建快照。'); return; }
  $('#bkList').innerHTML = '<div class="table-wrap"><table class="acc"><thead><tr>' +
    '<th>标签</th><th>Cookie 数</th><th>创建时间</th><th class="row-actions">操作</th></tr></thead><tbody>' +
    list.map(b => '<tr><td>' + esc(b.tag || '—') + '</td>' +
      '<td class="num">' + fmtNum(b.cookie_count) + '</td>' +
      '<td class="muted">' + esc(fmtTime(b.created_at)) + ' · ' + esc(relTime(b.created_at)) + '</td>' +
      '<td class="row-actions"><button class="btn xs" data-act="bk-restore" data-id="' + esc(b.backup_id) + '">恢复</button></td></tr>').join('') +
    '</tbody></table></div>';
}

async function createBackup() {
  const id = $('#bkAccountId').value;
  const tag = $('#bkTag').value.trim() || 'manual';
  const r = await safe(api('/api/accounts/' + encodeURIComponent(id) + '/backups?tag=' + encodeURIComponent(tag), { method: 'POST' }));
  if (r.ok) { toast('备份已创建', 'ok'); refreshBackups(id); }
  else toast('备份失败：' + r.error, 'bad');
}

async function restoreBackup(backupId) {
  const id = $('#bkAccountId').value;
  const yes = await confirmDialog('确认用快照 <code class="mono">' + esc(backupId) + '</code> 覆盖当前凭据？', { yes: '恢复' });
  if (!yes) return;
  const r = await safe(api('/api/accounts/' + encodeURIComponent(id) + '/restore?backup_id=' + encodeURIComponent(backupId), { method: 'POST' }));
  if (r.ok) { toast('已恢复', 'ok'); closeModal('m-backups'); loadAccounts(); }
  else toast('恢复失败：' + r.error, 'bad');
}

async function deleteAccount(id) {
  const a = accountById(id);
  const yes = await confirmDialog('确认删除账号 <b>' + esc(a ? (a.name || a.id) : id) + '</b>？此操作不可恢复。');
  if (!yes) return;
  const r = await safe(api('/api/accounts/' + encodeURIComponent(id), { method: 'DELETE' }));
  if (r.ok) { toast('账号已删除', 'ok'); loadAccounts(); }
  else toast('删除失败：' + r.error, 'bad');
}

/* ---- 导入账号 ---- */
let impTab = 'paste';
function switchImpTab(tab) {
  impTab = tab;
  $$('#impTabs button').forEach(b => b.classList.toggle('active', b.dataset.impTab === tab));
  ['paste', 'file', 'sms', 'pwd', 'oauth'].forEach(t => {
    const el = $('#impPane-' + t);
    if (el) el.classList.toggle('hidden', t !== tab);
  });
  // 按渠道提示该渠道声明的登录方式
  const ch = $('#impChannel').value;
  const meta = state.channels.find(c => c.name === ch) || {};
  const methods = (meta.capabilities && meta.capabilities.login_methods) || [];
  const noteId = { sms: 'impSmsNote', pwd: 'impPwdNote', oauth: 'impOauthNote' }[tab];
  const label = { sms: '手机号+短信', pwd: '账密', oauth: '授权链接' }[tab];
  if (noteId && $('#' + noteId)) {
    const el = $('#' + noteId);
    if (methods.length && methods.indexOf(tab) < 0) {
      el.className = 'notice warn';
      el.textContent = label + '：该渠道未声明支持此方式，可能失败。该渠道支持：' + methods.join(' / ');
    } else {
      el.className = 'notice';
    }
  }
  // 切到授权链接 tab 时自动列出各渠道官网登录链接
  if (tab === 'oauth' && $('#impOauthOut')) startOauth();
}

/* 发送短信验证码（手机号登录） */
async function sendSmsCode() {
  const channel = $('#impChannel').value;
  const phone = $('#impPhone').value.trim();
  const area = $('#impAreaCode').value.trim() || '86';
  if (!channel) { toast('请选择渠道', 'warn'); return; }
  if (!phone) { toast('请填写手机号', 'warn'); return; }
  const btn = $('#impSendSms');
  btn.disabled = true; btn.textContent = '发送中…';
  const r = await safe(api('/api/accounts/login/sms/send', {
    method: 'POST', body: { channel, phone, area_code: area }
  }));
  btn.disabled = false; btn.textContent = '发送验证码';
  if (r.ok) toast('验证码已发送，请查收短信', 'ok');
  else toast('发送失败：' + r.error, 'bad');
}

/* 选中渠道后，直接显示该渠道的官网登录链接 + 跳转按钮 */
async function refreshChannelLogin() {
  const box = $('#impChannelLogin');
  if (!box) return;
  const ch = $('#impChannel').value;
  if (!ch) { box.style.display = 'none'; return; }
  box.style.display = 'flex';
  box.className = 'notice';
  box.innerHTML = '<span class="muted">加载官网登录链接…</span>';
  const r = await safe(api('/api/accounts/login/oauth?channel=' + encodeURIComponent(ch)));
  const url = r.ok ? ((r.data || {}).login_url || '') : '';
  if (!url) {
    box.innerHTML = '<span class="muted">该渠道未提供官网登录链接</span>';
    return;
  }
  box.innerHTML =
    '<span>官网登录：<a href="' + esc(url) + '" target="_blank" rel="noopener noreferrer">' + esc(url) + '</a></span>' +
    '<span style="white-space:nowrap">' +
    '<a class="btn xs primary" href="' + esc(url) + '" target="_blank" rel="noopener noreferrer">打开官网登录</a> ' +
    '<button class="btn xs" data-act="copy-text" data-text="' + esc(url) + '">复制</button>' +
    '</span>';
}

/* 选中渠道后，直接在渠道下方显示该渠道官网登录链接 + 跳转按钮 */
async function updateChannelLogin() {
  const ch = $('#impChannel').value;
  const box = $('#impChannelLogin');
  if (!box) return;
  if (!ch) { box.classList.add('hidden'); return; }
  const r = await safe(api('/api/accounts/login/oauth?channel=' + encodeURIComponent(ch)));
  const d = r.ok ? (r.data || {}) : {};
  const url = d.login_url || '';
  if (!url) { box.classList.add('hidden'); return; }
  box.classList.remove('hidden');
  box.innerHTML =
    '<div class="card" style="padding:10px 12px;display:flex;justify-content:space-between;' +
    'align-items:center;gap:10px;flex-wrap:wrap">' +
    '<div style="min-width:0"><div class="muted" style="font-size:12px;margin-bottom:2px">' +
    '官网登录链接（用无痕窗口打开，不影响本地浏览器账号）</div>' +
    '<code style="font-size:12px;word-break:break-all">' + esc(url) + '</code></div>' +
    '<div style="display:flex;gap:6px">' +
    '<a class="btn xs primary" href="' + esc(url) + '" target="_blank" rel="noopener noreferrer">打开官网登录</a>' +
    '<button class="btn xs" data-act="copy-text" data-text="' + esc(url) + '">复制</button>' +
    '</div></div>';
}

/* 生成授权链接 / 书签脚本（OAuth 方式）—— 列出每个渠道的官网登录链接 + 跳转按钮 */
async function startOauth() {
  const out = $('#impOauthOut');
  out.innerHTML = '<div class="loading"><div class="spinner"></div>加载各渠道登录链接…</div>';
  const chans = state.channels || [];
  if (!chans.length) { out.innerHTML = '<div class="notice warn">渠道未加载</div>'; return; }
  const cur = $('#impChannel').value;
  const cards = [];
  for (const ch of chans) {
    const r = await safe(api('/api/accounts/login/oauth?channel=' + encodeURIComponent(ch.name)));
    const d = r.ok ? (r.data || {}) : {};
    const url = d.login_url || '';
    const isCur = ch.name === cur;
    const head = '<div style="display:flex;justify-content:space-between;align-items:center;gap:8px">' +
      '<div><b>' + esc(ch.display_name || ch.name) + '</b> ' +
      '<span class="muted" style="font-size:12px">' + esc(ch.name) + '</span></div>' +
      (url
        ? '<a class="btn xs primary" href="' + esc(url) + '" target="_blank" rel="noopener noreferrer">打开官网登录</a>'
        : '<span class="muted">无登录链接</span>') +
      '</div>';
    const box = url
      ? '<div class="reveal-box" style="margin-top:8px"><code>' + esc(url) + '</code>' +
        '<button class="btn xs" data-act="copy-text" data-text="' + esc(url) + '">复制</button></div>'
      : '';
    const cb = d.callback_url || '';
    const bm = d.bookmarklet || '';
    const cbLine = cb
      ? '<div class="muted" style="font-size:12px;margin-top:8px">回调地址：<code>' + esc(cb) + '</code></div>'
      : '';
    const bmBox = bm
      ? '<label class="field" style="margin-top:8px"><span>登录后在官网页面执行（F12 → Console 粘贴）</span>' +
        '<div class="reveal-box"><code style="font-size:11px">' + esc(bm) + '</code>' +
        '<button class="btn xs" data-act="copy-text" data-text="' + esc(bm) + '">复制</button></div></label>'
      : '';
    const hint = d.hint ? '<div class="hint" style="margin-top:6px">' + esc(d.hint) + '</div>' : '';
    cards.push('<div class="card" style="padding:12px;' +
      (isCur ? 'border-color:var(--accent)' : '') + '">' + head + box + cbLine + bmBox + hint + '</div>');
  }
  out.innerHTML = cards.join('');
}

function openImport() {
  if (!state.channels.length) { toast('渠道未加载，请先配置密钥', 'warn'); return; }
  $('#impName').value = '';
  $('#impCookies').value = '';
  $('#impFile').value = '';
  $('#impProxy').value = '';
  $('#impNote').value = '';
  if (!$('#impChannel').value) $('#impChannel').value = state.channels[0].name;
  switchImpTab('paste');
  refreshChannelLogin();
  openModal('m-import');
}

async function submitImport() {
  const channel = $('#impChannel').value;
  const name = $('#impName').value.trim();
  const proxy = $('#impProxy').value.trim();
  const note = $('#impNote').value.trim();
  if (!channel) { toast('请选择渠道', 'warn'); return; }

  if (impTab === 'file') {
    const f = $('#impFile').files[0];
    if (!f) { toast('请选择文件', 'warn'); return; }
    const form = new FormData();
    form.append('channel', channel);
    form.append('name', name);
    form.append('proxy', proxy);
    form.append('file', f);
    const r = await safe(api('/api/accounts/import-file', { method: 'POST', form }));
    if (r.ok) { toast('导入成功', 'ok'); closeModal('m-import'); loadAccounts(); }
    else toast('导入失败：' + r.error, 'bad');
    return;
  }

  if (impTab === 'sms') {
    const phone = $('#impPhone').value.trim();
    const code = $('#impSmsCode').value.trim();
    const area = $('#impAreaCode').value.trim() || '86';
    if (!phone || !code) { toast('请填写手机号与验证码', 'warn'); return; }
    const r = await safe(api('/api/accounts/login/sms', {
      method: 'POST', body: { channel, phone, code, area_code: area, name, proxy, note }
    }));
    if (r.ok) { toast('登录成功', 'ok'); closeModal('m-import'); loadAccounts(); }
    else toast('登录失败：' + r.error, 'bad');
    return;
  }
  if (impTab === 'pwd') {
    const account = $('#impLoginUser').value.trim();
    const password = $('#impLoginPass').value;
    if (!account || !password) { toast('请填写账号与密码', 'warn'); return; }
    const r = await safe(api('/api/accounts/login/password', {
      method: 'POST', body: { channel, account, password, name, proxy, note }
    }));
    if (r.ok) { toast('登录成功', 'ok'); closeModal('m-import'); loadAccounts(); }
    else toast('登录失败：' + r.error, 'bad');
    return;
  }
  if (impTab === 'oauth') { toast('请先点「生成授权链接」，再按提示回传 Cookie', 'warn'); return; }

  const raw = $('#impCookies').value.trim();
  let cookies = {};
  if (raw) {
    try { cookies = JSON.parse(raw); }
    catch (e) { toast('Cookie 不是合法 JSON', 'bad'); return; }
  }
  const r = await safe(api('/api/accounts/import', { method: 'POST', body: { channel, name, cookies, proxy, note } }));
  if (r.ok) { toast('导入成功', 'ok'); closeModal('m-import'); loadAccounts(); }
  else toast('导入失败：' + r.error, 'bad');
}

/* ============================================================================
 * 视图 3 · 代理出口
 * ========================================================================== */
async function loadProxies() {
  const box = $('#body-proxies');
  const poolBox = $('#body-pools');
  box.innerHTML = loadingHtml();
  poolBox.innerHTML = loadingHtml();

  const q = state.pxChannel ? '?channel=' + encodeURIComponent(state.pxChannel) : '';
  const [accR, statR, poolR] = await Promise.all([
    safe(api('/api/proxy/accounts' + q)),
    safe(api('/api/proxy/stats')),
    safe(api('/api/proxy/pools')),
  ]);

  state.proxy.available = accR.ok;
  state.proxy.error = accR.ok ? '' : accR.error;
  state.proxy.rows = accR.ok ? (accR.data.accounts || []) : [];
  state.proxy.stats = statR.ok ? statR.data : null;
  state.proxy.pools = poolR.ok ? (poolR.data.realms || []) : [];

  $('#pxStats').innerHTML = proxyStatsHtml(state.proxy.stats);
  box.innerHTML = (!accR.ok)
    ? '<div class="notice warn">代理接口不可用（' + esc(accR.error) + '）。后端可能尚未实现 <code class="mono">/api/proxy/*</code>。</div>' + proxyTable([])
    : proxyTable(state.proxy.rows);
  poolBox.innerHTML = poolR.ok ? poolsHtml(state.proxy.pools)
    : '<div class="notice warn">订阅池接口不可用（' + esc(poolR.error) + '）。</div>' + poolsHtml([]);
}

function proxyStatsHtml(stats) {
  const s = stats || {};
  const over = (s.over_limit || []).length;
  return '<div class="stat-row" style="margin-bottom:16px">' +
    '<div class="stat ok"><div class="s-val">' + fmtNum(s.bound) + '</div><div class="s-lbl">已绑定代理</div></div>' +
    '<div class="stat"><div class="s-val">' + fmtNum(s.unbound) + '</div><div class="s-lbl">未绑定</div></div>' +
    '<div class="stat ' + (over ? 'bad' : '') + '"><div class="s-val">' + fmtNum(over) + '</div><div class="s-lbl">同 IP 超限账号</div></div>' +
    '</div>';
}

function proxyTable(rows) {
  if (!rows.length) {
    return '<div class="table-wrap">' + emptyHtml('暂无代理绑定', '可用「批量导入」粘贴 账号ID 代理链接，或对单个账号「配置」。') + '</div>';
  }
  const body = rows.map(r => {
    const st = !r.bound ? 'unbound' : (r.state || 'unknown');
    const actual = r.actual_ip || '';
    const expected = r.expected_ip || '';
    const mismatch = actual && expected && actual !== expected;
    return '<tr data-id="' + esc(r.account_id) + '">' +
      '<td class="mark ' + markCls(st) + '"></td>' +
      '<td><div class="cell-main">' + esc(r.name || r.account_id) + '</div>' +
        '<div class="cell-sub">' + esc(r.account_id) + '</div></td>' +
      '<td class="mono ellipsis" title="' + esc(r.proxy || '') + '">' + (r.proxy ? esc(r.proxy) : '<span class="muted">—</span>') + '</td>' +
      '<td class="mono">' + (expected ? esc(expected) : '<span class="muted">—</span>') + '</td>' +
      '<td class="mono">' + (actual
        ? (mismatch ? '<span style="color:var(--warn)">' + esc(actual) + ' ⚠</span>' : esc(actual))
        : '<span class="muted">未测</span>') +
        (r.checked_at ? '<div class="cell-sub">' + esc(relTime(r.checked_at)) + '</div>' : '') + '</td>' +
      '<td>' + badge(st) + (r.enabled === false ? ' ' + badge('disabled') : '') +
        (r.last_error ? '<div class="cell-sub" title="' + esc(r.last_error) + '">' + esc(String(r.last_error).slice(0, 40)) + '</div>' : '') + '</td>' +
      '<td class="row-actions"><div class="ops">' +
        '<button class="btn xs" data-act="px-config" data-id="' + esc(r.account_id) + '">配置</button>' +
        '<button class="btn xs" data-act="px-check" data-id="' + esc(r.account_id) + '">校验</button>' +
        '<button class="btn xs" data-act="px-direct" data-id="' + esc(r.account_id) + '">直连</button>' +
      '</div></td></tr>';
  }).join('');
  return '<div class="table-wrap"><div class="table-scroll"><table class="acc"><thead><tr>' +
    '<th class="mark"></th><th>账号</th><th>代理链接</th><th>声明 IP</th><th>实测 IP</th><th>状态</th><th class="row-actions">操作</th>' +
    '</tr></thead><tbody>' + body + '</tbody></table></div></div>';
}

function poolsHtml(realms) {
  const defaults = ['global', 'cn'];
  const map = {};
  (realms || []).forEach(r => { map[r.realm] = r; });
  const list = defaults.concat(Object.keys(map).filter(k => !defaults.includes(k)));

  return '<div class="grid cols-3">' + list.map(realm => {
    const r = map[realm];
    const pool = (r && r.pool) || {};
    const ok = Number(pool.ok) || 0, cool = Number(pool.cooling) || 0, dead = Number(pool.dead) || 0;
    const tot = Number(pool.total) || (ok + cool + dead);
    const pct = n => tot ? (n / tot * 100).toFixed(1) + '%' : '0%';
    return '<div class="card pool-card">' +
      '<div class="pool-head"><span class="realm">' + esc(realm) + '</span>' +
        '<span class="sp"></span>' +
        '<span class="muted" style="font-size:11.5px">' + (r ? (r.subs_count || 0) + ' 订阅 · ' + (r.entries || 0) + ' 条目' : '未配置') + '</span></div>' +
      '<div class="pool-bar">' +
        '<span class="b-ok" style="width:' + pct(ok) + '"></span>' +
        '<span class="b-cool" style="width:' + pct(cool) + '"></span>' +
        '<span class="b-dead" style="width:' + pct(dead) + '"></span>' +
      '</div>' +
      '<div class="legend">' +
        '<span class="l-ok"><i></i>可用 ' + ok + '</span>' +
        '<span class="l-cool"><i></i>冷却 ' + cool + '</span>' +
        '<span class="l-dead"><i></i>不可用 ' + dead + '</span>' +
        '<span>合计 ' + tot + '</span>' +
      '</div>' +
      '<div class="hint">刷新于 ' + esc(r && r.refreshed_at ? relTime(r.refreshed_at) : '—') + '</div>' +
      (r && r.last_error ? '<div class="hint" style="color:var(--bad)">' + esc(r.last_error) + '</div>' : '') +
      '<div class="ops">' +
        '<button class="btn xs" data-act="pool-save" data-id="' + esc(realm) + '">保存订阅</button>' +
        '<button class="btn xs" data-act="pool-refresh" data-id="' + esc(realm) + '">刷新</button>' +
        '<button class="btn xs danger" data-act="pool-clear" data-id="' + esc(realm) + '">清空</button>' +
      '</div></div>';
  }).join('') + '</div>';
}

function openProxyConfig(id) {
  const row = state.proxy.rows.find(r => r.account_id === id) || {};
  $('#pxAccount').value = id;
  $('#pxChannel').value = row.channel || '';
  $('#pxProxy').value = row.proxy || '';
  $('#pxExpected').value = row.expected_ip || '';
  $('#pxLabel').value = row.label || '';
  $('#pxNote').value = row.note || '';
  $('#pxEnabled').checked = row.enabled !== false;
  $('#pxAccountLabel').innerHTML = '账号 <code class="mono">' + esc(row.name || id) + '</code> · ' + esc(row.channel || '') +
    ' · ' + esc(id);
  openModal('m-proxy');
}

async function saveProxy() {
  const body = {
    account_id: $('#pxAccount').value,
    channel: $('#pxChannel').value,
    proxy: $('#pxProxy').value.trim(),
    expected_ip: $('#pxExpected').value.trim(),
    enabled: $('#pxEnabled').checked,
    label: $('#pxLabel').value.trim(),
    note: $('#pxNote').value.trim(),
  };
  if (!body.proxy) { toast('代理链接不能为空', 'warn'); return; }
  const r = await safe(api('/api/proxy/save', { method: 'POST', body }));
  if (r.ok) { toast('代理已保存', 'ok'); closeModal('m-proxy'); loadProxies(); }
  else toast('保存失败：' + r.error, 'bad');
}

async function proxyDirect(id) {
  const r = await safe(api('/api/proxy/direct', { method: 'POST', body: { account_id: id } }));
  if (r.ok) { toast('已切换为直连', 'ok'); closeModal('m-proxy'); loadProxies(); }
  else toast('操作失败：' + r.error, 'bad');
}

async function proxyCheck(id) {
  toast('正在校验代理…', 'info');
  const r = await safe(api('/api/proxy/check', { method: 'POST', body: { account_id: id } }));
  if (r.ok) {
    const d = r.data || {};
    toast('校验完成' + (d.actual_ip ? '：' + d.actual_ip : '') + (d.state ? ' · ' + d.state : ''), 'ok');
  } else toast('校验失败：' + r.error, 'bad');
  loadProxies();
}

async function proxyCheckAll() {
  toast('正在批量校验…', 'info');
  const q = state.pxChannel ? '?channel=' + encodeURIComponent(state.pxChannel) : '';
  const r = await safe(api('/api/proxy/check-all' + q, { method: 'POST' }));
  if (r.ok) toast('批量校验已提交', 'ok');
  else toast('批量校验失败：' + r.error, 'bad');
  loadProxies();
}

async function submitBulk() {
  const text = $('#bulkText').value;
  if (!text.trim()) { toast('请粘贴内容', 'warn'); return; }
  const r = await safe(api('/api/proxy/bulk', { method: 'POST', body: { text } }));
  if (r.ok) {
    const d = r.data || {};
    toast('已导入 ' + fmtNum(d.saved) + '/' + fmtNum(d.total), 'ok');
    closeModal('m-proxy-bulk');
    loadProxies();
  } else toast('导入失败：' + r.error, 'bad');
}

function openPoolSave(realm) {
  const r = state.proxy.pools.find(x => x.realm === realm) || {};
  $('#poolRealm').value = realm;
  $('#poolMode').value = 'replace';
  $('#poolSubs').value = (r.subs || []).join('\n');
  openModal('m-pool-save');
}

async function savePool() {
  const realm = $('#poolRealm').value;
  const mode = $('#poolMode').value;
  const subs = $('#poolSubs').value.split('\n').map(s => s.trim()).filter(Boolean);
  const r = await safe(api('/api/proxy/pools/save', { method: 'POST', body: { realm, subs, mode } }));
  if (r.ok) { toast('订阅池已保存', 'ok'); closeModal('m-pool-save'); loadProxies(); }
  else toast('保存失败：' + r.error, 'bad');
}

async function refreshPool(realm) {
  toast('正在刷新订阅池 ' + realm + '…', 'info');
  const r = await safe(api('/api/proxy/pools/refresh', { method: 'POST', body: { realm } }));
  if (r.ok) toast('已刷新', 'ok'); else toast('刷新失败：' + r.error, 'bad');
  loadProxies();
}

async function clearPool(realm) {
  const yes = await confirmDialog('确认清空订阅池 <b class="mono">' + esc(realm) + '</b>？所有订阅链接与解析条目将被移除。');
  if (!yes) return;
  const r = await safe(api('/api/proxy/pools/clear', { method: 'POST', body: { realm } }));
  if (r.ok) { toast('已清空', 'ok'); loadProxies(); }
  else toast('清空失败：' + r.error, 'bad');
}

/* ============================================================================
 * 视图 4 · 任务
 * ========================================================================== */
async function loadJobs() {
  const box = $('#body-jobs');
  box.innerHTML = loadingHtml();
  const params = new URLSearchParams();
  params.set('limit', '100');
  if (state.jobStatus) params.set('status', state.jobStatus);
  if (state.jobChannel) params.set('channel', state.jobChannel);
  const r = await safe(api('/api/jobs?' + params.toString()));
  if (!r.ok) { box.innerHTML = errorHtml(r.error); return; }
  state.jobs = (r.data && r.data.jobs) || [];
  box.innerHTML = jobsHtml(state.jobs);
  updateNavCounts();
}

function jobsHtml(jobs) {
  if (!jobs.length) return '<div class="table-wrap">' + emptyHtml('暂无任务', '通过 /v1/videos 等接口提交生成任务后会出现在这里。') + '</div>';
  const body = jobs.map(j => {
    const terminal = ['success', 'failed', 'cancelled'].includes(j.status);
    return '<tr data-id="' + esc(j.id) + '">' +
      '<td class="mark ' + markCls(j.status) + '"></td>' +
      '<td class="mono ellipsis" title="' + esc(j.id) + '">' + esc(j.id) + '</td>' +
      '<td>' + chChip(j.channel) + '</td>' +
      '<td class="mono ellipsis" title="' + esc(j.model || '') + '">' + esc(j.model || '—') + '</td>' +
      '<td>' + badge(j.status) + (j.error_class ? ' <span class="chip">' + esc(j.error_class) + '</span>' : '') + '</td>' +
      '<td class="muted nowrap" title="' + esc(fmtTime(j.created_at)) + '">' + esc(relTime(j.created_at)) + '</td>' +
      '<td class="row-actions">' + (terminal
        ? '<span class="muted" style="font-size:11.5px">—</span>'
        : '<button class="btn xs danger" data-act="job-cancel" data-id="' + esc(j.id) + '">取消</button>') + '</td></tr>';
  }).join('');
  return '<div class="table-wrap"><div class="table-scroll"><table class="acc"><thead><tr>' +
    '<th class="mark"></th><th>任务 ID</th><th>渠道</th><th>模型</th><th>状态</th><th>创建时间</th><th class="row-actions">操作</th>' +
    '</tr></thead><tbody>' + body + '</tbody></table></div></div>';
}

async function cancelJob(id) {
  const yes = await confirmDialog('确认取消任务 <code class="mono">' + esc(id) + '</code>？');
  if (!yes) return;
  const r = await safe(api('/api/jobs/' + encodeURIComponent(id), { method: 'DELETE' }));
  if (r.ok) { toast('任务已取消', 'ok'); loadJobs(); }
  else toast('取消失败：' + r.error, 'bad');
}

/* ============================================================================
 * 视图 5 · 密钥
 * ========================================================================== */
async function loadKeys() {
  const box = $('#body-keys');
  box.innerHTML = loadingHtml();
  const r = await safe(api('/api/keys'));
  if (!r.ok) { box.innerHTML = errorHtml(r.error); return; }
  state.keys = (r.data && r.data.keys) || [];
  box.innerHTML = keysHtml(state.keys);
}

function keysHtml(keys) {
  if (!keys.length) return '<div class="table-wrap">' + emptyHtml('暂无密钥', '点击右上角「新建密钥」创建一把用于外部调用的 Key。') + '</div>';
  const body = keys.map(k => {
    const st = k.enabled ? 'enabled' : 'disabled';
    const chs = (k.allowed_channels || []).join(', ') || '全部渠道';
    return '<tr data-id="' + esc(k.id) + '">' +
      '<td class="mark ' + markCls(st) + '"></td>' +
      '<td><div class="cell-main">' + esc(k.name || '—') + '</div>' +
        '<div class="cell-sub">' + esc(chs) + (k.rpm_limit ? ' · ' + k.rpm_limit + ' rpm' : '') + '</div></td>' +
      '<td class="mono">' + esc(k.hint || ('u2a_' + (k.prefix || '') + '_***')) + '</td>' +
      '<td>' + badge(st) + '</td>' +
      '<td class="muted nowrap" title="' + esc(fmtTime(k.created_at)) + '">' + esc(relTime(k.created_at)) + '</td>' +
      '<td class="muted nowrap">' + (k.last_used_at ? esc(relTime(k.last_used_at)) : '—') + '</td>' +
      '<td class="row-actions"><div class="ops">' +
        '<button class="btn xs" data-act="key-reveal" data-id="' + esc(k.id) + '">查看</button>' +
        (k.enabled
          ? '<button class="btn xs" data-act="key-disable" data-id="' + esc(k.id) + '">禁用</button>'
          : '<button class="btn xs" data-act="key-enable" data-id="' + esc(k.id) + '">启用</button>') +
        '<button class="btn xs danger" data-act="key-del" data-id="' + esc(k.id) + '">删除</button>' +
      '</div></td></tr>';
  }).join('');
  return '<div class="table-wrap"><div class="table-scroll"><table class="acc"><thead><tr>' +
    '<th class="mark"></th><th>名称</th><th>前缀</th><th>状态</th><th>创建时间</th><th>最后使用</th><th class="row-actions">操作</th>' +
    '</tr></thead><tbody>' + body + '</tbody></table></div></div>';
}

function openKeyCreate() {
  $('#kcName').value = 'client';
  $('#kcNote').value = '';
  $('#kcRpm').value = '120';
  $('#kcMax').value = '8';
  $('#kcChannels').value = '';
  openModal('m-key-create');
}

async function submitKeyCreate() {
  const body = {
    name: $('#kcName').value.trim() || 'client',
    note: $('#kcNote').value.trim(),
    rpm_limit: Number($('#kcRpm').value) || 120,
    max_concurrent: Number($('#kcMax').value) || 8,
    allowed_channels: $('#kcChannels').value.split(',').map(s => s.trim()).filter(Boolean),
  };
  const r = await safe(api('/api/keys', { method: 'POST', body }));
  if (r.ok) {
    toast('密钥已创建', 'ok');
    closeModal('m-key-create');
    showReveal((r.data && r.data.api_key) || '');
    loadKeys();
  } else toast('创建失败：' + r.error, 'bad');
}

function showReveal(val) {
  $('#revealVal').textContent = val || '（空）';
  openModal('m-reveal');
}

async function revealKey(id) {
  const r = await safe(api('/api/keys/' + encodeURIComponent(id) + '/reveal'));
  if (r.ok) showReveal((r.data && r.data.api_key) || '');
  else toast('查看失败：' + r.error, 'bad');
}

async function toggleKey(id, enable) {
  const r = await safe(api('/api/keys/' + encodeURIComponent(id) + '/' + (enable ? 'enable' : 'disable'), { method: 'POST' }));
  if (r.ok) { toast(enable ? '已启用' : '已禁用', 'ok'); loadKeys(); }
  else toast('操作失败：' + r.error, 'bad');
}

async function deleteKey(id) {
  const k = state.keys.find(x => x.id === id);
  const yes = await confirmDialog('确认删除密钥 <b>' + esc(k ? k.name : id) + '</b>？使用该 Key 的调用将立即失效。');
  if (!yes) return;
  const r = await safe(api('/api/keys/' + encodeURIComponent(id), { method: 'DELETE' }));
  if (r.ok) { toast('密钥已删除', 'ok'); loadKeys(); }
  else toast('删除失败：' + r.error, 'bad');
}

/* ============================================================================
 * 视图 6 · 日志
 * ========================================================================== */
/* 日志分类：优先按 URL 特征（对话/生成），再按 direction（错误归系统） */
function logCategory(e) {
  const url = String((e && e.url) || '').toLowerCase();
  const dir = String((e && e.direction) || '').toLowerCase();
  if (url.includes('/chat') || url.includes('completion') || url.includes('conversation')) return 'chat';
  if (dir === 'error') return 'system';
  if (dir === 'request' || dir === 'response') return 'jobs';
  return 'system';
}
const CAT_LABEL = { jobs: '任务', chat: '对话', system: '系统' };

function filteredLogs() {
  const tab = state.logTab || 'all';
  if (tab === 'all') return state.logs;
  return state.logs.filter(e => logCategory(e) === tab);
}

function switchLogTab(tab) {
  state.logTab = tab;
  $$('#logChips .chip-btn').forEach(b => b.classList.toggle('active', b.dataset.logTab === tab));
  const box = $('#body-logs');
  if (state.route === 'logs' && box) box.innerHTML = logsHtml(filteredLogs());
}

async function loadLogs() {
  const box = $('#body-logs');
  const statsBox = $('#logStats');
  box.innerHTML = loadingHtml();
  const params = new URLSearchParams();
  params.set('limit', String(state.logLimit));
  if (state.logChannel) params.set('channel', state.logChannel);
  const [logR, runR] = await Promise.all([
    safe(api('/api/audit?' + params.toString())),
    safe(api('/api/stats/runtime')),
  ]);
  if (!logR.ok) { if (statsBox) statsBox.innerHTML = ''; box.innerHTML = errorHtml(logR.error); toast('日志加载失败：' + logR.error, 'bad'); return; }
  state.logs = (logR.data && logR.data.events) || [];
  state.logRuntime = runR.ok ? (runR.data || {}) : null;
  if (statsBox) statsBox.innerHTML = logStatsHtml(state.logs, state.logRuntime);
  box.innerHTML = logsHtml(filteredLogs());
}

function logStatsHtml(events, runtime) {
  const nowTs = Date.now() / 1000;
  const day = events.filter(e => Number(e.ts) >= nowTs - 86400);
  const count24 = (runtime && runtime.audit_24h != null) ? runtime.audit_24h : day.length;
  const byCh = {};
  day.forEach(e => { const k = e.channel || ''; byCh[k] = (byCh[k] || 0) + 1; });
  const keys = Object.keys(byCh).sort((a, b) => byCh[b] - byCh[a]);
  const rows = keys.map(k => ({ label: k ? chLabel(k) : '未知', title: k || '未知', value: byCh[k] }));
  return '<div class="stat-row" style="margin-bottom:14px">' +
    statCard('24h 事件数', count24) +
    statCard('加载窗口', events.length) +
    statCard('24h 渠道数', keys.length) +
    '</div>' +
    (rows.length
      ? '<div class="card" style="margin-bottom:16px"><div class="card-h"><h3>24h 渠道分布</h3></div>' +
        barList(rows, {}) + '</div>'
      : '');
}

function logsHtml(events) {
  if (!events.length) {
    const hint = state.logTab === 'all' ? '上游请求与响应会以脱敏形式记录在此。' : '当前分类下没有事件，切换「全部」查看。';
    return '<div class="table-wrap">' + emptyHtml('暂无审计事件', hint) + '</div>';
  }
  const body = events.map(e => {
    const ok = Number(e.status) >= 200 && Number(e.status) < 400;
    const cls = !e.status ? 'idle' : (ok ? 'ok' : 'bad');
    const stCls = !e.status ? 'st-idle' : (ok ? 'st-ok' : 'st-bad');
    return '<tr>' +
      '<td class="mark ' + cls + '"></td>' +
      '<td class="muted nowrap" title="' + esc(fmtTime(e.ts)) + '">' + esc(relTime(e.ts)) + '</td>' +
      '<td><span class="spec-tag">' + esc(CAT_LABEL[logCategory(e)] || '—') + '</span></td>' +
      '<td>' + (e.channel ? chChip(e.channel) : '<span class="muted">—</span>') + '</td>' +
      '<td class="mono ellipsis" title="' + esc(e.account_id || '') + '">' + esc(e.account_id || '—') + '</td>' +
      '<td class="mono">' + esc(e.method || '—') + '</td>' +
      '<td class="mono ellipsis" title="' + esc(e.url || '') + '">' + esc(e.url || '—') + '</td>' +
      '<td><span class="st-badge ' + stCls + '">' + esc(e.status || '—') + '</span></td></tr>';
  }).join('');
  return '<div class="table-wrap"><div class="table-scroll"><table class="acc"><thead><tr>' +
    '<th class="mark"></th><th>时间</th><th>类别</th><th>渠道</th><th>账号</th><th>方法</th><th>URL</th><th>状态</th>' +
    '</tr></thead><tbody>' + body + '</tbody></table></div></div>';
}

/* ============================================================================
 * 视图 7 · 用量统计
 * ========================================================================== */
async function loadUsage() {
  const statsBox = $('#usageStats');
  const box = $('#body-usage');
  box.innerHTML = loadingHtml();
  const params = new URLSearchParams();
  params.set('days', String(state.usage.days));
  if (state.usage.channel) params.set('channel', state.usage.channel);
  const [usageR, ovR] = await Promise.all([
    safe(api('/api/stats/usage?' + params.toString())),
    safe(api('/api/stats/overview')),
  ]);
  if (!usageR.ok) {
    if (statsBox) statsBox.innerHTML = '';
    box.innerHTML = errorHtml(usageR.error);
    toast('用量统计加载失败：' + usageR.error, 'bad');
    return;
  }
  const usage = usageR.data || {};
  const overview = ovR.ok ? (ovR.data || {}) : null;
  if (statsBox) statsBox.innerHTML = usageStatsHtml(usage, overview);
  box.innerHTML = usageHtml(usage);
}

function usageStatsHtml(usage, overview) {
  const chans = usage.by_channel || [];
  let total = 0, ok = 0, bad = 0;
  chans.forEach(c => {
    total += Number(c.jobs_total) || 0;
    ok += Number(c.jobs_success) || 0;
    bad += Number(c.jobs_failed) || 0;
  });
  // 无按渠道数据时退回全局总量
  if (!chans.length && overview && overview.totals) {
    total = Number(overview.totals.jobs_total) || 0;
    ok = Number(overview.totals.jobs_success) || 0;
    bad = Number(overview.totals.jobs_failed) || 0;
  }
  const rate = rateOf(ok, total);
  return '<div class="stat-row" style="margin-bottom:16px">' +
    statCard('任务总数', total) +
    statCard('成功', ok, 'ok') +
    statCard('失败', bad, 'bad') +
    statCard('成功率', rate == null ? '—' : pct(rate)) +
    '</div>';
}

function usageHtml(usage) {
  const byChannel = usage.by_channel || [];
  const byModel = usage.by_model || [];
  const byDay = usage.by_day || [];
  const byAccount = usage.by_account || [];

  const chRows = byChannel.map(c => ({
    label: chLabel(c.channel), title: c.channel,
    value: c.jobs_total || 0,
    rate: c.success_rate != null ? Number(c.success_rate) : rateOf(c.jobs_success, c.jobs_total),
    cls: (Number(c.jobs_failed) || 0) > 0 ? '' : 'ok',
  }));

  const modelRows = byModel.slice()
    .sort((a, b) => (Number(b.count) || 0) - (Number(a.count) || 0))
    .map(m => ({
      label: m.model || '—', title: chLabel(m.channel) + ' · ' + (m.model || ''),
      value: m.count || 0, rate: rateOf(m.success, m.count),
    }));

  const dayRows = byDay.map(d => ({
    label: shortDate(d.date), title: d.date + ' · 成功 ' + fmtNum(d.success),
    value: d.count || 0, rate: rateOf(d.success, d.count), cls: 'ok',
  }));

  return '' +
    '<div class="section-title">按渠道</div>' +
    '<div class="card">' + barList(chRows, { showPct: true, emptyTitle: '暂无渠道用量', emptySub: '所选窗口内没有任务记录。' }) + '</div>' +

    '<div class="section-title">按模型</div>' +
    '<div class="card">' + barList(modelRows, { showPct: true, emptyTitle: '暂无模型用量' }) + '</div>' +

    '<div class="section-title">按日趋势（最近 ' + fmtNum(usage.window_days || state.usage.days) + ' 天）</div>' +
    '<div class="card">' + barList(dayRows, { showPct: true, emptyTitle: '暂无按日数据' }) + '</div>' +

    '<div class="section-title">按账号</div>' +
    usageAccountTable(byAccount);
}

function usageAccountTable(list) {
  if (!list.length) return '<div class="table-wrap">' + emptyHtml('暂无账号用量', '所选窗口内没有任务记录。') + '</div>';
  const sorted = list.slice().sort((a, b) => (Number(b.count) || 0) - (Number(a.count) || 0));
  const body = sorted.map(r => {
    const total = Number(r.count) || 0, ok = Number(r.success) || 0, bad = Number(r.failed) || 0;
    const rate = rateOf(ok, total);
    return '<tr>' +
      '<td><div class="cell-main">' + esc(r.name || r.account_id) + '</div>' +
        '<div class="cell-sub">' + esc(r.account_id) + '</div></td>' +
      '<td>' + chChip(r.channel) + '</td>' +
      '<td class="num">' + fmtNum(total) + '</td>' +
      '<td class="num" style="color:var(--ok)">' + fmtNum(ok) + '</td>' +
      '<td class="num" style="color:var(--bad)">' + fmtNum(bad) + '</td>' +
      '<td class="num">' + (rate == null ? '—' : pct(rate)) + '</td></tr>';
  }).join('');
  return '<div class="table-wrap"><div class="table-scroll"><table class="acc"><thead><tr>' +
    '<th>账号</th><th>渠道</th><th>任务数</th><th>成功</th><th>失败</th><th>成功率</th>' +
    '</tr></thead><tbody>' + body + '</tbody></table></div></div>';
}

/* ============================================================================
 * 视图 8 · 模型与档位
 * ========================================================================== */
async function loadModels() {
  const box = $('#body-models');
  box.innerHTML = loadingHtml();
  const r = await safe(api('/api/models'));
  if (!r.ok) {
    box.innerHTML = errorHtml(r.error);
    toast('模型目录加载失败：' + r.error, 'bad');
    return;
  }
  const data = r.data || {};
  state.models.data = data;
  state.models.liveHtml = '';
  const chans = Object.keys(data.by_channel || {});
  // 逐渠道拉取规格（分辨率/时长/图生视频），失败静默降级
  const specResults = await Promise.all(chans.map(ch => safe(api('/api/models/' + encodeURIComponent(ch) + '/specs'))));
  state.models.specs = {};
  chans.forEach((ch, i) => { if (specResults[i].ok) state.models.specs[ch] = specResults[i].data || {}; });
  box.innerHTML = modelsHtml();
}

function modelsHtml() {
  const data = state.models.data || {};
  const all = data.models || [];
  const specs = state.models.specs || {};
  const q = (state.models.search || '').trim().toLowerCase();

  const groups = {};
  all.forEach(m => { (groups[m.channel] = groups[m.channel] || []).push(m); });
  const order = state.channels.filter(c => !c.error).map(c => c.name);
  Object.keys(groups).forEach(ch => { if (!order.includes(ch)) order.push(ch); });
  const chans = order.filter(ch => groups[ch]);

  const sections = chans.map(ch => {
    let list = groups[ch];
    if (q) {
      list = list.filter(m => [m.alias, m.id, m.upstream_id, m.display_name]
        .some(v => String(v == null ? '' : v).toLowerCase().includes(q)));
    }
    if (!list.length) return '';
    const specCh = (specs[ch] && specs[ch].models) || {};
    return '<div class="model-ch">' +
      '<div class="model-ch-head">' +
        '<span class="ch-dot ch-' + (CH_META[ch] ? ch : 'other') + '"></span>' +
        '<h3>' + esc(chLabel(ch)) + '</h3>' +
        '<span class="g-meta mono">' + esc(ch) + '</span>' +
        '<span class="sp"></span>' +
        '<span class="g-meta">' + fmtNum(list.length) + ' 个模型</span>' +
      '</div>' +
      '<div class="model-grid">' + list.map(m => modelCard(m, specCh)).join('') + '</div></div>';
  }).join('');

  const bodyHtml = sections || emptyHtml(q ? '无匹配模型' : '暂无模型',
    q ? '换个关键词试试。' : '渠道未注册或描述符未声明 models。');
  return bodyHtml + (state.models.liveHtml || '');
}

function modelCard(m, specMap) {
  const alias = m.alias || m.id || '—';
  const up = m.upstream_id || m.id || '';
  const spec = specMap[m.id] || specMap[alias] || {};
  const res = spec.resolutions || [];
  const dur = spec.durations || [];
  const tags = [];
  const kind = m.kind || spec.kind;
  const avail = m.available !== false;
  tags.push('<span class="spec-tag ' + (avail ? 'k' : 'i2v') + '">' + (avail ? '可用' : '不可用') + '</span>');
  if (m.price) tags.push('<span class="spec-tag">' + esc(m.price) + '</span>');
  if (kind) tags.push('<span class="spec-tag k">' + esc(kind) + '</span>');
  if (spec.supports_image_input) tags.push('<span class="spec-tag i2v">图生视频</span>');
  if (res.length) tags.push('<span class="spec-tag">' + esc(res.join(' / ')) + '</span>');
  if (dur.length) tags.push('<span class="spec-tag">' + esc(dur.map(d => d + 's').join(' / ')) + '</span>');
  if (!tags.length) tags.push('<span class="spec-tag">无规格声明</span>');
  return '<div class="model-card"' + (avail ? '' : ' style="opacity:.5"') + '>' +
    '<div class="m-alias">' + esc(m.display_name || alias) + '</div>' +
    '<div class="m-up">' + esc(alias) + (up && up !== alias ? ' → ' + esc(up) : '') + '</div>' +
    '<div class="m-tags">' + tags.join('') + '</div></div>';
}

async function probeModels() {
  const ch = $('#modelProbeChannel').value;
  if (!ch) { toast('请先选择渠道', 'warn'); return; }
  toast('正在探测 ' + chLabel(ch) + ' 实时目录…', 'info');
  const r = await safe(api('/api/models/' + encodeURIComponent(ch) + '/probe', { method: 'POST' }));
  if (!r.ok) { toast('探测失败：' + r.error, 'bad'); return; }
  const d = r.data || {};
  const models = d.models || [];
  toast('探测完成：' + fmtNum(d.count != null ? d.count : models.length) + ' 个模型', 'ok');
  state.models.liveHtml = liveModelsHtml(d);
  if (state.route === 'models') $('#body-models').innerHTML = modelsHtml();
}

function liveModelsHtml(d) {
  const models = d.models || [];
  const names = models.map(m => (typeof m === 'string' ? m : (m.id || m.alias || m.name || JSON.stringify(m))));
  return '<div class="section-title">实时探测 · ' + esc(chLabel(d.channel)) +
    (d.account_id ? ' · ' + esc(d.account_id) : '') + '</div>' +
    '<div class="card"><div class="model-grid">' +
    (names.length
      ? names.map(n => '<div class="model-card"><div class="m-alias mono" style="font-size:12px">' + esc(n) + '</div></div>').join('')
      : emptyHtml('上游未返回模型', '')) +
    '</div></div>';
}

/* 刷新目录：重载渠道描述符（POST /api/config/reload）后重拉模型 */
async function reloadCatalog() {
  toast('正在刷新渠道目录…', 'info');
  const r = await safe(api('/api/config/reload', { method: 'POST' }));
  if (!r.ok) { toast('刷新目录失败：' + r.error, 'bad'); return; }
  const d = r.data || {};
  const errs = d.errors || {};
  toast('目录已刷新' + (Object.keys(errs).length ? '，' + Object.keys(errs).length + ' 个渠道报错' : ''),
    Object.keys(errs).length ? 'warn' : 'ok');
  await loadChannels();
  loadModels();
}

/* ============================================================================
 * 视图 9 · 配置
 * ========================================================================== */
const CFG_SECTIONS = ['antiban', 'session', 'proxy', 'job', 'checkin', 'api'];
const CFG_META = {
  'antiban.enabled': '启用防风控（限速 / 熔断 / 指纹）',
  'antiban.rpm_limit': '每账号每 60 秒最多请求次数（滑动窗口）',
  'antiban.min_interval': '同一账号两次请求最小间隔（秒）',
  'antiban.jitter': '间隔随机抖动上限（秒），模拟人类节奏',
  'antiban.failure_threshold': '连续风控/并发错误达到该值触发熔断',
  'antiban.cooldown': '熔断冷却时长（秒），到期转半开探测',
  'antiban.proxy_pool': '代理池（逗号分隔，留空不绑定）',
  'antiban.fingerprint': '固定设备指纹（同账号固定 UA / 语言）',
  'session.enabled': '启用会话保活',
  'session.keepalive_interval': '心跳保活间隔（秒）',
  'session.lead_time': '距过期多少秒视为即将过期',
  'session.backup_keep': '凭据快照保留份数',
  'session.relogin_threshold': '连续保活失败达到该值建议重登',
  'proxy.enabled': '启用账号级代理（一账号一固定出口 IP）',
  'proxy.probe_timeout': '出口 IP 探测超时（秒）',
  'proxy.max_per_ip': '同出口 IP 允许账号数上限（0=不限制）',
  'job.poll_interval': '任务轮询间隔（秒）',
  'job.max_poll_interval': '最大轮询间隔（秒）',
  'job.video_timeout': '视频任务超时（秒）',
  'job.max_network_retries': '网络错误最大重试次数',
  'checkin.enabled': '启用每日签到',
  'checkin.timezone': '签到时区',
  'checkin.primary_time': '主签到时间（HH:MM）',
  'checkin.retry_time': '重试签到时间（HH:MM）',
  'api.api_key': '控制台/对外调用主密钥（留空只用数据库密钥）',
  'api.auth_disabled': '关闭鉴权（危险，仅本地调试）',
};

async function loadConfig() {
  const box = $('#body-config');
  box.innerHTML = loadingHtml();
  const [cfgR, chR] = await Promise.all([
    safe(api('/api/config')),
    safe(api('/api/config/channels')),
  ]);
  if (!cfgR.ok) {
    box.innerHTML = errorHtml(cfgR.error);
    toast('配置加载失败：' + cfgR.error, 'bad');
    return;
  }
  state.config.data = cfgR.data || {};
  state.config.channels = chR.ok ? (chR.data || {}) : null;
  state.config.channelsError = chR.ok ? '' : chR.error;
  state.config.openChannel = '';
  box.innerHTML = configHtml();
}

function configHtml() {
  const cfg = state.config.data || {};
  const sections = cfg.sections || {};
  const path = cfg.path || 'config/setting.toml';
  const order = CFG_SECTIONS.concat(Object.keys(sections).filter(k => CFG_SECTIONS.indexOf(k) < 0));
  const secHtml = order
    .filter(s => sections[s] && typeof sections[s] === 'object')
    .map(s => cfgSectionHtml(s, sections[s]))
    .join('');

  return '' +
    '<div class="toolbar" style="margin-bottom:6px"><span class="cfg-path">配置文件：' + esc(path) + '</span></div>' +
    (secHtml || emptyHtml('无可编辑配置', 'config/setting.toml 为空或读取失败。')) +

    '<div class="section-title">渠道描述符</div>' +
    descListHtml() +

    '<div class="section-title">运维</div>' +
    '<div class="card"><div class="inline" style="flex-wrap:wrap;gap:10px">' +
      '<button class="btn danger" data-act="antiban-reset">重置熔断</button>' +
      '<span class="hint" style="margin:0">清空全部账号的熔断状态，冷却立即解除。</span>' +
    '</div></div>';
}

function cfgSectionHtml(section, values) {
  const rows = Object.keys(values).map(k => cfgRowHtml(section, k, values[k])).join('');
  return '<div class="cfg-sec"><div class="section-title">' + esc(section) + '</div>' +
    '<div class="card"><div class="cfg-grid">' + rows + '</div></div></div>';
}

function cfgRowHtml(section, key, val) {
  const full = section + '.' + key;
  const desc = CFG_META[full] || '';
  const attr = ' data-cfg="' + esc(full) + '"';
  if (typeof val === 'boolean') {
    return '<div class="cfg-row">' +
      '<label class="check"><input type="checkbox"' + attr + ' data-type="bool"' + (val ? ' checked' : '') + '> ' + esc(key) + '</label>' +
      (desc ? '<div class="cfg-desc">' + esc(desc) + '</div>' : '') + '</div>';
  }
  let input;
  if (typeof val === 'number') {
    input = '<input type="number" step="any"' + attr + ' data-type="number" value="' + esc(val) + '">';
  } else if (Array.isArray(val)) {
    input = '<input type="text"' + attr + ' data-type="list" value="' + esc(val.join(', ')) + '">';
  } else {
    input = '<input type="text"' + attr + ' data-type="text" value="' + esc(val == null ? '' : val) + '">';
  }
  return '<div class="cfg-row">' +
    '<div class="cfg-key">' + esc(key) + '</div>' + input +
    (desc ? '<div class="cfg-desc">' + esc(desc) + '</div>' : '') + '</div>';
}

function descListHtml() {
  if (!state.config.channels) {
    return '<div class="notice warn">渠道描述符接口不可用（' + esc(state.config.channelsError || '未知错误') + '）。</div>';
  }
  const list = state.config.channels.channels || [];
  if (!list.length) return '<div class="table-wrap">' + emptyHtml('暂无渠道描述符', '检查 config/channels/*.json') + '</div>';
  return list.map(c => {
    const caps = Object.keys(c.capabilities || {}).filter(k => c.capabilities[k])
      .map(k => '<span class="spec-tag">' + esc(CAP_LABELS[k] || k) + '</span>').join('') ||
      '<span class="muted" style="font-size:11.5px">无能力声明</span>';
    return '<div class="desc-item" data-desc="' + esc(c.name) + '">' +
      '<button class="desc-head" data-act="desc-toggle" data-id="' + esc(c.name) + '">' +
        '<span class="chev">▾</span>' +
        '<b>' + esc(c.display_name || c.name) + '</b>' +
        '<span class="g-meta mono">' + esc(c.name) + ' · ' + esc(c.kind || '') + '</span>' +
        '<span class="sp"></span>' +
        '<span class="g-meta mono ellipsis" style="max-width:240px">' + esc(c.base_url || '') + '</span>' +
        '<span class="g-meta">' + fmtNum(c.model_count) + ' 模型</span>' +
      '</button>' +
      '<div class="desc-body hidden">' +
        '<div class="caps" style="margin-bottom:10px">' + caps + '</div>' +
        (c.load_error ? '<div class="notice bad">加载错误：' + esc(c.load_error) + '</div>' : '') +
        '<pre class="json">点击展开加载描述符原文…</pre>' +
      '</div></div>';
  }).join('');
}

async function toggleDesc(name) {
  const item = $$('.desc-item').find(x => x.dataset.desc === name);
  if (!item) return;
  const open = !item.classList.contains('open');
  item.classList.toggle('open', open);
  const body = item.querySelector('.desc-body');
  if (body) body.classList.toggle('hidden', !open);
  if (!open) return;
  const pre = item.querySelector('pre.json');
  if (!pre || pre.dataset.loaded === '1') return;
  pre.textContent = '加载中…';
  const r = await safe(api('/api/config/channels/' + encodeURIComponent(name)));
  if (r.ok) { pre.textContent = JSON.stringify(r.data, null, 2); pre.dataset.loaded = '1'; }
  else pre.textContent = '加载失败：' + r.error;
}

async function saveConfig() {
  const cfg = state.config.data || {};
  const inputs = $$('#body-config [data-cfg]');
  if (!inputs.length) { toast('没有可保存的配置项', 'warn'); return; }
  const yes = await confirmDialog(
    '确认写入配置文件 <code class="mono">' + esc(cfg.path || 'config/setting.toml') + '</code>？<br>部分配置需重启服务后生效。',
    { yes: '保存' });
  if (!yes) return;

  const grouped = {};
  inputs.forEach(el => {
    const full = el.dataset.cfg;
    const i = full.indexOf('.');
    const sec = full.slice(0, i), key = full.slice(i + 1);
    let v;
    if (el.dataset.type === 'bool') v = el.checked;
    else if (el.dataset.type === 'number') { v = Number(el.value); if (!isFinite(v)) return; }
    else if (el.dataset.type === 'list') v = el.value.split(',').map(s => s.trim()).filter(Boolean);
    else v = el.value;
    (grouped[sec] = grouped[sec] || {})[key] = v;
  });

  const secs = Object.keys(grouped);
  let okN = 0;
  const fail = [];
  for (let i = 0; i < secs.length; i++) {
    const sec = secs[i];
    const r = await safe(api('/api/config', { method: 'POST', body: { section: sec, values: grouped[sec] } }));
    if (r.ok) okN++; else fail.push(sec + '(' + r.error + ')');
  }
  if (fail.length) toast('保存完成 ' + okN + '/' + secs.length + '，失败：' + fail.join('；'), 'warn');
  else toast('配置已保存（' + okN + ' 个分区）', 'ok');
  loadConfig();
}

async function antibanReset() {
  const yes = await confirmDialog('确认重置全部账号的熔断状态？冷却中的账号将立即恢复可用。', { yes: '重置' });
  if (!yes) return;
  const r = await safe(api('/api/ops/antiban-reset', { method: 'POST' }));
  if (r.ok) toast('已重置熔断：' + ((r.data && r.data.reset) || 'all'), 'ok');
  else toast('重置失败：' + r.error, 'bad');
}

/* ---------------------------------------------------------------- 导航角标 */
function updateNavCounts() {
  const ac = $('#nav-accounts-count');
  if (ac) ac.textContent = state.accounts.length ? String(state.accounts.length) : '';
  const jc = $('#nav-jobs-count');
  if (jc) {
    const pending = state.jobs.filter(j => ['queued', 'submitted', 'processing'].includes(j.status)).length;
    jc.textContent = pending ? String(pending) : '';
  }
}

/* ---------------------------------------------------------------- 登录（账号密码） */
function showLogin(msg, kind) {
  const veil = $('#loginVeil');
  if (!veil) return;
  const m = $('#loginMsg');
  if (m) {
    m.textContent = msg || '请输入管理员账号密码登录。';
    m.className = 'login-msg' + (kind ? ' ' + kind : '');
  }
  veil.classList.add('on');
  const u = $('#loginUser');
  if (u) setTimeout(() => { try { u.focus(); } catch (_) {} }, 60);
}
function hideLogin() {
  const v = $('#loginVeil');
  if (v) v.classList.remove('on');
}
function loginMsg(text, kind) {
  const m = $('#loginMsg');
  if (m) { m.textContent = text; m.className = 'login-msg' + (kind ? ' ' + kind : ''); }
}
function togglePw() {
  const inp = $('#loginPass'), eye = $('#loginEye');
  if (!inp) return;
  const show = inp.type === 'password';
  inp.type = show ? 'text' : 'password';
  if (eye) { eye.textContent = show ? '🙈' : '👁'; eye.setAttribute('aria-label', show ? '隐藏密码' : '显示密码'); }
  inp.focus();
}
async function doLogin() {
  const username = ($('#loginUser').value || '').trim();
  const password = $('#loginPass').value || '';
  const btn = $('#loginBtn');
  if (!username || !password) {
    loginMsg('请输入用户名与密码', 'bad');
    (!username ? $('#loginUser') : $('#loginPass')).focus();
    return;
  }
  btn.disabled = true; btn.textContent = '登录中…';
  loginMsg('正在验证…');
  try {
    const r = await fetch('/api/auth/login', {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password })
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) {
      loginMsg((d && d.detail) ? d.detail : ('登录失败 ' + r.status), 'bad');
      $('#loginPass').value = '';
      $('#loginPass').focus();
      return;
    }
    state.user = d.user || { username };
    $('#loginPass').value = '';
    hideLogin();
    renderUser();
    toast('登录成功', 'ok');
    await loadChannels();
    renderRoute();
  } catch (e) {
    loginMsg('网络错误：' + (e.message || ''), 'bad');
    toast(e.message || '登录失败', 'bad');
  } finally {
    btn.disabled = false; btn.textContent = '登 录';
  }
}
async function doLogout() {
  try { await fetch('/api/auth/logout', { method: 'POST', credentials: 'same-origin' }); } catch (_) {}
  state.user = null;
  renderUser();
  showLogin('已退出登录');
}
async function checkAuth() {
  try {
    const r = await fetch('/api/auth/me', { credentials: 'same-origin' });
    if (!r.ok) return false;
    const d = await r.json();
    state.user = { username: d.username, role: d.role };
    renderUser();
    return true;
  } catch (_) { return false; }
}
function renderUser() {
  const el = $('#userName');
  if (el) el.textContent = state.user ? (state.user.username + (state.user.role === 'admin' ? ' · 管理员' : '')) : '';
  const btn = $('#userLogout');
  if (btn) btn.style.display = state.user ? '' : 'none';
}
async function changePassword() {
  const o = ($('#pwOld').value || ''), n = ($('#pwNew').value || '');
  if (!o || !n) { toast('请填写原密码与新密码', 'warn'); return; }
  if (n.length < 6) { toast('新密码至少 6 位', 'warn'); return; }
  const r = await safe(api('/api/auth/password', { method: 'POST', body: { old_password: o, new_password: n } }));
  if (r.ok) { toast('密码已修改，请重新登录', 'ok'); closeModal('m-user'); doLogout(); }
  else toast(r.error || '修改失败', 'bad');
}

/* ---------------------------------------------------------------- 密钥弹窗 */
function openKeyModal(msg) {
  $('#keyHint').textContent = msg || 'API 密钥用于程序调用（/v1/*），与控制台登录无关。';
  $('#keyInput').value = state.key || '';
  openModal('m-key');
}
async function saveKey() {
  const v = $('#keyInput').value.trim();
  state.key = v;
  if (v) localStorage.setItem(LS_KEY, v);
  else localStorage.removeItem(LS_KEY);
  closeModal('m-key');
  if (!v) { toast('已清除密钥', 'warn'); setConn(null, '未连接'); return; }
  toast('正在连接…', 'info');
  await loadChannels();
  renderRoute();
}

/* ---------------------------------------------------------------- 事件委托 */
async function handleAction(act, el) {
  const id = el.dataset.id || '';
  switch (act) {
    case 'open-key': openKeyModal(); break;
    case 'copy-text': {
      const t = el.dataset.text || '';
      try { await navigator.clipboard.writeText(t); toast('已复制', 'ok'); }
      catch (_) { toast('复制失败，请手动选择', 'warn'); }
      break;
    }
    case 'import-open': openImport(); break;
    case 'acct-tab': {
      state.acctChannel = el.dataset.ch || '';
      $$('#acctTabs .ch-tab').forEach(b => b.classList.toggle('active', (b.dataset.ch || '') === state.acctChannel));
      renderAcctBody();
      break;
    }
    case 'toggle-group': {
      const ch = el.dataset.ch;
      state.collapsed[ch] = !state.collapsed[ch];
      const g = el.closest('.group');
      if (g) g.classList.toggle('collapsed', !!state.collapsed[ch]);
      break;
    }
    case 'acct-verify': doVerify(id); break;
    case 'acct-checkin': doCheckin(id); break;
    case 'acct-edit': openEdit(id); break;
    case 'acct-backup': openBackups(id); break;
    case 'acct-del': deleteAccount(id); break;
    case 'bk-restore': restoreBackup(id); break;
    case 'px-config': openProxyConfig(id); break;
    case 'px-check': proxyCheck(id); break;
    case 'px-direct': proxyDirect(id); break;
    case 'pool-save': openPoolSave(id); break;
    case 'pool-refresh': refreshPool(id); break;
    case 'pool-clear': clearPool(id); break;
    case 'job-cancel': cancelJob(id); break;
    case 'key-reveal': revealKey(id); break;
    case 'key-enable': toggleKey(id, true); break;
    case 'key-disable': toggleKey(id, false); break;
    case 'key-del': deleteKey(id); break;
    case 'antiban-reset': antibanReset(); break;
    case 'desc-toggle': toggleDesc(id); break;
  }
}

/* ---------------------------------------------------------------- 初始化 */
function applyTheme() {
  document.documentElement.setAttribute('data-theme', state.theme);
  $('#themeBtn').textContent = state.theme === 'dark' ? '◐' : '◑';
}

function wire() {
  // 顶栏
  $('#refreshBtn').addEventListener('click', () => { renderRoute(); toast('已刷新', 'info'); });
  $('#themeBtn').addEventListener('click', () => {
    state.theme = state.theme === 'dark' ? 'light' : 'dark';
    localStorage.setItem(LS_THEME, state.theme);
    applyTheme();
  });
  $('#keyBtn').addEventListener('click', () => { location.hash = '#keys'; });

  // 视图工具条
  // 渠道切换由账号页的渠道标签页（#acctTabs）承担，见 handleAction 的 acct-tab
  $('#acctCheckAll').addEventListener('click', opsVerifyAll);
  $('#acctCheckinAll').addEventListener('click', opsCheckinAll);
  $('#acctKeepaliveAll').addEventListener('click', opsKeepaliveAll);
  $('#acctBalanceAll').addEventListener('click', opsBalanceAll);
  $('#acctRefresh').addEventListener('click', loadAccounts);
  $('#acctImport').addEventListener('click', openImport);

  $('#pxChannelFilter').addEventListener('change', e => { state.pxChannel = e.target.value; loadProxies(); });
  $('#pxCheckAll').addEventListener('click', proxyCheckAll);
  $('#pxRefresh').addEventListener('click', loadProxies);
  $('#pxBulk').addEventListener('click', () => { $('#bulkText').value = ''; openModal('m-proxy-bulk'); });

  $('#jobStatus').addEventListener('change', e => { state.jobStatus = e.target.value; loadJobs(); });
  $('#jobChannel').addEventListener('change', e => { state.jobChannel = e.target.value; loadJobs(); });
  $('#jobsRefresh').addEventListener('click', loadJobs);

  $('#keyNew').addEventListener('click', openKeyCreate);
  $('#keysRefresh').addEventListener('click', loadKeys);

  $('#logChannel').addEventListener('change', e => { state.logChannel = e.target.value; loadLogs(); });
  $('#logLimit').addEventListener('change', e => { state.logLimit = Number(e.target.value) || 200; loadLogs(); });
  $('#logsRefresh').addEventListener('click', loadLogs);
  $$('#logChips .chip-btn').forEach(b => b.addEventListener('click', () => switchLogTab(b.dataset.logTab)));

  // 用量统计
  $('#usageDays').addEventListener('change', e => { state.usage.days = Number(e.target.value) || 7; loadUsage(); });
  $('#usageChannel').addEventListener('change', e => { state.usage.channel = e.target.value; loadUsage(); });
  $('#usageRefresh').addEventListener('click', loadUsage);

  // 模型与档位
  $('#modelSearch').addEventListener('input', e => {
    state.models.search = e.target.value;
    if (state.route === 'models') $('#body-models').innerHTML = modelsHtml();
  });
  $('#modelReload').addEventListener('click', reloadCatalog);
  $('#modelProbe').addEventListener('click', probeModels);
  $('#modelProbeChannel').addEventListener('change', e => { state.models.probeChannel = e.target.value; });
  $('#modelsRefresh').addEventListener('click', loadModels);

  // 配置
  $('#configReload').addEventListener('click', loadConfig);
  $('#configSave').addEventListener('click', saveConfig);

  // 弹窗关闭
  document.addEventListener('click', e => {
    const c = e.target.closest('[data-close]');
    if (c) { closeModal(c.dataset.close); return; }
    if (e.target.classList && e.target.classList.contains('modal')) closeModal(e.target.id);
  });
  document.addEventListener('keydown', e => {
    if (e.key !== 'Escape') return;
    const open = $$('.modal.open');
    if (open.length) closeModal(open[open.length - 1].id);
  });

  // 动作委托
  document.addEventListener('click', e => {
    const t = e.target.closest('[data-act]');
    if (t) handleAction(t.dataset.act, t);
  });

  // 密钥弹窗
  $('#keySave').addEventListener('click', saveKey);
  $('#keyInput').addEventListener('keydown', e => { if (e.key === 'Enter') saveKey(); });

  // 导入
  $$('#impTabs button').forEach(b => b.addEventListener('click', () => switchImpTab(b.dataset.impTab)));
  $('#impSubmit').addEventListener('click', submitImport);
  $('#impSendSms').addEventListener('click', sendSmsCode);
  $('#impOauthStart').addEventListener('click', startOauth);
  $('#impChannel').addEventListener('change', () => { refreshChannelLogin(); switchImpTab(impTab); });

  // 编辑
  $('#editSave').addEventListener('click', saveEdit);

  // 备份
  $('#bkCreate').addEventListener('click', createBackup);

  // 代理
  $('#pxSave').addEventListener('click', saveProxy);
  $('#pxDirect').addEventListener('click', () => proxyDirect($('#pxAccount').value));
  $('#bulkSubmit').addEventListener('click', submitBulk);

  // 订阅池
  $('#poolSave').addEventListener('click', savePool);

  // 密钥创建 / 查看
  $('#kcSubmit').addEventListener('click', submitKeyCreate);
  $('#revealCopy').addEventListener('click', async () => {
    const v = $('#revealVal').textContent;
    try { await navigator.clipboard.writeText(v); toast('已复制到剪贴板', 'ok'); }
    catch (_) { toast('复制失败，请手动选择', 'warn'); }
  });

  // 确认框
  $('#confirmYes').addEventListener('click', () => resolveConfirm(true));
  $('#confirmNo').addEventListener('click', () => resolveConfirm(false));

  // 登录 / 用户
  const lf = $('#loginForm'); if (lf) lf.addEventListener('submit', e => { e.preventDefault(); doLogin(); });
  const le = $('#loginEye'); if (le) le.addEventListener('click', togglePw);
  const lu = $('#loginUser'); if (lu) lu.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); $('#loginPass').focus(); } });
  const lo = $('#userLogout'); if (lo) lo.addEventListener('click', doLogout);
  const ub = $('#userBtn'); if (ub) ub.addEventListener('click', () => {
    const n = $('#uName'); if (n && state.user) n.textContent = state.user.username;
    openModal('m-user');
  });
  const pw = $('#pwSubmit'); if (pw) pw.addEventListener('click', changePassword);

  // 路由
  window.addEventListener('hashchange', navigate);
}

async function init() {
  applyTheme();
  wire();
  const ok = await checkAuth();
  if (!ok) {
    showLogin('请输入管理员账号密码登录。');
    navigate();
    return;
  }
  loadChannels().then(() => navigate());
}

init();
