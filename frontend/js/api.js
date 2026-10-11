/* 与后端交互的薄封装：所有错误都抛出带可读中文信息的 Error */

async function request(url, options = {}) {
  let resp;
  try {
    resp = await fetch(url, options);
  } catch (err) {
    throw new Error(`无法连接本地服务：${err.message}`);
  }
  const text = await resp.text();
  let data = null;
  if (text) {
    try { data = JSON.parse(text); } catch { data = { error: text.slice(0, 400) }; }
  }
  if (!resp.ok) {
    // FastAPI 的 detail 有几种形态：字符串、校验错误数组、或本项目约定的 {error, detail} 对象。
    // 统一压成「一条可读消息 + 可选字符串补充」。关键点：error.detail 必须是**字符串**，
    // 否则界面会把对象渲染成 [object Object]，把真正的原因（如「页码格式错误」）整个盖住。
    const d = data && data.detail;
    let msg = '';
    let detail = '';
    if (typeof d === 'string') {
      msg = d;
    } else if (Array.isArray(d)) {
      msg = d.map((it) => (it && (it.msg || it.error)) || JSON.stringify(it)).join('；');
    } else if (d && typeof d === 'object') {
      msg = typeof d.error === 'string' ? d.error : '';
      if (typeof d.detail === 'string') detail = d.detail;
    }
    if (!msg) {
      msg = (data && typeof data.error === 'string' && data.error) || resp.statusText || '请求失败';
    }
    if (!detail && data && typeof data.detail === 'string') detail = data.detail;
    const error = new Error(msg);
    error.detail = detail;
    error.status = resp.status;
    throw error;
  }
  return data;
}

const json = (body) => ({
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body ?? {}),
});

export const api = {
  base: '',

  /** 资产（渲染图 / PDF）路径 */
  asset(relPath) { return `/files/${relPath}`; },

  getConfig() { return request('/api/config'); },
  /** 连通性自检；传入 pending 可「先试后存」（不落盘） */
  testConfig(pending = null) {
    return request('/api/config/test', pending ? json(pending) : { method: 'POST' });
  },
  /** 保存界面设置（写入 data/runtime-config.json，立即生效） */
  saveConfig(payload) { return request('/api/config', { ...json(payload), method: 'PUT' }); },
  /** 清除界面设置，回落到 .env / config.json */
  clearConfigOverride() { return request('/api/config/override', { method: 'DELETE' }); },

  listDocuments() { return request('/api/documents'); },
  getDocument(docId) { return request(`/api/documents/${docId}`); },
  deleteDocument(docId, dropCache = false) {
    return request(`/api/documents/${docId}?drop_cache=${dropCache ? 'true' : 'false'}`, { method: 'DELETE' });
  },

  /** 直接以二进制 body 上传 PDF */
  uploadPdf(file, force = false) {
    const url = `/api/documents?filename=${encodeURIComponent(file.name)}&force=${force ? 'true' : 'false'}`;
    return request(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/pdf' },
      body: file,
    });
  },

  getPage(docId, pageNo) { return request(`/api/documents/${docId}/pages/${pageNo}`); },
  /** 用当前解析器重新解析已导入的 PDF（解析逻辑升级后需要） */
  reparsePdf(docId) { return request(`/api/documents/${docId}/reparse`, { method: 'POST' }); },
  getPlan(docId, pages = 'all', force = false) {
    return request(`/api/documents/${docId}/plan?pages=${encodeURIComponent(pages)}&force=${force}`);
  },

  translate(docId, pages = 'all', force = false) {
    return request(`/api/documents/${docId}/translate`, json({ pages, force }));
  },
  getJob(jobId) { return request(`/api/jobs/${jobId}`); },
  cancelJob(jobId) { return request(`/api/jobs/${jobId}/cancel`, { method: 'POST' }); },

  cacheStats() { return request('/api/cache/stats'); },
  clearCache(docId = '') {
    return request(`/api/cache${docId ? `?doc_id=${docId}` : ''}`, { method: 'DELETE' });
  },

  overlayUrl(docId, pageNo) { return `/api/debug/overlay/${docId}/${pageNo}`; },

  /** 订阅任务进度（SSE），返回可 close() 的 EventSource */
  subscribeJob(jobId, onSnapshot, onError) {
    const es = new EventSource(`/api/jobs/${jobId}/events`);
    es.onmessage = (ev) => {
      try { onSnapshot(JSON.parse(ev.data)); } catch (e) { /* 忽略心跳等非 JSON */ }
    };
    es.onerror = (err) => { if (onError) onError(err); };
    return es;
  },
};
