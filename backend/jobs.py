"""翻译任务管理：后台线程执行 + 进度广播（供 SSE 订阅）。"""
from __future__ import annotations

import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class PageState:
    page_no: int
    state: str = "pending"  # pending|running|done|partial|failed|skipped
    blocks: int = 0
    translated: int = 0
    cached: int = 0
    failed: int = 0
    chars: int = 0
    seconds: float = 0.0

    def to_dict(self) -> dict:
        return {
            "pageNo": self.page_no,
            "state": self.state,
            "blocks": self.blocks,
            "translated": self.translated,
            "cached": self.cached,
            "failed": self.failed,
            "chars": self.chars,
            "seconds": round(self.seconds, 2),
        }


@dataclass
class Job:
    id: str
    doc_id: str
    pages: list[int]
    force: bool = False
    status: str = "pending"  # pending|running|done|failed|cancelled
    message: str = ""
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    totals: dict = field(
        default_factory=lambda: {
            "pages": 0,
            "blocks": 0,
            "translated": 0,
            "cached": 0,
            "failed": 0,
            "apiCalls": 0,
            "chunks": 0,
            "chunksDone": 0,
            "tokens": 0,
        }
    )
    pages_state: dict[int, PageState] = field(default_factory=dict)
    errors: list[dict] = field(default_factory=list)
    log: list[dict] = field(default_factory=list)
    cancel_event: threading.Event = field(default_factory=threading.Event)
    _subscribers: list[queue.Queue] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # ------------------------------------------------------------------
    def snapshot(self) -> dict:
        return {
            "jobId": self.id,
            "docId": self.doc_id,
            "status": self.status,
            "message": self.message,
            "pages": self.pages,
            "force": self.force,
            "createdAt": self.created_at,
            "startedAt": self.started_at,
            "finishedAt": self.finished_at,
            "durationSec": round((self.finished_at or time.time()) - (self.started_at or self.created_at), 2),
            "totals": dict(self.totals),
            "pageStates": [self.pages_state[p].to_dict() for p in sorted(self.pages_state)],
            "errors": self.errors[-20:],
            "log": self.log[-30:],
        }

    def broadcast(self) -> None:
        snap = self.snapshot()
        with self._lock:
            for q in list(self._subscribers):
                try:
                    q.put_nowait(snap)
                except queue.Full:
                    try:
                        q.get_nowait()
                        q.put_nowait(snap)
                    except queue.Empty:
                        pass

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=64)
        with self._lock:
            self._subscribers.append(q)
        q.put(self.snapshot())
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    # ------------------------------------------------------------------
    def add_log(self, message: str, level: str = "info") -> None:
        self.log.append({"t": time.time(), "level": level, "message": message})
        self.message = message
        self.broadcast()

    def cancel(self) -> None:
        self.cancel_event.set()
        self.add_log("收到取消请求，正在停止……", "warn")


class JobManager:
    def __init__(self, max_jobs: int = 30):
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._max_jobs = max_jobs

    def create(self, doc_id: str, pages: list[int], force: bool = False) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], doc_id=doc_id, pages=list(pages), force=force)
        for p in pages:
            job.pages_state[p] = PageState(page_no=p)
        job.totals["pages"] = len(pages)
        with self._lock:
            self._jobs[job.id] = job
            if len(self._jobs) > self._max_jobs:
                for old in sorted(self._jobs.values(), key=lambda j: j.created_at)[: -self._max_jobs]:
                    self._jobs.pop(old.id, None)
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list(self) -> list[dict]:
        return [j.snapshot() for j in sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)]

    def any_running(self, doc_id: str | None = None) -> bool:
        return any(
            j.status in ("pending", "running") and (doc_id is None or j.doc_id == doc_id)
            for j in self._jobs.values()
        )
