/* 对照视图引擎：坐标系换算、缩放联动、悬停高亮、滚动同步
 *
 * 渲染模型 —— 连续滚动（多页纵向堆叠）
 * ------------------------------------
 * 左右两栏各是一个**纵向堆叠**的页面列表（`.page-stack`），滚轮往下就是下一页：
 *   左栏 stage = 原页渲染图 + 命中层（`.overlay`）
 *   右栏 stage = 原页渲染图（底衬） + 译文层（`.trans-layer`）
 * 文档一打开就按元数据里的 width/height 把**所有页的占位**铺好（滚动条长度立刻正确），
 * 每页的块 / 译文在滚到附近时才按需取回并填充；已填充的页保留（来回滚动不抖、不重复请求）。
 *
 * 与旧版的区别：旧版一次只渲染一页（`setPage`），前后翻页靠重新加载；现在同一个
 * `.page-stack` 里逐页排开，`scrollToPage(n)` 只是滚动定位，`_updateCurrentPage()`
 * 依据滚动位置反推"当前第几页"。
 *
 * 坐标：PDF 坐标（pt，左上原点）由后端给出，前端只用同一个 `scale` 换算，左右共用。
 */
import { api } from './api.js';

/* 渲染要点
 * --------
 * 右栏「译文层」以**原页渲染图**为底（可关），只有可译片段才用白底 + 中文覆盖上去。
 * 于是图片 / 表格 / 公式 / 参考文献这些"按设计不翻译"的区域，天然原样呈现，
 * 不会出现中文盖住公式、把表格行列拆散的问题。前端要做的只有两件事：
 *   1. 可译片段 → 白底译文块（覆盖掉原来的英文）；
 *   2. 不可译片段 → 不渲染任何文字，只在悬停时给一个轮廓，另外把「保留区域」
 *      （图片框 / 表格框）画成虚线框，方便用户看出这块是原样保留的。
 */
const STRUCTURAL = new Set(['margin', 'pageno', 'meta', 'empty']); // 完全不进交互层
const KIND_LABEL = {
  formula: '公式', reference: '参考文献', figure: '图片区域', table: '表格', caption: '图表标题',
};

const FIT_MIN_RATIO = 0.55;   // 自动缩字的下限（相对原文字号），再小就没法读了
const LATIN_UNITS = 0.52;     // 一个拉丁字符约占 0.52 em 宽
const CJK_FROM = 0x2e80;      // 码点大于此值按 1 em 宽估算（中日韩文字与全角标点）
const MATH_TOKEN_RE = /⟦\s*(\d+)\s*⟧/g;   // 行内公式占位符（与后端 MATH_TOKEN_RE 对应）
const LINE_UNIT = 1.32;       // 折行估算里的默认行高（em）

const PREFETCH_FIRST = 3;     // 打开文档时先取前几页
const PREFETCH_AHEAD = 3;     // 滚动时向后预取几页
const FIT_WINDOW = 2;         // 需要做"实测校正"的页：当前页 ± 该值

export class Viewer {
  /**
   * @param {object} refs DOM 引用集合
   * @param {object} hooks {
   *   onSelect(block, event), onStatus(text), onFit(info), onZoom(scale),
   *   onPageChange(pageNo), loadPage(pageNo) -> Promise<pageData>
   * }
   */
  constructor(refs, hooks = {}) {
    this.el = refs;
    this.hooks = hooks;
    this.doc = null;             // { docId, pageCount, pages: [{pageNo,width,height,image}] }
    this.scale = 1;
    this.fontScale = 1;
    this.strict = true;
    this.overflowMode = false;   // false = 按可用高度自动缩字（缩不下才向下延展）；true = 不缩字，完整显示
    this.showUnderlay = true;    // 原图底衬：右栏是否以原页图为底（默认开）
    this.hoverHighlight = false; // 鼠标悬停是否联动高亮（默认关：路过页面时不打扰）
    this.activeId = null;

    this.slots = new Map();      // pageNo -> slot（含左右两栏的 stage 与块元素）
    this.pageOrder = [];         // 页号升序
    this.loading = new Set();    // 正在请求的页
    this.elById = new Map();     // blockId / regionId -> { hit, tblock, preserve, slot, regionId }
    this.targetById = new Map(); // blockId / regionId -> 块对象（供弹层显示）
    this._current = 1;
    this._syncing = false;
    this._suppress = false;
    this._bind();
  }

  get currentPage() { return this._current; }

  /** 当前页的页面尺寸（供外部判断"是否处于适应宽度"） */
  get pageSize() { return this._sizeOf(this._current); }

  /** 当前页的页面尺寸（来自元数据，未加载内容时也可用） */
  _sizeOf(pageNo) {
    const slot = this.slots.get(pageNo);
    return slot ? { width: slot.meta.width, height: slot.meta.height } : null;
  }

