"""生成一份用于自检 / 演示的英文"论文"PDF（双栏版式 + 公式 + 图 + 参考文献）。

用法：python tools/make_sample_pdf.py [输出路径]
默认输出：samples/sample_paper_en.pdf

版式刻意模仿真实论文：通栏标题/摘要 + 双栏正文 + 居中公式 + 图与图注 + 参考文献
+ 页边竖排水印 + 页码 + 一张有线框的表格，用来验证解析器的阅读顺序、片段分类与表格拆格能力。
"""
from __future__ import annotations

import sys
from pathlib import Path

try:
    import pymupdf as fitz
except ImportError:  # pragma: no cover
    import fitz

ROOT = Path(__file__).resolve().parent.parent

TITLE = "A Survey on Collaborative SLAM with 3D Gaussian Splatting"
AUTHORS = (
    "Phieu Nguyen Xuan *, Thanh Nguyen Canh **, Huu-Hung Nguyen *, "
    "Nak Young Chong ** and Xiem HoangVan *"
)
AFFIL = (
    "* Institute of System Integration, Le Quoc Dung Technical University, Hanoi, Vietnam.\n"
    "** Japan Advanced Institute of Science and Technology, Nomi, Ishikawa 923-1211, Japan.\n"
    "E-mail: {phucx, hungnh}@isict.edu.vn, {thanh, nakyoung, xiem}@jaist.ac.jp"
)
ABSTRACT = (
    "ABSTRACT: This survey comprehensively reviews the evolving field of multi-robot collaborative "
    "Simultaneous Localization and Mapping (SLAM) using 3D Gaussian Splatting (3DGS). As an explicit "
    "scene representation, 3DGS has enabled unprecedented real-time, high-fidelity rendering, which is "
    "ideal for robotics. However, its use in multi-robot systems introduces significant challenges in "
    "maintaining global consistency, managing communication, and fusing data from heterogeneous sources. "
    "We systematically categorize existing approaches by their coordination architecture: centralized, "
    "distributed and hybrid, and analyze core components such as multi-agent consistency and alignment, "
    "communication-efficient Gaussian representation, semantic distillation, fusion and pose optimization, "
    "and real-time scalability. In addition, a summary of critical datasets and evaluation metrics is "
    "provided to contextualize reported performance. Finally, we identify key open challenges and chart "
    "future research directions, including lifelong mapping, semantic association and mapping, multi-model "
    "robotics, and bridging the Sim2Real gap."
)
KEYWORDS = (
    "Keywords: Gaussian Splatting, Multi-Robot SLAM, Collaborative SLAM, Radiance Fields, "
    "3D Scene Representation, Novel View Synthesis"
)

H1 = "1.  INTRODUCTION"
INTRO_1 = (
    "Simultaneous Localization and Mapping (SLAM) is a fundamental capability in robotics and computer "
    "vision, enabling an autonomous agent to construct a map of an unknown environment while "
    "simultaneously determining its own position within it (Kazerouni et al., 2022). For applications "
    "ranging from autonomous driving (Wang et al., 2024) to planetary exploration (Yang et al., 2023), a "
    "detailed and accurate understanding of the environment is crucial for safe navigation and "
    "intelligent decision-making. Historically, SLAM systems have relied on sparse representations such "
    "as point clouds and surfels, or dense volumetric grids. While these methods achieved accurate "
    "localization, their inherent sparsity makes it difficult to fulfill utility tasks that require a "
    "detailed understanding of scene geometry and appearance, such as realistic rendering or collision "
    "avoidance."
)
INTRO_2 = (
    "Although single-agent SLAM has seen remarkable progress, mapping large-scale environments quickly "
    "and accurately remains a long-term objective. Multi-robot systems have recently captured "
    "significant attention as a solution to the limitations of single-agent systems, such as limited "
    "coverage and the lack of robustness in large-scale or perceptually degraded scenarios."
)
H2 = "2.  METHODOLOGY"
METHOD = (
    "We propose a unified taxonomy that organizes collaborative 3DGS-SLAM systems along three axes: the "
    "coordination architecture, the shared representation, and the consistency mechanism. The "
    "photometric residual between the rendered image and the observed frame is minimized jointly over "
    "the camera pose and the Gaussian parameters:"
)
EQUATION = "L = Sum_i || I_i - R(T_i , G) ||^2 + lambda * Sum_g min(0, s_g - s_min)"
FORMULA_NOTE = (
    "where I_i denotes the i-th input view, R is the differentiable rasterizer, T_i the camera pose and "
    "G the set of anisotropic Gaussians with scale s_g."
)
RESULT = (
    "Quantitative results indicate that distributed pipelines retain on average 92.4% of the "
    "centralized accuracy while reducing the exchanged payload by 6.8x through anchor-based pruning. "
    "These findings suggest that communication-aware representation design, rather than raw model "
    "scale, is the decisive factor for deployable multi-robot 3DGS-SLAM."
)
FIGURE_CAPTION = (
    "Figure 1: Overview of the proposed collaborative 3DGS-SLAM pipeline. Each agent maintains a local "
    "Gaussian field; a server aggregates keyframes and broadcasts the fused map back to all agents."
)
MARGIN_TEXT = "arXiv:2510.23881v1  [cs.RO]  28 Oct 2025"

