/* 对照视图引擎：坐标系换算、缩放联动、悬停高亮、滚动同步 */
import { api } from './api.js';

/* 渲染模型
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

export class Viewer {
  /**
   * @param {object} refs DOM 引用集合
   * @param {object} hooks { onSelect(block, event), onStatus(text), onFit(info), onZoom(scale) }
   */
  constructor(refs, hooks = {}) {
    this.el = refs;
    this.hooks = hooks;
    this.page = null;
    this.scale = 1;
    this.fontScale = 1;
    this.strict = true;
    this.overflowMode = false;   // false = 按可用高度自动缩字（缩不下才向下延展）；true = 不缩字，完整显示
    this.showUnderlay = true;    // 原图底衬：右栏是否以原页图为底（默认开）
    this.activeId = null;
    this.blockEls = new Map();   // blockId -> {hit, tblock, preserve}
    this.regionEls = new Map();  // regionIndex -> {hit, el}
    this.targets = [];           // 可命中的对象（块 + 保留区域），用于命中测试
    this._syncing = false;
    this._bind();
  }

  // ---------------------------------------------------------------- 事件绑定
  _bind() {
    const { overlayLeft, scrollLeft, scrollRight, transLayer } = this.el;

    overlayLeft.addEventListener('mousemove', (ev) => this._onPointerMove(ev, 'left'));
    overlayLeft.addEventListener('mouseleave', () => { this._clearMarker(); this.highlight(null); });
    overlayLeft.addEventListener('click', (ev) => {
      const blk = this._onPointerMove(ev, 'left', true);
      if (blk) this.hooks.onSelect?.(blk, ev);
    });

    transLayer.addEventListener('mousemove', (ev) => {
      const id = ev.target?.dataset?.id;
      if (id) { const blk = this.block(id); if (blk) { this.highlight(id); this._setMarker(blk, 'right'); } }
    });
    transLayer.addEventListener('mouseleave', () => { this._clearMarker(); this.highlight(null); });
    transLayer.addEventListener('click', (ev) => {
      const id = ev.target?.dataset?.id;
      if (id) { const blk = this.block(id); if (blk) this.hooks.onSelect?.(blk, ev); }
    });

    // 滚动同步（比例映射，等比联动）
    const sync = (src, dst) => () => {
      if (this._syncing) return;
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
    };
    scrollLeft.addEventListener('scroll', sync(scrollLeft, scrollRight), { passive: true });
    scrollRight.addEventListener('scroll', sync(scrollRight, scrollLeft), { passive: true });

    // Ctrl + 滚轮缩放（以指针为锚点）
    const wheel = (pane) => (ev) => {
      if (!ev.ctrlKey && !ev.metaKey) return;
      ev.preventDefault();
      const anchor = this._stageAnchor(ev, pane);
      const factor = Math.exp(-ev.deltaY * 0.0018);
      this.setScale(this.scale * factor, anchor);
    };
    scrollLeft.addEventListener('wheel', wheel('left'), { passive: false });
    scrollRight.addEventListener('wheel', wheel('right'), { passive: false });
  }

  // ---------------------------------------------------------------- 渲染
  setPage(pageData) {
    this.page = pageData;
    this.activeId = null;
    this.blockEls.clear();
    this.regionEls.clear();
    this.targets = [];
    const { stageLeft, stageRight, pageImage, pageUnderlay, overlayLeft, transLayer } = this.el;

    const url = api.asset(pageData.image);
    pageImage.src = url;
    pageUnderlay.src = url;
    pageUnderlay.hidden = !this.showUnderlay;

    overlayLeft.innerHTML = '';
    transLayer.innerHTML = '';

    // ---- 保留区域（图片 / 表格）：画虚框 + 可命中，不渲染任何文字 ----
    const regions = pageData.regions || [];
    regions.forEach((reg, i) => {
      const box = document.createElement('div');
      box.className = `preserve region type-${reg.type}`;
      box.dataset.id = `region-${i}`;
      box.title = `${reg.type === 'table' ? '表格' : '图片'}区域：按设定原样保留，不翻译`
        + (reg.label ? `\n${reg.label}` : '');
      transLayer.appendChild(box);
      const hit = document.createElement('div');
      hit.className = 'hit region-hit';
      hit.dataset.region = String(i);
      hit.title = box.title;
      overlayLeft.appendChild(hit);
      this.regionEls.set(i, { el: box, hit });
      const target = {
        id: `region-${i}`, isRegion: true, regionIndex: i,
        kind: reg.type === 'table' ? 'table' : 'figure',
        bbox: reg.bbox, text: reg.label || '', fontSize: 6,
        translatable: false, translation: '', lineCount: 0,
      };
      this.targets.push(target);
    });

    // ---- 文本块 ----
    for (const blk of pageData.blocks) {
      if (STRUCTURAL.has(blk.kind) || blk.hidden) continue;
      const inRegion = blk.regionIndex !== null && blk.regionIndex !== undefined;

      // 左栏（原文）命中区：始终按真实坐标建，悬停即可高亮
      const hit = document.createElement('div');
      hit.className = `hit kind-${blk.kind}`;
      hit.dataset.id = blk.id;
      hit.title = blk.text ? blk.text.slice(0, 160) : '';
      overlayLeft.appendChild(hit);

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
          this._fillTranslation(tblock, blk);
        }
        transLayer.appendChild(tblock);
      } else if (!inRegion) {
        // 不在区域内的"保留原文"块（公式 / 参考文献 / 图表标题）：不写文字，
        // 只留一个轮廓层，悬停时会亮起来，让用户知道这里保留了原文。
        preserve = document.createElement('div');
        preserve.className = `preserve kind-${blk.kind}`;
        preserve.dataset.id = blk.id;
        const label = KIND_LABEL[blk.kind] || '该片段';
        preserve.title = `${label}：按设定保留原文，不翻译（可在「⚙ 设置」里调整）`;
        transLayer.appendChild(preserve);
      }

      this.blockEls.set(blk.id, {
        hit, tblock, preserve,
        region: inRegion ? blk.regionIndex : null,
        el: tblock || preserve,
      });
      this.targets.push(blk);
    }
    this._computeRooms();
    this.layout();
  }

  /**
   * 把译文写进译文块。
   *
   * 含行内公式的片段，译文里会带 ⟦k⟧ 占位符（送模型时就已替换），这里在占位符位置
   * 贴回**原图的小块**——公式永远以原文像素呈现，不会被模型改写，也不会被白底盖住。
   * 模型漏掉占位符时，缺失的公式块补在段末，保证公式不会丢。
   */
  _fillTranslation(el, blk) {
    const text = blk.translation || '';
    const runs = new Map((blk.math || []).map((r) => [String(r.key), r]));
    let last = 0;
    let m;
    MATH_TOKEN_RE.lastIndex = 0;
    while ((m = MATH_TOKEN_RE.exec(text)) !== null) {
      if (m.index > last) el.appendChild(document.createTextNode(text.slice(last, m.index)));
      const run = runs.get(m[1]);
      if (run) {
        el.appendChild(this._mathPatch(run, blk));
        runs.delete(m[1]);
      }
      last = m.index + m[0].length;
    }
    if (last < text.length) el.appendChild(document.createTextNode(text.slice(last)));
    for (const run of runs.values()) {           // 模型漏掉的公式：补在段末
      el.appendChild(document.createTextNode(' '));
      el.appendChild(this._mathPatch(run, blk, true));
    }
  }

  /** 行内公式小块：用整页渲染图做背景，按坐标切出原文，作为行内元素随文字自然流转 */
  _mathPatch(run, blk, inlineAtEnd = false) {
    const [x0, y0, x1, y1] = run.bbox;
    const box = document.createElement('span');
    box.className = 'math-patch' + (inlineAtEnd ? ' at-end' : '');
    const geo = this._patchGeom(run, blk);
    box.style.width = `${geo.w.toFixed(1)}px`;
    box.style.height = `${geo.h.toFixed(1)}px`;
    box.style.backgroundImage = `url(${api.asset(this.page.image)})`;
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
  _patchGeom(run, blk) {
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
      bgW: this.page.width * s * k,
      bgH: this.page.height * s * k,
      bgX: -x0 * s * k,
      bgY: -y0 * s * k,
    };
  }

  block(id) {
    if (typeof id === 'string' && id.startsWith('region-')) {
      const i = Number(id.slice(7));
      return this.targets.find((t) => t.isRegion && t.regionIndex === i) || null;
    }
    return this.page?.blocks.find((b) => b.id === id) || null;
  }

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
  _computeRooms() {
    const obstacles = [];
    for (const t of this.targets) obstacles.push(t.bbox);
    this.rooms = new Map();
    for (const blk of this.page.blocks) {
      if (!this.blockEls.has(blk.id)) continue;
      const [x0, y0, x1, y1] = blk.bbox;
      let room = Infinity;
      for (const [ox0, oy0, ox1, oy1] of obstacles) {
        if (ox0 === x0 && oy0 === y0 && ox1 === x1 && oy1 === y1) continue;  // 自己
        if (oy1 <= y1 + 0.5) continue;                          // 没伸到我下面
        const ov = Math.min(x1, ox1) - Math.max(x0, ox0);
        if (ov <= 0.3 * Math.min(x1 - x0, ox1 - ox0)) continue;  // 不同栏
        room = Math.min(room, Math.max(0, oy0 - y1));            // 重叠时为 0
      }
      this.rooms.set(blk.id, Number.isFinite(room) ? room : 0);
    }
  }

  /** 依据当前缩放重新计算所有几何量（左右两栏共用同一套坐标） */
  layout() {
    if (!this.page) return;
    const s = this.scale;
    const w = this.page.width * s;
    const h = this.page.height * s;
    for (const stage of [this.el.stageLeft, this.el.stageRight]) {
      stage.style.width = `${w}px`;
      stage.style.height = `${h}px`;
    }
    this.el.pageImage.style.width = `${w}px`;
    this.el.pageImage.style.height = `${h}px`;
    this.el.pageUnderlay.style.width = `${w}px`;
    this.el.pageUnderlay.style.height = `${h}px`;

    const place = (node, bbox) => {
      const [x0, y0, x1, y1] = bbox;
      Object.assign(node.style, {
        left: `${x0 * s}px`, top: `${y0 * s}px`,
        width: `${Math.max(1, (x1 - x0) * s)}px`, height: `${Math.max(1, (y1 - y0) * s)}px`,
      });
    };

    for (const [i, { el, hit }] of this.regionEls.entries()) {
      place(el, this.page.regions[i].bbox);
      place(hit, this.page.regions[i].bbox);
    }

    for (const blk of this.page.blocks) {
      const els = this.blockEls.get(blk.id);
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
        const room = ((this.rooms && this.rooms.get(blk.id)) || 0) * s;
        const patches = this._patchWidths(blk);
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
    this._correctFit();
    this._markFit();
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
  _correctFit() {
    for (let pass = 0; pass < 3; pass += 1) {
      let fixed = 0;
      for (const blk of this.page.blocks) {
        const els = this.blockEls.get(blk.id);
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
    for (const blk of this.page.blocks) {
      const els = this.blockEls.get(blk.id);
      const el = els && els.tblock;
      if (!el || !el.textContent) continue;
      const avail = parseFloat(el.dataset.avail || '0');
      el.dataset.overflow = el.getBoundingClientRect().height > avail + 1 ? '1' : '0';
    }
  }

  /** 每个行内公式占位符贴出来以后占多大（像素），供折行估算使用 */
  _patchWidths(blk) {
    const map = new Map();
    for (const r of blk.math || []) {
      const g = this._patchGeom(r, blk);
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
   * 标注版式适配结果：
   * * shrunk     —— 为了塞进原块（含可借用的空白）而缩小了字号
   * * overflowed —— 缩到下限仍放不下，框已向下长高、可能压到下一个块（保证文字完整）
   */
  _markFit() {
    let shrunk = 0;
    let clipped = 0;
    let spilling = 0;
    let total = 0;
    for (const { tblock } of this.blockEls.values()) {
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
    this.hooks.onFit?.({ shrunk, clipped, spilling, total });
  }

  // ---------------------------------------------------------------- 缩放
  setScale(scale, anchor = null) {
    const old = this.scale;
    const next = Math.max(0.2, Math.min(5, scale));
    if (Math.abs(next - old) < 1e-4) return;
    const anchorX = anchor ? anchor.x : (this.el.scrollLeft.scrollLeft + this.el.scrollLeft.clientWidth / 2 - this.el.stageLeft.offsetLeft);
    const anchorY = anchor ? anchor.y : (this.el.scrollLeft.scrollTop + this.el.scrollLeft.clientHeight / 2 - this.el.stageLeft.offsetTop);
    this.scale = next;
    this.layout();
    const k = next / Math.max(old, 1e-6);
    this._centerOn(anchorX * k, anchorY * k);
    this._clearMarker();
    this.hooks.onZoom?.(next);
  }

  _centerOn(px, py) {
    for (const [scroll, stage] of [[this.el.scrollLeft, this.el.stageLeft], [this.el.scrollRight, this.el.stageRight]]) {
      scroll.scrollLeft = stage.offsetLeft + px - scroll.clientWidth / 2;
      scroll.scrollTop = stage.offsetTop + py - scroll.clientHeight / 2;
    }
  }

  get viewportSize() {
    const el = this.el.scrollLeft;
    return { width: el.clientWidth - 32, height: el.clientHeight - 32 };
  }

  fitWidth() {
    if (!this.page) return;
    this.setScale(this.viewportSize.width / this.page.width);
  }

  fitPage() {
    if (!this.page) return;
    const { width, height } = this.viewportSize;
    this.setScale(Math.min(width / this.page.width, height / this.page.height));
  }

  setFontScale(v) {
    this.fontScale = v;
    this.layout();
    this._clearMarker();
  }

  setStrict(v) { this.strict = v; this.layout(); }

  /** 溢出模式：不缩字号，译文超出原块高度时完整显示（可能压住下方块） */
  setOverflow(v) { this.overflowMode = !!v; this.layout(); }

  /**
   * 原图底衬：右栏以整页渲染图为底。
   * 开启时（默认）图片 / 表格 / 公式等不翻译的区域直接透出原图，只有译文块是白底；
   * 关闭时右栏回到纯白页面，仅显示译文（保留原文的块显示为虚线空框）。
   */
  setUnderlay(v) {
    this.showUnderlay = !!v;
    this.el.pageUnderlay.hidden = !this.showUnderlay;
    document.body.classList.toggle('original-base', this.showUnderlay);
  }

  // ---------------------------------------------------------------- 高亮
  highlight(id) {
    if (this.activeId === id) return;
    if (this.activeId) this._paint(this.activeId, false);
    this.activeId = id;
    if (id) this._paint(id, true);
  }

  _paint(id, on) {
    const act = (node) => node && node.classList[on ? 'add' : 'remove']('active');
    if (id.startsWith('region-')) {
      const r = this.regionEls.get(Number(id.slice(7)));
      act(r?.hit);
      act(r?.el);
      return;
    }
    const els = this.blockEls.get(id);
    if (!els) return;
    act(els.hit);
    act(els.tblock);
    act(els.preserve);
    // 块落在某个保留区域内时，连带把整个区域一起点亮
    if (els.region !== null && els.region !== undefined) {
      const r = this.regionEls.get(els.region);
      act(r?.hit);
      act(r?.el);
    }
  }

  /** 把译文层滚动到可见位置（点击定位时使用） */
  revealRight(id) {
    const cur = this.blockEls.get(id);
    const node = cur?.tblock || cur?.preserve;
    if (!node) return;
    const sc = this.el.scrollRight;
    const top = node.offsetTop;
    const bottom = top + node.offsetHeight;
    const viewTop = sc.scrollTop;
    const viewBottom = viewTop + sc.clientHeight;
    if (top < viewTop + 40 || bottom > viewBottom - 40) {
      sc.scrollTo({ top: Math.max(0, top - sc.clientHeight * 0.35), behavior: 'smooth' });
    }
  }

  /** 命中测试：把鼠标位置换算成 PDF 坐标，再找包含它的文本块 */
  _onPointerMove(ev, pane, select = false) {
    if (!this.page) return null;
    const stage = pane === 'left' ? this.el.stageLeft : this.el.stageRight;
    const rect = stage.getBoundingClientRect();
    const px = ev.clientX - rect.left;
    const py = ev.clientY - rect.top;
    const ptX = px / this.scale;
    const ptY = py / this.scale;
    const blk = this._hitTest(ptX, ptY);
    if (!blk) { if (!select) { this.highlight(null); this._clearMarker(); } return null; }
    this.highlight(blk.id);
    this._setMarker(blk, pane, px, py);
    return blk;
  }

  /** 命中测试：把鼠标位置换算成 PDF 坐标，再找包含它的块（块优先于保留区域） */
  _hitTest(x, y) {
    let best = null;
    let bestArea = Infinity;
    for (const blk of this.targets) {
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
    for (const blk of this.targets) {
      const [x0, y0, x1, y1] = blk.bbox;
      const cx = Math.min(Math.max(x, x0), x1);
      const cy = Math.min(Math.max(y, y0), y1);
      const d = Math.hypot(x - cx, y - cy);
      if (d < nd) { nd = d; nearest = blk; }
    }
    return nearest;
  }

  /** 十字标记：左右两栏放在同一屏幕偏移处，直观体现"同坐标" */
  _setMarker(blk, pane, px, py) {
    let x = px;
    let y = py;
    if (x === undefined) {
      const [x0, y0, x1, y1] = blk.bbox;
      x = ((x0 + x1) / 2) * this.scale;
      y = ((y0 + y1) / 2) * this.scale;
    }
    for (const m of [this.el.markerLeft, this.el.markerRight]) {
      m.hidden = false;
      m.querySelector('.marker-h').style.top = `${y}px`;
      m.querySelector('.marker-v').style.left = `${x}px`;
      const dot = m.querySelector('.marker-dot');
      dot.style.left = `${x}px`;
      dot.style.top = `${y}px`;
    }
  }

  _clearMarker() {
    this.el.markerLeft.hidden = true;
    this.el.markerRight.hidden = true;
  }

  _stageAnchor(ev, pane) {
    const stage = pane === 'left' ? this.el.stageLeft : this.el.stageRight;
    const rect = stage.getBoundingClientRect();
    return { x: ev.clientX - rect.left, y: ev.clientY - rect.top };
  }
}