  // ---------------------------------------------------------------- 事件绑定
  _bind() {
    const { stackLeft, stackRight, scrollLeft, scrollRight } = this.el;
    const clearHover = () => {
      if (!this.hoverHighlight) return;
      this._clearMarker();
      this.highlight(null);
    };

    // 事件委托：所有页面都在同一个 .page-stack 里，监听挂在容器上即可
    stackLeft.addEventListener('mousemove', (ev) => this._onPointerMove(ev, 'left'));
    stackLeft.addEventListener('mouseleave', clearHover);
    stackLeft.addEventListener('click', (ev) => {
      const blk = this._onPointerMove(ev, 'left', true);
      if (blk) this.hooks.onSelect?.(blk, ev);
    });

    const idOf = (ev) => {
      const node = ev.target instanceof Element ? ev.target.closest('[data-id]') : null;
      return node ? node.dataset.id : null;
    };
    stackRight.addEventListener('mousemove', (ev) => {
      if (!this.hoverHighlight) return;
      const id = idOf(ev);
      const blk = id ? this.block(id) : null;
      if (blk) { this.highlight(id); this._setMarker(blk, 'right', this.elById.get(id)?.slot); }
    });
    stackRight.addEventListener('mouseleave', clearHover);
    stackRight.addEventListener('click', (ev) => {
      const id = idOf(ev);
      const blk = id ? this.block(id) : null;
      if (blk) this.hooks.onSelect?.(blk, ev);
    });

    // 滚动同步（比例映射，等比联动）+ 当前页判定 + 按需预取
    const onScroll = (src, dst) => () => {
      if (!this._syncing) {
        this._syncing = true;
        requestAnimationFrame(() => {
          try {
            const sx = src.scrollLeft, sy = src.scrollTop;
            const rx = src.scrollWidth - src.clientWidth;
            const ry = src.scrollHeight - src.clientHeight;
            const dx = dst.scrollWidth - dst.clientWidth;
            const dy = dst.scrollHeight - dst.clientHeight;
            dst.scrollLeft = rx > 0 ? (sx / rx) * dx : 0;
            dst.scrollTop = ry > 0 ? (sy / ry) * dy : 0;
          } finally {
            this._syncing = false;
          }
        });
      }
      this._updateCurrentPage();
      this.prefetch();
    };
    scrollLeft.addEventListener('scroll', onScroll(scrollLeft, scrollRight), { passive: true });
    scrollRight.addEventListener('scroll', onScroll(scrollRight, scrollLeft), { passive: true });

    // Ctrl + 滚轮缩放（以指针为锚点）
    const wheel = (pane) => (ev) => {
      if (!ev.ctrlKey && !ev.metaKey) return;
      ev.preventDefault();
      const anchor = this._contentPoint(ev, pane);
      const factor = Math.exp(-ev.deltaY * 0.0018);
      this.setScale(this.scale * factor, anchor);
    };
    scrollLeft.addEventListener('wheel', wheel('left'), { passive: false });
    scrollRight.addEventListener('wheel', wheel('right'), { passive: false });
  }

  // ---------------------------------------------------------------- 文档 / 页面铺位
  /**
   * 打开一份文档：按元数据把所有页的占位铺好（尺寸正确 → 滚动条长度立刻正确），
   * 然后预取前几页的内容。
   */
  setDocument(doc) {
    this.doc = doc;
    this.activeId = null;
    this.slots.clear();
    this.pageOrder = [];
    this.loading.clear();
    this.elById.clear();
    this.targetById.clear();
    // 只移除「页面槽」，保留堆叠容器里的十字标记元素（它们是常驻的）
    for (const stack of [this.el.stackLeft, this.el.stackRight]) {
      stack.querySelectorAll('.page-slot').forEach((n) => n.remove());
    }

    const pages = doc.pages || [];
    for (const meta of pages) this._createSlot(meta);

    this._current = 1;
    this.layout();
    for (let p = 1; p <= Math.min(pages.length, PREFETCH_FIRST); p += 1) this.ensurePage(p);
  }

  _createSlot(meta) {
    const pageNo = meta.pageNo;
    const url = api.asset(meta.image);

    const lslot = document.createElement('div');
    lslot.className = 'page-slot';
    lslot.dataset.page = String(pageNo);
    const lstage = document.createElement('div');
    lstage.className = 'stage';
    const limg = document.createElement('img');
    limg.className = 'page-image';
    limg.loading = 'lazy';
    limg.decoding = 'async';
    limg.alt = `第 ${pageNo} 页原文`;
    limg.src = url;
    const loverlay = document.createElement('div');
    loverlay.className = 'overlay';
    lstage.append(limg, loverlay);
    lslot.appendChild(lstage);
    this.el.stackLeft.appendChild(lslot);

    const rslot = document.createElement('div');
    rslot.className = 'page-slot';
    rslot.dataset.page = String(pageNo);
    const rstage = document.createElement('div');
    rstage.className = 'stage';
    const runder = document.createElement('img');
    runder.className = 'page-image underlay';
    runder.loading = 'lazy';
    runder.decoding = 'async';
    runder.alt = '';
    runder.src = url;
    const rtrans = document.createElement('div');
    rtrans.className = 'trans-layer';
    rstage.append(runder, rtrans);
    rslot.appendChild(rstage);
    this.el.stackRight.appendChild(rslot);

    const slot = {
      pageNo, meta, data: null,
      left: { stage: lstage, img: limg, overlay: loverlay },
      right: { stage: rstage, underlay: runder, trans: rtrans },
      blockEls: new Map(), targets: [], rooms: new Map(), fit: null, hasContent: false,
    };
    this.slots.set(pageNo, slot);
    this.pageOrder.push(pageNo);
    return slot;
  }

  /** 取回某页内容（带并发去重）；页面内容到位后填充 DOM */
  async ensurePage(pageNo) {
    const slot = this.slots.get(pageNo);
    if (!slot || slot.hasContent || this.loading.has(pageNo) || !this.hooks.loadPage) return;
    this.loading.add(pageNo);
    try {
      const data = await this.hooks.loadPage(pageNo);
      if (data) this.setPageData(pageNo, data);
    } catch { /* 单页取失败不影响其它页 */ } finally {
      this.loading.delete(pageNo);
    }
  }

  /** 滚动到附近时预取后续页 */
  prefetch() {
    if (!this.doc) return;
    const total = this.slots.size;
    for (let p = this._current; p <= Math.min(total, this._current + PREFETCH_AHEAD); p += 1) {
      this.ensurePage(p);
    }
  }

