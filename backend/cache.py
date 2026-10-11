"""本地翻译缓存（SQLite）。

两张表：
* ``translations``：以「内容指纹」为主键的译文仓库，换模型/换提示词版本自动失效，
  相同段落跨页、跨文档复用，杜绝重复调用 API；
* ``page_index``：记录「某文档某页某块 -> 内容指纹」的映射，用于整页取回译文。
"""
from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from .config import PROMPT_VERSION

_SCHEMA = """
CREATE TABLE IF NOT EXISTS translations (
    cache_key  TEXT PRIMARY KEY,
    provider   TEXT,
    model      TEXT,
    src        TEXT NOT NULL,
    tgt        TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS page_index (
    doc_id     TEXT NOT NULL,
    page_no    INTEGER NOT NULL,
    block_id   TEXT NOT NULL,
    cache_key  TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (doc_id, page_no, block_id)
);
CREATE INDEX IF NOT EXISTS idx_index_page ON page_index(doc_id, page_no);
"""


def make_key(provider: str, model: str, src_lang: str, tgt_lang: str, text: str) -> str:
    payload = "\x1f".join([PROMPT_VERSION, provider, model, src_lang, tgt_lang, text.strip()])
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


class TranslationCache:
    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    def get_many(self, keys: list[str]) -> dict[str, str]:
        """按内容指纹批量取译文（含跨文档复用）。"""
        if not keys:
            return {}
        out: dict[str, str] = {}
        with self._lock, self._conn() as conn:
            for i in range(0, len(keys), 400):
                chunk = keys[i : i + 400]
                marks = ",".join("?" * len(chunk))
                for r in conn.execute(
                    f"SELECT cache_key, tgt FROM translations WHERE cache_key IN ({marks})", chunk
                ):
                    out[r["cache_key"]] = r["tgt"]
        return out

    def put_many(self, rows: list[tuple]) -> int:
        """rows: (cache_key, provider, model, src, tgt)"""
        if not rows:
            return 0
        now = time.time()
        with self._lock, self._conn() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO translations"
                " (cache_key, provider, model, src, tgt, created_at) VALUES (?,?,?,?,?,?)",
                [(*r, now) for r in rows],
            )
        return len(rows)

    def index_page(self, doc_id: str, page_no: int, mapping: dict[str, str]) -> int:
        """mapping: {block_id: cache_key}"""
        if not mapping:
            return 0
        now = time.time()
        with self._lock, self._conn() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO page_index (doc_id, page_no, block_id, cache_key, updated_at)"
                " VALUES (?,?,?,?,?)",
                [(doc_id, page_no, bid, key, now) for bid, key in mapping.items()],
            )
        return len(mapping)

    def get_page(self, doc_id: str, page_no: int) -> dict[str, str]:
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "SELECT g.block_id AS block_id, t.tgt AS tgt FROM page_index g"
                " JOIN translations t ON t.cache_key = g.cache_key"
                " WHERE g.doc_id=? AND g.page_no=?",
                (doc_id, page_no),
            ).fetchall()
        return {r["block_id"]: r["tgt"] for r in rows}

    def stats(self) -> dict:
        with self._lock, self._conn() as conn:
            total = conn.execute("SELECT COUNT(*) AS c FROM translations").fetchone()["c"]
            indexed = conn.execute("SELECT COUNT(*) AS c FROM page_index").fetchone()["c"]
            docs = conn.execute("SELECT COUNT(DISTINCT doc_id) AS c FROM page_index").fetchone()["c"]
        size = self.db_path.stat().st_size if self.db_path.exists() else 0
        return {
            "entries": total,
            "indexedBlocks": indexed,
            "documents": docs,
            "dbBytes": size,
            "dbPath": str(self.db_path),
        }

    def clear(self, doc_id: str | None = None) -> int:
        with self._lock, self._conn() as conn:
            if doc_id:
                cur = conn.execute("DELETE FROM page_index WHERE doc_id=?", (doc_id,))
                return cur.rowcount
            n = conn.execute("DELETE FROM translations").rowcount
            conn.execute("DELETE FROM page_index")
            return n
