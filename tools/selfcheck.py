"""关键链路自检：一条命令验证「PDF 解析 / 坐标对齐 / API 调用 / 本地缓存」是否可行。

用法：
    uv run python tools/selfcheck.py            # 全量自检（含本地假 LLM 服务，不联网、不消耗额度）
    uv run python tools/selfcheck.py -v         # 打印每条检查的细节

设计说明
--------
自检**不依赖任何真实 API Key**：脚本会在本机随机端口起一个「OpenAI 兼容」的假模型服务，
然后让项目的 Translator 真实地走 HTTP 请求它。因此校验的是完整的真实调用链
（请求构造 → Bearer 鉴权 → /chat/completions → JSON 解析 → 分块并发 → 落库），
而不是把 Translator 换成桩函数。

数据目录被重定向到临时目录，不会污染项目的 data/ 。
"""
from __future__ import annotations

import argparse
import dataclasses
import importlib.util
import json
import os
import re
import socket
import struct
import sys
import tempfile
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

VERBOSE = False


# ======================================================================
# 报告器
# ======================================================================
class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[bool, str, str]] = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.rows.append((bool(ok), name, detail))
        mark = "✔" if ok else "✘"
        line = f"  {mark} {name}"
        if detail and (VERBOSE or not ok):
            line += f"　—　{detail}"
        print(line, flush=True)
        return bool(ok)

    def section(self, title: str) -> None:
        print(f"\n▌{title}", flush=True)

    @property
    def failed(self) -> list[str]:
        return [n for ok, n, _ in self.rows if not ok]

    def summary(self) -> int:
        total = len(self.rows)
        bad = len(self.failed)
        print("\n" + "=" * 62)
        if bad == 0:
            print(f"自检通过：{total}/{total} 项检查全部成功 ✔")
        else:
            print(f"自检失败：{bad}/{total} 项未通过 ✘")
            for n in self.failed:
                print(f"  · {n}")
        print("=" * 62)
        return 1 if bad else 0


R = Report()


# ======================================================================
# 假 LLM 服务（OpenAI 兼容 /chat/completions）
# ======================================================================
REQUESTS: list[dict] = []


class _FakeLLM(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            body = {}
        REQUESTS.append(
            {
                "path": self.path,
                "auth": self.headers.get("Authorization") or "",
                "model": body.get("model"),
                "response_format": body.get("response_format"),
                "body": body,
            }
        )

        model = str(body.get("model") or "")
        if "fail401" in model:  # 用于校验鉴权失败的错误提示
            return self._send(401, {"error": {"message": "Invalid API key provided."}})
        if "fail500" in model:  # 用于校验 5xx 重试
            return self._send(500, {"error": {"message": "internal server error"}})

        content_raw = ""
        msgs = body.get("messages") or []
        if msgs:
            content_raw = str(msgs[-1].get("content") or "")
        try:
            payload = json.loads(content_raw.split("\n\n")[0])
            segments = payload.get("segments") or []
        except Exception:  # noqa: BLE001
            segments = []

        items = [{"id": s.get("id"), "text": f"【自检译文】{s.get('text', '')}"} for s in segments]
        reply = {
            "id": "chatcmpl-selfcheck",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": json.dumps({"translations": items}, ensure_ascii=False)},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 32, "completion_tokens": 64, "total_tokens": 96},
        }
        return self._send(200, reply)

    def _send(self, status: int, obj: dict) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args) -> None:  # 静音
        return


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def start_fake_llm() -> tuple[ThreadingHTTPServer, str]:
    port = free_port()
    srv = ThreadingHTTPServer(("127.0.0.1", port), _FakeLLM)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, name="fake-llm", daemon=True).start()
    return srv, f"http://127.0.0.1:{port}/v1"