  /** 内容到位：填充块 / 译文，并做一次版式适配 */
  setPageData(pageNo, data) {
    const slot = this.slots.get(pageNo);
    if (!slot) return;
    slot.data = data;
    if (slot.hasContent) return;
    this._buildContent(slot, data);
    slot.hasContent = true;
    this._layoutSlot(slot);
    this._correctFitSlot(slot);
    this._markFitSlot(slot);
    if (this._inFitWindow(pageNo)) this._emitFit();
  }

  _inFitWindow(pageNo) {
    return Math.abs(pageNo - this._current) <= FIT_WINDOW;
  }

  /** 已经填充过内容的页号（用于"翻译完成后只刷新这些页"） */
  renderedPages() {
    return this.pageOrder.filter((p) => this.slots.get(p)?.hasContent);
  }

  /** 用新数据就地重建某页内容（翻译完成 / 换模型后刷新该页译文，不动其它页与滚动位置） */
  refreshPage(pageNo, data) {
    const slot = this.slots.get(pageNo);
    if (!slot) return;
    const pre = `p${pageNo}-`;        // 块 id 形如 p3-b5、区域 id 形如 p3-region-0
    for (const id of [...this.elById.keys()]) if (id.startsWith(pre)) this.elById.delete(id);
    for (const id of [...this.targetById.keys()]) if (id.startsWith(pre)) this.targetById.delete(id);
    slot.hasContent = false;
    this.setPageData(pageNo, data);
  }

  // ---------------------------------------------------------------- 内容渲染
  _buildContent(slot, data) {
    const { left, right } = slot;
    left.overlay.innerHTML = '';
    right.trans.innerHTML = '';
    slot.blockEls.clear();
    slot.targets = [];

    // ---- 保留区域（图片 / 表格）：画虚框 + 可命中，不渲染任何文字 ----
    const regions = data.regions || [];
    regions.forEach((reg, i) => {
      const id = `p${slot.pageNo}-region-${i}`;
      const box = document.createElement('div');
      box.className = `preserve region type-${reg.type}`;
      box.dataset.id = id;
      box.title = `${reg.type === 'table' ? '表格' : '图片'}区域：按设定原样保留，不翻译`
        + (reg.label ? `\n${reg.label}` : '');
      right.trans.appendChild(box);
      const hit = document.createElement('div');
      hit.className = 'hit region-hit';
      hit.dataset.id = id;
      hit.dataset.region = String(i);
      hit.title = box.title;
      left.overlay.appendChild(hit);
      const target = {
        id, isRegion: true, regionIndex: i, pageNo: slot.pageNo,
        kind: reg.type === 'table' ? 'table' : 'figure',
        bbox: reg.bbox, text: reg.label || '', fontSize: 6,
        translatable: false, translation: '', lineCount: 0,
      };
      slot.targets.push(target);
      this.targetById.set(id, target);
      this.elById.set(id, { hit, preserve: box, tblock: null, slot, regionId: null });
    });

    // ---- 文本块 ----
    for (const blk of data.blocks) {
      if (STRUCTURAL.has(blk.kind) || blk.hidden) continue;
      const inRegion = blk.regionIndex !== null && blk.regionIndex !== undefined;

      // 左栏（原文）命中区：始终按真实坐标建，悬停即可高亮
      const hit = document.createElement('div');
      hit.className = `hit kind-${blk.kind}`;
      hit.dataset.id = blk.id;
      hit.title = blk.text ? blk.text.slice(0, 160) : '';
      left.overlay.appendChild(hit);

      let tblock = null;
      let preserve = null;
      if (blk.translatable) {
        tblock = document.createElement('div');
        const empty = !blk.translation;
        tblock.className = `tblock kind-${blk.kind}` + (empty ? ' empty' : '');
        tblock.dataset.id = blk.id;
        if (empty) {
          tblock.textContent = '';
        } else {
          this._fillTranslation(tblock, blk, slot.meta);
        }
        right.trans.appendChild(tblock);
      } else if (!inRegion) {
        // 不在区域内的"保留原文"块（公式 / 参考文献 / 图表标题）：不写文字，
        // 只留一个轮廓层，悬停时会亮起来，让用户知道这里保留了原文。
        preserve = document.createElement('div');
        preserve.className = `preserve kind-${blk.kind}`;
        preserve.dataset.id = blk.id;
        const label = KIND_LABEL[blk.kind] || '该片段';
        preserve.title = `${label}：按设定保留原文，不翻译（可在「⚙ 设置」里调整）`;
        right.trans.appendChild(preserve);
      }

      slot.blockEls.set(blk.id, {
        hit, tblock, preserve,
        region: inRegion ? blk.regionIndex : null,
        regionId: inRegion ? `p${slot.pageNo}-region-${blk.regionIndex}` : null,
      });
      slot.targets.push(blk);
      this.targetById.set(blk.id, blk);
      this.elById.set(blk.id, {
        hit, tblock, preserve, slot,
        regionId: inRegion ? `p${slot.pageNo}-region-${blk.regionIndex}` : null,
      });
    }
    this._computeRooms(slot);
  }

  /**
   * 把译文写进译文块。
   *
   * 含行内公式的片段，译文里会带 ⟦k⟧ 占位符（送模型时就已替换），这里在占位符位置
   * 贴回**原图的小块**——公式永远以原文像素呈现，不会被模型改写，也不会被白底盖住。
   * 模型漏掉占位符时，缺失的公式块补在段末，保证公式不会丢。
   */
  _fillTranslation(el, blk, meta) {
    const text = blk.translation || '';
    const runs = new Map((blk.math || []).map((r) => [String(r.key), r]));
    let last = 0;
    let m;
    MATH_TOKEN_RE.lastIndex = 0;
    while ((m = MATH_TOKEN_RE.exec(text)) !== null) {
      if (m.index > last) el.appendChild(document.createTextNode(text.slice(last, m.index)));
      const run = runs.get(m[1]);
      if (run) {
        el.appendChild(this._mathPatch(run, blk, meta));
        runs.delete(m[1]);
      }
      last = m.index + m[0].length;
    }
    if (last < text.length) el.appendChild(document.createTextNode(text.slice(last)));
    for (const run of runs.values()) {           // 模型漏掉的公式：补在段末
      el.appendChild(document.createTextNode(' '));
      el.appendChild(this._mathPatch(run, blk, meta, true));
    }
  }

