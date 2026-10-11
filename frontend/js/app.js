/* 应用主控：文档加载、翻页、翻译任务、缩放与交互编排 */
import { api } from './api.js';
import { Viewer } from './viewer.js';

const $ = (id) => document.getElementById(id);

const el = {
  viewer: $('viewer'),
  paneLeft: $('pane-left'),
  paneRight: $('pane-right'),
  divider: $('divider'),
  scrollLeft: $('scroll-left'),
  scrollRight: $('scroll-right'),
  stageLeft: $('stage-left'),
  stageRight: $('stage-right'),
  pageImage: $('page-image'),
  pageUnderlay: $('page-underlay'),
  overlayLeft: $('overlay-left'),
  transLayer: $('trans-layer'),
  markerLeft: $('marker-left'),
  markerRight: $('marker-right'),
  paneRightHint: $('pane-right-hint'),
  fileInput: $('file-input'),
  docSelect: $('doc-select'),
  docName: $('doc-name'),
  pageInput: $('page-input'),
  pageTotal: $('page-total'),
  rangeInput: $('range-input'),
  zoomRange: $('zoom-range'),
  zoomLabel: $('zoom-label'),
  fontRange: $('font-range'),
  chkForce: $('chk-force'),
  chkUnderlay: $('chk-underlay'),
  chkOverflow: $('chk-overflow'),
  btnCancel: $('btn-cancel'),
  btnLayout: $('btn-layout'),
  chipModel: $('chip-model'),
  chipCache: $('chip-cache'),
  chipPage: $('chip-page'),
  progressFill: $('progress-fill'),
  statusText: $('status-text'),
  errorBar: $('error-bar'),
  errorText: $('error-text'),
  popover: $('popover'),
  popSrc: $('pop-src'),
  popTgt: $('pop-tgt'),
  popTitle: $('pop-title'),
  toastWrap: $('toast-wrap'),
  dropHint: $('drop-hint'),
  helpDialog: $('help-dialog'),
  settingsDialog: $('settings-dialog'),
  cfgProvider: $('cfg-provider'),
  cfgBaseUrl: $('cfg-base-url'),
  cfgModel: $('cfg-model'),
  cfgApiKey: $('cfg-api-key'),
  cfgKeyToggle: $('cfg-key-toggle'),
  cfgKeyHint: $('cfg-key-hint'),
  cfgTemperature: $('cfg-temperature'),
  cfgMaxConcurrency: $('cfg-max-concurrency'),
  cfgSourceLang: $('cfg-source-lang'),
  cfgTargetLang: $('cfg-target-lang'),
  cfgRenderZoom: $('cfg-render-zoom'),
  cfgSkipRefs: $('cfg-skip-refs'),
  cfgTranslateFormulas: $('cfg-translate-formulas'),
  cfgTranslateTables: $('cfg-translate-tables'),
  cfgTranslateCaptions: $('cfg-translate-captions'),
  cfgInlineMath: $('cfg-inline-math'),
  cfgMock: $('cfg-mock'),
  cfgStatus: $('cfg-status'),
  cfgFile: $('cfg-file'),
  cfgTest: $('cfg-test'),
  cfgSave: $('cfg-save'),
};

const state = {
  cfg: null,
  doc: null,
  page: 1,
  pageData: null,
  job: null,
  es: null,
  busy: false,
  baseHint: '',     // 右栏提示的固定部分（本页已译 N/M 片段）
  fitInfo: null,    // 版式适配统计：自动缩字 / 向下延展
};

const viewer = new Viewer(
  {
    ...el,
    pageImage: el.pageImage,
  },
  {
    onSelect: (block, ev) => showPopover(block, ev),
    onZoom: (s) => {
      el.zoomRange.value = String(s);
      el.zoomLabel.textContent = `${Math.round(s * 100)}%`;
      saveLocal('scale', s);
    },
    onFit: (info) => renderFitHint(info),
  },
);

// ======================================================================
// 通用 UI 工具
// ======================================================================
function toast(message, type = 'info', ms = 3600) {
  const div = document.createElement('div');
  div.className = `toast ${type}`;
  div.textContent = message;
  el.toastWrap.appendChild(div);
  setTimeout(() => div.remove(), ms);
}

function setStatus(text) { el.statusText.textContent = text; }

function showError(message, detail = '') {
  el.errorText.textContent = detail ? `${message}\n${detail}` : message;
  el.errorBar.classList.remove('hidden');
}
function clearError() { el.errorBar.classList.add('hidden'); }

function setProgress(ratio) {
  el.progressFill.style.width = `${Math.max(0, Math.min(1, ratio)) * 100}%`;
}