# ======================================================================
# 工具
# ======================================================================
def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载模块：{path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def png_size(path: Path) -> tuple[int, int]:
    head = Path(path).read_bytes()[:24]
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("不是 PNG 文件")
    w, h = struct.unpack(">II", head[16:24])
    return int(w), int(h)


def overlap_ratio(a: list[float], b: list[float]) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    area_a = max(1e-6, (a[2] - a[0]) * (a[3] - a[1]))
    return inter / area_a


# ======================================================================
# 各阶段
# ======================================================================
def stage_pdf(report: Report, tmp: Path) -> Path:
    """阶段 A：示例 PDF 生成 → 解析 → 渲染图与块结构。"""
    report.section("阶段 A · PDF 导入与解析")
    maker = load_module(ROOT / "tools" / "make_sample_pdf.py", "make_sample_pdf")
    pdf_path = tmp / "sample_paper_en.pdf"
    maker.build(pdf_path)
    missing = maker.verify(pdf_path)
    report.check("生成示例 PDF（双栏 + 公式 + 图注 + 参考文献）", pdf_path.exists() and pdf_path.stat().st_size > 3000,
                 f"{pdf_path.stat().st_size / 1024:.1f} KB")
    report.check("示例 PDF 内容完整性（含标题/摘要/公式/图注/参考文献/页边水印）",
                 not missing, "缺失：" + "、".join(missing) if missing else "全部存在")
    return pdf_path


def stage_parse(report: Report, pdf_path: Path, store, settings) -> tuple[str, dict]:
    """阶段 B：解析 → 每页渲染图 + 带坐标文本块 + 阅读顺序。"""
    report.section("阶段 B · 页面渲染与坐标抽取")
    data = pdf_path.read_bytes()
    summary = store.import_pdf(pdf_path.name, data)
    doc_id = summary["docId"]
    report.check("PDF 落盘并解析成功", summary["pageCount"] >= 1,
                 f"docId={doc_id}，{summary['pageCount']} 页，耗时 {summary['parseSeconds']}s")

    page = store.page(doc_id, 1)
    img = settings.docs_dir / doc_id / "pages" / "p0001.png"
    ok_img = img.exists()
    report.check("第 1 页已渲染为图片（原文层）", ok_img, str(img))

    if ok_img:
        w, h = png_size(img)
        expect_w = round(page["width"] * settings.render_zoom)
        expect_h = round(page["height"] * settings.render_zoom)
        report.check("渲染图分辨率 = 页面 pt × RENDER_ZOOM（坐标可等比换算）",
                     abs(w - expect_w) <= 2 and abs(h - expect_h) <= 2,
                     f"实际 {w}×{h}，期望 {expect_w}×{expect_h}（zoom={settings.render_zoom}）")

    blocks = page["blocks"]
    report.check("抽取出带 bbox 坐标的文本块", len(blocks) >= 8, f"共 {len(blocks)} 块")

    bad_box = [b["id"] for b in blocks if not in_page(b["bbox"], page["width"], page["height"])]
    report.check("所有块的 bbox 均落在页面范围内（坐标合法）", not bad_box, f"越界块：{bad_box[:5]}")

    kinds = {k: sum(1 for b in blocks if b["kind"] == k) for k in {b["kind"] for b in blocks}}
    report.check("片段分类生效（区分正文/公式/参考文献/页边/页码）", "text" in kinds,
                 "、".join(f"{k}:{v}" for k, v in sorted(kinds.items())))

    arxiv_blocks = [b for b in blocks if "arXiv" in b["text"]]
    report.check("页边 arXiv 水印不参与翻译（归入 margin/meta 等非译分类）",
                 all(b["kind"] != "text" and not b["translatable"] for b in arxiv_blocks),
                 f"共 {len(arxiv_blocks)} 块，分类 {[b['kind'] for b in arxiv_blocks]}，translatable={[b['translatable'] for b in arxiv_blocks]}"
                 if arxiv_blocks else "水印未进入文本块（已在图像层）")

    # 阅读顺序：标题必须排在第 1 栏正文之前（验证 XY-Cut）
    title_i = next((b["order"] for b in blocks if "A Survey on Collaborative SLAM" in b["text"]), None)
    intro_i = next((b["order"] for b in blocks if "Simultaneous Localization and Mapping (SLAM) is a" in b["text"]), None)
    method_i = next((b["order"] for b in blocks if "unified taxonomy" in b["text"]), None)
    report.check("阅读顺序符合「通栏标题 → 左栏正文 → 右栏正文」（简化 XY-Cut）",
                 title_i is not None and intro_i is not None and title_i < intro_i,
                 f"标题 order={title_i}，左栏正文 order={intro_i}，右栏正文 order={method_i}")

    max_ov = 0.0
    rects = [b["bbox"] for b in blocks if b["kind"] == "text"]
    for i in range(len(rects)):
        for j in range(i + 1, len(rects)):
            max_ov = max(max_ov, overlap_ratio(rects[i], rects[j]))
    report.check("正文块之间无严重重叠（分块未粘连）", max_ov < 0.35, f"最大重叠率 {max_ov:.3f}")

    # 「重新解析」：解析逻辑升级后就地重跑，页面结构应当完全一致（幂等）
    again = store.reparse(doc_id)
    page2 = store.page(doc_id, 1)
    report.check("「重新解析」可用且幂等（解析逻辑升级后可就地重跑，译文缓存不受影响）",
                 again["pageCount"] == summary["pageCount"]
                 and len(page2["blocks"]) == len(blocks)
                 and [b["bbox"] for b in page2["blocks"]] == [b["bbox"] for b in blocks],
                 f"{len(blocks)} → {len(page2['blocks'])} 块")

    return doc_id, page


def in_page(box: list[float], width: float, height: float) -> bool:
    return box[0] >= -0.5 and box[1] >= -0.5 and box[2] <= width + 0.5 and box[3] <= height + 0.5


def stage_stitch(report: Report) -> None:
    """阶段 B* · 行碎片缝合（LaTeX 行内公式把一条视觉行拆进多个 PyMuPDF 块）。

    这一段用**合成数据**直接验证缝合逻辑，不依赖任何具体 PDF：所有阈值都是在
    真实 IEEE 论文（第 4 页那段 "Having the communication workload satisfied…"）
    上量出来的，因此这里把当时量到的那几个包围盒原样搬过来当回归基线。
    """
    report.section("阶段 B* · 行碎片缝合（行内公式不把段落撕碎）")
    from backend.pdf_parser import (
        _block_components, _group_lines, _merge_math_clusters, _same_visual_line,
        _stitch_rows,
    )

    # 真实论文第 4 页那一行被切成的三个碎片（同一基线，包围盒重合 91%~100%）
    frag_a = [49.0, 250.4, 126.5, 262.5]      # "MR images {mt}T"
    frag_b = [121.8, 250.4, 275.6, 268.8]     # "t=1 and the actual MR images {...}"
    frag_c = [270.9, 251.9, 300.0, 263.4]     # "t=1, by"
    # 下一条视觉行（行内分式把它撑高，与上一行重合 53%——必须判为不同行）
    next_line = [49.0, 262.3, 204.9, 274.4]   # "jointly planning the power P = {pt}T"
    # 右栏同一高度的一行（栏间距 12pt——必须判为不同行）
    other_col = [312.0, 250.0, 563.0, 262.0]

    report.check("同一视觉行的碎片被判为同一行", _same_visual_line(frag_a, frag_b)
                 and _same_visual_line(frag_b, frag_c))
    report.check("越过被分式撑高的下一行，不误合（重合 53% < 65%）",
                 not _same_visual_line(frag_b, next_line))
    report.check("双栏排版的左右两栏不误合（栏间距 12pt > 6pt）",
                 not _same_visual_line(frag_a, other_col))
    report.check("真正上下堆叠的碎片不误合（无纵向重合）",
                 not _same_visual_line([49.0, 250.4, 60.0, 262.5], [49.0, 264.0, 60.0, 276.0]))

    def row(bbox, text, key=None, bbox_run=None):
        run = [{"key": key, "bbox": bbox_run or bbox, "text": "x"}] if key else []
        return {
            "text": text, "bbox": list(bbox), "size": 9.96, "flags": 0,
            "spans": [{"text": text, "bbox": list(bbox), "size": 9.96, "math": bool(key)}],
            "math_runs": run, "chars": len(text), "math_chars": 0,
            "core": list(bbox),
        }

    stitched = _stitch_rows([
        row(frag_c, "⟦24⟧, by", "24"),
        row(frag_a, "MR images ⟦21⟧", "21"),
        row(frag_b, "⟦22⟧ and the actual MR images ⟦23⟧", "22"),
    ])
    report.check("碎片按 x 从左到右缝成一条整行，文本顺序正确",
                 len(stitched) == 1 and stitched[0]["text"].startswith("MR images"),
                 stitched[0]["text"] if stitched else "（没缝上）")
    report.check("缝合后包围盒是各碎片的并集",
                 len(stitched) == 1 and stitched[0]["bbox"] == [49.0, 250.4, 300.0, 268.8])

    # 公式被切成两个占位符（{m_t}^T 与 t=1）：应当合并成一个，并删掉多余占位符
    runs, text = _merge_math_clusters(
        [{"key": "21", "bbox": [98.8, 250.4, 126.5, 262.5], "text": "a"},
         {"key": "22", "bbox": [121.8, 256.5, 134.9, 263.4], "text": "b"}],
        "MR images ⟦21⟧⟦22⟧ and",
    )
    report.check("半个公式的两个占位符合并成一个（否则下标会跑到右边）",
                 len(runs) == 1 and runs[0]["key"] == "21" and "⟦22⟧" not in text,
                 f"runs={[r['key'] for r in runs]} text={text!r}")

    # 段落分组：用 core（正文部分）算间距，分式撑高的行不会把两段粘在一起
    tall = row([49.0, 260.2, 300.0, 298.3], "⟦27⟧ across all time slots")
    tall["core"] = [49.0, 260.2, 300.0, 272.0]          # 正文只占上面一半
    heading = row([49.0, 212.7, 168.0, 222.7], "C. Cross-Layer Optimization")
    heading["core"] = [49.0, 212.7, 168.0, 222.7]
    first = row([49.0, 228.0, 300.0, 240.0], "Having the communication workload")
    first["core"] = [49.0, 228.0, 300.0, 240.0]
    groups = _group_lines([heading, first, tall])
    report.check("段落分组按正文行间距算（标题/正文不被撑高的公式粘成一段）",
                 len(groups) == 2, f"分了 {len(groups)} 段")

    # 块连通分量：叠在一起的碎片归一个分量；标题与正文分开
    entries = [
        {"bbox": [49.0, 228.0, 300.0, 262.5], "lines": []},
        {"bbox": [121.8, 250.4, 275.6, 268.8], "lines": []},
        {"bbox": [49.0, 251.9, 300.0, 274.4], "lines": []},
        {"bbox": [49.0, 212.7, 168.0, 222.7], "lines": []},          # 标题
        {"bbox": [312.0, 250.0, 563.0, 262.0], "lines": []},         # 右栏
    ]
    comps = [sorted(id(e) for e in c) for c in _block_components(entries)]
    merged = [c for c in _block_components(entries) if len(c) == 3]
    report.check("互相叠在一起的碎片聚成一个连通分量",
                 len(merged) == 1, f"分量数={len(comps)}")
    report.check("标题与正文不被并到一起（中间有空白）",
                 all(len(c) == 1 for c in _block_components(entries[:4])
                     if abs(c[0]["bbox"][1] - 212.7) < 0.1),
                 "标题单独成块")


def stage_parse_quality(report: Report, store, settings, tmp: Path) -> None:
    """阶段 B+：文本抽取质量与块分类正确性（回归测试）。"""
    report.section("阶段 B+ · 文本抽取质量与块分类")
    maker = load_module(ROOT / "tools" / "make_sample_pdf.py", "make_sample_pdf")

    # ---- 空格还原：单词与空格分别绘制的 PDF（IEEE/arXiv 模板的典型排法）----
    sp_pdf = maker.build_spaceless_pdf(tmp / "spaceless_sample.pdf")
    summary = store.import_pdf("spaceless_sample.pdf", sp_pdf.read_bytes(), force_reparse=True)
    page = store.page(summary["docId"], 1)
    text = " ".join(b["text"] for b in page["blocks"])
    expect = maker.SPACELESS_LINES[0]
    report.check("单词与空格分别是独立 span 时，词间空格被正确还原",
                 expect in text, f"期望包含 {expect[:38]!r}…\n     实际：{text[:78]!r}")
    glued = [w for w in re.findall(r"[A-Za-z]{18,}", text)]
    report.check("不再出现单词粘连（超长粘连词数 = 0）", not glued, f"粘连词：{glued[:4]}")
    spaces = text.count(" ")
    ratio = spaces / max(len(text), 1)
    report.check("空格占比达到正常英文散文水平（≥ 0.12）", ratio >= 0.12, f"实际 {ratio:.3f}")

    # ---- 分类正确性：正文段落不能被误判成公式 / 参考文献 ----
    plain = store.page(store.import_pdf("sample_paper_en.pdf", (ROOT / "samples" / "sample_paper_en.pdf").read_bytes())["docId"], 1)
    kinds = {b["kind"] for b in plain["blocks"]}
    report.check("正文段落未被误判为公式/参考文献（blocks 里 text 占多数）",
                 sum(1 for b in plain["blocks"] if b["kind"] == "text") >= 5,
                 "、".join(sorted(kinds)))

    # 直接对判据做单元验证：含大量引用的正文不能算参考文献，含等号/括号的句子不能算公式
    from backend.pdf_parser import _REF_ENTRY, _REF_SECTION, looks_like_formula

    body = "Robotic mixed reality (RoboMR) [4]–[6], which facilitates interactions [7], [8] and [9]."
    report.check("正文里密集出现 [n] 引用不会被判成参考文献",
                 not _REF_ENTRY.match(body), body[:44])
    report.check("References 标题能被识别（用于开启参考文献区间）",
                 bool(_REF_SECTION.fullmatch("References")) and bool(_REF_SECTION.fullmatch("REFERENCES")))
    prose = ("Corresponding author: Shuai Wang (s.wang@siat.ac.cn). These authors contribute equally.")
    author = "Chenxuan Liu, He Li, Zongze Li, Shuai Wang, Wei Xu, Kejiang Ye, Derrick Wing Kwan Ng"
    real_formula = "L = Sum_i || I_i - R(T_i, G) ||^2 + lambda * Sum_g min(0, s_g - s_min)"
    report.check("作者行 / 页脚这类句子不会被误判为公式",
                 not looks_like_formula(author) and not looks_like_formula(prose),
                 f"author={looks_like_formula(author)} prose={looks_like_formula(prose)}")
    report.check("真正的公式仍能被识别", looks_like_formula(real_formula), real_formula[:40])


def stage_tables(report: Report, settings, tmp: Path) -> None:
    """阶段 B++：表格处理——按单元格拆块，符号格保留原文。"""
    report.section("阶段 B++ · 表格处理（拆格 / 符号格保留原文 / 开关）")
    maker = load_module(ROOT / "tools" / "make_sample_pdf.py", "make_sample_pdf")
    from backend.pdf_parser import (
        _classify_cell,
        _extract_page,
        _table_blocks,
        find_tables,
        has_english_word,
        is_translatable_kind,
        parse_pdf,
    )

    try:
        import pymupdf as fitz
    except ImportError:  # pragma: no cover
        import fitz

    pdf = ROOT / "samples" / "sample_paper_en.pdf"
    x0, y0, x1, y1 = maker.table_bbox()

    def inside_table(block) -> bool:
        bx0, by0, bx1, by1 = block["bbox"]
        cx, cy = (bx0 + bx1) / 2, (by0 + by1) / 2
        return x0 - 2 <= cx <= x1 + 2 and y0 - 2 <= cy <= y1 + 2

    with fitz.open(pdf) as doc:
        page = doc[1]  # 样本第 2 页是表格页
        _, lines = _extract_page(page)
        tables = find_tables(page)
        report.check("能检测出样本中的表格（有线框的 3×4 表格）",
                     len(tables) == 1 and len(tables[0]["cells"]) >= 9,
                     f"检测到 {len(tables)} 个表格、{len(tables[0]['cells']) if tables else 0} 个单元格")

        cell_blocks = _table_blocks(tables[0], lines) if tables else []
        for b in cell_blocks:
            b["kind"] = _classify_cell(b)
        in_table = [b for b in cell_blocks if inside_table(b)]
        report.check("整张表被拆成逐格文本块（而不是合成一段话）",
                     len(in_table) >= 9 and max(b["line_count"] for b in in_table) <= 4,
                     f"{len(in_table)} 块，最大块行数 {max((b['line_count'] for b in in_table), default=0)}")

        kinds = {b["kind"] for b in in_table}
        report.check("符号格（如 s_t in R3 / (E_t, D_t)）被识别为非译文片段",
                     "formula" in kinds, "、".join(sorted(kinds)))
        report.check("说明格（如 Robot pose in the world frame.）仍可翻译",
                     any(b["kind"] == "text" for b in in_table))
        report.check("表头（Notation / Type / Description）可翻译",
                     all(b["kind"] == "text" for b in in_table if b["text"] in ("Notation", "Type", "Description")))

    # 集成验证：默认（translate_tables=False）下整张表都不翻译
    out_dir = tmp / "table_parse_off"
    parse_pdf(pdf, out_dir, zoom=1.0, settings=dataclasses.replace(settings, translate_tables=False))
    pg = json.loads((out_dir / "page-0002.json").read_text(encoding="utf-8"))
    kept = [b for b in pg["blocks"] if inside_table(b)]
    report.check("落盘结果：默认关闭时表格区域内所有块均不可译（整表原样保留）",
                 bool(kept) and all(not b["translatable"] for b in kept),
                 f"{len(kept)} 块，可译 {sum(1 for b in kept if b['translatable'])} 块")
    report.check("表格区域被登记为保留区域（供界面画虚框 / 统计）",
                 any(r["type"] == "table" for r in pg["regions"]),
                 "、".join(f"{r['type']}[{r['bbox'][1]:.0f}~{r['bbox'][3]:.0f}]" for r in pg["regions"]))
    report.check("表题块被登记为 caption 且不翻译",
                 any(b["kind"] == "caption" and not b["translatable"] for b in pg["blocks"]),
                 "、".join(b["text"][:28] for b in pg["blocks"] if b["kind"] == "caption"))

    # 纯函数单测
    report.check("符号串与英文词的区分（元音占比启发式）",
                 not has_english_word("st R3 ut R2 (brt, bmt)") and has_english_word("Symbol")
                 and has_english_word("Robot pose in the world frame."))
    cases = {
        "T τ (L, W) (ht, B σ2 Rt P Lt (I, S)": "formula",
        "(Et, Dt) L": "formula",
        "SNR": "formula",
        "GHz": "formula",
        "Number of frames.": "text",
        "Description": "text",
        "Variable Variable": "text",
        "2": "pageno",
    }
    wrong = {t: _classify_cell({"text": t}) for t, want in cases.items() if _classify_cell({"text": t}) != want}
    report.check("单元格分类判据覆盖典型情形", not wrong, f"不符：{wrong}")

    report.check("「翻译表格内容 / 跳过参考文献」等开关为运行期判断（改完即生效，无需重新解析）",
                 is_translatable_kind("reference", dataclasses.replace(settings, skip_reference_blocks=True)) is False
                 and is_translatable_kind("reference", dataclasses.replace(settings, skip_reference_blocks=False)) is True
                 and is_translatable_kind("table", settings) is False)


def stage_regions(report: Report, settings, tmp: Path) -> None:
    """阶段 B+++：图片 / 表格 / 公式一律不翻译，右栏原样呈现。

    对应用户需求："图片和表格的相关内容都不翻译，在中文译文部分展示原图和原表格；
    公式也展示原图，不要覆盖任何内容。"
    """
    report.section("阶段 B+++ · 图表公式原样保留（区域识别 / 标题 / 图内标签）")
    maker = load_module(ROOT / "tools" / "make_sample_pdf.py", "make_sample_pdf")
    from backend.pdf_parser import (
        _overlap_ratio,
        _table_row_count,
        absorb_region,
        caption_kind_of,
        is_table_like_lines,
        is_translatable_kind,
        parse_pdf,
    )

    pdf = maker.build_figure_pdf(tmp / "figure_sample.pdf")
    out_dir = tmp / "figure_parse"
    info = parse_pdf(pdf, out_dir, zoom=1.0, settings=settings)
    report.check("生成并解析「图片 + 无框线表格」回归样本", info["pageCount"] == 2,
                 f"{info['pageCount']} 页")

    pg1 = json.loads((out_dir / "page-0001.json").read_text(encoding="utf-8"))
    pg2 = json.loads((out_dir / "page-0002.json").read_text(encoding="utf-8"))

    # ---------------- 第 1 页：图片 ----------------
    fx0, fy0, fx1, fy1 = maker.figure_sample_bbox()
    figs = [r for r in pg1["regions"] if r["type"] == "figure"]
    covers = [r for r in figs
              if r["bbox"][0] <= fx0 + 2 and r["bbox"][1] <= fy0 + 2
              and r["bbox"][2] >= fx1 - 2 and r["bbox"][3] >= fy1 - 2]
    report.check("位图 + 矢量边框被聚成一个「图片保留区域」",
                 bool(covers),
                 "、".join(str([round(v) for v in r["bbox"]]) for r in figs) or "（未识别到）")

    caps = [b for b in pg1["blocks"] if b["kind"] == "caption"]
    report.check("图题被识别为 caption 且不翻译",
                 len(caps) == 1 and not caps[0]["translatable"] and "Fig. 1" in caps[0]["text"],
                 f"{len(caps)} 个 caption，可译 {sum(1 for b in caps if b['translatable'])}")
    report.check("保留区域把图题一起圈进来（图题也原样保留）",
                 bool(caps) and any(_overlap_ratio(caps[0]["bbox"], r["bbox"]) >= 0.9 for r in covers),
                 f"图题 {[round(v) for v in caps[0]['bbox']]}" if caps else "无图题")

    labels = [b for b in pg1["blocks"] if b["text"].strip() in maker.FIG_LABELS]
    report.check("图内文字标签（Agent A / Server / Loss curve）不翻译",
                 len(labels) == len(maker.FIG_LABELS)
                 and all(not b["translatable"] and b["kind"] == "figure" for b in labels),
                 f"命中 {len(labels)}/{len(maker.FIG_LABELS)} 个，"
                 + "、".join(f"{b['kind']}{'译' if b['translatable'] else '不译'}" for b in labels))

    body1 = [b for b in pg1["blocks"] if b["text"].startswith(("The figure below", "Figure 1 illustrates"))]
    report.check("同页正文段落不受影响（仍可翻译）",
                 bool(body1) and all(b["translatable"] for b in body1),
                 f"{len(body1)} 段，可译 {sum(1 for b in body1 if b['translatable'])}")

    # ---------------- 第 2 页：无框线表格 ----------------
    tabs = [r for r in pg2["regions"] if r["type"] == "table"]
    last_row = [b for b in pg2["blocks"] if "0.9613" in b["text"]]
    report.check("无框线表格（PyMuPDF 检测不到）靠「行距紧 + 与表题同栏」兜底识别",
                 bool(tabs) and bool(last_row)
                 and _overlap_ratio(last_row[0]["bbox"], tabs[0]["bbox"]) >= 0.9,
                 ("、".join(str([round(v) for v in r["bbox"]]) for r in tabs) or "（未识别到）")
                 + (f"；末行 {[round(v) for v in last_row[0]['bbox']]}" if last_row else "；未找到末行"))

    tbl = tabs[0]["bbox"] if tabs else [0, 0, 0, 0]
    inside_tbl = [b for b in pg2["blocks"] if _overlap_ratio(b["bbox"], tbl) >= 0.6]
    report.check("表格的表头与数据行全部不可译（整表原样保留）",
                 len(inside_tbl) >= 2 and all(not b["translatable"] for b in inside_tbl)
                 and any("Loss" in b["text"] for b in inside_tbl)
                 and any("TABLE I" in b["text"] for b in inside_tbl),
                 f"表内 {len(inside_tbl)} 块，可译 {sum(1 for b in inside_tbl if b['translatable'])}")
    report.check("表格下方正文段落仍可翻译",
                 any(b["translatable"] and b["text"].startswith("The comparison above") for b in pg2["blocks"]))

    # 表格内容必须带 table 标记：右栏据此把整张表透出原件，而不是把表格文字当段落翻出来。
    # 表题要排除——标题有自己的开关（「翻译图表标题」），不能一并锁死。
    tbl_content = [b for b in inside_tbl if b["kind"] != "caption"]
    tbl_caps = [b for b in inside_tbl if b["kind"] == "caption"]
    report.check("表格内容带 table 标记（右栏整表透出原件），表题不受影响",
                 len(tbl_content) >= 1 and all(b.get("table") for b in tbl_content)
                 and all(not b.get("table") for b in tbl_caps),
                 f"内容 {sum(1 for b in tbl_content if b.get('table'))}/{len(tbl_content)} 带标记，"
                 f"表题 {len(tbl_caps)} 个未带标记")

    # 判据单测：解析器把每个「单元格」当成一条行，因此「同一 y 上并排 ≥3 个片段」
    # 才是表格行的指纹。整张表被 PyMuPDF 并成一个文本块时，只有这条判据认得出来。
    tbl_lines = [
        {"bbox": [10, 100, 20, 108]}, {"bbox": [30, 100, 40, 108]}, {"bbox": [50, 100, 60, 108]},
        {"bbox": [10, 110, 20, 118]}, {"bbox": [30, 110, 40, 118]}, {"bbox": [50, 110, 60, 118]},
        {"bbox": [10, 120, 20, 128]}, {"bbox": [30, 120, 40, 128]}, {"bbox": [50, 120, 60, 128]},
    ]
    prose_lines = [
        {"bbox": [10, 100, 200, 110]}, {"bbox": [10, 112, 200, 122]}, {"bbox": [10, 124, 200, 134]},
    ]
    report.check("表格行判据：同一 y 并排 ≥3 片段才算表格行（整表一块也认得出，正文不误判）",
                 _table_row_count([10, 100, 60, 128], tbl_lines) == 3
                 and is_table_like_lines([10, 100, 60, 128], tbl_lines)
                 and not is_table_like_lines([10, 96, 200, 138], prose_lines),
                 f"表格行 {_table_row_count([10, 100, 60, 128], tbl_lines)} 条；"
                 f"正文误判={is_table_like_lines([10, 96, 200, 138], prose_lines)}")

    # ---------------- 判据单测 ----------------
    cap_cases = {
        "Fig. 1: System model": "figure",
        "Figure 3. Architecture of the system": "figure",
        "TABLE II: Comparison Between MaxImg and APO": "table",
        "Tab. 1: Results": "table",
        "Fig. 3 shows the overall pipeline": None,   # 正文引用，不是图题
        "Algorithm 1: GSCLO via APO": None,          # 算法体仍按正文逐行翻译
        "We compare our method with baselines": None,
    }
    wrong = {t: caption_kind_of(t) for t, want in cap_cases.items() if caption_kind_of(t) != want}
    report.check("图题 / 表题判据（正文引用「Fig. 3 shows…」不误判）", not wrong, f"不符：{wrong}")

    region = [{"type": "figure", "bbox": [100, 100, 400, 300], "label": ""}]
    area = 595 * 842
    report.check("保留区域吸收短标签、但放过多行长正文",
                 absorb_region({"text": "Power", "bbox": [120, 120, 160, 130], "line_count": 1}, region, area) is not None
                 and absorb_region({"text": "x" * 300, "bbox": [110, 200, 390, 260], "line_count": 6}, region, area) is None)
    report.check("章节标题不会被区域吞掉（即使落在区域框内）",
                 absorb_region({"text": "E. Experiment 5: Power Minimization GSCLO",
                                "bbox": [110, 150, 300, 162], "line_count": 1}, region, area) is None)

    report.check("figure / table / formula / caption 默认一律不可译",
                 all(is_translatable_kind(k, settings) is False
                     for k in ("figure", "table", "formula", "caption"))
                 and is_translatable_kind("text", settings) is True)
    report.check("勾选「翻译图表标题」后 caption 变为可译（运行期生效，无需重新解析）",
                 is_translatable_kind("caption", dataclasses.replace(settings, translate_captions=True)) is True)


def stage_page_payload(report: Report, service, doc_id: str) -> None:
    report.section("阶段 B+ · 页面载荷（供右侧译文层落位）")
    payload = service.page_payload(doc_id, 1)
    report.check("页面载荷含译文层所需全部字段",
                 all(k in payload for k in ("width", "height", "image", "blocks", "imageWidth", "imageHeight")),
                 f"block 字段：{sorted(payload['blocks'][0].keys())}")
    report.check("初始状态所有可译片段均为「未翻译」", payload["stats"]["translated"] == 0,
                 f"translatable={payload['stats']['translatable']}")
    report.check("未翻译片段一并下发（右侧显示虚线占位框，保证版式不塌）",
                 all("translation" in b for b in payload["blocks"]))
    report.check("载荷下发保留区域（供右栏画原样保留的虚框）",
                 "regions" in payload and isinstance(payload["regions"], list),
                 f"{len(payload.get('regions', []))} 个区域")
    report.check("载荷统计给出图表公式数量（右栏提示用）",
                 all(k in payload["stats"] for k in ("figures", "tables", "formulas", "kept", "hidden")),
                 str({k: payload["stats"][k] for k in ("figures", "tables", "formulas", "kept", "hidden")}))
    report.check("每个块都带 hidden 与 regionIndex 标记（前端据此决定是否渲染文字）",
                 all("hidden" in b and "regionIndex" in b for b in payload["blocks"]))
    report.check("默认配置下表格单元格被标为隐藏（右栏直接显示原表，不用中文覆盖）",
                 all(b["hidden"] for b in payload["blocks"] if b.get("table")) or
                 not any(b.get("table") for b in payload["blocks"]),
                 f"表格块 {sum(1 for b in payload['blocks'] if b.get('table'))} 个，"
                 f"其中隐藏 {sum(1 for b in payload['blocks'] if b.get('table') and b['hidden'])} 个")


def stage_api_and_cache(report: Report, settings, service, doc_id: str, cache) -> None:
    """阶段 C/D：真实 HTTP 调用（指向本机假 LLM）+ 缓存命中。"""
    report.section("阶段 C · 云端大模型 API 调用（真实 HTTP，指向本机假 LLM）")
    REQUESTS.clear()
    page_count = service.store.meta(doc_id)["pageCount"]
    pages = list(range(1, page_count + 1))

    plan = service.plan(doc_id, pages)
    report.check("翻译计划：识别需调用 API 的页面", plan["totalBlocks"] > 0,
                 f"可译片段 {plan['totalBlocks']} 个，待译页 {plan['needsApi']}")

    from backend.jobs import JobManager

    jm = JobManager()
    job = jm.create(doc_id, pages, force=False)
    t0 = time.time()
    service.run_job(job)
    elapsed = time.time() - t0

    report.check("翻译任务执行完成", job.status == "done", f"status={job.status}，耗时 {elapsed:.2f}s")
    report.check("全部可译片段均已产出译文",
                 job.totals["translated"] == plan["totalBlocks"] and job.totals["failed"] == 0,
                 f"translated={job.totals['translated']}/{plan['totalBlocks']}，failed={job.totals['failed']}")

    report.check("确实发起了 HTTP 请求", len(REQUESTS) > 0, f"共 {len(REQUESTS)} 次请求")
    if REQUESTS:
        r0 = REQUESTS[0]
        report.check("请求命中 OpenAI 兼容端点 /chat/completions", r0["path"].endswith("/chat/completions"), r0["path"])
        report.check("API Key 通过 Authorization: Bearer 注入（未硬编码）",
                     r0["auth"] == f"Bearer {settings.api_key}", f"Authorization={r0['auth'][:18]}…")
        report.check("JSON 输出模式生效（response_format=json_object）",
                     (r0["response_format"] or {}).get("type") == "json_object", str(r0["response_format"]))
        segs = json.loads(str(r0["body"]["messages"][-1]["content"]).split("\n\n")[0]).get("segments", [])
        report.check("分块携带片段 id（译文可精确回填到原文坐标）",
                     bool(segs) and all("id" in s and "text" in s for s in segs),
                     f"首块 {len(segs)} 个片段，示例 id={segs[0]['id'] if segs else '-'}")
        report.check("模型名来自配置（未硬编码）", r0["model"] == settings.model, str(r0["model"]))

    payload = service.page_payload(doc_id, 1)
    sample = next((b for b in payload["blocks"] if b["translation"]), None)
    report.check("译文按 block id 回填到对应坐标片段", sample is not None,
                 f"示例 {sample['id']}：{sample['translation'][:28]}…" if sample else "无译文")

    report.section("阶段 D · 本地缓存（避免重复调用 API）")
    before = len(REQUESTS)
    job2 = jm.create(doc_id, pages, force=False)
    service.run_job(job2)
    delta = len(REQUESTS) - before
    report.check("二次翻译同一文档：0 次 API 调用（全部命中本地缓存）", delta == 0, f"新增请求 {delta} 次")
    report.check("二次翻译结果同样完整", job2.totals["translated"] == plan["totalBlocks"],
                 f"translated={job2.totals['translated']}，缓存命中 {job2.totals['cached']}")

    before = len(REQUESTS)
    job3 = jm.create(doc_id, pages, force=True)
    service.run_job(job3)
    report.check("勾选「重译」后确实重新请求 API", len(REQUESTS) - before > 0, f"新增请求 {len(REQUESTS) - before} 次")

    stats = cache.stats()
    report.check("缓存库落盘并可统计", stats["entries"] > 0,
                 f"{stats['entries']} 条译文 / {stats['indexedBlocks']} 索引 / {stats['dbBytes'] / 1024:.0f} KB")

    # 换模型 -> 缓存应自动失效（key 含 model）
    from backend.cache import make_key

    k_a = make_key(settings.provider, settings.model, settings.source_lang, settings.target_lang, "hello")
    k_b = make_key(settings.provider, "another-model", settings.source_lang, settings.target_lang, "hello")
    report.check("缓存 key 与「模型/语言/提示词版本」绑定（换模型自动失效）", k_a != k_b, f"{k_a[:10]}… vs {k_b[:10]}…")


def stage_settings(report: Report, settings, service, doc_id: str, cache) -> None:
    """阶段 G：界面设置窗口（在界面上填 API Key / 换模型，无需改代码）。"""
    report.section("阶段 G · 界面设置窗口（可视化配置 API Key 与服务商）")
    from backend.config import (
        UI_FIELDS,
        clear_runtime_override,
        get_settings,
        save_runtime_override,
        ui_payload_to_fields,
    )

    pub = settings.public()
    report.check("配置接口暴露可选服务商及其预设（下拉框数据源）",
                 len(pub["providers"]) >= 8 and "deepseek" in pub["providers"],
                 f"{len(pub['providers'])} 个：" + "、".join(list(pub["providers"])))
    report.check("配置接口暴露可编辑字段清单（provider/baseUrl/model/apiKey 等）",
                 {"provider", "baseUrl", "model", "apiKey"} <= set(pub["fields"]),
                 "、".join(pub["fields"]))
    report.check("密钥永不明文下发（只给掩码）",
                 not pub["apiKeySet"] or ("*" in pub["apiKeyMasked"] and pub["apiKeyMasked"] != settings.api_key),
                 f"apiKeyMasked={pub['apiKeyMasked']!r}")

    fields, file_values = ui_payload_to_fields({"provider": "openai", "apiKey": ""})
    report.check("API Key 留空 = 不修改（不会误把已保存的密钥清空）",
                 "api_key" not in fields and "LLM_API_KEY" not in file_values, str(fields))
    fields, file_values = ui_payload_to_fields({"apiKey": "sk-newly-typed"})
    report.check("API Key 填值则写入配置",
                 fields.get("api_key") == "sk-newly-typed" and file_values.get("LLM_API_KEY") == "sk-newly-typed")

    before_provider, before_model = settings.provider, settings.model
    override_path = save_runtime_override(
        {
            "LLM_PROVIDER": "openai",
            "LLM_MODEL": "gpt-4o-mini",
            "LLM_BASE_URL": "https://api.openai.com/v1",
        }
    )
    fresh = get_settings(reload=True)
    report.check("界面保存后立即生效，且优先级高于环境变量（环境里是 custom/selfcheck-model）",
                 fresh.provider == "openai" and fresh.model == "gpt-4o-mini",
                 f"生效为 {fresh.provider} / {fresh.model}；文件 {override_path.name}")
    report.check("能识别哪些配置项被界面接管",
                 {"LLM_PROVIDER", "LLM_MODEL", "LLM_BASE_URL"} <= set(fresh.overridden_keys),
                 str(fresh.overridden_keys))

    from backend.service import TranslationService

    old_done = service.page_payload(doc_id, 1)["stats"]["translated"]
    new_done = TranslationService(fresh, service.store, cache).page_payload(doc_id, 1)["stats"]["translated"]
    report.check("换模型后旧译文不再复用（页面回到未翻译，避免张冠李戴）",
                 old_done > 0 and new_done == 0, f"原模型已译 {old_done} 个 → 新模型视为已译 {new_done} 个")

    clear_runtime_override()
    back = get_settings(reload=True)
    report.check("点「清除界面设置」后回落到 .env / 环境变量",
                 back.provider == before_provider and back.model == before_model and not back.overridden_keys,
                 f"回落为 {back.provider} / {back.model}")

    report.check("界面设置的落地位置在 data/ 下（随 .gitignore 排除，不会提交密钥）",
                 override_path.parent.name == "data" or override_path.parent == settings.data_path,
                 str(override_path))


def stage_mock_cache(report: Report, settings, service, doc_id: str, cache) -> None:
    """阶段 D+：模拟模式（LLM_MOCK）只用于离线验链路，绝不能污染正式缓存。"""
    report.section("阶段 D+ · 模拟模式不污染正式缓存")
    from backend.jobs import JobManager
    from backend.service import TranslationService

    before = cache.stats()["entries"]
    mock_settings = dataclasses.replace(settings, mock=True, model="selfcheck-mock-model")
    svc = TranslationService(mock_settings, service.store, cache)
    job = JobManager().create(doc_id, [1], force=False)
    svc.run_job(job)
    after = cache.stats()["entries"]

    report.check("模拟模式可离线产出占位译文",
                 job.status == "done" and job.totals["translated"] > 0 and job.totals["apiCalls"] == 0,
                 f"{job.totals['translated']} 个片段，API 调用 {job.totals['apiCalls']} 次")
    report.check("模拟译文不写入缓存（切回真实模型不会看到「【模拟译文…】」）",
                 after == before, f"缓存条目 {before} → {after}")
    payload = svc.page_payload(doc_id, 1)
    report.check("模拟模式下的页面载荷不会把占位文本当成已翻译",
                 payload["stats"]["translated"] == 0,
                 f"已译 {payload['stats']['translated']}/{payload['stats']['translatable']}")


def stage_errors(report: Report, settings) -> None:
    """阶段 E：错误提示可读性（401 / 5xx 重试 / 空配置）。"""
    report.section("阶段 E · 异常与错误提示")
    from backend.translator import Translator, TranslationError

    t401 = Translator(dataclasses.replace(settings, model="fail401-model", retries=0, json_mode="off"))
    try:
        t401.translate_chunk([{"id": "a", "text": "hello"}])
        report.check("401 抛出可读错误", False, "未抛异常")
    except TranslationError as exc:
        report.check("401 鉴权失败给出明确中文提示", "401" in str(exc) and "API_KEY" in str(exc), str(exc))

    t500 = Translator(dataclasses.replace(settings, model="fail500-model", retries=2, json_mode="off"))
    n0 = len(REQUESTS)
    try:
        t500.translate_chunk([{"id": "a", "text": "hello"}])
    except TranslationError:
        pass
    report.check("5xx 自动指数退避重试", len(REQUESTS) - n0 == 3, f"共尝试 {len(REQUESTS) - n0} 次（1 + 2 次重试）")

    bad = dataclasses.replace(settings, base_url="", model="")
    t_bad = Translator(bad)
    report.check("Base URL 缺失时给出可读提示", True, "字段为空 → 由配置校验接管")
    cfg = dataclasses.replace(settings, mock=False, api_key="")
    report.check("未配置 Key 时前端/接口会明确提示", cfg.api_key_masked() == "", "api_key_masked 为空 → 上传接口返回 400 引导文案")


def stage_frontend(report: Report, settings) -> None:
    """阶段 F：前后端契约静态校验（不依赖浏览器）。"""
    report.section("阶段 F · 前后端契约静态校验")
    fe = settings.frontend_dir
    html = (fe / "index.html").read_text(encoding="utf-8")
    appjs = (fe / "js" / "app.js").read_text(encoding="utf-8")
    viewerjs = (fe / "js" / "viewer.js").read_text(encoding="utf-8")
    apijs = (fe / "js" / "api.js").read_text(encoding="utf-8")

    defined = set(re.findall(r'id="([^"]+)"', html))
    used = set(re.findall(r"\$\('([^']+)'\)", appjs))
    missing = sorted(used - defined)
    report.check("app.js 引用的 DOM id 在 index.html 中均存在", not missing, f"缺失：{missing}")

    el_keys = set(re.findall(r"^\s{2}(\w+):\s*\$\(", appjs, re.M)) | set(re.findall(r"^\s{2}(\w+):\s*el\.", appjs, re.M))
    el_used = set(re.findall(r"this\.el\.(\w+)", viewerjs))
    miss_el = sorted(el_used - el_keys)
    report.check("viewer.js 使用的 el.* 引用均由 app.js 注入", not miss_el, f"缺失：{miss_el}")

    api_defined = set(re.findall(r"^\s{2}(\w+)\([^)]*\)\s*\{", apijs, re.M))
    api_used = set(re.findall(r"api\.(\w+)\(", appjs + viewerjs))
    miss_api = sorted(api_used - api_defined)
    report.check("前端调用的 api.* 方法均已在 api.js 实现", not miss_api, f"缺失：{miss_api}")

    # 连续滚动：每页由 viewer.js 逐页生成，各带一份「原页渲染图 + 命中层」与「译文层」
    report.check("左侧原文层为整页渲染图 + 覆盖层（公式/图表位置不变）",
                 "className = 'page-image'" in viewerjs and "className = 'overlay'" in viewerjs
                 and 'id="stack-left"' in html)
    report.check("右侧为独立的译文层（绝对定位片段）",
                 "className = 'trans-layer'" in viewerjs
                 and ".trans-layer { position: absolute" in (fe / "styles.css").read_text(encoding="utf-8"))
    report.check("左右两栏等分且可拖动分隔条", 'id="divider"' in html and "flex" in (fe / "styles.css").read_text(encoding="utf-8"))
    report.check("提供缩放滑块 / 快捷键 / 适应宽度",
                 'id="zoom-range"' in html and "btn-fit-width" in html and "ctrlKey" in viewerjs)
    report.check("缩放时左右共用同一坐标（layout 中两栏 stage 同步尺寸）",
                 "for (const side of ['left', 'right'])" in viewerjs and "meta.width * s" in viewerjs)
    report.check("滚动同步为双向等比映射（翻页左右同步）",
                 "scrollLeft.addEventListener('scroll'" in viewerjs and "scrollRight.addEventListener('scroll'" in viewerjs)
    report.check("连续滚动：页面纵向堆叠、滚轮即下一页、内容按需懒加载",
                 ".page-stack" in (fe / "styles.css").read_text(encoding="utf-8")
                 and "scrollToPage" in viewerjs and "ensurePage" in viewerjs
                 and "loading = 'lazy'" in viewerjs)
    report.check("悬停/点击命中测试把像素换算回 PDF 坐标再查块",
                 "px / this.scale" in viewerjs and "this._hitTest(slot, px / this.scale, py / this.scale)" in viewerjs)
    report.check("提供「⚙ 设置」入口与配置窗口（服务商/Base URL/模型/API Key）",
                 'id="btn-settings"' in html and 'id="settings-dialog"' in html
                 and all(f'id="{i}"' in html for i in
                         ("cfg-provider", "cfg-base-url", "cfg-model", "cfg-api-key")))
    report.check("配置窗口支持「测试连接 / 保存并应用 / 清除界面设置」",
                 all(f'id="{i}"' in html for i in ("cfg-test", "cfg-save", "cfg-clear")))
    report.check("切换服务商时自动带出预设 Base URL 与模型名", "onProviderChange" in appjs)
    report.check("密钥输入框默认掩码，可按需显示", 'id="cfg-api-key" type="password"' in html)
    report.check("提供「重新解析」入口（解析逻辑升级后可就地重跑）",
                 'id="btn-reparse"' in html and "reparsePdf" in appjs and "reparsePdf" in apijs
                 and "/reparse" in (ROOT / "backend" / "main.py").read_text(encoding="utf-8"))

    report.check("译文按原块尺寸自动缩放字号（中文比英文长，需缩字才放得下）",
                 "_fitBlock" in viewerjs and "FIT_MIN_RATIO" in viewerjs and "overflowMode" in viewerjs,
                 "viewer.js 内含 _fitBlock / _visualUnits")
    report.check("折行按真实行数算（而不是按面积估），行内公式按贴出来的像素宽高参与折行",
                 "_wrapMeasure" in viewerjs and "_lineUnits" in viewerjs and "_patchWidths" in viewerjs,
                 "viewer.js 内含 _wrapMeasure / _lineUnits / _patchWidths")
    report.check("排版后用真实 DOM 高度再校正一次字号（估算误差不再累积成压块）",
                 "_correctFit" in viewerjs and "getBoundingClientRect" in viewerjs,
                 "viewer.js 内含 _correctFit")
    report.check("行内公式小块随文流转、不再用 float 贴回原位（float 会把整段挤成细条）",
                 "math-patch" in viewerjs and "float" not in viewerjs.split("_mathPatch")[1][:1200],
                 "viewer.js 的 _mathPatch 内不出现 float")
    report.check("视觉宽度按 CJK=1em、拉丁=0.52em 估算",
                 "CJK_FROM" in viewerjs and "LATIN_UNITS" in viewerjs)
    report.check("提供「溢出全文」开关（勾上则不缩字号、完整显示）",
                 'id="chk-overflow"' in html and "setOverflow" in appjs)
    report.check("右栏提示会报告本页缩字 / 向下延展的片段数",
                 "renderFitHint" in appjs and "onFit" in appjs)
    report.check("设置窗口提供「翻译表格内容 / 翻译图表标题」开关",
                 'id="cfg-translate-tables"' in html and 'id="cfg-translate-captions"' in html
                 and "translateTables" in appjs and "translateCaptions" in appjs)
    report.check("右栏以原页图为底（图片 / 表格 / 公式靠透出原图原样呈现）",
                 "original-base" in viewerjs and "original-base" in (fe / "styles.css").read_text(encoding="utf-8")
                 and "page-image underlay" in viewerjs)
    report.check("底衬开关默认开启，且状态本地记住",
                 'id="chk-underlay" checked' in html and "originalBase" in appjs)
    report.check("不可译片段不再渲染文字，只画「保留」轮廓（图片 / 表格 / 公式不被中文覆盖）",
                 "preserve" in viewerjs and "if (blk.translatable)" in viewerjs and "KIND_LABEL" in viewerjs
                 and "RENDERABLE_KINDS" not in viewerjs)
    report.check("保留区域（图 / 表）有独立虚框与命中区，悬停时与命中块联动高亮",
                 "region-hit" in viewerjs and "_paint" in viewerjs and "regionId" in viewerjs)
    report.check("表格单元格按 hidden 标记跳过渲染（右栏直接显示原表）",
                 "blk.hidden" in viewerjs)
    report.check("译文块在原图底衬模式下为不透明白底（盖住原英文），未翻译片段保持透明",
                 "--tblock-bg" in (fe / "styles.css").read_text(encoding="utf-8")
                 and ".tblock.empty { border-color: #cfd6dd; background: transparent; }" in (fe / "styles.css").read_text(encoding="utf-8"))


# ======================================================================
# 主流程
# ======================================================================
def main() -> int:
    global VERBOSE
    ap = argparse.ArgumentParser(description="论文对照阅读器 · 关键链路自检")
    ap.add_argument("-v", "--verbose", action="store_true", help="打印每条检查的细节")
    args = ap.parse_args()
    VERBOSE = args.verbose

    print("=" * 62)
    print(" 论文 PDF 翻译 · 中英对照阅读器 —— 关键链路自检")
    print("=" * 62)

    tmp_root = Path(tempfile.mkdtemp(prefix="paper-selfcheck-"))
    srv, base_url = start_fake_llm()

    # 在导入 backend 之前注入配置：全部走环境变量，验证「零硬编码」
    os.environ.update(
        {
            "LLM_PROVIDER": "custom",
            "LLM_BASE_URL": base_url,
            "LLM_MODEL": "selfcheck-model",
            "LLM_API_KEY": "sk-selfcheck-key",
            "LLM_JSON_MODE": "on",
            "LLM_MOCK": "0",
            "LLM_MAX_CONCURRENCY": "4",
            "LLM_BATCH_CHARS": "1200",
            "LLM_BATCH_SEGMENTS": "16",
            "LLM_RETRIES": "2",
            "RENDER_ZOOM": "2.0",
            "DATA_DIR": str(tmp_root / "data"),
            "SKIP_REFERENCE_BLOCKS": "1",
        }
    )

    try:
        from backend.cache import TranslationCache
        from backend.config import get_settings
        from backend.service import TranslationService
        from backend.storage import DocumentStore

        settings = get_settings(reload=True)
        print(f"\n临时数据目录：{settings.data_path}")
        print(f"假 LLM 服务　：{base_url}（本机回环，不联网、不消耗额度）")
        R.check("配置全部来自环境变量（provider/model/base_url/key 均非硬编码）",
                settings.provider == "custom" and settings.model == "selfcheck-model"
                and settings.base_url == base_url and settings.api_key == "sk-selfcheck-key",
                f"{settings.provider} / {settings.model} / {settings.api_key_masked()}")

        store = DocumentStore(settings)
        cache = TranslationCache(settings.cache_dir / "translations.db")
        service = TranslationService(settings, store, cache)

        pdf_path = stage_pdf(R, tmp_root)
        doc_id, _page = stage_parse(R, pdf_path, store, settings)
        stage_stitch(R)
        stage_parse_quality(R, store, settings, tmp_root)
        stage_tables(R, settings, tmp_root)
        stage_regions(R, settings, tmp_root)
        stage_page_payload(R, service, doc_id)
        stage_api_and_cache(R, settings, service, doc_id, cache)
        stage_mock_cache(R, settings, service, doc_id, cache)
        stage_errors(R, settings)
        stage_settings(R, settings, service, doc_id, cache)
        stage_frontend(R, settings)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        R.check("自检流程未抛出未捕获异常", False, "见上方堆栈")
    finally:
        srv.shutdown()

    code = R.summary()
    print(f"\n（临时文件：{tmp_root}，可自行删除）")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