  /** 行内公式小块：用整页渲染图做背景，按坐标切出原文，作为行内元素随文字自然流转 */
  _mathPatch(run, blk, meta, inlineAtEnd = false) {
    const [x0, y0, x1, y1] = run.bbox;
    const box = document.createElement('span');
    box.className = 'math-patch' + (inlineAtEnd ? ' at-end' : '');
    const geo = this._patchGeom(run, blk, meta);
    box.style.width = `${geo.w.toFixed(1)}px`;
    box.style.height = `${geo.h.toFixed(1)}px`;
    box.style.backgroundImage = `url(${api.asset(meta.image)})`;
    box.style.backgroundSize = `${geo.bgW.toFixed(1)}px ${geo.bgH.toFixed(1)}px`;
    box.style.backgroundPosition = `${geo.bgX.toFixed(1)}px ${geo.bgY.toFixed(1)}px`;
    box.title = `行内公式（原文 ${(x1 - x0).toFixed(0)}×${(y1 - y0).toFixed(0)}pt）：按原图呈现，不翻译`;
    return box;
  }

  /**
   * 行内公式小块的几何量：把整页渲染图按坐标裁一小块当背景。
   *
   * 宽度受片段宽度限制——超宽的公式（整段求和、长分式）按比例整体缩放，
   * 背景偏移与背景尺寸同步缩放，保证**裁出来的还是同一块图**，只是小一点。
   * 否则它会撑破一行、把中文挤成一条细缝（踩过：用 float 贴回原位时，
   * 每个公式都在右侧"挖"掉一块行宽，整段被挤成 27 行）。
   */
  _patchGeom(run, blk, meta) {
    const s = this.scale;
    const [x0, y0, x1, y1] = run.bbox;
    const contentW = Math.max(8, (blk.bbox[2] - blk.bbox[0]) * s - 2);
    let w = Math.max(3, (x1 - x0) * s);
    let h = Math.max(3, (y1 - y0) * s);
    let k = 1;
    if (w > contentW) {
      k = contentW / w;
      w = contentW;
      h *= k;
    }
    return {
      w, h, k,
      bgW: meta.width * s * k,
      bgH: meta.height * s * k,
      bgX: -x0 * s * k,
      bgY: -y0 * s * k,
    };
  }

  block(id) { return this.targetById.get(id) || null; }

  /**
   * 预计算每个片段正下方"还能借用多少空白"（pt）。
   *
   * 中文通常比英文长，硬塞进原块高度就会被裁掉——而原段落之间的空白、以及
   * 下一个障碍物之前的位置其实是空的。这里算出"到下一个障碍物为止"的可用高度，
   * 供字号反解使用；障碍物包括页面上的**所有**片段与保留区域（图/表/公式），
   * 因此借用空白时不会压到图或别的文字。
   *
   * 判据用"障碍物是否伸到我下面"（`oy1 > y1`）而不是"是否从下面开始"
   * （`oy0 >= y1`）：行内分式会把片段包围盒撑到下一段里去，那种情况下两者重叠，
   * 可借用的空白是 0。旧写法会把这种障碍物当成"不在下方"跳过，于是去量更远的
   * 那一块，让中文一路长过去压住下一段（踩过）。
   */
  _computeRooms(slot) {
    const obstacles = [];
    for (const t of slot.targets) obstacles.push(t.bbox);
    slot.rooms = new Map();
    const blocks = slot.data?.blocks || [];
    for (const blk of blocks) {
      if (!slot.blockEls.has(blk.id)) continue;
      const [x0, y0, x1, y1] = blk.bbox;
      let room = Infinity;
      for (const [ox0, oy0, ox1, oy1] of obstacles) {
        if (ox0 === x0 && oy0 === y0 && ox1 === x1 && oy1 === y1) continue;  // 自己
        if (oy1 <= y1 + 0.5) continue;                          // 没伸到我下面
        const ov = Math.min(x1, ox1) - Math.max(x0, ox0);
        if (ov <= 0.3 * Math.min(x1 - x0, ox1 - ox0)) continue;  // 不同栏
        room = Math.min(room, Math.max(0, oy0 - y1));            // 重叠时为 0
      }
      slot.rooms.set(blk.id, Number.isFinite(room) ? room : 0);
    }
  }

  // ---------------------------------------------------------------- 版式
  /** 依据当前缩放重新计算所有几何量（左右两栏共用同一套坐标） */
  layout() {
    for (const slot of this.slots.values()) this._layoutSlot(slot);
    for (const slot of this.slots.values()) {
      if (slot.hasContent && this._inFitWindow(slot.pageNo)) this._correctFitSlot(slot);
    }
    for (const slot of this.slots.values()) {
      if (slot.hasContent && this._inFitWindow(slot.pageNo)) this._markFitSlot(slot);
    }
    this._cacheTops();
    this._emitFit();
  }

  /**
   * 缓存每页在"滚动内容坐标"里的顶部位置。
   * stage 的高度由 layout 显式指定（不依赖图片加载），因此该偏移是稳定的，
   * 滚动时无需反复做布局查询（41 页 × 每帧 = 卡顿来源）。
   */
  _cacheTops() {
    const base = this.el.stackLeft.getBoundingClientRect().top;
    for (const p of this.pageOrder) {
      const slot = this.slots.get(p);
      slot.topInStack = slot.left.stage.getBoundingClientRect().top - base;
    }
  }