function saveLocal(k, v) { try { localStorage.setItem(`paperReader.${k}`, JSON.stringify(v)); } catch { /* ignore */ } }
function loadLocal(k, dflt) {
  try {
    const raw = localStorage.getItem(`paperReader.${k}`);
    return raw === null ? dflt : JSON.parse(raw);
  } catch { return dflt; }
}

/** 右栏提示：本页翻译进度 + 原样保留的图表公式数量 + 版式适配情况 */
function pageHint(data) {
  const st = data.stats || {};
  const parts = [st.translatable
    ? `本页已译 ${st.translated}/${st.translatable} 片段`
    : '本页无可翻译片段'];
  const kept = [];
  if (st.figures) kept.push(`${st.figures} 图`);
  if (st.tables) kept.push(`${st.tables} 表`);
  if (st.formulas) kept.push(`${st.formulas} 公式`);
  if (kept.length) parts.push(`原样保留 ${kept.join(' / ')}`);
  if (st.hidden) parts.push(`表格 ${st.hidden} 格不译`);
  return parts.join(' · ');
}

/** 右栏提示：本页翻译进度 + 版式适配情况（自动缩字 / 向下延展） */
function renderFitHint(info = state.fitInfo) {
  if (info !== undefined) state.fitInfo = info;
  const parts = [];
  if (state.baseHint) parts.push(state.baseHint);
  const f = state.fitInfo;
  if (f && f.total) {
    if (f.shrunk) parts.push(`${f.shrunk} 处缩字落位`);
    if (f.spilling) parts.push(`${f.spilling} 处向下延展`);
  }
  el.paneRightHint.textContent = parts.join(' · ');
  el.paneRightHint.title = f && f.total
    ? `本页 ${f.total} 个译文片段：${f.shrunk} 个为落进原块（含下方可借用的空白）自动缩小了字号；`
      + `${f.spilling} 个缩到下限仍放不下，已向下延展以保证完整显示（可能压住下方片段）。\n`
      + '想看得更清楚：调大工具栏「字号」滑块，或放大页面缩放；勾「溢出全文」则不缩字、完整铺开。'
    : '';
}

// ======================================================================
// 启动
// ======================================================================
async function boot() {
  bindUI();
  try {
    state.cfg = await api.getConfig();
  } catch (err) {
    showError('读取服务端配置失败', err.message);
  }
  renderChips();
  if (state.cfg?.mock) {
    toast('当前为模拟翻译模式（LLM_MOCK=1），不会调用真实 API', 'warn', 6000);
  }

  const scale = loadLocal('scale', 0);
  const fontScale = loadLocal('fontScale', 1);
  el.fontRange.value = String(fontScale);
  viewer.setFontScale(fontScale);
  // 原图底衬默认开启：图片 / 表格 / 公式靠它原样呈现
  const originalBase = loadLocal('originalBase', true);
  el.chkUnderlay.checked = originalBase;
  viewer.setUnderlay(originalBase);
  if (scale) {
    viewer.setScale(scale);
    el.zoomRange.value = String(scale);
    el.zoomLabel.textContent = `${Math.round(scale * 100)}%`;
  }

  await refreshDocList();
  // 深链接：?doc=<docId>&page=<n> 可直接打开某份文档的某一页（可分享 / 刷新回到原处）
  const params = new URLSearchParams(location.search);
  const docParam = params.get('doc');
  const pageParam = Math.round(Number(params.get('page') || 0)) || 0;
  const last = docParam || loadLocal('lastDoc', null);
  if (last) {
    try {
      await loadDocument(last, { keepPage: true });
      if (pageParam > 1) await gotoPage(pageParam);
    } catch { /* 忽略，回落到空状态 */ }
  } else {
    setStatus('就绪：请点击「打开 PDF」导入英文论文（可直接拖拽文件到页面）');
  }

  // 首次运行 / 未配置密钥时，直接把配置窗口推到用户面前
  const needSetup = !state.cfg?.apiKeySet && !state.cfg?.mock;
  if (needSetup) {
    await openSettings();
    cfgStatus(
      '还没有可用的 API Key：\n· 在下面填好 API Key 后点「测试连接」验证，再点「保存并应用」；\n'
        + '· 也可以先勾选「模拟模式（不调用 API）」保存，用占位译文跑通整条链路。',
      '',
    );
  } else if (state.cfg?.overrideActive) {
    setStatus(`配置由「界面设置」接管：${(state.cfg.overriddenKeys || []).join('、')}`);
  }
}

