"""PDF 解析：按页渲染为图片 + 按阅读顺序抽取带坐标的文本块。

设计要点
--------
1. 左侧原文层 = 整页渲染图（``page.get_pixmap``），公式 / 图表 / 排版位置 100% 不变；
2. 右侧译文层需要"落在原文对应位置"，因此每个文本块都携带 PDF 坐标系的
   包围盒 ``bbox = [x0, y0, x1, y1]``（单位 pt，原点在左上角），前端乘以同一个
   缩放系数即可复现位置；
3. 阅读顺序用简化版 XY-Cut（先横切后纵切递归）推导，兼容"通栏标题 + 双栏正文"
   这类典型论文版式，使译文片段顺序与原文一致；
4. 词间空格需要显式还原：不少 IEEE / arXiv 模板把每个单词甚至每个空格都画成
   独立 span，若按 ``sp.text.strip()`` 过滤就会丢掉"只有空格"的 span，抽出
   ``CommunicationEfficientRoboticMixedReality…`` 这种粘连文本，既毁掉翻译质量，
   也让保留原文的公式/参考文献块显示成天书。
5. **图片 / 表格 / 公式一律不翻译**：右栏以原页渲染图为底，只有可译文本片段
   才用白底译文覆盖，于是这些区域天然"原样保留"。为此解析阶段要做两件事：
   ① 把栅格图与矢量绘图聚成"保留区域"（并吸附其 caption）；
   ② 把落在区域内的文字（图里的 ``Power`` / ``Control`` 之类标签）标为不可译，
   否则译出来的中文会盖在图上。表格同理（默认整表保留）。
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
from pathlib import Path
from typing import Any, Iterable

try:  # PyMuPDF >= 1.24 推荐使用 pymupdf 包名，旧版仍叫 fitz
    import pymupdf as fitz  # type: ignore
except ImportError:  # pragma: no cover
    import fitz  # type: ignore

# 真正的"强数学符号"：只有它们在文本里密集出现，才可能是在写公式而不是普通句子
MATH_SYMBOLS = set(
    "∫∮∑∏√∛±∓≈≠≤≥≪≫∞∂∇∈∉∋⊂⊃⊆⊇∪∩∧∨¬∀∃→←↔⇒⇔"
    "αβγδεζηθικλμνξοπρστυφχψωΓΔΘΛΞΠΣΦΨΩ×÷⋅·∘⋯…"
)
# 运算符：公式里常见，但单独出现说明不了什么（普通英文里也有连字符）
MATH_OPERATORS = set("=+−-<>^_{}\\|*")
_LIGATURES = {"ﬁ": "fi", "ﬂ": "fl", "ﬀ": "ff", "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "st"}

# ---- 行内公式识别 --------------------------------------------------
# 论文里的数学排版有固定字体族：LaTeX 默认用 Computer Modern（CMR/CMBX/CMMI/CMSY/CMEX…），
# 其它常见的有 AMS 的 MSAM/MSBM、Euler 的 EUSM/EUFM、MathTime 的 MTMI/MTSY、STIX/XITS 等。
# 按字体判断远比"看字符像不像公式"可靠——这也是"公式嵌在正文里"最稳的识别方式。
_MATH_FONT_RE = re.compile(
    r"(CMR|CMBX|CMMI|CMSY|CMEX|CMSS|CMTT|CMTI|CMEX|MSAM|MSBM|EUSM|EUFM|RSFS|MTMI|MTSY|MTEX"
    r"|XITS|STIX|LMMATH|LATINMODERN|MATHPI|MATHTYPE|EUCLID|WASY|WNCY|CAMBRIA ?MATH|ASANA"
    r"|SYMBOL|MARVOSYM|RSFS)",
    re.I,
)
# 行内公式占位符：⟦1⟧ ⟦2⟧ …（送到模型时替换掉公式，回填时再换成原图小块）
MATH_TOKEN_RE = re.compile(r"⟦\s*(\d+)\s*⟧")

# 参考文献：章节标题 与 条目起始标记
_REF_SECTION = re.compile(r"^(references|bibliography|参考文献)\s*$", re.I)
_REF_ENTRY = re.compile(r"^\s*\[\d{1,3}\]\s")

# 表格行优先排序时的高度分桶容差（pt）：同一行的格子顶部 y 会有零点几磅的差异
TABLE_ROW_TOL = 4.0

# ---- 保留区域（图片 / 表格）：不翻译，右栏原样显示 ----------------
# caption 形如 "Fig. 1: …" / "Figure 3." / "TABLE I: …" / "Algorithm 2: …"。
# 必须带编号后的分隔符，否则 "Fig. 3 shows …" 这类正文引用会被误认成标题。
_CAPTION_RE = re.compile(
    r"^\s*(fig(?:ure)?\.?|tab(?:le)?\.?)"
    r"\s*([0-9]{1,2}|[IVXLC]{1,5})\s*[:.\u2013\u2014-]",
    re.I,
)
_CAPTION_TABLE_RE = re.compile(r"^\s*(tab(?:le)?\.?)", re.I)

# 章节标题（"III. SYSTEM MODEL" / "A. Overview" / "2. Method"）：即便落在区域框内也不吸收
_HEADING_RE = re.compile(r"^\s*(?:[IVXLC]{1,4}|\d{1,2}|[A-Z])\.\s+\S")

VISUAL_IMAGE_GAP = 12.0   # 栅格图之间小于此间距就并入同一张图
VISUAL_CAPTION_GAP = 26.0  # 图块 / 表格行与 caption 之间允许的最大间距
TABLE_ROW_GAP = 18.0       # 表格行块之间允许的最大竖直间距
TABLE_ROW_SPACING = 9.5    # 行距超过它视为正文行，表格聚类到此为止
TABLE_ROW_MAX_CHARS = 400  # 单行超过这么多字符也不像表格行
COL_BAND_PT = 4.0          # 同一 y 上下 ±这么宽的片段，算作「同一表格行」
COL_BAND_MIN = 3           # 一行里至少这么多并排片段，才算「表格行」
COL_MIN_ROWS = 3           # 块内至少这么多表格行 → 整块判为表格内容
TABLE_CELL_MAX_CHARS = 150  # 超过这么多字符的"单元格"判为长句，放回普通块照常翻译
MIN_REGION_RATIO = 0.012          # 最终保留区域的最小面积占比
MAX_REGION_RATIO = 0.62           # 过大（几乎整页）的区域判为误检，丢弃
MAX_DRAW_RATIO = 0.35             # 单条矢量路径超过这一占比视为页面装饰/裁切框

BLOCK_KINDS = (
    "text", "formula", "reference", "table", "caption", "figure",
    "margin", "pageno", "meta", "empty",
)


# ----------------------------------------------------------------------
# 工具函数
# ----------------------------------------------------------------------
def doc_id_for_bytes(data: bytes) -> str:
    """文档指纹：同一份 PDF 复用解析结果与翻译缓存。"""
    return hashlib.sha1(data).hexdigest()[:16]


def text_hash(text: str) -> str:
    return hashlib.sha1(text.strip().encode("utf-8")).hexdigest()[:16]


def _norm_text(text: str) -> str:
    for src, dst in _LIGATURES.items():
        text = text.replace(src, dst)
    text = text.replace("\u00a0", " ").replace("\u2019", "'").replace("\u201c", '"').replace("\u201d", '"')
    return re.sub(r"[ \t]+", " ", text).strip()


def is_math_span(span: dict) -> bool:
    """判断一个 span 是不是"数学排版"。

    优先看字体族（最可靠）；字体名认不出来时再看字符本身：
    控制码（很多数学字体的编码落在 0x00–0x1F）、强数学符号 + 没有像样的英文词。
    """
    text = span.get("text", "")
    if not text.strip():
        return False
    font = str(span.get("font", "") or "")
    if font and _MATH_FONT_RE.search(font):
        return True
    core = [ch for ch in text if not ch.isspace()]
    if any(ord(ch) < 0x20 for ch in core):
        return True
    if any(ch in MATH_SYMBOLS for ch in core) and not has_english_word(text):
        return True
    return False


def _line_text_with_math(spans: list[dict], runs: list[dict], counter: list[int]) -> str:
    """拼一行文本；数学 span 换成占位符 ``⟦k⟧``，并把它的坐标记进 ``runs``。

    这样"公式嵌在正文里"就能做到：正文照常翻译，而公式那一段永远按原图呈现
    （前端拿到 `runs` 里的 bbox 后，从整页渲染图上切一小块贴回去）。

    ``counter`` 是整页共享的自增计数器——编号必须**全页唯一**，
    否则同一个块里出现两个 ⟦1⟧ 时前端会贴错公式（踩过）。
    """
    parts: list[str] = []
    prev_x1: float | None = None
    prev_size = 0.0
    cur: dict | None = None
    for sp in spans:
        txt = sp.get("text", "")
        if txt == "":
            continue
        bbox = sp.get("bbox") or [0.0, 0.0, 0.0, 0.0]
        x0, x1 = float(bbox[0]), float(bbox[2])
        size = float(sp.get("size", 0.0)) or prev_size or 10.0
        need_space = (
            prev_x1 is not None
            and parts
            and not parts[-1].endswith(" ")
            and not txt.startswith(" ")
            and (x0 - prev_x1) > max(1.0, 0.22 * size)
        )
        if sp.get("math"):
            if cur is None:
                if need_space:
                    parts.append(" ")
                counter[0] += 1
                cur = {"key": str(counter[0]), "bbox": [float(v) for v in bbox], "text": txt}
                runs.append(cur)
                parts.append(f"⟦{cur['key']}⟧")
            else:
                cur["bbox"] = [
                    min(cur["bbox"][0], float(bbox[0])),
                    min(cur["bbox"][1], float(bbox[1])),
                    max(cur["bbox"][2], float(bbox[2])),
                    max(cur["bbox"][3], float(bbox[3])),
                ]
                cur["text"] += txt
        else:
            cur = None
            if need_space:
                parts.append(" ")
            parts.append(txt)
        prev_x1 = x1
        prev_size = size
    return "".join(parts)


def _line_text(spans: list[dict]) -> str:
    """把同一行的 spans 拼成文本，尽最大努力还原词间空格。

    两个坑：

    * 有些 PDF（IEEE / arXiv 模板很常见）把空格画成**独立的 span**，其 text 是
      ``" "``。若用 ``sp.text.strip()`` 做过滤，这个 span 会被当成"空"丢掉，
      结果 ``Communication Efficient Robotic`` 变成 ``CommunicationEfficientRobotic``。
      所以这里只丢弃 text 真正为空的 span。
    * 另有一类 PDF 干脆不画空格字符，靠字符定位排版。此时用相邻 span 的水平
      间隙做兜底：间隙超过约 0.22 em 且两侧都没有空格，就补一个空格。
    """
    parts: list[str] = []
    prev_x1: float | None = None
    prev_size = 0.0
    for sp in spans:
        txt = sp.get("text", "")
        if txt == "":
            continue
        bbox = sp.get("bbox") or [0.0, 0.0, 0.0, 0.0]
        x0, x1 = float(bbox[0]), float(bbox[2])
        size = float(sp.get("size", 0.0)) or prev_size or 10.0
        if (
            prev_x1 is not None
            and parts
            and not parts[-1].endswith(" ")
            and not txt.startswith(" ")
            and (x0 - prev_x1) > max(1.0, 0.22 * size)
        ):
            parts.append(" ")
        parts.append(txt)
        prev_x1 = x1
        prev_size = size
    return "".join(parts)


def _merge_lines(lines: list[dict]) -> str:
    """合并同一段落的行，处理行尾连字符断词。"""
    if not lines:
        return ""
    out = lines[0]["text"].strip()
    for ln in lines[1:]:
        nxt = ln["text"].strip()
        if not nxt:
            continue
        if out.endswith("-") and not out.endswith(("--", "->")):
            # 行尾连字符：绝大多数是排版断词，直接拼接
            out = out[:-1] + nxt
        elif out.endswith(("/", "(", "[", "{")):
            out = out + nxt
        else:
            out = out + " " + nxt
    return _norm_text(out)


_LIST_MARK_RE = re.compile(r"^(?:\d{1,2}[.)]|\(\d{1,2}\)|\(?[A-Za-z][.)]|[•·▪◦‣])\s+\S")
_REF_MARK_RE = re.compile(r"^\[\d{1,3}\]\s")
_SENT_END_RE = re.compile(r"[.:;!?)\]”’]\s*$")
# 严格的"句子结束"：只认 . ! ?（可带右引号/括号）。跨块接续用它判断是否同段续行
_SENT_END_STRICT_RE = re.compile(r"[.!?][\"'”’)\]]*\s*$")


def _looks_continued(prev_text: str, next_text: str) -> bool:
    """跨块接续时判断"下一块是不是同一段落的续行"。

    参考文献这类列表里，每一条之间只隔约 1pt，**与段内行距完全一样**，靠间距
    根本分不开；若一律按间距接续，整页参考文献会被并成一大块（踩过：实测
    单块从 400 字涨到 4500 字）。因此再叠一条文本判据：上一行没有以句末标点
    收尾，或下一行以小写字母开头 → 认为是同一段的续行。
    """
    p = (prev_text or "").strip()
    n = (next_text or "").strip()
    if not p or not n:
        return False
    if n[0].islower():
        return True
    return not _SENT_END_STRICT_RE.search(p)


def _row_style_size(line: dict) -> float:
    """一行"正文部分"的字号（排除行内公式，避免数学符号把字号抬上去）。"""
    return float(_row_size([line]) or 0.0)


_BOLD_FONT_KEYS = ("bold", "medi", "black", "heavy", "semib", "demi")


def _row_is_bold(line: dict) -> bool:
    """这一行是否**整体加粗**（行内个别强调词不算）。

    字号断点分不开的标题就靠它：实测某论文的小节标题是 10.91pt / NimbusRomNo9L-Medi
    （flags 含 bold 位），正文 9.96pt / NimbusRomNo9L-Regu —— 只差 0.95pt（9.5%），
    不够 ``_style_changed`` 的 1.5pt / 18% 阈值，标题于是被并进正文一起翻译（踩过）。
    要求「加粗 span 占比 ≥ 60%」而不是「任一行内粗体」：正文里偶尔出现一个加粗术语
    很常见，那种不能当段落边界。
    """
    spans = [s for s in (line.get("spans") or []) if (s.get("text") or "").strip()]
    if not spans:
        return False
    hit = 0
    for s in spans:
        font = (s.get("font") or "").lower()
        if (int(s.get("flags") or 0) & 16) or any(k in font for k in _BOLD_FONT_KEYS):
            hit += 1
    return hit >= 0.6 * len(spans)


def _style_changed(a: dict, b: dict) -> bool:
    """相邻两行字号或字重不同 → 标题与正文的交界，必须断开。

    标题与正文的行距常常只有 4–5pt（正文段内是 1pt 左右），只按行距判断一定会
    并成一段，于是**标题被并进正文、跟着一起翻译**（踩过两次）。
    判据有两条，任一命中即断：
    - 字号差 ≥1.5pt 或 ≥18%（实测 11.96 vs 9.96）——同一段内行内公式造成的
      0.1~0.2pt 抖动不会触发；
    - **整行加粗状态不同**（实测标题 10.91pt/Medi vs 正文 9.96pt/Regu，字号只差 0.95pt，
      单靠字号分不开）。这条也能覆盖"同字号但加粗"的小标题。
    """
    sa, sb = _row_style_size(a), _row_style_size(b)
    if sa > 0 and sb > 0:
        hi, lo = max(sa, sb), min(sa, sb)
        if (hi - lo) >= 1.5 or hi / lo >= 1.18:
            return _not_same_visual_row(a, b)
    if _row_is_bold(a) != _row_is_bold(b):
        return _not_same_visual_row(a, b)
    return False


def _not_same_visual_row(a: dict, b: dict) -> bool:
    """两块是否属于不同的视觉行。

    **同一 y 上的并排片段绝不算段落边界**：run-in 标题（`Definition of Action Levels.`）
    与紧随其后的正文首句（`In embodied agent research, …`）就是同一行的左右两段，
    中间只隔 11pt（超过 ``_stitch_rows`` 的 6pt 缝合阈值，因此没被缝成一行）。
    按跨度判会把它们拆成两块，而两块的 bbox 纵向完全重叠 → 渲染时正文把标题盖住，
    标题文字直接消失（踩过，EmbodiedBench p3）。
    """
    ab, bb = a["bbox"], b["bbox"]
    ha = max(1e-6, ab[3] - ab[1])
    hb = max(1e-6, bb[3] - bb[1])
    overlap = min(ab[3], bb[3]) - max(ab[1], bb[1])
    return overlap <= 0.5 * min(ha, hb)


def _is_list_start(line: dict, prev: dict | None) -> bool:
    """这一行是不是一个新的列表项（``1.`` / ``(a)`` / ``•`` …）。

    列表项之间往往只隔 3pt，靠行距分不开，会把整串列表并成一段（踩过）。
    只在「上一行以句末标点收尾」时才断，避免正文里以编号开头的续行被误切。
    """
    text = (line.get("text") or "").strip()
    if not _LIST_MARK_RE.match(text):
        return False
    if prev is None:
        return True
    if _SENT_END_RE.search((prev.get("text") or "").strip()):
        return True
    # 悬挂缩进：列表项从更靠左的边界起排，续行通常缩进更深
    return float(line["bbox"][0]) < float(prev["bbox"][0]) - 4.0


def _group_lines(lines: list[dict]) -> list[dict]:
    """按行间垂直间距把块内的行分组为自然段（间距过大视为换段）。

    注意：这里**刻意不按水平重叠再切**。公式区域里同一 y 上有大量并列的片段
    （``σ2`` / ``(`` / ``if g(t) ≤ µ``…），按水平重叠切会把公式打碎成一堆小段，
    既看不出是公式（送模型去翻毫无意义的碎片），也会在右侧糊上一片小白框。

    间距用行的 ``core``（正文部分）算而不是整行包围盒：行内分式会把行撑高，
    用整行算间距时会算出负数，隔着两段的行也有可能被并成一段。

    另外两条"结构断点"必须显式判断，光看间距不够：

    * :func:`_style_changed` —— 字号变了（标题 → 正文），否则标题会被吞进正文；
    * :func:`_is_list_start` —— 新列表项，否则整串列表会被并成一大段。
    """
    if not lines:
        return []
    bound = lambda l: l.get("core") or l["bbox"]  # noqa: E731
    lines = sorted(lines, key=lambda l: (bound(l)[1], l["bbox"][0]))
    groups: list[list[dict]] = [[lines[0]]]
    for prev, cur in zip(lines, lines[1:]):
        gap = bound(cur)[1] - bound(prev)[3]
        limit = max(6.0, 0.62 * max(cur.get("size", 10.0), 6.0))
        # 列表项内部行距更紧（实测 1pt）；一旦当前段是列表项，稍大的空行（>4.5pt）
        # 就是"列表结束、进入下一段"，否则最后一项会把后面的正文段落吞掉（踩过）
        in_list = bool(_LIST_MARK_RE.match((groups[-1][0].get("text") or "").strip()))
        # 参考文献条目之间只隔约 1pt（与段内行距相同），且 PyMuPDF 的块边界常常
        # 横跨两条文献；靠间距/接续都分不开，只能认 ``[12]`` 这个条目开头标记。
        is_ref = bool(_REF_MARK_RE.match((cur.get("text") or "").strip()))
        if (gap > limit or _style_changed(prev, cur) or _is_list_start(cur, prev)
                or (in_list and gap > 4.5) or is_ref):
            groups.append([cur])
        else:
            groups[-1].append(cur)
    return groups


def _merge_adjacent_streams(streams: list[list[dict]]) -> list[list[dict]]:
    """把「同一栏、上下紧邻」的分量接成一条流，再统一分段。

    ``_block_components`` 只按**几何重叠**聚块，而 PyMuPDF 经常把**一个段落**拆成
    好几个互不重叠的块（实测某段被拆成 3 块，还在 ``Euler an- | gles`` 处断成两半）。
    若仍按分量各自分段，这一段就会被切成 3 段分别翻译、排版错乱（踩过）。

    接续条件：水平投影重叠 ≥ 50%、**宽度相近**（≥60%）、竖直间距 ≤ 段落阈值。
    宽度必须相近——整页宽的大标题不能和半栏正文接成一条流，否则左右两栏的行
    会按 y 交错排到一起，整页排版都会乱。
    """
    items: list[dict] = []
    for rows in streams:
        if rows:
            ordered = sorted(rows, key=lambda r: ((r.get("core") or r["bbox"])[1], r["bbox"][0]))
            items.append({"rows": ordered, "bbox": _union([r["bbox"] for r in ordered])})
    items.sort(key=lambda it: (it["bbox"][1], it["bbox"][0]))

    out: list[dict] = []
    for it in items:
        nb = it["bbox"]
        nw = nb[2] - nb[0]
        for cur in out:
            cb = cur["bbox"]
            cw = cb[2] - cb[0]
            if cw <= 0 or nw <= 0:
                continue
            if min(cw, nw) < 0.6 * max(cw, nw):
                continue                                   # 宽度差太多：整页标题 vs 半栏正文
            ov = min(cb[2], nb[2]) - max(cb[0], nb[0])
            if ov <= 0.5 * min(cw, nw):
                continue                                   # 不同栏
            limit = max(6.0, 0.62 * max(_row_style_size(it["rows"][0]), 6.0))
            if nb[1] - cb[3] > limit:
                continue                                   # 已换段
            if not _looks_continued(cur["rows"][-1].get("text"), it["rows"][0].get("text")):
                continue                                   # 上一块以句末收尾：是新段落（如参考文献每条）
            cur["rows"].extend(it["rows"])
            cur["bbox"] = _union([cb, nb])
            break
        else:
            out.append({"rows": list(it["rows"]), "bbox": list(nb)})
    return [it["rows"] for it in out]


# ----------------------------------------------------------------------
# 行缝合：TeX 的行内公式会把「一条视觉行」拆成好几个 PyMuPDF 块
# ----------------------------------------------------------------------
def _v_overlap(a: list[float], b: list[float]) -> float:
    return min(a[3], b[3]) - max(a[1], b[1])


def _h_overlap(a: list[float], b: list[float]) -> float:
    return min(a[2], b[2]) - max(a[0], b[0])


def _h_gap(a: list[float], b: list[float]) -> float:
    """两个盒子的水平间隔；重叠时为负。"""
    return max(a[0], b[0]) - min(a[2], b[2])


def _same_visual_line(a: list[float], b: list[float]) -> bool:
    """两个盒子是否落在**同一条视觉行**上。

    纵向只要求"高度重合度足够高"：同一行的碎片共享同一条基线，包围盒几乎完全
    重合（实测 91%~100%）。而相邻两行最多重合 53%——行内分式把行撑高之后会压到
    下一行上。0.65 这条线正好落在两个区间中间。

    横向要求"相接"：要么水平方向有重叠（TeX 把 ``{m_t}^{T}`` 和 ``t=1`` 水平错开
    几个点），要么间隔不超过 6pt。这一条顺带把双栏排版的左右两栏挡在外面
    （栏间距通常 > 10pt），否则同一高度的左右两栏会被缝成一行。
    """
    ha = a[3] - a[1]
    hb = b[3] - b[1]
    if ha <= 0.1 or hb <= 0.1:
        return False
    if _v_overlap(a, b) < 0.65 * min(ha, hb):
        return False
    if _h_overlap(a, b) > 0:
        return True
    return _h_gap(a, b) <= 6.0


def _row_core(pieces: list[dict]) -> list[float]:
    """一行里"正文（非数学）部分"的纵向范围，作为段落分组的依据。"""
    boxes: list[list[float]] = []
    for p in pieces:
        for sp in p.get("spans") or []:
            if sp.get("math"):
                continue
            b = sp.get("bbox")
            if b and (float(b[3]) - float(b[1])) > 0.1:
                boxes.append([float(v) for v in b])
    if boxes:
        return _union(boxes)
    return _union([p["bbox"] for p in pieces])


def _row_size(pieces: list[dict]) -> float:
    """一行正文的字号。取非数学 span 的最大字号——行内一个 20pt 的数学符号
    不该让整段中文按 20pt 去排版（那会把字号越缩越小）。
    整行都是数学时才退回全体最大字号。"""
    body = [float(sp.get("size", 0.0)) for p in pieces for sp in (p.get("spans") or []) if not sp.get("math")]
    if body:
        return round(max(body), 2)
    return round(max([float(p.get("size", 0.0)) for p in pieces] or [0.0]), 2)


def _merge_math_clusters(runs: list[dict], text: str) -> tuple[list[dict], str]:
    """把"同一个公式被切成两个占位符"的情况合并回一个。

    例如 ``{m_t}^{T}_{t=1}`` 会被拆成 ``{m_t}^{T}`` 与 ``t=1`` 两段，各拿半个
    包围盒。若分别贴图，下标会跑到右边而不是下面。按水平重叠度合并后只留一个
    占位符，贴回去才是完整的原公式。
    """
    ordered = sorted(runs, key=lambda r: (r["bbox"][0], r["bbox"][1]))
    kept: list[dict] = []
    drop: list[str] = []
    for r in ordered:
        if kept:
            k = kept[-1]
            wk = k["bbox"][2] - k["bbox"][0]
            wr = r["bbox"][2] - r["bbox"][0]
            hk = k["bbox"][3] - k["bbox"][1]
            hr = r["bbox"][3] - r["bbox"][1]
            if (
                min(wk, wr) > 0.1
                and min(hk, hr) > 0.1
                and _h_overlap(k["bbox"], r["bbox"]) > 0.25 * min(wk, wr)
                and _v_overlap(k["bbox"], r["bbox"]) > 0.30 * min(hk, hr)
            ):
                k["bbox"] = [
                    min(k["bbox"][0], r["bbox"][0]),
                    min(k["bbox"][1], r["bbox"][1]),
                    max(k["bbox"][2], r["bbox"][2]),
                    max(k["bbox"][3], r["bbox"][3]),
                ]
                k["text"] = k.get("text", "") + r.get("text", "")
                drop.append(str(r["key"]))
                continue
        kept.append(r)
    for key in drop:
        text = re.sub(rf"⟦\s*{re.escape(key)}\s*⟧", "", text, count=1)
    return kept, text


def _merge_row(pieces: list[dict]) -> dict:
    """把同一条视觉行上的若干碎片合成一行（按 x 从左到右拼接文本）。"""
    pieces = sorted(pieces, key=lambda p: (p["bbox"][0], p["bbox"][1]))
    if len(pieces) == 1:
        out = dict(pieces[0])
        out["core"] = _row_core(pieces)
        return out
    text = ""
    spans: list[dict] = []
    runs: list[dict] = []
    flags = 0
    prev_x1: float | None = None
    prev_size = float(pieces[0].get("size", 10.0))
    for p in pieces:
        t = p.get("text", "")
        if (
            prev_x1 is not None
            and t
            and text
            and not text.endswith(" ")
            and not t.startswith(" ")
            and (p["bbox"][0] - prev_x1) > max(1.0, 0.22 * prev_size)
        ):
            text += " "
        text += t
        spans.extend(p.get("spans") or [])
        runs.extend(p.get("math_runs") or [])
        flags |= int(p.get("flags", 0))
        prev_x1 = p["bbox"][2]
        prev_size = float(p.get("size", prev_size))
    runs, text = _merge_math_clusters(runs, text)
    return {
        "text": text,
        "bbox": _union([p["bbox"] for p in pieces]),
        "size": _row_size(pieces),
        "flags": flags,
        "spans": spans,
        "math_runs": runs,
        "chars": sum(int(p.get("chars", 0)) for p in pieces),
        "math_chars": sum(int(p.get("math_chars", 0)) for p in pieces),
        "core": _row_core(pieces),
    }


def _stitch_rows(lines: list[dict]) -> list[dict]:
    """把被打散的"行碎片"重新缝成整行。

    起因：IEEE / arXiv 的 LaTeX 排版里，只要行内出现分式、上下标这类纵向伸展的
    公式，PyMuPDF 就会把**同一条视觉行**切进好几个文本块。例如
    ``MR images {m_t}^T_{t=1} and the actual MR images ...`` 会被切成
    ``MR images {mt}T`` / ``t=1 and the actual MR images {bmt}T`` / ``t=1, by``
    三块。若照块边界直接当段落，右侧译文就会变成一堆互相重叠、语序错乱的碎片
    （踩过：整段中文像被撕成三片叠在一起）。

    分两步：

    1. 按严格的几何规则聚行（同一行的碎片包围盒几乎完全重合）；
    2. 把**纯数学的碎片**"吸附"到最近的整行上。

    第 2 步是必需的：分式的分子、上下标被 TeX 排在不同的高度上，与正文行的
    纵向重合度会落在 53%~64% 这个区间里——而"相邻两行"的上限也正好是 53%，
    所以拿重合度当阈值会时对时错（实测两个真实案例分别落在 53% 与 64%，只差
    11 个百分点）。改成"离最近的整行中心有多远"就稳定了：它们本来就在同一行，
    只是高低不同。
    """
    n = len(lines)
    if n <= 1:
        return [_merge_row(lines)] if lines else []
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(n):
        for j in range(i + 1, n):
            if _same_visual_line(lines[i]["bbox"], lines[j]["bbox"]):
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[rj] = ri
    buckets: dict[int, list[dict]] = {}
    for i, ln in enumerate(lines):
        buckets.setdefault(find(i), []).append(ln)
    plists = list(buckets.values())

    # ---- 第二轮：纯数学碎片吸附到最近的整行 ----
    def has_text(group: list[dict]) -> bool:
        return any(not sp.get("math") for ln in group for sp in (ln.get("spans") or []))

    anchors = [g for g in plists if has_text(g)]
    floaters = [g for g in plists if not has_text(g)]
    if anchors and floaters:
        center = lambda g: (_union([ln["bbox"] for ln in g])[1] + _union([ln["bbox"] for ln in g])[3]) / 2.0  # noqa: E731
        anchors.sort(key=center)
        cs = [center(g) for g in anchors]
        steps = [abs(b - a) for a, b in zip(cs, cs[1:]) if 0 < abs(b - a) < 40]
        steps.sort()
        lead = steps[len(steps) // 2] if steps else 12.0     # 正文行距（中位数）
        alive = [True] * len(plists)
        for f in floaters:
            box = _union([ln["bbox"] for ln in f])
            fc = (box[1] + box[3]) / 2.0
            best, best_d = None, None
            for a in anchors:
                abox = _union([ln["bbox"] for ln in a])
                if _h_overlap(abox, box) <= 0 and _h_gap(abox, box) > 12.0:
                    continue                                  # 不同栏
                d = abs(((abox[1] + abox[3]) / 2.0) - fc)
                if best_d is None or d < best_d:
                    best, best_d = a, d
            if best is not None and best_d is not None and best_d <= max(6.0, 0.6 * lead):
                best.extend(f)
                alive[plists.index(f)] = False
        plists = [g for g, ok in zip(plists, alive) if ok]

    rows = [_merge_row(g) for g in plists]
    rows.sort(key=lambda r: ((r.get("core") or r["bbox"])[1], r["bbox"][0]))
    return rows


def _block_components(entries: list[dict]) -> list[list[dict]]:
    """把 PyMuPDF 的文本块按"是否叠在一起"聚成连通分量。

    被公式拆散的碎片，包围盒必然互相重叠——这正是"可能是同一段"的信号。
    这里**刻意放宽**：只要纵向重叠超过 2.5pt、横向相接就并到一起。
    因为"到底是不是同一段"由后面的 ``_group_lines`` 用正文行间距判定，
    分量只是候选池；但漏并的代价很大——碎片会自成一块、叠在原文上（踩过）。

    横向要求"相接"：水平方向有重叠，或间隔不超过 6pt。这一条把双栏排版的左右两栏
    挡住（栏间距通常 > 10pt）。**不能**改成长度比例限制（试过
    ``min(w) >= 0.45*max(w)``）：行内公式碎片窄到一个 ``⟦80⟧`` 只有 4pt 宽，
    长度比 1.8%，会被挡在段落外面，段落尾巴就被撕下来叠在原文上。
    """
    n = len(entries)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(n):
        bi = entries[i]["bbox"]
        wi, hi = bi[2] - bi[0], bi[3] - bi[1]
        if wi <= 0 or hi <= 0:
            continue
        for j in range(i + 1, n):
            bj = entries[j]["bbox"]
            wj, hj = bj[2] - bj[0], bj[3] - bj[1]
            if wj <= 0 or hj <= 0:
                continue
            if _v_overlap(bi, bj) <= 2.5:
                continue
            if _h_overlap(bi, bj) < 0.30 * min(wi, wj) and _h_gap(bi, bj) > 6.0:
                continue
            ri, rj = find(i), find(j)
            if ri != rj:
                parent[rj] = ri
    groups: dict[int, list[dict]] = {}
    for i, e in enumerate(entries):
        groups.setdefault(find(i), []).append(e)
    return list(groups.values())


def _union(boxes: Iterable[list[float]]) -> list[float]:
    boxes = list(boxes)
    return [
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    ]


# ----------------------------------------------------------------------
# 阅读顺序：简化 XY-Cut
# ----------------------------------------------------------------------
def _find_h_gap(boxes: list[list[float]], min_gap: float = 10.0):
    """找一条完全没被块覆盖的水平空白带（返回 (gap, y_center) 中最大者）。"""
    ys = sorted({b[1] for b in boxes} | {b[3] for b in boxes})
    best = None
    for a, b in zip(ys, ys[1:]):
        if b - a < min_gap:
            continue
        mid = (a + b) / 2.0
        if any(box[1] < mid < box[3] for box in boxes):
            continue
        if best is None or (b - a) > best[0]:
            best = (b - a, mid)
    return best


def _find_v_gaps(boxes: list[list[float]], min_gap: float = 9.0):
    """找完全没被块覆盖的竖直空白带，要求两侧都有块（真正的中缝）。"""
    xs = sorted({b[0] for b in boxes} | {b[2] for b in boxes})
    gaps = []
    xmin = min(b[0] for b in boxes)
    xmax = max(b[2] for b in boxes)
    for a, b in zip(xs, xs[1:]):
        if b - a < min_gap:
            continue
        mid = (a + b) / 2.0
        if any(box[0] < mid < box[2] for box in boxes):
            continue
        if not any(box[2] <= a + 0.5 for box in boxes):
            continue
        if not any(box[0] >= b - 0.5 for box in boxes):
            continue
        if mid <= xmin + 4 or mid >= xmax - 4:
            continue
        gaps.append((b - a, a, b))
    return gaps


def reading_order(items: list[dict], depth: int = 0) -> list[dict]:
    """递归 XY-Cut 排序：返回阅读顺序的块列表。"""
    if len(items) <= 1 or depth > 12:
        return sorted(items, key=lambda it: (round(it["bbox"][1], 1), it["bbox"][0]))

    boxes = [it["bbox"] for it in items]
    h_gap = _find_h_gap(boxes)
    v_gaps = _find_v_gaps(boxes)
    v_best = max(v_gaps, key=lambda g: g[0]) if v_gaps else None

    # 横向空白带更"显眼"时先横切（例如通栏标题与下方双栏正文之间）
    if h_gap and (v_best is None or h_gap[0] >= 0.8 * v_best[0]):
        y = h_gap[1]
        top = [it for it in items if (it["bbox"][1] + it["bbox"][3]) / 2 < y]
        bottom = [it for it in items if (it["bbox"][1] + it["bbox"][3]) / 2 >= y]
        if top and bottom:
            return reading_order(top, depth + 1) + reading_order(bottom, depth + 1)

    if v_best:
        _, gx0, gx1 = v_best
        left = [it for it in items if it["bbox"][2] <= gx0 + 0.5]
        right = [it for it in items if it["bbox"][0] >= gx1 - 0.5]
        spanning = [it for it in items if it not in left and it not in right]
        if spanning:
            # 通栏块充当分隔符：按 y 分带，逐带递归
            spanning.sort(key=lambda it: it["bbox"][1])
            bands: list[list[dict]] = [[] for _ in range(len(spanning) + 1)]
            rest = left + right
            for it in rest:
                yc = (it["bbox"][1] + it["bbox"][3]) / 2
                idx = 0
                for k, sp in enumerate(spanning):
                    if yc >= (sp["bbox"][1] + sp["bbox"][3]) / 2:
                        idx = k + 1
                bands[idx].append(it)
            out: list[dict] = []
            for k, sp in enumerate(spanning):
                out.extend(reading_order(bands[k], depth + 1))
                out.append(sp)
            out.extend(reading_order(bands[-1], depth + 1))
            return out
        if left and right:
            return reading_order(left, depth + 1) + reading_order(right, depth + 1)

    return sorted(items, key=lambda it: (round(it["bbox"][1], 1), it["bbox"][0]))


# ----------------------------------------------------------------------
# 块抽取与分类
# ----------------------------------------------------------------------
def looks_like_formula(text: str) -> bool:
    """判断一段文本是不是数学公式。

    旧做法是「非字母数字字符占比 > 0.12 就算公式」，结果正文里只要逗号、括号、
    引用编号稍多就被误判——实测把 499 个块判成公式（比正文块还多），
    连带整段正文不翻译、右侧显示英文原文。

    新做法看两件事：强数学符号 / 运算符的数量，以及英文字母占比（自然语言的
    字母占比通常 > 0.7）。实测同一篇论文：命中 66 块，且完全覆盖旧判据里
    真正的公式，误判清零。
    """
    core = [ch for ch in text if not ch.isspace()]
    n = len(core)
    if n < 6:
        return False
    strong = sum(1 for ch in core if ch in MATH_SYMBOLS)
    operators = sum(1 for ch in core if ch in MATH_OPERATORS)
    alpha_ratio = sum(1 for ch in core if ch.isascii() and ch.isalpha()) / n
    if strong >= 2 and alpha_ratio < 0.55:
        return True
    return (strong + operators) >= 3 and alpha_ratio < 0.60


def _classify(block: dict, page_width: float) -> str:
    text = block["text"].strip()
    if not text:
        return "empty"
    # 页码 / 图表编号 / 公式编号：极短的纯数字或罗马数字（不翻译，但仍占位）
    if len(text) <= 4 and re.fullmatch(r"[0-9ivxlcdmIVXLCDM\-–—.\s]+", text):
        return "pageno"
    if re.match(r"^arxiv:\s*\d{4}\.\d{4,5}", text, re.I):
        return "meta"
    x0, _y0, x1, _y1 = block["bbox"]
    # 侧边栏（arXiv 竖排水印、页边行号）不参与翻译
    if x1 < page_width * 0.10 or x0 > page_width * 0.90:
        return "margin"
    # 数学排版占比高 → 整块就是公式/公式碎片（如 'T X'、't=1'、'{'）：
    # 这些送模型只会得到莫名其妙的输出，也会在右侧糊出一串小框，直接保留原图。
    if block.get("math_ratio", 0.0) >= 0.6:
        return "formula"
    if looks_like_formula(text):
        return "formula"
    # 以 [12] 开头的条目几乎一定是参考文献
    if _REF_ENTRY.match(text):
        return "reference"
    return "text"


def _extract_page(page: fitz.Page, inline_math: bool = True) -> tuple[list[dict], list[dict]]:
    """抽取本页的文本块与全部文本行。

    返回 ``(按段落分组后的文本块, 页面全部文本行)``。
    后者单独返回是为了按表格单元格重新分组——表格里一行横跨多个单元格，
    照段落走会把整张表拼成一段话。

    流程比"照 PyMuPDF 的块直接当段落"多两步，都是为了对付 LaTeX 论文里
    行内公式把一行拆散的问题：

    1. ``_block_components`` —— 把互相叠在一起的块聚成连通分量，先把"同一段的
       碎片"收拢，同时靠空白守住段落边界；
    2. ``_stitch_rows`` —— 在分量内把行碎片按几何缝回整行；
    3. ``_group_lines`` —— 再按正文行的间距切自然段。

    ``inline_math=True`` 时，行内的数学 span 会被换成占位符 ``⟦k⟧`` 并记录其坐标，
    于是正文可以照常翻译、公式仍按原图呈现。
    """
    data = page.get_text("dict")
    counter = [0]  # 行内公式占位符编号：整页唯一（见 _line_text_with_math）
    entries: list[dict] = []
    for raw in data.get("blocks", []):
        if raw.get("type") != 0:  # 图片块不进文本层（图已在渲染页里）
            continue
        lines: list[dict] = []
        for ln in raw.get("lines", []):
            # 保留"只有空格"的 span（见 _line_text 说明），仅丢弃真正空的
            spans: list[dict] = []
            for sp in ln.get("spans", []):
                txt = sp.get("text", "")
                if txt == "":
                    continue
                spans.append(
                    {
                        "text": txt,
                        "bbox": [float(v) for v in sp.get("bbox", [0, 0, 0, 0])],
                        "size": float(sp.get("size", 0.0)),
                        "font": str(sp.get("font", "") or ""),
                        "flags": int(sp.get("flags", 0)),
                    }
                )
            if not spans:
                continue
            for sp in spans:
                sp["math"] = is_math_span(sp)  # 数学排版判定（始终算，供分类与占位符使用）
            runs: list[dict] = []
            txt = _line_text_with_math(spans, runs, counter) if inline_math else _line_text(spans)
            if not txt.strip():
                continue
            flags = 0
            for sp in spans:
                flags |= int(sp.get("flags", 0))
            line = {
                "text": txt,
                "bbox": [float(v) for v in ln["bbox"]],
                "size": max(float(sp.get("size", 0.0)) for sp in spans),
                "flags": flags,
                "spans": spans,
                "math_runs": runs,
                "chars": sum(len(sp["text"]) for sp in spans),
                "math_chars": sum(len(sp["text"]) for sp in spans if sp["math"]),
            }
            line["core"] = _row_core([line])
            lines.append(line)
        if not lines:
            continue
        entries.append({"bbox": [float(v) for v in raw["bbox"]], "lines": lines})

    blocks: list[dict] = []
    all_rows: list[dict] = []
    streams: list[list[dict]] = []
    for entry in _block_components(entries):
        streams.append(_stitch_rows([ln for e in entry for ln in e["lines"]]))
    # 先跨分量接续（PyMuPDF 常把一段拆成好几块），再统一分段；
    # 否则同一段会被切成几段分别翻译，排版错乱（踩过）。
    for rows in _merge_adjacent_streams(streams):
        all_rows.extend(rows)
        for group in _group_lines(rows):
            text = _merge_lines(group)
            if text:
                blocks.append(_make_block(group, text))
    return blocks, all_rows


def _make_block(group: list[dict], text: str) -> dict:
    chars = sum(g.get("chars", 0) for g in group) or 1
    math_chars = sum(g.get("math_chars", 0) for g in group)
    return {
        "text": text,
        "bbox": _union([g["bbox"] for g in group]),
        "size": round(max(g["size"] for g in group), 2),
        "line_count": len(group),
        "bold": bool(any(g["flags"] & 16 for g in group)),
        # 数学排版占比：>=0.6 直接判为公式块（不翻译），见 _classify
        "math_ratio": round(math_chars / chars, 3),
        "math_runs": [r for g in group for r in (g.get("math_runs") or [])],
    }


def _extract_raw_blocks(page: fitz.Page) -> list[dict]:
    """兼容入口：只取文本块，不关心文本行。"""
    return _extract_page(page)[0]


# ----------------------------------------------------------------------
# 表格识别：把表格拆成单元格，避免整张表被当成一段话翻译
# ----------------------------------------------------------------------
def _center_inside(bbox: list[float], rect: list[float], margin: float = 1.0) -> bool:
    cx = (bbox[0] + bbox[2]) / 2.0
    cy = (bbox[1] + bbox[3]) / 2.0
    return (
        rect[0] - margin <= cx <= rect[2] + margin
        and rect[1] - margin <= cy <= rect[3] + margin
    )


def find_tables(page: fitz.Page, min_cells: int = 2) -> list[dict]:
    """用 PyMuPDF 的表格检测拿到每个表格的单元格坐标。

    检测失败绝不能影响整页解析，因此所有异常都吞掉并退化为"没有表格"。
    """
    try:
        finder = page.find_tables()
    except Exception:  # noqa: BLE001
        return []
    tables: list[dict] = []
    for t in getattr(finder, "tables", []):
        cells = [[float(v) for v in c] for c in t.cells if c]
        if len(cells) < min_cells:  # 少于两个格子基本是误检
            continue
        tables.append({"bbox": [float(v) for v in t.bbox], "cells": cells})
    return tables


def _table_blocks(table: dict, all_lines: list[dict]) -> list[dict]:
    """把一张表拆成"每格一块"的文本块，每块各自带坐标。

    表格里一行横跨多个单元格，照 PyMuPDF 的块走会把整张表拼成一段话，
    送模型只会得到一坨。拆格后：译文能逐行落回原来的格子（开关打开时），
    或者整表按原样保留（默认，单元格块在页面载荷里被标为 hidden）。
    """
    grouped: list[dict] = []
    for rect in table["cells"]:
        inside = [ln for ln in all_lines if _center_inside(ln["bbox"], rect)]
        if not inside:
            continue
        for group in _group_lines(inside):
            text = _merge_lines(group)
            if text:
                grouped.append(_make_block(group, text))
    if not grouped:
        return []
    grouped = _row_major(grouped)  # 行优先
    for b in grouped:
        b["table"] = True
    return grouped


_VOWELS = set("aeiouyAEIOUY")
_WORDLIKE = re.compile(r"[A-Za-z]{3,}")


def has_english_word(text: str) -> bool:
    """文本里是否存在"像英文单词"的词。

    表格里大量是缩写与变量（``brt`` / ``bmt`` / ``ht`` / ``SNR`` / ``GHz``），
    看着像单词却几乎没有元音；真英文单词的元音占比通常不低于 0.15
    （如 ``Symbol`` 是 1/6）。用这一点把"符号格"和"说明格"分开，
    避免把变量表送去做无意义的翻译。
    """
    for word in _WORDLIKE.findall(text):
        vowels = sum(1 for ch in word if ch in _VOWELS)
        if vowels and vowels / len(word) >= 0.15:
            return True
    return False


def _classify_cell(block: dict) -> str:
    """表格单元格内片段的分类。

    表格里大部分格子是符号、缩写、数值（如 ``s ∈R3`` / ``T τ (L, W)``），
    送去翻译只会得到莫名其妙的输出；只有像样的词或成句的说明才值得翻译。
    """
    text = block["text"].strip()
    if not text:
        return "empty"
    if len(text) <= 4 and re.fullmatch(r"[0-9ivxlcdmIVXLCDM\-–—.\s]+", text):
        return "pageno"
    if block.get("math_ratio", 0.0) >= 0.6:
        return "formula"
    if looks_like_formula(text):
        return "formula"
    if not has_english_word(text):
        return "formula"
    core = [ch for ch in text if not ch.isspace()]
    if core and sum(1 for ch in core if ch.isascii() and ch.isalpha()) / len(core) < 0.5:
        return "formula"  # 以符号为主
    return "text"


def _row_major(items: list[dict]) -> list[dict]:
    """按「行优先」排序：先在垂直方向上分桶成行，再按 x 从左到右。

    直接用 y 排序会被几十分之一磅的差异打乱——同一行里不同列的格子顶部
    y 并不完全相等，结果就是从第 3 列读回第 2 列。
    """
    if not items:
        return items
    return sorted(
        items,
        key=lambda b: (round(b["bbox"][1] / TABLE_ROW_TOL), b["bbox"][0]),
    )


def _looks_like_table_block(block: dict, page_width: float, page_area: float) -> bool:
    """兜底：表格检测失效时，识别"横跨大半页 + 行数多 + 行距极紧"的块。

    正文行的行距通常在 9.5pt 以上，而表格行距只有 4–8pt，用这个差异区分，
    避免把单栏正文整块误判成表格（那样会整页不翻译）。
    """
    x0, y0, x1, y1 = block["bbox"]
    width, height = x1 - x0, y1 - y0
    if block.get("line_count", 0) < 14 or width <= 0 or height <= 0:
        return False
    if width < 0.55 * page_width or (width * height) < 0.18 * page_area:
        return False
    return (height / block["line_count"]) < 8.5


# ----------------------------------------------------------------------
# 保留区域：图片 / 表格（不翻译，右栏原样显示）
# ----------------------------------------------------------------------
def caption_kind_of(text: str) -> str | None:
    """判断一个块是不是图/表/算法的标题（caption）。

    返回 ``"table"`` / ``"figure"``，不是标题则返回 ``None``。
    要求编号后必须紧跟 ``:`` 或 ``.``，这样 "Fig. 3 shows the …" 这类
    正文引用不会被误判——它们后面跟的是空格 + 动词。
    """
    stripped = (text or "").strip()
    if len(stripped) > 900:
        return None
    m = _CAPTION_RE.match(stripped)
    if not m:
        return None
    return "table" if _CAPTION_TABLE_RE.match(stripped) else "figure"


def _area(box: list[float]) -> float:
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


def _rect_gap(a: list[float], b: list[float]) -> float:
    """两个矩形的最近距离（相交为 0）。"""
    dx = max(0.0, max(a[0], b[0]) - min(a[2], b[2]))
    dy = max(0.0, max(a[1], b[1]) - min(a[3], b[3]))
    return math.hypot(dx, dy)


def _overlap_ratio(a: list[float], b: list[float]) -> float:
    """交叠面积占较小矩形面积的比例（0~1）。"""
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    if w <= 0 or h <= 0:
        return 0.0
    return (w * h) / max(1e-6, min(_area(a), _area(b)))


def merge_boxes(boxes: list[list[float]], gap: float) -> list[list[float]]:
    """把间距小于 ``gap`` 的矩形合并成一个（用于把拼图的各个图块并成整张图）。"""
    pending = [list(b) for b in boxes]
    merged = True
    while merged:
        merged = False
        out: list[list[float]] = []
        for box in pending:
            for cur in out:
                if _rect_gap(box, cur) <= gap:
                    cur[0] = min(cur[0], box[0])
                    cur[1] = min(cur[1], box[1])
                    cur[2] = max(cur[2], box[2])
                    cur[3] = max(cur[3], box[3])
                    merged = True
                    break
            else:
                out.append(box)
        pending = out
    return pending


def _visual_boxes(page: fitz.Page) -> tuple[list[list[float]], list[list[float]]]:
    """取本页的「视觉元素」坐标：栅格图块 + 矢量绘图框。

    论文里的插图有两种画法：位图（xfig / matplotlib 导出的 PNG）和矢量图
    （PDF 内嵌矢量图形）。只认位图会漏掉一大类插图，因此两者都收：
    矢量部分过滤掉越界的裁切框、横跨整页的装饰线，以及过小的线段。
    """
    pr = page.rect
    page_area = pr.width * pr.height
    images: list[list[float]] = []
    try:
        for b in page.get_text("dict")["blocks"]:
            if b.get("type") != 1:
                continue
            box = [float(v) for v in b["bbox"]]
            if box[2] > box[0] and box[3] > box[1]:
                images.append(box)
    except Exception:  # noqa: BLE001
        pass

    drawings: list[list[float]] = []
    try:
        for d in page.get_drawings():
            r = d["rect"]
            if r.width < 15 or r.height < 10:
                continue
            if r.x0 < -2 or r.y0 < -2 or r.x1 > pr.width + 2 or r.y1 > pr.height + 2:
                continue  # 越界路径多为裁切框/背景板，会把整页吞进去
            if r.width * r.height > MAX_DRAW_RATIO * page_area or r.width * r.height < 250:
                continue
            drawings.append([r.x0, r.y0, r.x1, r.y1])
    except Exception:  # noqa: BLE001
        pass
    return images, drawings


def _x_overlap(a: list[float], b: list[float], need: float = 6.0) -> bool:
    return min(a[2], b[2]) - max(a[0], b[0]) >= need


def _is_real_table(table: dict, blocks: list[dict], regions: list[dict]) -> bool:
    """过滤表格检测的误报。

    ``find_tables`` 会把"排列整齐的图表坐标区"也判成表格（实测一篇论文的
    6 个图区全被判成表格）。真表格旁边一定有表题（``TABLE n:``），或者本身
    就落在保留区域（图区）里；两者都不满足的检测结果直接丢弃——否则它覆盖的
    正文会被当成"表格单元格"而永不翻译，属于静默丢内容。
    """
    t_box = table["bbox"]
    if any(b.get("caption_kind") == "table" and _rect_gap(t_box, b["bbox"]) <= 60 for b in blocks):
        return True
    return any(_overlap_ratio(t_box, r["bbox"]) >= 0.5 for r in regions)


def _table_row_count(box: list[float], lines: list[dict]) -> int:
    """块内「表格行」的条数：同一 y 上并排着 ≥``COL_BAND_MIN`` 个文本片段的那些行。

    解析器把每个**单元格**当成一条"行"（同一表格行的多个格子 y 相同、x 分开），
    因此"同一 y 上并排多个片段"正是表格行的指纹；正文一行只会有一个片段
    （行内公式的碎片也被缝进同一行里）。实测某对比表每个表格行并排 9 个片段，
    而同一页的所有正文块一律是 1 个——这是把「整张表被并成一个文本块」从正文里
    认出来的**唯一稳的判据**：那张表的行距 9.25pt 与正文 9.5pt 只差 0.25pt，
    文本长度又超过 ``TABLE_ROW_MAX_CHARS``，靠行距/长度都判不出来（踩过）。
    """
    bands: dict[int, int] = {}
    for ln in lines:
        if not _center_inside(ln["bbox"], box):
            continue
        k = int(round(ln["bbox"][1] / COL_BAND_PT))
        bands[k] = bands.get(k, 0) + 1
    return sum(1 for n in bands.values() if n >= COL_BAND_MIN)


def is_table_like_lines(box: list[float], lines: list[dict]) -> bool:
    """块内有足够多的「表格行」→ 判为表格内容（而不是正文段落）。"""
    return _table_row_count(box, lines) >= COL_MIN_ROWS


def _table_row_boxes(
    cap_box: list[float],
    blocks: list[dict],
    seed: list[list[float]],
    lines: list[dict],
) -> list[list[float]]:
    """无框线表格兜底：把标题下方「属于表格」的文本块聚成表格区域。

    booktabs 风格的表格常常没有完整框线，``find_tables`` 检测不出来（实测
    有一篇论文的两张对比表就是），这些表的每一行会被当成普通正文送去翻译，
    表格结构荡然无存。两条判据，任一命中即视为表格行：

    * **多列排布**（``is_table_like_lines``）：一行里有多个列分隔。这是主判据——
      整张表被 PyMuPDF 并成一个文本块时，文本会远超 ``TABLE_ROW_MAX_CHARS``、
      行距也可能和正文只差零点几磅，只有"多列"这一点是稳的（踩过）。
    * **行距紧 + 文本短**：兜住那些没有明显列分隔的表格行。

    一旦遇到两条都不满足的块（正文段落）就立刻停止，绝不把正文卷进来。
    """
    cur = _union([cap_box] + seed) if seed else list(cap_box)
    picked: list[list[float]] = []
    rows = sorted((b for b in blocks if not b.get("caption_kind")), key=lambda b: b["bbox"][1])
    for blk in rows:
        bb = list(blk["bbox"])
        if bb[1] < cap_box[1] - 2 or not _x_overlap(bb, cap_box):
            continue
        if bb[1] - cur[3] > TABLE_ROW_GAP:
            continue
        if is_table_like_lines(bb, lines):
            picked.append(bb)
            cur = _union([cur, bb])
            continue
        spacing = (bb[3] - bb[1]) / max(1, blk.get("line_count", 1))
        if spacing > TABLE_ROW_SPACING or len(blk.get("text", "")) > TABLE_ROW_MAX_CHARS:
            break
        picked.append(bb)
        cur = _union([cur, bb])
    return picked


def build_regions(
    page: fitz.Page,
    blocks: list[dict],
    tables: list[dict],
    lines: list[dict],
) -> list[dict]:
    """推导本页的「保留区域」：图片（含矢量插图）与表格。

    做法是"以 caption 为锚点"：论文的插图/表格几乎都带 ``Fig. N:`` / ``TABLE N:``
    标题，从标题出发把与之相邻（≤ VISUAL_CAPTION_GAP）的图块逐步吞并进来，
    直到再也接不上为止。相比"先把所有图块合并再找块"更稳——后者会把同页
    不相干的两张图、甚至图与正文粘成一大片，导致正文被误判为图而不翻译。

    两个关键细节（都是实测踩出来的）：

    * **矢量绘图不做预合并**。图表由成百上千条路径组成，提前按间距合并会把
      整页图纸连成一个 ``526x360`` 的大框（实测 p9），区域随之吞掉正文。
      只把每条路径当成独立的相邻元素，让"从 caption 逐跳生长"来控制范围。
    * **方向限制**。图题在图下方、表题在表上方，所以图题只向上生长、
      表题只向下生长，否则会越过标题把下一张图卷进来。

    返回 ``[{"type": "figure"|"table", "bbox": [...], "label": str}]``。
    """
    pr = page.rect
    page_area = pr.width * pr.height
    images, drawings = _visual_boxes(page)

    img_clusters = merge_boxes(images, VISUAL_IMAGE_GAP)
    pool = img_clusters + [list(d) for d in drawings]

    regions: list[dict] = []

    def add_region(box: list[float], rtype: str, label: str) -> None:
        box = [round(v, 2) for v in box]
        if _area(box) > MAX_REGION_RATIO * page_area:
            return
        for r in regions:
            if r["type"] == rtype and _overlap_ratio(box, r["bbox"]) > 0.55:
                r["bbox"] = [round(v, 2) for v in _union([r["bbox"], box])]
                return
        regions.append({"type": rtype, "bbox": box, "label": label[:120]})

    for blk in blocks:
        ctype = blk.get("caption_kind")
        if not ctype:
            continue
        cap_box = list(blk["bbox"])
        cluster: list[list[float]] = []

        if ctype == "table":
            # 表格优先用检测到的单元格范围（检测不到再靠行块拼）
            best, best_gap = None, VISUAL_CAPTION_GAP
            for t in tables:
                g = _rect_gap(cap_box, t["bbox"])
                if g <= best_gap:
                    best, best_gap = t, g
            if best is not None:
                cluster.append(list(best["bbox"]))
            # 无框线表格（booktabs 风格）常检测不出来：用「行距紧 / 多列排布 + 与标题同栏」补上
            cluster.extend(_table_row_boxes(cap_box, blocks, cluster, lines))
            # 表格内容都是文本，不再向图块生长——否则会把紧随其后的插图卷进来
            candidates: list[list[float]] = []
        else:
            # 图片标题在下、图片在上：不向标题以下扩展
            candidates = [b for b in pool if b[1] <= cap_box[3] + 12]

        cur = _union([cap_box] + cluster) if cluster else cap_box
        growing = True
        while growing:
            growing = False
            for box in candidates:
                if box in cluster:
                    continue
                if _rect_gap(cur, box) <= VISUAL_CAPTION_GAP:
                    cluster.append(box)
                    cur = _union([cur] + cluster)
                    growing = True
        add_region(cur, ctype, blk["text"])

    # 没有 caption 的独立插图（少数情况）：面积够大就自成一个保留区域
    for box in img_clusters:
        if _area(box) < 0.015 * page_area:
            continue
        cx = (box[0] + box[2]) / 2.0
        cy = (box[1] + box[3]) / 2.0
        if any(
            r["bbox"][0] <= cx <= r["bbox"][2] and r["bbox"][1] <= cy <= r["bbox"][3]
            for r in regions
        ):
            continue
        add_region(box, "figure", "")
    return regions


def absorb_region(
    block: dict,
    regions: list[dict],
    page_area: float,
    page_width: float = 0.0,
    lines: list[dict] | None = None,
) -> dict | None:
    """判断一个文本块是否落在某个保留区域内（落入即不翻译）。

    只吸收"标签型"内容：图里的 ``Power`` / ``Control``、刻度值、坐标轴标题、
    表格的每一行，以及图题本身。长段落（正文）与章节标题永不吸收——区域检测
    万一偏大，也不会让正文变成不翻译。

    表格行是这里最容易漏的一类：一行横跨多个单元格，PyMuPDF 会把同一列的
    多行拼成一个块（实测 lineCount 能到 28），因此判据不能只看行数，
    还要看行距——表格行距只有 1–2pt，正文行距 ≥9.5pt。

    **整张三线表被并成一个上千字的长块**时，长度上限（1200 字）与宽度上限
    （0.75 页宽，通栏表必超）会把它挡在区域外，表格正文于是被逐段翻译、
    排版全毁（踩过）。这类块有唯一稳的特征——「同一 y 上并排 ≥3 个片段」
    （:func:`is_table_like_lines`），命中就放行，不再看长短与宽度。
    """
    for r in regions:
        if block.get("caption_kind"):
            if _center_inside(block["bbox"], r["bbox"], margin=2.0):
                return r
            continue
        if _overlap_ratio(block["bbox"], r["bbox"]) < 0.75:
            continue
        text = block.get("text", "")
        # 章节标题不吸收（区域万一偏大也不能让正文变不翻译）。但"像标题"的判据只对
        # 1–2 行的块成立：图片里的编号任务清单（`1. Put the KeyChain in the Drawer 2. …`）
        # 会被 `_HEADING_RE` 误认成章节标题，于是落在图区域内却仍被送去翻译、
        # 用白底中文把整张图盖住（踩过，CoCoBench p5 的 Figure 2）。
        if _HEADING_RE.match(text) and int(block.get("line_count", 1) or 1) <= 2:
            continue
        n_lines = max(1, int(block.get("line_count", 1) or 1))
        spacing = (block["bbox"][3] - block["bbox"][1]) / n_lines
        width = block["bbox"][2] - block["bbox"][0]
        if spacing <= 7.5 and len(text) <= 1200 and (not page_width or width <= 0.75 * page_width):
            # 行距很紧 = 表格行 / 刻度簇 / 多行公式碎片，长度放宽
            # （表格一行可能横跨很多列；正文行距 ≥9.5pt，不会被卷进来）
            # 宽度限制用于排除"横跨整页的正文块"（PyMuPDF 偶尔把两栏并成一块，
            # 这种块的"行距"会假性偏小，实测在一篇论文的第 13 页出现过）
            return r
        if spacing <= 7.5 and lines is not None and is_table_like_lines(block["bbox"], lines):
            # 长/宽都超标，但确是多列排布的表格正文（整表一块）→ 放行
            return r
        if n_lines <= 2 and len(text) <= 200:
            # 单行/双行的短标签（图里的 Power / Control / Real State…）
            return r
        if len(text) <= 40:
            # 极短：多是公式碎片（分式被拆成上下几行）
            return r
    return None



# ----------------------------------------------------------------------
# 对外主流程
# ----------------------------------------------------------------------
def parse_pdf(
    pdf_path: Path,
    doc_dir: Path,
    zoom: float = 2.0,
    settings: Any | None = None,
    progress=None,
) -> dict:
    """解析整份 PDF，返回文档元信息（含每页块结构），并写出渲染图与页面 JSON。"""
    pdf_path = Path(pdf_path)
    doc_dir = Path(doc_dir)
    pages_dir = doc_dir / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)

    out_pages: list[dict] = []
    t0 = time.time()
    in_references = False  # 一旦遇到 References 标题，后面的块都视为参考文献
    inline_math = bool(getattr(settings, "inline_math_image", True))
    with fitz.open(pdf_path) as doc:
        page_count = doc.page_count
        meta = {
            "title": (doc.metadata or {}).get("title") or "",
            "author": (doc.metadata or {}).get("author") or "",
            "page_count": page_count,
        }
        for idx in range(page_count):
            page_no = idx + 1
            page = doc[idx]
            rect = page.rect
            img_name = f"p{page_no:04d}.png"
            img_path = pages_dir / img_name
            if not img_path.exists():
                pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
                pix.save(str(img_path))

            raw_blocks, all_lines = _extract_page(page, inline_math)
            tables = find_tables(page)
            table_boxes = [t["bbox"] for t in tables]
            page_area = rect.width * rect.height

            # 1) 分类 + 标出图/表标题（caption）
            for b in raw_blocks:
                b["kind"] = _classify(b, rect.width)
                b["caption_kind"] = caption_kind_of(b["text"])

            # 2) 推导「保留区域」（图片 / 表格）。落在区域内的文字随之不翻译，
            #    否则译出来的中文会盖在图上（图里的 Power / Control 等标签）。
            regions = build_regions(page, raw_blocks, tables, all_lines)
            for i, r in enumerate(regions):
                r["index"] = i

            def region_of(box: list[float]) -> dict | None:
                best, best_ov = None, 0.0
                for r in regions:
                    ov = _overlap_ratio(box, r["bbox"])
                    if ov > best_ov:
                        best, best_ov = r, ov
                return best if best_ov >= 0.5 else None

            def finalize(b: dict) -> dict:
                """补齐一个「普通文本块」的分类与保留区域归属。"""
                b.setdefault("kind", _classify(b, rect.width))
                b.setdefault("caption_kind", caption_kind_of(b["text"]))
                if b["kind"] == "text" and _looks_like_table_block(b, rect.width, page_area):
                    b["kind"] = "table"  # 检测失效时的兜底
                region = absorb_region(b, regions, page_area, rect.width, all_lines)
                b["region_index"] = region["index"] if region else None
                b["region_type"] = region["type"] if region else None
                if b["caption_kind"]:
                    b["kind"] = "caption"
                elif region and b["kind"] == "text":
                    b["kind"] = region["type"]  # 图内文字标签 → figure；表格内容 → table
                # 表格区域内的块 = 表格内容：打上 table 标记，默认不翻译、右栏直接透出原表
                # （否则整张表会被拼成一段话送去翻译，右栏变成一团连续文字——踩过）。
                # **图/表标题要排除**：标题有自己的开关（「翻译图表标题」），不能一并锁死。
                if not b["caption_kind"] and (
                    b["kind"] == "table" or (region and region["type"] == "table")
                ):
                    b["table"] = True
                return b

            # 表格区域内的原始文本块交给表格逻辑重新分组，
            # 否则整张表会被拼成一段话（表格一行的多个单元格会被连起来）
            outside: list[dict] = []
            for b in raw_blocks:
                if any(_center_inside(b["bbox"], tb) for tb in table_boxes):
                    continue
                outside.append(finalize(b))

            # 每张表用一个占位项参与阅读顺序排序，展开后仍是完整的一块
            table_items: list[dict] = []
            table_cells: dict[int, list[dict]] = {}
            for i, t in enumerate(tables):
                cells = _table_blocks(t, all_lines)
                if not cells or not _is_real_table(t, raw_blocks, regions):
                    # 误检（图表坐标区常被判成表格）直接忽略
                    continue
                reg = region_of(list(t["bbox"]))
                kept: list[dict] = []
                for c in cells:
                    if len(c["text"]) > TABLE_CELL_MAX_CHARS:
                        # 长句不是表格内容，通常是误检把正文圈了进来：
                        # 放回普通块照常翻译，绝不能让它静默不译。
                        c.pop("table", None)  # 摘掉表格标记，否则仍会被判为不可译
                        outside.append(finalize(c))
                        continue
                    c["kind"] = _classify_cell(c)
                    c["region_index"] = reg["index"] if reg else None
                    c["region_type"] = reg["type"] if reg else None
                    kept.append(c)
                if not kept:
                    continue
                table_cells[i] = kept
                table_items.append(
                    {
                        "text": "",
                        "bbox": list(t["bbox"]),
                        "size": 0.0,
                        "line_count": 0,
                        "bold": False,
                        "_table_index": i,
                    }
                )

            ordered: list[dict] = []
            for item in reading_order(outside + table_items):
                idx = item.get("_table_index")
                if idx is None:
                    ordered.append(item)
                else:
                    ordered.extend(table_cells.get(idx, []))

            blocks: list[dict] = []
            for order, b in enumerate(ordered):
                kind = b["kind"]
                # 参考文献按「章节」识别：正文里含 [4]–[6] 这类引用不是参考文献，
                # 但 References 标题之后的所有块都是
                if not in_references and _REF_SECTION.fullmatch(b["text"].strip()):
                    in_references = True
                elif in_references and kind != "caption" and not b.get("table"):
                    # 图表题注（`Table 9: …`）与表格内容永远不算参考文献：有的论文
                    # References 之后还跟着 Appendix，`in_references` 会一直延续下去，
                    # 把附录里的表题一并标成 reference → 题注不再翻译（踩过 4 处）
                    kind = "reference"
                block = {
                    "id": f"p{page_no}-b{order}",
                    "order": order,
                    "kind": kind,
                    "bbox": [round(v, 2) for v in b["bbox"]],
                    "text": b["text"],
                    "fontSize": b["size"],
                    "lineCount": b["line_count"],
                    "bold": b["bold"],
                    "table": bool(b.get("table", False)),  # 是否来自表格（供前端与排查用）
                    "caption": bool(b.get("caption_kind")),
                    "mathRatio": b.get("math_ratio", 0.0),
                    # 行内公式小块：正文译文里会在占位符处贴回原图（坐标为 PDF pt）
                    "math": [
                        {"key": r["key"], "bbox": [round(v, 2) for v in r["bbox"]]}
                        for r in (b.get("math_runs") or [])
                    ],
                    "regionIndex": b.get("region_index"),
                    "regionType": b.get("region_type"),
                    # 注意用的是覆盖后的 kind：References 区间内的块要按 reference 判定
                    "translatable": block_is_translatable({**b, "kind": kind}, settings),
                }
                blocks.append(block)

            page_info = {
                "pageNo": page_no,
                "width": round(rect.width, 2),
                "height": round(rect.height, 2),
                "rotation": page.rotation,
                "image": f"docs/{doc_dir.name}/pages/{img_name}",
                "imageWidth": round(rect.width * zoom, 2),
                "imageHeight": round(rect.height * zoom, 2),
                "regions": regions,
                "blocks": blocks,
            }
            (doc_dir / f"page-{page_no:04d}.json").write_text(
                json.dumps(page_info, ensure_ascii=False), encoding="utf-8"
            )
            out_pages.append(
                {
                    "pageNo": page_no,
                    "width": page_info["width"],
                    "height": page_info["height"],
                    "image": page_info["image"],
                    "blockCount": len(blocks),
                    "translatableCount": sum(1 for b in blocks if b["translatable"]),
                    "regionCount": len(regions),
                }
            )
            if progress:
                progress(page_no, page_count)

    info = {
        "pageCount": page_count,
        "pages": out_pages,
        "renderZoom": zoom,
        "pdfMeta": meta,
        "parseSeconds": round(time.time() - t0, 2),
    }
    (doc_dir / "meta.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    return info


def is_translatable_kind(kind: str, settings: Any | None) -> bool:
    """按「块类型 + 当前配置」判断该块要不要翻译。

    刻意做成**运行期**判断：这样在「⚙ 设置」里切换「翻译表格内容 / 翻译图表标题 /
    跳过参考文献 / 翻译公式块」之后立刻生效，不必重新解析 PDF。
    只有 RENDER_ZOOM 是写死在渲染图里的，改它才必须重新解析。
    """
    if kind == "text":
        return True
    if kind == "formula":
        return bool(getattr(settings, "translate_formula_blocks", False))
    if kind == "reference":
        return not bool(getattr(settings, "skip_reference_blocks", True))
    if kind == "caption":
        return bool(getattr(settings, "translate_captions", False))
    # figure = 图片区域内的文字标签；table = 整张表保留。两者都不翻译。
    return False


def block_is_translatable(block: dict, settings: Any | None) -> bool:
    """块的最终可译判定：表格单元格还要看「翻译表格内容」开关。"""
    if block.get("table") and not bool(getattr(settings, "translate_tables", False)):
        return False
    return is_translatable_kind(block.get("kind", "text"), settings)


def _is_translatable(block: dict, settings: Any | None, kind: str | None = None) -> bool:
    return block_is_translatable(block if kind is None else {**block, "kind": kind}, settings)


def load_meta(doc_dir: Path) -> dict:
    return json.loads((Path(doc_dir) / "meta.json").read_text(encoding="utf-8"))


def load_page(doc_dir: Path, page_no: int) -> dict:
    return json.loads((Path(doc_dir) / f"page-{page_no:04d}.json").read_text(encoding="utf-8"))


def draw_debug_overlay(doc_dir: Path, page_no: int, out_path: Path) -> Path:
    """在渲染图上画出「保留区域 + 块框 + 阅读顺序编号」，用于人工校验。

    颜色约定：
    * 紫/绿虚线大框 = 保留区域（紫=图片，绿=表格）；这些区域及其内部文字不翻译；
    * 红框 = 参与翻译的文本块；
    * 蓝框 = 按配置保留原文的块（公式 / 参考文献 / 图内标签 / 整表）。
    """
    page_info = load_page(doc_dir, page_no)
    zoom = (page_info["imageWidth"] / page_info["width"]) if page_info.get("width") else 1.0
    image_path = Path(doc_dir) / "pages" / f"p{page_no:04d}.png"

    out_doc = fitz.open()
    pg = out_doc.new_page(width=page_info["width"] * zoom, height=page_info["height"] * zoom)
    pg.insert_image(pg.rect, filename=str(image_path))

    for r in page_info.get("regions", []):
        x0, y0, x1, y1 = [v * zoom for v in r["bbox"]]
        color = (0.05, 0.55, 0.25) if r["type"] == "table" else (0.55, 0.1, 0.7)
        rect = fitz.Rect(x0, y0, x1, y1)
        pg.draw_rect(rect, color=color, width=2.2, dashes="[5 4] 0")
        pg.insert_text(
            fitz.Point(x0 + 3, max(11.0, y0 - 4)),
            f"{'TABLE' if r['type'] == 'table' else 'FIGURE'} · 原样保留",
            fontsize=9,
            color=color,
        )

    for b in page_info["blocks"]:
        x0, y0, x1, y1 = [v * zoom for v in b["bbox"]]
        color = (0.85, 0.1, 0.1) if b["translatable"] else (0.1, 0.35, 0.85)
        pg.draw_rect(fitz.Rect(x0, y0, x1, y1), color=color, width=1.0)
        pg.insert_text(
            fitz.Point(x0 + 2, max(9.0, y0 + 9)),
            str(b["order"]),
            fontsize=9,
            color=color,
        )
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pg.get_pixmap(matrix=fitz.Matrix(1, 1), alpha=False).save(str(out_path))
    out_doc.close()
    return out_path