# ----------------------------------------------------------------------
# 表格样本（第 3 页）：用于回归测试"表格按单元格拆块"的能力
# ----------------------------------------------------------------------
TABLE_ORIGIN = (56.0, 140.0)
TABLE_COL_WIDTHS = (92.0, 92.0, 190.0)
TABLE_ROW_HEIGHT = 26.0
TABLE_CAPTION = "TABLE II: Notations and Parameter Descriptions"
TABLE_ROWS = [
    ("Notation", "Type", "Description"),
    ("s_t in R3", "Data", "Robot pose in the world frame."),
    ("N", "Parameter", "Number of frames in the video sequence."),
    ("(E_t, D_t)", "Function", "Encoder and decoder functions of the system."),
]


def table_bbox() -> tuple[float, float, float, float]:
    """表格外接矩形，供自检断言"表格区域内的块"使用。"""
    x0, y0 = TABLE_ORIGIN
    width = sum(TABLE_COL_WIDTHS)
    height = TABLE_ROW_HEIGHT * len(TABLE_ROWS)
    return (x0, y0, x0 + width, y0 + height)


def build_table_page(doc: "fitz.Document") -> None:
    """新增一页：一张有线框的 3 列表格 + 表题 + 表下一段说明。

    刻意画出演框：PyMuPDF 的表格检测依赖这些线条，没有框是检测不出来的。
    """
    page = doc.new_page(width=595, height=842)
    x0, y0 = TABLE_ORIGIN
    x3 = x0 + sum(TABLE_COL_WIDTHS)
    y3 = y0 + TABLE_ROW_HEIGHT * len(TABLE_ROWS)

    page.insert_textbox(
        fitz.Rect(x0, y0 - 22, x3, y0 - 6), TABLE_CAPTION, fontsize=9.5, align=1
    )
    page.draw_rect(fitz.Rect(x0, y0, x3, y3), color=(0, 0, 0), width=0.7)
    for i in range(1, len(TABLE_COL_WIDTHS)):
        x = x0 + sum(TABLE_COL_WIDTHS[:i])
        page.draw_line(fitz.Point(x, y0), fitz.Point(x, y3), color=(0, 0, 0), width=0.7)
    for r in range(1, len(TABLE_ROWS)):
        y = y0 + TABLE_ROW_HEIGHT * r
        page.draw_line(fitz.Point(x0, y), fitz.Point(x3, y), color=(0, 0, 0), width=0.7)

    for r, row in enumerate(TABLE_ROWS):
        cy = y0 + TABLE_ROW_HEIGHT * r
        cx = x0
        for c, cell in enumerate(row):
            page.insert_textbox(
                fitz.Rect(cx + 3, cy + 5, cx + TABLE_COL_WIDTHS[c] - 3, cy + TABLE_ROW_HEIGHT),
                cell,
                fontsize=8.5,
            )
            cx += TABLE_COL_WIDTHS[c]

    page.insert_textbox(
        fitz.Rect(x0, y3 + 10, x3, y3 + 46),
        "The table lists the main notations used throughout this paper. "
        "Detailed derivations are deferred to the appendix.",
        fontsize=9,
    )
    page.insert_text(fitz.Point(24, 800), MARGIN_TEXT, fontsize=7.5, rotate=90, color=(0.35, 0.35, 0.35))

