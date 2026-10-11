"""文档存取：PDF 落盘、解析结果读写、文档列表。"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from .config import Settings
from .pdf_parser import doc_id_for_bytes, load_meta, load_page, parse_pdf


class DocNotFound(FileNotFoundError):
    pass


class DocumentStore:
    def __init__(self, settings: Settings):
        self.s = settings
        self.s.ensure_dirs()

    # ------------------------------------------------------------------
    def doc_dir(self, doc_id: str) -> Path:
        return self.s.docs_dir / doc_id

    def pdf_path(self, doc_id: str) -> Path:
        return self.s.uploads_dir / f"{doc_id}.pdf"

    def _doc_json(self, doc_id: str) -> Path:
        return self.doc_dir(doc_id) / "doc.json"

    def read_doc_json(self, doc_id: str) -> dict:
        p = self._doc_json(doc_id)
        if not p.exists():
            raise DocNotFound(f"文档 {doc_id} 不存在")
        return json.loads(p.read_text(encoding="utf-8"))

    # ------------------------------------------------------------------
    def reparse(self, doc_id: str) -> dict:
        """用当前版本的解析器重新解析一份已导入的 PDF。

        解析逻辑升级后（例如新增了行碎片缝合），既有文档的 ``page-*.json`` 还是旧
        结构，右侧排版就会与新版前端对不上。译文缓存不受影响：缓存键只含译文本身
        的文本与模型，重新解析后文本没变的片段照样命中缓存。
        """
        pdf = self.pdf_path(doc_id)
        if not pdf.exists():
            raise DocNotFound(f"文档 {doc_id} 的原始 PDF 不存在")
        try:
            name = self.read_doc_json(doc_id).get("filename") or pdf.name
        except DocNotFound:
            name = pdf.name
        return self.import_pdf(name, pdf.read_bytes(), force_reparse=True)

    def import_pdf(self, filename: str, data: bytes, force_reparse: bool = False) -> dict:
        """保存 PDF 并解析（同一份文件自动复用既有解析结果）。"""
        if not data:
            raise ValueError("上传内容为空")
        if not data[:5] == b"%PDF-":
            raise ValueError("这不是一个有效的 PDF 文件（缺少 %PDF- 文件头）")

        doc_id = doc_id_for_bytes(data)
        pdf_path = self.pdf_path(doc_id)
        doc_dir = self.doc_dir(doc_id)
        doc_dir.mkdir(parents=True, exist_ok=True)
        if not pdf_path.exists() or force_reparse:
            pdf_path.write_bytes(data)

        meta_path = doc_dir / "meta.json"
        need_parse = force_reparse or not meta_path.exists()
        if need_parse:
            if force_reparse and (doc_dir / "pages").exists():
                shutil.rmtree(doc_dir / "pages", ignore_errors=True)
            parse_pdf(pdf_path, doc_dir, zoom=self.s.render_zoom, settings=self.s)

        prev: dict = {}
        if self._doc_json(doc_id).exists():
            try:
                prev = json.loads(self._doc_json(doc_id).read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                prev = {}
        doc = {
            "docId": doc_id,
            "filename": filename or prev.get("filename") or f"{doc_id}.pdf",
            "sizeBytes": len(data),
            "createdAt": prev.get("createdAt") or time.time(),
            "parsedAt": time.time() if need_parse else (prev.get("parsedAt") or time.time()),
            "reparsed": need_parse,
        }
        self._doc_json(doc_id).write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        return self.summary(doc_id)

    # ------------------------------------------------------------------
    def summary(self, doc_id: str) -> dict:
        doc = self.read_doc_json(doc_id)
        meta = load_meta(self.doc_dir(doc_id))
        return {
            **doc,
            "pageCount": meta["pageCount"],
            "renderZoom": meta.get("renderZoom", self.s.render_zoom),
            "parseSeconds": meta.get("parseSeconds"),
            "pages": meta["pages"],
            "pdfMeta": meta.get("pdfMeta", {}),
        }

    def meta(self, doc_id: str) -> dict:
        if not (self.doc_dir(doc_id) / "meta.json").exists():
            raise DocNotFound(f"文档 {doc_id} 的解析结果不存在")
        return load_meta(self.doc_dir(doc_id))

    def page(self, doc_id: str, page_no: int) -> dict:
        meta = self.meta(doc_id)
        if page_no < 1 or page_no > meta["pageCount"]:
            raise IndexError(f"页码 {page_no} 超出范围（1-{meta['pageCount']}）")
        return load_page(self.doc_dir(doc_id), page_no)

    def list_docs(self) -> list[dict]:
        out = []
        for d in sorted(self.s.docs_dir.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True):
            if not d.is_dir() or not (d / "doc.json").exists():
                continue
            try:
                out.append(self.summary(d.name))
            except Exception:  # noqa: BLE001
                continue
        return out

    def delete_doc(self, doc_id: str, drop_cache: bool = False) -> None:
        shutil.rmtree(self.doc_dir(doc_id), ignore_errors=True)
        self.pdf_path(doc_id).unlink(missing_ok=True)