  _layoutSlot(slot) {
    const s = this.scale;
    const meta = slot.meta;
    const w = meta.width * s;
    const h = meta.height * s;
    for (const side of ['left', 'right']) {
      const st = slot[side].stage;
      st.style.width = `${w}px`;
      st.style.height = `${h}px`;
      const img = side === 'left' ? slot.left.img : slot.right.underlay;
      img.style.width = `${w}px`;
      img.style.height = `${h}px`;
    }
    if (!slot.hasContent) return;

    const place = (node, bbox) => {
      const [x0, y0, x1, y1] = bbox;
      Object.assign(node.style, {
        left: `${x0 * s}px`, top: `${y0 * s}px`,
        width: `${Math.max(1, (x1 - x0) * s)}px`, height: `${Math.max(1, (y1 - y0) * s)}px`,
      });
    };

    const regions = slot.data.regions || [];
    for (let i = 0; i < regions.length; i += 1) {
      const e = this.elById.get(`p${slot.pageNo}-region-${i}`);
      if (!e) continue;
      place(e.preserve, regions[i].bbox);
      place(e.hit, regions[i].bbox);
    }

    for (const blk of slot.data.blocks) {
      const els = slot.blockEls.get(blk.id);
      if (!els) continue;
      const [x0, y0, x1, y1] = blk.bbox;
      const left = x0 * s;
      const top = y0 * s;
      const bw = Math.max(1, (x1 - x0) * s);
      const bh = Math.max(1, (y1 - y0) * s);
      Object.assign(els.hit.style, {
        left: `${left}px`, top: `${top}px`, width: `${bw}px`, height: `${bh}px`,
      });
      if (els.preserve) {
        Object.assign(els.preserve.style, { left: `${left}px`, top: `${top}px`, width: `${bw}px`, height: `${bh}px` });
      }
      if (els.tblock) {
        const lineHeight = blk.kind === 'formula' ? 1.5 : 1.32;
        const base = Math.max(6, Math.min(46, blk.fontSize * s * this.fontScale));
        const text = els.tblock.textContent || '';
        const room = ((slot.rooms && slot.rooms.get(blk.id)) || 0) * s;
        const patches = this._patchWidths(blk, slot.meta);
        const fit = this._fitBlock(text, bw, bh, bh + room, base, lineHeight, patches);
        // 有比行盒还高的公式小块时改用 normal：固定 line-height 不会为行内块让位，
        // 高出来的部分会直接压到上下两行上（分式尤其明显，实测第 6 页 "Proof." 那段
        // 的字叠在一起）。normal 让浏览器按内容自然撑高，多出来的高度由 _correctFit 收。
        const tallPatch = [...patches.values()].some((p) => p.h > lineHeight * fit.fs + 1);
        Object.assign(els.tblock.style, {
          left: `${left}px`, top: `${top}px`,
          width: `${bw}px`,
          fontSize: `${fit.fs.toFixed(2)}px`,
          lineHeight: tallPatch ? 'normal' : String(lineHeight),
          minHeight: `${bh}px`,
          height: 'auto',
        });
        els.tblock.dataset.h = String(bh);
        els.tblock.dataset.base = String(base);
        els.tblock.dataset.fs = String(fit.fs);
        els.tblock.dataset.avail = String(bh + room);
        els.tblock.dataset.overflow = fit.h > bh + room + 1 ? '1' : '0';
      }
    }
  }

  /**
   * 用真实 DOM 高度校正字号。
   *
   * `_wrapMeasure` 是估算，与浏览器实际排版差几个百分点（行内公式那种
   * inline-block 会把行盒撑得比"行高 × 字号"更高；`vertical-align: middle` 还会
   * 再多让出一点）。单块看只是几像素，但框一"长胖"就会压到下一块上——实测 24 块
   * 里有 4 块这样轻轻叠住。这里按 可用高 / 实际高 的比例再缩一次字号，最多迭代两轮：
   * 字号只会变小，因此一定收敛，不会来回抖。
   */
  _correctFitSlot(slot) {
    const blocks = slot.data?.blocks || [];
    for (let pass = 0; pass < 3; pass += 1) {
      let fixed = 0;
      for (const blk of blocks) {
        const els = slot.blockEls.get(blk.id);
        const el = els && els.tblock;
        if (!el || !el.textContent) continue;
        const avail = parseFloat(el.dataset.avail || '0');
        const base = parseFloat(el.dataset.base || '0');
        const fs = parseFloat(el.dataset.fs || '0');
        if (!avail || !fs) continue;
        const real = el.getBoundingClientRect().height;
        if (real <= avail + 1) continue;
        const floor = Math.max(4, FIT_MIN_RATIO * base);
        const next = Math.max(floor, fs * (avail / real) * 0.985);
        if (next >= fs - 0.05) continue;             // 已到下限，不再缩（交给 overflow 标记）
        el.style.fontSize = `${next.toFixed(2)}px`;
        el.dataset.fs = String(next);
        fixed += 1;
      }
      if (!fixed) break;
    }
    for (const blk of blocks) {
      const els = slot.blockEls.get(blk.id);
      const el = els && els.tblock;
      if (!el || !el.textContent) continue;
      const avail = parseFloat(el.dataset.avail || '0');
      el.dataset.overflow = el.getBoundingClientRect().height > avail + 1 ? '1' : '0';
    }
  }

  /** 每个行内公式占位符贴出来以后占多大（像素），供折行估算使用 */
  _patchWidths(blk, meta) {
    const map = new Map();
    for (const r of blk.math || []) {
      const g = this._patchGeom(r, blk, meta);
      map.set(String(r.key), { w: g.w, h: g.h });
    }
    return map;
  }

  /** 视觉宽度，以 1 em 为单位：中日韩文字/全角标点算 1，拉丁字母与半角标点算 0.52 */
  _visualUnits(text) {
    let units = 0;
    for (const ch of text) {
      units += ch.codePointAt(0) >= CJK_FROM ? 1.0 : LATIN_UNITS;
    }
    return units;
  }