function renderChips() {
  const c = state.cfg || {};
  const unconfigured = !c.apiKeySet && !c.mock;
  el.chipModel.textContent = unconfigured
    ? '模型：未配置（点击设置）'
    : c.mock
      ? `模型：模拟模式 / ${c.model || '--'}`
      : `模型：${c.provider || '--'} · ${c.model || '--'}`;
  el.chipModel.classList.toggle('chip-warn', unconfigured);
  el.chipModel.title = [
    `Base URL: ${c.baseUrl || '（未配置）'}`,
    `API Key: ${c.apiKeySet ? c.apiKeyMasked : '未配置'}`,
    c.overrideActive ? `界面设置接管：${(c.overriddenKeys || []).join('、')}` : '来源：.env / 环境变量',
    '点击打开「⚙ 设置」',
  ].join('\n');
  api.cacheStats().then((s) => {
    el.chipCache.textContent = `缓存：${s.entries} 条 / ${(s.dbBytes / 1024).toFixed(0)} KB`;
    el.chipCache.title = `缓存条目 ${s.entries}，已索引片段 ${s.indexedBlocks}，覆盖文档 ${s.documents}\n数据库：${s.dbPath}\n点击查看详情（Alt+点击清空缓存）`;
  }).catch(() => {});
}

async function refreshDocList() {
  try {
    const { documents } = await api.listDocuments();
    el.docSelect.innerHTML = '<option value="">（选择已导入文档）</option>';
    for (const d of documents) {
      const opt = document.createElement('option');
      opt.value = d.docId;
      opt.textContent = `${d.filename}（${d.pageCount} 页）`;
      el.docSelect.appendChild(opt);
    }
    if (state.doc) el.docSelect.value = state.doc.docId;
  } catch { /* ignore */ }
}

// ======================================================================
// 文档
// ======================================================================
async function openFile(file) {
  if (!file) return;
  if (!/\.pdf$/i.test(file.name) && file.type !== 'application/pdf') {
    showError('请选择 PDF 文件', `收到的文件类型：${file.type || '未知'}（${file.name}）`);
    return;
  }
  clearError();
  setStatus(`正在上传并解析「${file.name}」……`);
  setProgress(0.05);
  try {
    const summary = await api.uploadPdf(file, false);
    await applySummary(summary, '已解析');
  } catch (err) {
    setProgress(0);
    showError('PDF 导入失败', err.detail || err.message);
    setStatus('导入失败');
  }
}

/**
 * 用当前版本的解析器重新解析已打开的文档。
 *
 * 解析逻辑升级后（例如新增了行碎片缝合），磁盘上的 page-*.json 还是旧结构，
 * 右侧排版就会跟新版前端对不上——这个按钮就是给这种情况用的。译文缓存不受影响。
 */
async function reparseDoc() {
  if (!state.doc) {
    toast('请先打开一份 PDF', 'warn');
    return;
  }
  const { docId, filename } = state.doc;
  const ok = window.confirm(
    `用当前版本的解析器重新解析「${filename}」？\n\n`
    + '· 只重做「分块 / 坐标」，不改动译文缓存；\n'
    + '· 若某段原文被重新切分，该段需要重新翻译（勾上「重译」再点「翻译」）。',
  );
  if (!ok) return;
  clearError();
  setStatus('正在重新解析……');
  setProgress(0.05);
  try {
    const res = await api.reparsePdf(docId);
    await applySummary(res.document, '已重新解析');
  } catch (err) {
    setProgress(0);
    showError('重新解析失败', err.detail || err.message);
    setStatus('重新解析失败');
  }
}

/** 解析结果落地到界面：上传与「重新解析」共用同一条路径 */
async function applySummary(summary, label) {
  state.doc = summary;
  state.page = 1;
  await refreshDocList();
  renderDoc(summary);
  await gotoPage(1, { fitWidth: true });
  setProgress(1);
  setStatus(`${label}：${summary.filename}，共 ${summary.pageCount} 页（耗时 ${summary.parseSeconds ?? '-'}s）`);
  toast(`解析完成：${summary.pageCount} 页`, 'ok');
  autoPreview();
}

function renderDoc(doc) {
  state.doc = doc;
  el.docName.textContent = `${doc.filename} · ${doc.pageCount} 页`;
  el.pageTotal.textContent = `/ ${doc.pageCount}`;
  el.pageInput.max = String(doc.pageCount);
  el.chipPage.textContent = `第 1 页 / ${doc.pageCount}`;
  saveLocal('lastDoc', doc.docId);
  el.docSelect.value = doc.docId;
  syncUrl();
}

/** 把当前文档/页码写进地址栏（?doc=…&page=…），刷新或分享都能回到原处 */
function syncUrl() {
  if (!state.doc) return;
  const q = `?doc=${encodeURIComponent(state.doc.docId)}&page=${state.page || 1}`;
  if (location.search !== q) history.replaceState(null, '', q);
}