REFS = [
    "Kazerouni, I. A., et al. A survey on state estimation for autonomous robots. IEEE Access, 2022.",
    "Wang, Y., et al. Real-time mapping for autonomous driving in urban canyons. In Proceedings of the "
    "IEEE/CVF Conference on Computer Vision and Pattern Recognition, pp. 12045-12054, 2024.",
    "Kerbl, B., Kopanas, G., Leimkuehler, T., and Drettakis, G. 3D Gaussian splatting for real-time "
    "radiance field rendering. ACM Transactions on Graphics, vol. 42, pp. 1-14, 2023. doi:10.1145/3592433",
    "Saeedi, S., Trentini, M., Seto, M., and Li, H. Multiple-robot simultaneous localization and mapping: "
    "A review. Journal of Field Robotics, vol. 33, pp. 3-46, 2023.",
    "Yang, B., et al. Multi-robot collaborative mapping for planetary exploration. arXiv preprint "
    "arXiv:2303.09125, 2023.",
    "Matsuki, H., et al. Gaussian splatting SLAM. In Proceedings of the IEEE/CVF Conference on Computer "
    "Vision and Pattern Recognition, pp. 18039-18048, 2024.",
    "Zhu, Z., et al. Multi-agent collaborative Gaussian splatting. arXiv preprint arXiv:2404.12345, 2024.",
]


class Flow:
    """自上而下排布文本，自动累加实际占用高度，避免 insert_textbox 静默失败。"""

    def __init__(self, page: fitz.Page):
        self.page = page
        self.warnings: list[str] = []

    def put(self, x0, x1, y, text, *, size=9.5, font="helv", align=0, color=(0, 0, 0), lineheight=1.34, gap=0.0):
        height = 520.0
        rect = fitz.Rect(x0, y, x1, y + height)
        left = self.page.insert_textbox(
            rect, text, fontsize=size, fontname=font, align=align, color=color, lineheight=lineheight
        )
        used = height - left
        if left < 0:
            self.warnings.append(f"文本未完整写入（y={y:.0f}）：{text[:40]!r}")
        return y + max(used, size * 1.2) + gap