  /**
   * 贪心折行，返回需要的行数与高度（尽量贴近浏览器行为）。
   *
   * 浏览器里 CJK 可以在任意两字之间断行，拉丁词只在空格处断（除非单词本身就超宽）。
   * 只按"面积"估算会严重低估短块——例如 251×60pt 的块里放 104 字中文，
   * 面积公式算出"放得下"，实际需要 5 行、放不下，最后一行就被裁掉了：
   * 这正是"译文显示不全"的根因。按行数与行高算就准了。
   *
   * 行内公式占位符按它**实际贴出来的像素宽**计（不是按 4 个字符），并且会把
   * 那一行的行高抬到公式的高度——分式类公式比正文高得多，不算进去就会低估总高。
   */
  _wrapMeasure(text, w, fs, patches, lineHeight = LINE_UNIT) {
    const base = lineHeight * fs;
    if (!text) return { lines: 1, h: base };
    let lines = 0;
    let total = 0;
    for (const para of String(text).split('\n')) {
      if (!para) {
        lines += 1;
        total += base;
        continue;
      }
      let x = 0;
      let lineH = base;
      lines += 1;
      for (const u of this._lineUnits(para, fs, patches)) {
        if (x + u.w > w + 0.01 && x > 0) {
          total += lineH;
          lines += 1;
          x = 0;
          lineH = base;
          if (u.blank) continue;
        }
        if (u.h > lineH) lineH = u.h;
        if (u.w > w) {                       // 单词/公式本身比一行还宽：硬断
          const extra = Math.ceil(u.w / w) - 1;
          total += (extra - 1 >= 0 ? extra - 1 : 0) * lineH;
          lines += extra;
          x = u.w % w;
        } else {
          x += u.w;
        }
      }
      total += lineH;
    }
    return { lines, h: total };
  }