async function loadDocument(docId, { keepPage = false } = {}) {
  const doc = await api.getDocument(docId);
  state.doc = doc;
  renderDoc(doc);
  const page = keepPage ? Math.min(state.page || 1, doc.pageCount) : 1;
  await gotoPage(page, { fitWidth: true });
  setStatus(`已打开：${doc.filename}（${doc.pageCount} 页）`);
}

// ======================================================================
// 分页
// ======================================================================
async function gotoPage(pageNo, { fitWidth = false } = {}) {
  if (!state.doc) return;
  const total = state.doc.pageCount;
  const n = Math.max(1, Math.min(total, Math.round(pageNo) || 1));
  setStatus(`正在加载第 ${n} 页……`);
  let data;
  try {
    data = await api.getPage(state.doc.docId, n);
  } catch (err) {
    showError(`第 ${n} 页加载失败`, err.detail || err.message);
    setStatus('页面加载失败');
    return;
  }
  state.page = n;
  state.pageData = data;
  state.baseHint = pageHint(data);
  state.fitInfo = null;
  viewer.setPage(data);                       // 内部 layout 会触发 onFit → 刷新右栏提示
  if (fitWidth || loadLocal('scale', 0) === 0) viewer.fitWidth();
  el.pageInput.value = String(n);
  el.rangeInput.placeholder = `页码范围，如 ${n}-${Math.min(total, n + 9)}`;
  el.chipPage.textContent = `第 ${n} 页 / ${total}`;
  const s = viewer.scale;
  el.zoomRange.value = String(s);
  el.zoomLabel.textContent = `${Math.round(s * 100)}%`;
  el.scrollLeft.scrollTop = 0;
  el.scrollRight.scrollTop = 0;
  renderFitHint();
  const st = data.stats;
  setProgress(st.translatedRatio);
  syncUrl();
  setStatus(
    `第 ${n} 页：${st.blocks} 个片段（可译 ${st.translatable}，已译 ${st.translated}`
    + `；原样保留 ${data.regions?.length || 0} 个图表区域、${st.formulas} 个公式）`,
  );
  hidePopover();
}

const nextPage = (d) => gotoPage(state.page + d);
const prevPage = () => gotoPage(state.page - 1);

// ======================================================================
// 翻译
// ======================================================================
async function autoPreview() {
  if (!state.doc || !state.pageData) return;
  const st = state.pageData.stats;
  if (st.translated > 0 || st.translatable === 0) return;
  setStatus('正在翻译本页作为预览……');
  await startTranslate(String(state.page), false, { silent: true });
}

async function startTranslate(pages, force = false, { silent = false } = {}) {
  if (!state.doc) { toast('请先打开一份 PDF', 'warn'); return; }
  if (state.job) { toast('已有翻译任务在进行中', 'warn'); return; }
  clearError();
  try {
    const res = await api.translate(state.doc.docId, pages, force);
    state.job = res.snapshot;
    state.jobId = res.jobId;
    el.btnCancel.classList.remove('hidden');
    if (!silent) {
      const plan = res.plan;
      toast(`任务已开始：${plan.pages.length} 页，其中 ${plan.needsApi.length} 页需调用模型`, 'ok');
    }
    setStatus('翻译进行中……');
    watchJob(res.jobId);
  } catch (err) {
    showError('无法启动翻译任务', err.detail || err.message);
    setStatus('翻译启动失败');
  }
}

function watchJob(jobId) {
  if (state.es) { state.es.close(); state.es = null; }
  state.es = api.subscribeJob(
    jobId,
    (snap) => onJobSnapshot(snap),
    () => { /* 连接断开时由轮询兜底 */ },
  );
}

function onJobSnapshot(snap) {
  state.job = snap;
  const t = snap.totals;
  const ratio = t.blocks ? t.translated / t.blocks : 0;
  setProgress(ratio);
  const running = snap.status === 'running' || snap.status === 'pending';
  setStatus(
    `翻译${running ? '中' : '结束'}：片段 ${t.translated}/${t.blocks}（缓存 ${t.cached}，失败 ${t.failed}）· API ${t.apiCalls} 次`,
  );
  if (!running) {
    el.btnCancel.classList.add('hidden');
    if (state.es) { state.es.close(); state.es = null; }
    state.job = null;
    renderChips();
    if (snap.status === 'done') {
      toast(`翻译完成：${t.translated} 个片段（缓存命中 ${t.cached}）`, 'ok');
    } else if (snap.status === 'cancelled') {
      toast('任务已取消', 'warn');
    } else {
      const first = snap.errors?.[0];
      showError('翻译失败', first ? `${first.error}${first.detail ? `\n${first.detail}` : ''}` : '未知错误');
    }
    // 刷新当前页译文
    gotoPage(state.page);
  } else if (snap.errors?.length) {
    const first = snap.errors[snap.errors.length - 1];
    setStatus(`翻译中（有片段失败）：${first.error}`);
  }
}