def build(out_path: Path) -> Path:
    doc = fitz.open()

    # ============================ 第 1 页 ============================
    page = doc.new_page(width=595, height=842)
    f = Flow(page)
    LX0, LX1 = 56.0, 290.0
    RX0, RX1 = 305.0, 539.0

    y = 60.0
    y = f.put(56, 539, y, TITLE, size=17, align=1, lineheight=1.22, gap=6)
    y = f.put(56, 539, y, AUTHORS, size=10.5, align=1, gap=3)
    y = f.put(56, 539, y, AFFIL, size=8.6, align=1, lineheight=1.3, gap=6)
    page.draw_line(fitz.Point(56, y), fitz.Point(539, y), color=(0, 0, 0), width=0.6)
    y += 6
    y = f.put(56, 539, y, ABSTRACT, size=8.8, gap=5)
    y = f.put(56, 539, y, KEYWORDS, size=8.4, gap=6)
    page.draw_line(fitz.Point(56, y), fitz.Point(539, y), color=(0, 0, 0), width=0.6)

    # 双栏正文
    ly = f.put(LX0, LX1, y + 10, H1, size=10, gap=4)
    ly = f.put(LX0, LX1, ly, INTRO_1, size=8.7, gap=5)
    f.put(LX0, LX1, ly, INTRO_2, size=8.7)

    ry = f.put(RX0, RX1, y + 10, H2, size=10, gap=4)
    ry = f.put(RX0, RX1, ry, METHOD, size=8.7, gap=3)
    ry = f.put(RX0, RX1, ry, EQUATION, size=8.9, align=1, gap=5)
    ry = f.put(RX0, RX1, ry, FORMULA_NOTE, size=8.7, gap=8)

    fig = fitz.Rect(RX0, ry, RX1, ry + 92)
    page.draw_rect(fig, color=(0.15, 0.15, 0.15), width=0.8, fill=(0.94, 0.96, 0.98))
    page.draw_circle(fitz.Point(fig.x0 + 26, fig.y0 + 26), 9, color=(0.2, 0.35, 0.75), width=1.0)
    page.draw_circle(fitz.Point(fig.x1 - 26, fig.y1 - 26), 9, color=(0.75, 0.3, 0.2), width=1.0)
    page.draw_line(
        fitz.Point(fig.x0 + 26, fig.y0 + 26), fitz.Point(fig.x1 - 26, fig.y1 - 26), color=(0.4, 0.4, 0.4), width=0.8
    )
    f.put(fig.x0 + 30, fig.x1 - 30, fig.y0 + 40, "AGENT A  <-->  SERVER  <-->  AGENT B", size=8, align=1)
    ry = f.put(RX0, RX1, fig.y1 + 6, FIGURE_CAPTION, size=7.8, lineheight=1.3, gap=8)
    f.put(RX0, RX1, ry, RESULT, size=8.7)

    f.put(282, 313, 800, "1", size=9, align=1)
    page.insert_text(fitz.Point(24, 800), MARGIN_TEXT, fontsize=7.5, rotate=90, color=(0.35, 0.35, 0.35))

    # ============================ 第 2 页：表格 ============================
    build_table_page(doc)

    # ============================ 第 3 页：参考文献 ============================
    p2 = doc.new_page(width=595, height=842)
    f2 = Flow(p2)
    y2 = f2.put(56, 539, 70, "References", size=12, gap=8)
    for i, ref in enumerate(REFS, start=1):
        y2 = f2.put(56, 539, y2, f"[{i}] {ref}", size=8.6, lineheight=1.32, gap=6)
    f2.put(282, 313, 800, "3", size=9, align=1)
    p2.insert_text(fitz.Point(24, 800), MARGIN_TEXT, fontsize=7.5, rotate=90, color=(0.35, 0.35, 0.35))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out_path))
    doc.close()

    for w in f.warnings + f2.warnings:
        print(f"  ⚠ {w}", file=sys.stderr)
    return out_path


# ----------------------------------------------------------------------
# 回归样本：词间空格是「独立 span」的 PDF
# ----------------------------------------------------------------------
SPACELESS_LINES = [
    "Communication Efficient Robotic Mixed Reality with Gaussian Splatting",
    "Simultaneous Localization and Mapping is a fundamental capability in robotics",
    "and computer vision, enabling an autonomous agent to construct a map of an",
    "unknown environment while simultaneously determining its own position in it.",
]


def build_spaceless_pdf(out_path: Path, size: float = 11.0) -> Path:
    """构造一份「单词与空格分别绘制」的 PDF，用于回归测试空格还原逻辑。

    真实论文里非常常见（IEEE / arXiv 模板就是这样排的）：每个单词、每个空格
    都是独立的文本对象，PyMuPDF 会把它们拆成一个个 span，其中"只有空格"的
    span 其 ``text.strip()`` 为空。若抽取时按 strip 结果过滤，就会把这些空格
    丢掉，得到 ``CommunicationEfficientRoboticMixedReality`` 这种粘连文本。

    这里刻意给空格换一种颜色，确保 PyMuPDF 不把空格并进单词所在的 span，
    从而稳定复现该场景。
    """
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    y = 100.0
    for line in SPACELESS_LINES:
        x = 56.0
        for word in line.split():
            page.insert_text(fitz.Point(x, y), word, fontsize=size, color=(0, 0, 0))
            x += fitz.get_text_length(word, fontname="helv", fontsize=size)
            page.insert_text(fitz.Point(x, y), " ", fontsize=size, color=(0.6, 0.6, 0.6))
            x += fitz.get_text_length(" ", fontname="helv", fontsize=size)
        y += 16.0
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out_path))
    doc.close()
    return out_path


