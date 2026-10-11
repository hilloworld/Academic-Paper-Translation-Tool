"""生成「右侧译文栏效果预览图」——直观看出哪些地方会被中文覆盖、哪些区域保持原图。

用法：
    python tools/preview_preserve.py <docId> <页码> [输出路径] [--text]

* 不加 `--text`：示意模式。白雾块 = 会被中文覆盖的正文片段，虚框 = 原样保留的图表/公式；
* 加 `--text`：**按前端同样的规则把真实中文译文画进原页**（读本地翻译缓存），
  用来在不开浏览器的情况下核对"译文到底放不放得下、会不会被截断"。

不传输出路径时写到 ``docs/preview-<docId>-p<页码>[-text].png``。

图上标记说明：

* **白雾块 / 中文块** = 会被中文译文替换的正文片段（白底盖住原英文）；
* **紫/绿虚线大框** = 保留区域（紫＝图片，绿＝表格），右栏原样显示，不做任何覆盖；
* **蓝色点线框** = 其它保留原文的片段（公式、参考文献、图表标题）；
* `--text` 模式下**红框** = 缩到下限（0.55×）仍放不下、按原块高度会被截断的片段。

示例：
    python tools/preview_preserve.py 1b7359f1022e7c74 3
    python tools/preview_preserve.py 1b7359f1022e7c74 4 --text
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

try:
    import pymupdf as fitz
except ImportError:  # pragma: no cover
    import fitz

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.config import get_settings  # noqa: E402
from backend.pdf_parser import load_page  # noqa: E402

CJK = "china-s"          # PyMuPDF 内置中文字体
REGION_COLOR = {"figure": (0.55, 0.10, 0.70), "table": (0.05, 0.55, 0.25)}
KEPT_COLOR = (0.10, 0.35, 0.85)
WARN_COLOR = (0.85, 0.25, 0.10)
CJK_FROM = 0x2e80
LATIN_UNITS = 0.52
FIT_MIN_RATIO = 0.55     # 与 viewer.js 保持一致


def visual_units(text: str) -> float:
    return sum(1.0 if ord(ch) >= CJK_FROM else LATIN_UNITS for ch in text)


def fit_font(text: str, w: float, h: float, base: float, line_height: float = 1.32) -> float:
    """与前端 `_fitBlock` 同一思路（按真实行数二分字号），保证预览与浏览器一致。"""
    units = visual_units(text)
    if not units or w <= 4 or h <= 4:
        return base
    limit = math.sqrt((h * w) / (units * line_height)) * 0.96
    return max(FIT_MIN_RATIO * base, min(base, limit))


def render(page_info: dict, doc_dir: Path, page_no: int, out_path: Path,
           zoom: float = 1.6, with_text: bool = False) -> dict:
    src = doc_dir / "pages" / f"p{page_no:04d}.png"
    out = fitz.open()
    pw, ph = page_info["width"] * zoom, page_info["height"] * zoom
    pg = out.new_page(width=pw, height=ph + 46)
    pg.insert_image(fitz.Rect(0, 46, pw, ph + 46), filename=str(src))

    def to_px(box):
        x0, y0, x1, y1 = box
        return fitz.Rect(x0 * zoom, y0 * zoom + 46, x1 * zoom, y1 * zoom + 46)

    stats = {"covered": 0, "shrunk": 0, "overflow": 0, "worst": 0.0}
    for b in page_info["blocks"]:
        if not b.get("translatable") or b.get("hidden"):
            continue
        rect = to_px(b["bbox"])
        text = b.get("translation") or ""
        stats["covered"] += 1
        if not with_text or not text:
            pg.draw_rect(rect, color=None, fill=(1, 1, 1), fill_opacity=0.78)
            continue
        pg.draw_rect(rect, color=None, fill=(1, 1, 1), fill_opacity=1.0)  # 白底盖住原英文
        base = b["fontSize"] * zoom
        fs = fit_font(text, rect.width, rect.height, base)
        if fs < base * 0.995:
            stats["shrunk"] += 1
        inner = fitz.Rect(rect.x0 + 1, rect.y0 + 1, rect.x1 - 1, rect.y1 - 1)
        rc = pg.insert_textbox(inner, text, fontsize=fs, fontname=CJK, color=(0.09, 0.09, 0.10),
                               lineheight=1.32, align=0)
        if rc < 0:
            # 放不下：前端此时会向下延展（不裁字），这里用红框标出来并记录差多少
            stats["overflow"] += 1
            stats["worst"] = max(stats["worst"], -rc / max(fs, 1e-6))
            pg.draw_rect(rect, color=WARN_COLOR, width=0.9)

    for r in page_info.get("regions", []):
        rect = to_px(r["bbox"])
        color = REGION_COLOR.get(r["type"], REGION_COLOR["figure"])
        pg.draw_rect(rect, color=color, width=2.0, dashes="[6 4] 0")
        label = "图片区域 · 原样保留" if r["type"] == "figure" else "表格区域 · 原样保留"
        pg.insert_text(fitz.Point(rect.x0 + 3, max(58.0, rect.y0 - 5)), label,
                       fontname=CJK, fontsize=10, color=color)

    kept = 0
    for b in page_info["blocks"]:
        if b.get("translatable") or b.get("hidden") or b.get("regionIndex") is not None:
            continue
        if b["kind"] in ("margin", "pageno", "meta", "empty"):
            continue
        kept += 1
        pg.draw_rect(to_px(b["bbox"]), color=KEPT_COLOR, width=0.9, dashes="[2 3] 0")

    mode = "真实译文" if with_text else "示意"
    legend = (
        f"第 {page_no} 页 · 右栏效果预览（{mode}）　"
        f"译文片段 {stats['covered']} 处"
        + (f"（缩字 {stats['shrunk']}，放不下 {stats['overflow']}）" if with_text else "")
        + f"　原样保留 {len(page_info.get('regions', []))} 个图表区域 / {kept} 个公式等"
    )
    pg.insert_text(fitz.Point(10, 20), legend, fontname=CJK, fontsize=11, color=(0.1, 0.1, 0.1))
    note = ("说明：右栏以原页图为底，只有正文译文用白底覆盖；图片 / 表格 / 公式区域不做任何覆盖。"
            if not with_text else
            "说明：按前端同一套字号反解规则绘制译文；红框 = 缩到下限仍放不下的片段。")
    pg.insert_text(fitz.Point(10, 36), note, fontname=CJK, fontsize=9.5, color=(0.35, 0.35, 0.35))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    pg.get_pixmap(matrix=fitz.Matrix(1, 1), alpha=False).save(str(out_path))
    out.close()
    return stats


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    with_text = "--text" in sys.argv
    if len(args) < 2:
        print(__doc__)
        return 1
    doc_id, page_no = args[0], int(args[1])
    settings = get_settings(reload=True)
    doc_dir = settings.docs_dir / doc_id
    if not doc_dir.exists():
        print(f"找不到文档目录：{doc_dir}", file=sys.stderr)
        return 1

    page_info = load_page(doc_dir, page_no)
    if with_text:
        from backend.cache import TranslationCache, make_key
        from backend.pdf_parser import block_is_translatable
        from backend.storage import DocumentStore

        store = DocumentStore(settings)
        page = store.page(doc_id, page_no)
        # 与 service.page_payload 一致：按当前配置重算缓存键取译文
        cache = TranslationCache(settings.cache_dir / "translations.db")
        keys = {}
        for b in page["blocks"]:
            if b["text"].strip() and block_is_translatable(b, settings):
                keys[b["id"]] = make_key(settings.provider, settings.model, settings.source_lang,
                                         settings.target_lang, b["text"])
        hits = cache.get_many(list(keys.values())) if keys else {}
        for b in page_info["blocks"]:
            b["translation"] = hits.get(keys.get(b["id"], ""), "")

    out = Path(args[2]) if len(args) > 2 else ROOT / "docs" / (
        f"preview-{doc_id}-p{page_no}{'-text' if with_text else ''}.png"
    )
    stats = render(page_info, doc_dir, page_no, out, with_text=with_text)
    print(f"已生成：{out}（{out.stat().st_size / 1024:.0f} KB）")
    if with_text:
        print(f"  译文片段 {stats['covered']} 个：缩字 {stats['shrunk']} 个，"
              f"缩到下限仍放不下 {stats['overflow']} 个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