async function cancelJob() {
  if (!state.jobId) return;
  try { await api.cancelJob(state.jobId); toast('已请求取消', 'warn'); } catch (err) { toast(err.message, 'error'); }
}

// ======================================================================
// 弹层
// ======================================================================
function showPopover(block, ev) {
  viewer.highlight(block.id);
  viewer.revealRight(block.id);
  const kindLabel = {
    figure: '图片区域', table: '表格', formula: '公式',
    reference: '参考文献', caption: '图表标题',
  }[block.kind] || block.kind;
  el.popTitle.textContent = block.isRegion
    ? `${kindLabel}（保留区域）`
    : `片段 ${block.id} · ${kindLabel} · 字号 ${block.fontSize}pt`;
  el.popSrc.textContent = block.text || '（该区域无可抽取文字，内容都在原图里）';
  el.popTgt.textContent = block.isRegion || !block.translatable
    ? '（按设定原样保留，不翻译；右侧显示的就是原文原图）'
    : block.translation || '（尚未翻译：点击「翻译」按钮）';
  el.popover.classList.remove('hidden');
  const pad = 12;
  const w = el.popover.offsetWidth;
  const h = el.popover.offsetHeight;
  let x = ev?.clientX ? ev.clientX + 14 : 120;
  let y = ev?.clientY ? ev.clientY + 14 : 120;
  x = Math.min(Math.max(pad, x), window.innerWidth - w - pad);
  y = Math.min(Math.max(pad, y), window.innerHeight - h - pad);
  el.popover.style.left = `${x}px`;
  el.popover.style.top = `${y}px`;
}
function hidePopover() { el.popover.classList.add('hidden'); }

// ======================================================================
// 翻译服务设置窗口
// ======================================================================
function cfgStatus(text, kind = '') {
  el.cfgStatus.classList.toggle('hidden', !text);
  el.cfgStatus.className = `cfg-status ${kind}`.trim();
  el.cfgStatus.textContent = text || '';
}

/** 服务商下拉：由后端预设填充，切换时自动带出 base URL 与模型名 */
function fillProviders(cfg) {
  const providers = cfg.providers || {};
  const cur = cfg.provider || 'deepseek';
  el.cfgProvider.innerHTML = '';
  for (const [name, preset] of Object.entries(providers)) {
    const opt = document.createElement('option');
    opt.value = name;
    opt.textContent = preset.baseUrl ? `${name}（${preset.baseUrl}）` : `${name}（自定义，需手填）`;
    el.cfgProvider.appendChild(opt);
  }
  if (!providers[cur]) {
    const opt = document.createElement('option');
    opt.value = cur;
    opt.textContent = cur;
    el.cfgProvider.appendChild(opt);
  }
  el.cfgProvider.value = cur;
}

function fillSettingsForm(cfg) {
  const e = cfg.editable || {};
  fillProviders(cfg);
  el.cfgBaseUrl.value = e.baseUrl || '';
  el.cfgModel.value = e.model || '';
  el.cfgTemperature.value = e.temperature ?? 0.2;
  el.cfgMaxConcurrency.value = e.maxConcurrency ?? 4;
  el.cfgSourceLang.value = e.sourceLang || '英语';
  el.cfgTargetLang.value = e.targetLang || '简体中文';
  el.cfgRenderZoom.value = e.renderZoom ?? 2;
  el.cfgSkipRefs.checked = !!e.skipReferenceBlocks;
  el.cfgTranslateFormulas.checked = !!e.translateFormulaBlocks;
  el.cfgTranslateTables.checked = !!e.translateTables;
  el.cfgTranslateCaptions.checked = !!e.translateCaptions;
  el.cfgInlineMath.checked = e.inlineMathImage !== false;
  el.cfgMock.checked = !!e.mock;

  el.cfgApiKey.value = '';           // 密钥不回显，留空 = 不修改
  el.cfgApiKey.type = 'password';
  el.cfgKeyToggle.textContent = '显示';
  el.cfgKeyHint.textContent = e.apiKeySet
    ? `已配置（${e.apiKeyMasked}）· 留空表示不修改`
    : '尚未配置 · 填写后点「保存并应用」';
  el.cfgFile.textContent = cfg.overrideFile || 'data/runtime-config.json';

  const layers = [];
  if (cfg.overrideActive) layers.push(`界面设置（${(cfg.overriddenKeys || []).join('、')}）`);
  if (cfg.envFile) layers.push('.env');
  if (cfg.configFile) layers.push('config.json');
  layers.push('内置默认值');
  const srcNote = `当前生效顺序：${layers.join(' → ')}`;
  cfgStatus(cfg.overrideActive
    ? `已由界面设置接管部分配置项；点「清除界面设置」可恢复由 .env 控制。\n${srcNote}`
    : `${srcNote}\n当前未使用界面设置，配置全部来自 .env / 环境变量。`, '');
}