# ----------------------------------------------------------------------
# 回归样本：图片 / 表格 / 公式「原样保留」的判定
# ----------------------------------------------------------------------
FIG_IMG_RECT = (300.0, 90.0, 540.0, 210.0)   # 栅格图（位图）区域
FIG_FRAME = (296.0, 86.0, 544.0, 214.0)      # 矢量边框（画在图片外沿）
FIG_CAPTION = "Fig. 1: Overview of the proposed collaborative pipeline."
FIG_LABELS = ("Agent A", "Server", "Loss curve")   # 图内文字标签，不该被翻译
FIG_BODY = (
    "Figure 1 illustrates the proposed architecture. Each agent maintains a local Gaussian field "
    "and exchanges only compact anchors with the server, which keeps the bandwidth bounded."
)
RULELESS_CAPTION = "TABLE I: Comparison Between MaxImg and APO"
# 无框线表格（booktabs 风格）：只有文字行、没有边框，find_tables 检测不出来
RULELESS_ROWS = [
    ("Metric", "MaxImg", "APO (Ours)"),
    ("Loss", "0.0432", "0.0377"),
    ("PSNR", "26.485", "26.949"),
    ("SSIM", "0.9549", "0.9613"),
]
RULELESS_X = (70.0, 220.0, 360.0)
RULELESS_TOP = 300.0
RULELESS_LINE = 7.0          # 行距刻意压到 7pt（正文行距通常 ≥9.5pt）


def figure_sample_bbox() -> tuple[float, float, float, float]:
    """图片（含边框）外接矩形，供自检断言使用。"""
    return FIG_FRAME


def ruleless_table_bbox() -> tuple[float, float, float, float]:
    """无框线表格的文字外接矩形（行块从 TOP-4 到最后一行的下沿），供自检使用。"""
    x0 = RULELESS_X[0]
    x1 = RULELESS_X[-1] + 90.0
    y0 = RULELESS_TOP - 4.0
    y1 = RULELESS_TOP + RULELESS_LINE * (len(RULELESS_ROWS) - 1) + 4.0
    return (x0, y0, x1, y1)


def build_figure_pdf(out_path: Path) -> Path:
    """构造一份用于回归「图片 / 表格不翻译」的样本 PDF。

    第 1 页刻意做成"位图 + 矢量边框 + 图内文字标签 + 图题"的组合，用来验证：
    这些元素能被聚成一个保留区域，并且图内标签与图题都不会被送去翻译。
    第 2 页是一张**没有框线**的表格（booktabs 风格，PyMuPDF 检测不出来），
    靠"行距紧 + 与表题同栏"兜底识别，避免整张表被当成正文逐行翻译。
    """
    doc = fitz.open()

    # ---------------- 第 1 页：图片 ----------------
    page = doc.new_page(width=595, height=842)
    page.insert_textbox(
        fitz.Rect(56, 60, 540, 86),
        "The figure below summarises how the server fuses the local Gaussian fields.",
        fontsize=9,
    )
    # 位图：用纯色 pixmap 造一张"图"
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 240, 120))
    pix.set_rect(pix.irect, (232, 240, 254))
    page.insert_image(fitz.Rect(FIG_IMG_RECT), pixmap=pix)
    # 矢量边框（真实论文的插图常带边框/坐标轴）
    page.draw_rect(fitz.Rect(FIG_FRAME), color=(0.2, 0.2, 0.2), width=0.8)
    for i, label in enumerate(FIG_LABELS):
        page.insert_text(
            fitz.Point(FIG_IMG_RECT[0] + 12, FIG_IMG_RECT[1] + 26 + i * 22),
            label,
            fontsize=9,
        )
    page.insert_textbox(
        fitz.Rect(FIG_FRAME[0] - 6, FIG_FRAME[3] + 4, FIG_FRAME[2] + 6, FIG_FRAME[3] + 22),
        FIG_CAPTION,
        fontsize=8.5,
    )
    page.insert_textbox(fitz.Rect(56, FIG_FRAME[3] + 30, 540, FIG_FRAME[3] + 90), FIG_BODY, fontsize=9)

    # ---------------- 第 2 页：无框线表格 ----------------
    p2 = doc.new_page(width=595, height=842)
    p2.insert_textbox(
        fitz.Rect(RULELESS_X[0] - 4, RULELESS_TOP - 22, RULELESS_X[-1] + 90, RULELESS_TOP - 6),
        RULELESS_CAPTION,
        fontsize=9,
    )
    for r, row in enumerate(RULELESS_ROWS):
        y = RULELESS_TOP + r * RULELESS_LINE
        for c, cell in enumerate(row):
            p2.insert_text(fitz.Point(RULELESS_X[c], y), cell, fontsize=8)
    p2.insert_textbox(
        fitz.Rect(RULELESS_X[0], RULELESS_TOP + RULELESS_LINE * len(RULELESS_ROWS) + 24, 540, 420),
        "The comparison above shows that the proposed allocation reduces the photometric loss while "
        "slightly improving the peak signal-to-noise ratio and the structural similarity index.",
        fontsize=9,
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out_path))
    doc.close()
    return out_path