  /**
   * 把一段文本切成"断行单位"：连续拉丁字母/数字算一个词，其余（CJK、标点）逐字；
   * 行内公式占位符单独成为一个单位，宽度取它贴出来的像素宽、高度取贴出来的像素高。
   */
  _lineUnits(para, fs, patches) {
    const units = [];
    const pushText = (t) => {
      for (const u of t.match(/[A-Za-z0-9]+(?:['’.-][A-Za-z0-9]+)*|\s+|[^\s]/gu) || []) {
        const blank = u.trim() === '';
        units.push({
          w: blank ? 0.35 * fs : this._visualUnits(u) * fs,
          h: 0, blank,
        });
      }
    };
    let last = 0;
    let m;
    MATH_TOKEN_RE.lastIndex = 0;
    while ((m = MATH_TOKEN_RE.exec(para)) !== null) {
      if (m.index > last) pushText(para.slice(last, m.index));
      const p = patches && patches.get(m[1]);
      units.push(p ? { w: p.w, h: p.h, blank: false } : { w: 2 * fs, h: 0, blank: false });
      last = m.index + m[0].length;
    }
    if (last < para.length) pushText(para.slice(last));
    if (!units.length) pushText(para);
    return units;
  }

  /**
   * 求能把 text 尽量落进 w×availH 的最大字号，并给出实际需要的高度。
   *
   * 做法是二分搜索：字号越小行数越少，但"需要的行数"是阶梯函数，
   * 所以不能用解析式，直接按行数试。
   * 下限取 0.55× 原字号——再小就牺牲可读性，此时宁可让框向下长高（详见 layout）。
   */
  _fitBlock(text, w, bh, availH, base, lineHeight, patches) {
    const need = (fs) => this._wrapMeasure(text, w, fs, patches, lineHeight).h;
    if (!text || w <= 4) return { fs: base, h: 0, lines: 1 };
    if (this.overflowMode) {                       // 「溢出全文」：不缩字，完整显示
      const m = this._wrapMeasure(text, w, base, patches, lineHeight);
      return { fs: base, h: m.h, lines: m.lines };
    }
    const floor = Math.max(4, FIT_MIN_RATIO * base);
    const atBase = this._wrapMeasure(text, w, base, patches, lineHeight);
    if (atBase.h <= availH) return { fs: base, h: atBase.h, lines: atBase.lines };
    const atFloor = this._wrapMeasure(text, w, floor, patches, lineHeight);
    if (atFloor.h > availH) return { fs: floor, h: atFloor.h, lines: atFloor.lines };
    let lo = floor;
    let hi = base;
    for (let i = 0; i < 16; i += 1) {
      const mid = (lo + hi) / 2;
      if (need(mid) <= availH) lo = mid; else hi = mid;
    }
    const fin = this._wrapMeasure(text, w, lo, patches, lineHeight);
    return { fs: lo, h: fin.h, lines: fin.lines };
  }

  /**
   * 标注版式适配结果（每页各记一份，界面只汇报"当前页"）：
   * * shrunk     —— 为了塞进原块（含可借用的空白）而缩小了字号
   * * overflowed —— 缩到下限仍放不下，框已向下长高、可能压到下一个块（保证文字完整）
   */
  _markFitSlot(slot) {
    let shrunk = 0;
    let clipped = 0;
    let spilling = 0;
    let total = 0;
    for (const { tblock } of slot.blockEls.values()) {
      if (!tblock || !tblock.textContent) continue;
      tblock.classList.remove('clipped', 'spilling', 'shrunk', 'overflowed');
      tblock.classList.toggle('overflowed', tblock.dataset.overflow === '1');
      total += 1;
      const base = parseFloat(tblock.dataset.base || '0');
      const fs = parseFloat(tblock.dataset.fs || '0');
      if (base && fs && fs < base * 0.995) {
        tblock.classList.add('shrunk');
        shrunk += 1;
      }
      if (tblock.dataset.overflow === '1') {
        spilling += 1;
        const cur = tblock.getAttribute('title') || '';
        if (!cur.includes('向上/向下')) {
          tblock.setAttribute(
            'title',
            `${cur}${cur ? ' · ' : ''}中文比英文长，已缩到下限仍放不下，已向下延展以完整显示（可能压住下方片段）`,
          );
        }
      }
    }
    slot.fit = { shrunk, clipped, spilling, total };
  }

  _emitFit() {
    const slot = this.slots.get(this._current);
    this.hooks.onFit?.(slot?.fit || { shrunk: 0, clipped: 0, spilling: 0, total: 0 });
  }

  // ---------------------------------------------------------------- 滚动 / 当前页
  /**
   * 依据左栏滚动位置反推"当前第几页"（取视口上部 1/3 处落在哪一页），
   * 变化时通知外部（刷新页码、提示、预取）。
   */
  _updateCurrentPage() {
    if (!this.doc || this._suppress) return;
    const sc = this.el.scrollLeft;
    const probe = sc.scrollTop + sc.clientHeight * 0.34;
    let best = this._current;
    let bestDist = Infinity;
    for (const p of this.pageOrder) {
      const slot = this.slots.get(p);
      const top = this._slotTop(slot);
      const h = slot.left.stage.offsetHeight;
      if (probe >= top && probe < top + h) { best = p; bestDist = 0; break; }
      const d = Math.min(Math.abs(probe - top), Math.abs(probe - (top + h)));
      if (d < bestDist) { bestDist = d; best = p; }
    }
    if (best !== this._current) {
      this._current = best;
      this.hooks.onPageChange?.(best);
      this._emitFit();
      // 新进入适配窗口的页补做一次实测校正
      for (const p of this.pageOrder) {
        const slot = this.slots.get(p);
        if (slot.hasContent && this._inFitWindow(p)) { this._correctFitSlot(slot); this._markFitSlot(slot); }
      }
      this._emitFit();
    }
  }

  /** 某页 stage 在"滚动内容坐标"里的顶部位置（页面堆叠在同一列里） */
  _slotTop(slot) {
    if (typeof slot.topInStack === 'number') return slot.topInStack;
    const stack = this.el.stackLeft;
    return slot.left.stage.getBoundingClientRect().top - stack.getBoundingClientRect().top;
  }

  scrollToPage(pageNo) {
    const slot = this.slots.get(pageNo);
    if (!slot) return;
    const top = Math.max(0, this._slotTop(slot) - 10);
    this.el.scrollLeft.scrollTop = top;
    this.el.scrollRight.scrollTop = top;
    this._current = pageNo;
    // 程序化滚动会立刻触发 scroll 事件：短暂抑制"按滚动反推当前页"，
    // 免得它把刚定位好的页码又改掉（定位与反推互相打架）。
    this._suppress = true;
    clearTimeout(this._suppressTimer);
    this._suppressTimer = setTimeout(() => { this._suppress = false; }, 80);
  }

  // ---------------------------------------------------------------- 缩放
  setScale(scale, anchor = null) {
    const old = this.scale;
    const next = Math.max(0.2, Math.min(5, scale));
    if (Math.abs(next - old) < 1e-4) return;
    const scl = this.el.scrollLeft;
    const ax = anchor ? anchor.x : (scl.scrollLeft + scl.clientWidth / 2);
    const ay = anchor ? anchor.y : (scl.scrollTop + scl.clientHeight / 2);
    this.scale = next;
    this.layout();
    const k = next / Math.max(old, 1e-6);
    for (const sc of [this.el.scrollLeft, this.el.scrollRight]) {
      sc.scrollLeft = ax * k - sc.clientWidth / 2;
      sc.scrollTop = ay * k - sc.clientHeight / 2;
    }
    this._clearMarker();
    this.hooks.onZoom?.(next);
  }

  /** 指针在某一栏"滚动内容坐标"里的位置（用于 Ctrl+滚轮以指针为中心缩放） */
  _contentPoint(ev, pane) {
    const sc = pane === 'left' ? this.el.scrollLeft : this.el.scrollRight;
    const r = sc.getBoundingClientRect();
    return { x: ev.clientX - r.left + sc.scrollLeft, y: ev.clientY - r.top + sc.scrollTop };
  }

  get viewportSize() {
    const el = this.el.scrollLeft;
    return { width: el.clientWidth - 32, height: el.clientHeight - 32 };
  }

  fitWidth() {
    const p = this._sizeOf(this._current);
    if (!p) return;
    this.setScale(this.viewportSize.width / p.width);
  }

  fitPage() {
    const p = this._sizeOf(this._current);
    if (!p) return;
    const { width, height } = this.viewportSize;
    this.setScale(Math.min(width / p.width, height / p.height));
  }

  setFontScale(v) { this.fontScale = Number(v) || 1; this.layout(); }

  setOverflow(v) { this.overflowMode = !!v; this.layout(); }

  /**
   * 原图底衬：右栏以整页渲染图为底。
   * 开启时（默认）图片 / 表格 / 公式等不翻译的区域直接透出原图，只有译文块是白底；
   * 关闭时右栏回到纯白页面，仅显示译文（保留原文的块显示为虚线空框）。
   */
  setUnderlay(v) {
    this.showUnderlay = !!v;
    document.body.classList.toggle('original-base', this.showUnderlay);
  }

  /**
   * 悬停是否联动高亮。默认**关闭**：鼠标只是路过页面时不产生任何视觉变化；
   * 勾选工具栏「高亮跟随鼠标」后才恢复原来的同步定位（两栏同亮 + 十字线）。
   */
  setHoverHighlight(v) {
    this.hoverHighlight = !!v;
    if (!this.hoverHighlight) { this.highlight(null); this._clearMarker(); }
  }

  // ---------------------------------------------------------------- 高亮 / 定位
  highlight(id) {
    if (this.activeId === id) return;
    if (this.activeId) this._paint(this.activeId, false);
    this.activeId = id;
    if (id) this._paint(id, true);
  }

  _paint(id, on) {
    const e = this.elById.get(id);
    if (!e) return;
    const act = (node) => node && node.classList[on ? 'add' : 'remove']('active');
    act(e.hit);
    act(e.tblock);
    act(e.preserve);
    // 块落在某个保留区域内时，连带把整个区域一起点亮
    if (e.regionId) {
      const re = this.elById.get(e.regionId);
      if (re) { act(re.hit); act(re.preserve); }
    }
  }

  /** 把译文层滚动到可见位置（点击定位时使用） */
  revealRight(id) {
    const e = this.elById.get(id);
    const node = e && (e.tblock || e.preserve);
    if (!node) return;
    const sc = this.el.scrollRight;
    const nr = node.getBoundingClientRect();
    const top = nr.top - sc.getBoundingClientRect().top + sc.scrollTop;
    const bottom = top + node.offsetHeight;
    const viewTop = sc.scrollTop;
    const viewBottom = viewTop + sc.clientHeight;
    if (top < viewTop + 40 || bottom > viewBottom - 40) {
      sc.scrollTo({ top: Math.max(0, top - sc.clientHeight * 0.35), behavior: 'smooth' });
    }
  }

  /** 命中测试：把鼠标位置换算成 PDF 坐标，再找包含它的文本块 */
  _onPointerMove(ev, pane, select = false) {
    const found = this._slotUnder(ev, pane);
    if (!found) {
      if (!select && this.hoverHighlight) { this.highlight(null); this._clearMarker(); }
      return null;
    }
    const { slot, rect } = found;
    const px = ev.clientX - rect.left;
    const py = ev.clientY - rect.top;
    const blk = this._hitTest(slot, px / this.scale, py / this.scale);
    if (!blk) {
      if (!select && this.hoverHighlight) { this.highlight(null); this._clearMarker(); }
      return null;
    }
    // 只有勾选「高亮跟随鼠标」才在指向时高亮 + 画十字线；未勾选则悬停完全静默
    // （点击命中仍然返回 blk，保证弹详情、滚动定位照常工作）
    if (this.hoverHighlight) {
      this.highlight(blk.id);
      this._setMarker(blk, pane, slot, px, py);
    }
    return blk;
  }

  /** 找到指针所在的已渲染页面（返回该 slot 与该页 stage 的屏幕矩形） */
  _slotUnder(ev, pane) {
    for (const p of this.pageOrder) {
      const slot = this.slots.get(p);
      if (!slot.hasContent) continue;
      const rect = slot[pane].stage.getBoundingClientRect();
      if (ev.clientX >= rect.left && ev.clientX <= rect.right
        && ev.clientY >= rect.top && ev.clientY <= rect.bottom) {
        return { slot, rect };
      }
    }
    return null;
  }

  /** 命中测试：把鼠标位置换算成 PDF 坐标，再找包含它的块（块优先于保留区域） */
  _hitTest(slot, x, y) {
    let best = null;
    let bestArea = Infinity;
    for (const blk of slot.targets) {
      const [x0, y0, x1, y1] = blk.bbox;
      if (x >= x0 - 1.5 && x <= x1 + 1.5 && y >= y0 - 1.5 && y <= y1 + 1.5) {
        const area = (x1 - x0) * (y1 - y0);
        if (area < bestArea) { best = blk; bestArea = area; }
      }
    }
    if (best) return best;
    // 容差内取最近的块
    let nearest = null;
    let nd = 28; // pt
    for (const blk of slot.targets) {
      const [x0, y0, x1, y1] = blk.bbox;
      const cx = Math.min(Math.max(x, x0), x1);
      const cy = Math.min(Math.max(y, y0), y1);
      const d = Math.hypot(x - cx, y - cy);
      if (d < nd) { nd = d; nearest = blk; }
    }
    return nearest;
  }

  /**
   * 十字标记：左右两栏各自换算到"本页 stage 内"的偏移，直观体现"同坐标"。
   * 标记元素挂在两栏的 `.page-stack` 里（随内容滚动），坐标因此用堆叠内容坐标。
   */
  _setMarker(blk, pane, slot, px, py) {
    const target = slot || this.slots.get(this._current);
    if (!target) return;
    for (const [m, side] of [[this.el.markerLeft, 'left'], [this.el.markerRight, 'right']]) {
      const st = target[side].stage;
      const off = this._stageOffsetInStack(st);
      let x;
      let y;
      if (px === undefined) {
        const [x0, y0, x1, y1] = blk.bbox;
        x = ((x0 + x1) / 2) * this.scale + off.left;
        y = ((y0 + y1) / 2) * this.scale + off.top;
      } else {
        x = px + off.left;
        y = py + off.top;
      }
      m.hidden = false;
      m.querySelector('.marker-h').style.top = `${y}px`;
      m.querySelector('.marker-v').style.left = `${x}px`;
      const dot = m.querySelector('.marker-dot');
      dot.style.left = `${x}px`;
      dot.style.top = `${y}px`;
    }
  }

  /** stage 相对所属 `.page-stack`（含 padding）的偏移，用于把 stage 内坐标换成堆叠坐标 */
  _stageOffsetInStack(stage) {
    const stack = stage.closest('.page-stack');
    if (!stack) return { left: 0, top: 0 };
    const sr = stack.getBoundingClientRect();
    const gr = stage.getBoundingClientRect();
    return { left: gr.left - sr.left, top: gr.top - sr.top };
  }

  _clearMarker() {
    this.el.markerLeft.hidden = true;
    this.el.markerRight.hidden = true;
  }
}