/** 收集表单值（只提交有意义的字段；API Key 留空则不下发） */
function collectSettingsForm() {
  const payload = {
    provider: el.cfgProvider.value,
    baseUrl: el.cfgBaseUrl.value.trim(),
    model: el.cfgModel.value.trim(),
    temperature: Number(el.cfgTemperature.value),
    maxConcurrency: Number(el.cfgMaxConcurrency.value),
    sourceLang: el.cfgSourceLang.value.trim() || '英语',
    targetLang: el.cfgTargetLang.value.trim() || '简体中文',
    renderZoom: Number(el.cfgRenderZoom.value),
    skipReferenceBlocks: el.cfgSkipRefs.checked,
    translateFormulaBlocks: el.cfgTranslateFormulas.checked,
    translateTables: el.cfgTranslateTables.checked,
    translateCaptions: el.cfgTranslateCaptions.checked,
    inlineMathImage: el.cfgInlineMath.checked,
    mock: el.cfgMock.checked,
  };
  const key = el.cfgApiKey.value.trim();
  if (key) payload.apiKey = key;
  return payload;
}

function onProviderChange() {
  const name = el.cfgProvider.value;
  const preset = state.cfg?.providers?.[name];
  if (!preset) return;
  // 仅当目标预设非空才覆盖，避免把用户手填的内容清掉
  if (preset.baseUrl) el.cfgBaseUrl.value = preset.baseUrl;
  if (preset.model) el.cfgModel.value = preset.model;
  if (name === 'custom') {
    cfgStatus('custom 为自定义服务商：请手动填写 Base URL 与模型名（需兼容 OpenAI 的 /chat/completions 协议）。');
  } else {
    cfgStatus('');
  }
}

async function refreshConfig() {
  try {
    state.cfg = await api.getConfig();
  } catch (err) {
    showError('读取服务端配置失败', err.message);
    return;
  }
  renderChips();
}

async function openSettings() {
  try {
    state.cfg = await api.getConfig();
  } catch (err) {
    showError('读取服务端配置失败', err.message);
    return;
  }
  fillSettingsForm(state.cfg);
  renderChips();
  if (!el.settingsDialog.open) el.settingsDialog.showModal();
}

async function testSettingsConnection() {
  const payload = collectSettingsForm();
  el.cfgTest.disabled = true;
  cfgStatus('正在向服务商发起一次极短翻译请求……', 'busy');
  try {
    const r = await api.testConfig(payload);
    if (r.ok) {
      const tag = r.mock ? '（模拟模式：未发起真实请求）' : '';
      cfgStatus(
        `连接成功${tag}：${r.provider} · ${r.model}\nBase URL：${r.baseUrl}\n往返 ${r.latencyMs} ms\n示例译文：${r.sample}`,
        'ok',
      );
      toast('连接测试通过', 'ok');
    } else {
      cfgStatus(`连接失败：${r.error}${r.detail ? `\n\n${r.detail}` : ''}`, 'err');
    }
  } catch (err) {
    cfgStatus(`连接失败：${err.message}${err.detail ? `\n\n${err.detail}` : ''}`, 'err');
  } finally {
    el.cfgTest.disabled = false;
  }
}

async function saveSettings() {
  const payload = collectSettingsForm();
  const before = state.cfg?.editable || {};
  const modelChanged = before.provider !== payload.provider
    || before.model !== payload.model
    || before.sourceLang !== payload.sourceLang
    || before.targetLang !== payload.targetLang;
  el.cfgSave.disabled = true;
  cfgStatus('正在保存……', 'busy');
  try {
    const r = await api.saveConfig(payload);
    await refreshConfig();
    fillSettingsForm(state.cfg);
    const extra = modelChanged
      ? '\n\n注意：服务商 / 模型 / 语言已变更。缓存以「模型 + 语言 + 原文」为键，'
        + '因此已有译文不再复用，右侧会回到未翻译状态，点「翻译」即按新模型重新翻译（首次会调用 API）。'
      : '';
    cfgStatus(
      `已保存并立即生效：\n${r.savedTo}\n\n界面设置优先级最高，会覆盖 .env 的同名项。${extra}`,
      'ok',
    );
    toast(modelChanged ? '设置已保存，换模型后请重新翻译' : '设置已保存并生效', 'ok');
    if (r.warnings?.length) showError('配置提醒', r.warnings.join('；'));
    else clearError();
    if (modelChanged && state.doc) await gotoPage(state.page);
  } catch (err) {
    cfgStatus(`保存失败：${err.message}${err.detail ? `\n\n${err.detail}` : ''}`, 'err');
  } finally {
    el.cfgSave.disabled = false;
  }
}