def verify(path: Path) -> list[str]:
    """校验生成结果：关键文本必须都在（防止 insert_textbox 静默丢内容）。"""
    missing = []
    with fitz.open(path) as doc:
        raw = "\n".join(doc[i].get_text("text") for i in range(doc.page_count))
    text = " ".join(raw.split())  # 折行会插入换行，统一空白后再比对
    for key, sample in {
        "标题": TITLE,
        "作者": "Phieu Nguyen Xuan",
        "摘要": "ABSTRACT: This survey",
        "关键词": "Keywords: Gaussian Splatting",
        "一级标题": "1.  INTRODUCTION",
        "二级标题": "2.  METHODOLOGY",
        "正文": "Simultaneous Localization and Mapping (SLAM) is a fundamental",
        "公式": "photometric residual",
        "图注": "Figure 1:",
        "参考文献": "[1] Kazerouni",
        "页边水印": "arXiv:2510.23881v1",
        "表格表题": TABLE_CAPTION,
        "表格符号格": "s_t in R3",
        "表格描述格": "Encoder and decoder functions",
    }.items():
        # 期望值也做同样的空白归一，否则 "1.  INTRODUCTION" 这类双空格会被误判为缺失
        if " ".join(sample.split()) not in text:
            missing.append(f"{key}（{sample[:24]}…）")
    return missing


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    if "--help" in flags or "-h" in flags:
        print(__doc__)
        print(
            "选项：\n"
            "  --spaceless   额外生成 samples/spaceless_sample.pdf（词间空格为独立 span，用于回归测试）\n"
            "  --figure      额外生成 samples/figure_sample.pdf（图片 + 无框线表格，用于回归「图表原样保留」）"
        )
        return 0

    out = Path(args[0]) if args else ROOT / "samples" / "sample_paper_en.pdf"
    path = build(out)
    missing = verify(path)
    print(f"已生成示例 PDF：{path}（{path.stat().st_size / 1024:.1f} KB）")
    if missing:
        print("  ⚠ 缺失文本：" + "、".join(missing), file=sys.stderr)
        return 1
    print("  内容自检：标题 / 摘要 / 双栏正文 / 公式 / 图注 / 参考文献 / 页边水印 / 表格 均存在 ✔")

    if "--spaceless" in flags:
        sp = build_spaceless_pdf(ROOT / "samples" / "spaceless_sample.pdf")
        print(f"已生成回归样本：{sp}（{sp.stat().st_size / 1024:.1f} KB）")
    if "--figure" in flags:
        fp = build_figure_pdf(ROOT / "samples" / "figure_sample.pdf")
        print(f"已生成回归样本：{fp}（{fp.stat().st_size / 1024:.1f} KB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