async function clearSettingsOverride() {
  if (!confirm('确认清除「界面设置」？\n删除后配置将回落到 .env / config.json / 环境变量。')) return;
  try {
    const r = await api.clearConfigOverride();
    await refreshConfig();
    fillSettingsForm(state.cfg);
    cfgStatus(r.removed
      ? '已清除界面设置，配置重新由 .env / 环境变量控制。'
      : '当前没有界面设置文件，无需清除。', 'ok');
    toast('已清除界面设置', 'warn');
  } catch (err) {
    cfgStatus(`清除失败：${err.message}`, 'err');
  }
}

// ======================================================================
// 交互绑定
// ======================================================================
function bindUI() {
  $('btn-open').addEventListener('click', () => el.fileInput.click());
  $('btn-reparse').addEventListener('click', reparseDoc);
  el.fileInput.addEventListener('change', (e) => { openFile(e.target.files[0]); e.target.value = ''; });

  el.docSelect.addEventListener('change', async (e) => {
    if (!e.target.value) return;
    try { await loadDocument(e.target.value); } catch (err) { showError('打开文档失败', err.message); }
  });

  $('btn-first').addEventListener('click', () => gotoPage(1));
  $('btn-prev').addEventListener('click', prevPage);
  $('btn-next').addEventListener('click', () => nextPage(1));
  $('btn-last').addEventListener('click', () => state.doc && gotoPage(state.doc.pageCount));
  el.pageInput.addEventListener('change', (e) => gotoPage(Number(e.target.value)));

  $('btn-translate').addEventListener('click', () => {
    const range = el.rangeInput.value.trim();
    startTranslate(range || String(state.page), el.chkForce.checked);
  });
  $('btn-translate-all').addEventListener('click', () => startTranslate('all', el.chkForce.checked));
  el.btnCancel.addEventListener('click', cancelJob);

  el.zoomRange.addEventListener('input', (e) => viewer.setScale(Number(e.target.value)));
  $('btn-zoom-in').addEventListener('click', () => viewer.setScale(viewer.scale * 1.12));
  $('btn-zoom-out').addEventListener('click', () => viewer.setScale(viewer.scale / 1.12));
  $('btn-fit-width').addEventListener('click', () => viewer.fitWidth());
  $('btn-fit-page').addEventListener('click', () => viewer.fitPage());
  $('btn-zoom-reset').addEventListener('click', () => viewer.setScale(1));

  el.fontRange.addEventListener('input', (e) => {
    viewer.setFontScale(Number(e.target.value));
    saveLocal('fontScale', Number(e.target.value));
  });

  el.btnLayout.addEventListener('click', () => {
    const single = el.viewer.classList.toggle('single');
    el.btnLayout.textContent = single ? '仅原文' : '左右对照';
    if (!single) viewer.fitWidth();
  });
  el.chkUnderlay.addEventListener('change', (e) => {
    viewer.setUnderlay(e.target.checked);
    saveLocal('originalBase', e.target.checked);
    setStatus(e.target.checked
      ? '已开启「原图底衬」：右栏以原页图为底，图片 / 表格 / 公式原样显示，仅译文片段为白底中文'
      : '已关闭「原图底衬」：右栏只显示译文，保留原文的片段显示为虚线空框');
  });
  el.chkOverflow.addEventListener('change', (e) => {
    viewer.setOverflow(e.target.checked);
    setStatus(e.target.checked
      ? '已开启「溢出全文」：不再自动缩字，译文按原字号完整铺开（可能压住下方片段）'
      : '已关闭「溢出全文」：译文优先落进原块（必要时缩字 / 借用下方空白），仍放不下才向下延展');
  });

  el.chipModel.addEventListener('click', () => openSettings());

  el.chipCache.addEventListener('click', async (e) => {
    const s = await api.cacheStats();
    if (e.altKey) {
      if (!confirm(`确认清空全部翻译缓存？\n当前 ${s.entries} 条译文将被删除（下次翻译需重新调用 API）。`)) return;
      await api.clearCache();
      toast('缓存已清空', 'warn');
    } else {
      toast(`缓存：${s.entries} 条译文 / 已索引 ${s.indexedBlocks} 片段 / ${(s.dbBytes / 1024).toFixed(0)} KB\n（Alt+点击可清空）`, 'info', 5200);
    }
    renderChips();
  });

  $('btn-help').addEventListener('click', () => el.helpDialog.showModal());
  $('help-close').addEventListener('click', () => el.helpDialog.close());

  // ---- 翻译服务设置窗口 ----
  $('btn-settings').addEventListener('click', () => openSettings());
  $('cfg-close').addEventListener('click', () => el.settingsDialog.close());
  el.cfgTest.addEventListener('click', () => testSettingsConnection());
  el.cfgSave.addEventListener('click', () => saveSettings());
  $('cfg-clear').addEventListener('click', () => clearSettingsOverride());
  el.cfgProvider.addEventListener('change', () => onProviderChange());
  el.cfgKeyToggle.addEventListener('click', () => {
    const show = el.cfgApiKey.type === 'password';
    el.cfgApiKey.type = show ? 'text' : 'password';
    el.cfgKeyToggle.textContent = show ? '隐藏' : '显示';
    el.cfgApiKey.focus();
  });
  el.settingsDialog.addEventListener('click', (ev) => {
    // 点击遮罩区域关闭
    if (ev.target === el.settingsDialog) el.settingsDialog.close();
  });

  $('error-close').addEventListener('click', clearError);
  $('pop-close').addEventListener('click', hidePopover);
  $('pop-copy-src').addEventListener('click', () => {
    navigator.clipboard.writeText(el.popSrc.textContent || '').then(() => toast('已复制原文', 'ok', 1600));
  });
  $('pop-copy-tgt').addEventListener('click', () => {
    navigator.clipboard.writeText(el.popTgt.textContent || '').then(() => toast('已复制译文', 'ok', 1600));
  });

  // 拖动分隔条调整左右宽度
  el.divider.addEventListener('mousedown', (ev) => {
    ev.preventDefault();
    const rect = el.viewer.getBoundingClientRect();
    document.body.style.cursor = 'col-resize';
    const move = (e) => {
      const pct = Math.min(0.78, Math.max(0.22, (e.clientX - rect.left) / rect.width));
      el.paneLeft.style.flex = `0 0 ${(pct * 100).toFixed(2)}%`;
      el.paneRight.style.flex = '1 1 0';
      viewer.fitWidth();
    };
    const up = () => {
      document.removeEventListener('mousemove', move);
      document.removeEventListener('mouseup', up);
      document.body.style.cursor = '';
    };
    document.addEventListener('mousemove', move);
    document.addEventListener('mouseup', up);
  });

  // 拖拽打开 PDF
  let dragDepth = 0;
  window.addEventListener('dragenter', (e) => {
    if (![...(e.dataTransfer?.types || [])].includes('Files')) return;
    dragDepth += 1;
    el.dropHint.classList.remove('hidden');
  });
  window.addEventListener('dragover', (e) => e.preventDefault());
  window.addEventListener('dragleave', () => {
    dragDepth = Math.max(0, dragDepth - 1);
    if (!dragDepth) el.dropHint.classList.add('hidden');
  });
  window.addEventListener('drop', (e) => {
    e.preventDefault();
    dragDepth = 0;
    el.dropHint.classList.add('hidden');
    const file = e.dataTransfer?.files?.[0];
    if (file) openFile(file);
  });

  // 快捷键
  window.addEventListener('keydown', (e) => {
    const tag = (e.target.tagName || '').toLowerCase();
    const typing = tag === 'input' || tag === 'textarea' || tag === 'select';
    if (e.key === 'Escape') {
      hidePopover();
      el.helpDialog.close();
      el.settingsDialog.close();
      return;
    }
    if (typing && !(e.ctrlKey || e.metaKey)) return;
    if (el.settingsDialog.open || el.helpDialog.open) return;
    switch (e.key) {
      case 'ArrowLeft': e.preventDefault(); prevPage(); break;
      case 'ArrowRight': e.preventDefault(); nextPage(1); break;
      case '+': case '=': e.preventDefault(); viewer.setScale(viewer.scale * 1.12); break;
      case '-': case '_': e.preventDefault(); viewer.setScale(viewer.scale / 1.12); break;
      case '0': e.preventDefault(); viewer.fitWidth(); break;
      case '9': e.preventDefault(); viewer.fitPage(); break;
      case '8': e.preventDefault(); viewer.setScale(1); break;
      case 't': case 'T': e.preventDefault(); startTranslate(String(state.page), el.chkForce.checked); break;
      case 'a': case 'A': e.preventDefault(); startTranslate('all', el.chkForce.checked); break;
      default: break;
    }
  });

  // 窗口尺寸变化时保持"适应宽度"
  let timer = null;
  window.addEventListener('resize', () => {
    clearTimeout(timer);
    timer = setTimeout(() => {
      if (!state.doc) return;
      if (Math.abs(viewer.scale - viewer.viewportSize.width / viewer.page.width) < 0.02) viewer.fitWidth();
    }, 160);
  });
}

boot();
