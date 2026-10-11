"""翻译编排：分页调度 + 缓存命中 + 增量落库 + 任务进度上报。"""
from __future__ import annotations

import threading
import time

from .cache import TranslationCache, make_key
from .config import Settings
from .jobs import Job
from .pdf_parser import block_is_translatable
from .storage import DocumentStore
from .translator import Translator, TranslationError


class TranslationService:
    def __init__(self, settings: Settings, store: DocumentStore, cache: TranslationCache):
        self.s = settings
        self.store = store
        self.cache = cache
        self._translator_lock = threading.Lock()
        self._translator: Translator | None = None

    @property
    def translator(self) -> Translator:
        with self._translator_lock:
            if self._translator is None:
                self._translator = Translator(self.s)
            return self._translator

    def reset_translator(self) -> None:
        """配置变更后丢弃缓存的客户端，使新的 base_url / model / key 立即生效。"""
        with self._translator_lock:
            self._translator = None

    # ------------------------------------------------------------------
    def translatable_blocks(self, doc_id: str, page_no: int) -> list[dict]:
        page = self.store.page(doc_id, page_no)
        # 按「当前配置」动态判断，切换表格/公式/标题/参考文献开关后无需重新解析
        return [
            b
            for b in page["blocks"]
            if b["text"].strip() and block_is_translatable(b, self.s)
        ]

    def plan(self, doc_id: str, pages: list[int], force: bool = False) -> dict:
        """统计：哪些页已有缓存、哪些页需要调用 API。"""
        provider, model = self.s.provider, self.s.model
        result: dict = {"pages": [], "needsApi": [], "cachedPages": [], "totalBlocks": 0, "cachedBlocks": 0}
        for page_no in pages:
            blocks = self.translatable_blocks(doc_id, page_no)
            keys = [make_key(provider, model, self.s.source_lang, self.s.target_lang, b["text"]) for b in blocks]
            hits = 0 if force else len(self.cache.get_many(keys))
            state = "cached" if (blocks and hits == len(blocks) and not force) else "pending"
            result["pages"].append(
                {"pageNo": page_no, "blocks": len(blocks), "cached": hits, "state": state or "empty"}
            )
            result["totalBlocks"] += len(blocks)
            result["cachedBlocks"] += hits
            if state == "pending" and blocks:
                result["needsApi"].append(page_no)
            elif blocks:
                result["cachedPages"].append(page_no)
        return result

    def page_payload(self, doc_id: str, page_no: int) -> dict:
        """页面结构 + 已缓存译文 + 保留区域，一次性给前端。

        约定：**不可译的片段一律不渲染文字**——右栏以原页渲染图为底，
        图片 / 表格 / 公式 / 参考文献以及图内的文字标签都靠"透出原图"来呈现，
        只有可译片段才用白底译文覆盖。表格单元格在「翻译表格内容」关闭时
        整块隐藏（``hidden``），于是整张表原样可见。

        译文按「当前 provider/model/语言/提示词版本」重新计算缓存键后取回，
        而不是直接读 page_index 里的旧映射。这样在「⚙ 设置」里换了模型之后，
        右侧不会继续显示旧模型留下的译文，而是回到虚线占位框，等待按新模型重译。
        """
        page = self.store.page(doc_id, page_no)
        provider, model = self.s.provider, self.s.model
        hide_tables = not bool(self.s.translate_tables)
        key_of: dict[str, str] = {}
        for b in page["blocks"]:
            if b["text"].strip() and block_is_translatable(b, self.s):
                key_of[b["id"]] = make_key(provider, model, self.s.source_lang, self.s.target_lang, b["text"])
        hits = self.cache.get_many(list(key_of.values())) if key_of else {}

        blocks = []
        for b in page["blocks"]:
            item = dict(b)
            item["translatable"] = block_is_translatable(b, self.s)  # 以当前配置为准
            item["translation"] = hits.get(key_of.get(b["id"], ""), "")
            item["hidden"] = bool(b.get("table")) and hide_tables
            blocks.append(item)

        translatable = [b for b in blocks if b["translatable"] and b["text"].strip()]
        done = sum(1 for b in translatable if b["translation"])
        regions = page.get("regions", [])
        return {
            "pageNo": page_no,
            "width": page["width"],
            "height": page["height"],
            "image": page["image"],
            "imageWidth": page["imageWidth"],
            "imageHeight": page["imageHeight"],
            "regions": regions,
            "blocks": blocks,
            "stats": {
                "blocks": len(blocks),
                "translatable": len(translatable),
                "translated": done,
                # 不翻译、直接透出原始版面的片段（公式 / 参考文献 / 图表标题 / 图内标签）
                "kept": sum(
                    1 for b in blocks if b["text"].strip() and not b["translatable"] and not b["hidden"]
                ),
                "hidden": sum(1 for b in blocks if b["hidden"]),
                "figures": sum(1 for r in regions if r.get("type") == "figure"),
                "tables": sum(1 for r in regions if r.get("type") == "table"),
                "formulas": sum(1 for b in blocks if b["kind"] == "formula"),
                "translatedRatio": round(done / len(translatable), 3) if translatable else 0.0,
            },
        }

    # ------------------------------------------------------------------
    def run_job(self, job: Job) -> Job:
        """在后台线程中执行一个翻译任务。"""
        job.status = "running"
        job.started_at = time.time()
        job.add_log(f"开始翻译：第 {job.pages[0]}–{job.pages[-1]} 页，共 {len(job.pages)} 页")

        provider, model = self.s.provider, self.s.model
        cancelled = False
        try:
            for page_no in job.pages:
                if job.cancel_event.is_set():
                    cancelled = True
                    break
                st = job.pages_state[page_no]
                st.state = "running"
                t0 = time.time()
                blocks = self.translatable_blocks(job.doc_id, page_no)
                st.blocks = len(blocks)
                st.chars = sum(len(b["text"]) for b in blocks)

                if not blocks:
                    st.state = "skipped"
                    job.add_log(f"第 {page_no} 页：无可翻译文本块，跳过")
                    continue

                job.totals["blocks"] += len(blocks)
                job.broadcast()

                keys = [make_key(provider, model, self.s.source_lang, self.s.target_lang, b["text"]) for b in blocks]
                cached = {} if job.force else self.cache.get_many(keys)
                index_map: dict[str, str] = {}
                pending: list[dict] = []
                for b, key in zip(blocks, keys):
                    if key in cached:
                        index_map[b["id"]] = key
                        st.cached += 1
                        st.translated += 1
                        job.totals["cached"] += 1
                        job.totals["translated"] += 1
                    else:
                        pending.append({"id": b["id"], "text": b["text"], "key": key})

                if index_map:
                    self.cache.index_page(job.doc_id, page_no, index_map)
                if st.cached:
                    job.add_log(f"第 {page_no} 页：{st.cached} 个片段命中本地缓存")
                job.broadcast()

                if pending:
                    job.add_log(f"第 {page_no} 页：{len(pending)} 个片段待调用模型")

                    def on_chunk_done(done_chunks, total_chunks, partial, page_no=page_no, st=st):
                        job.totals["chunksDone"] += 1
                        job.totals["translated"] += len(partial)
                        job.totals["apiCalls"] = self.translator.stats["requests"]
                        st.translated += len(partial)
                        # 模拟模式（LLM_MOCK）产出的只是占位文本，绝不能写进正式缓存，
                        # 否则之后切回真实模型时会把「【模拟译文…】」当成正经译文显示出来。
                        if partial and not self.s.mock:
                            rows = []
                            idx = {}
                            by_id = {b["id"]: b for b in blocks}
                            for bid, tgt in partial.items():
                                blk = by_id.get(bid)
                                if not blk:
                                    continue
                                key = make_key(
                                    provider, model, self.s.source_lang, self.s.target_lang, blk["text"]
                                )
                                rows.append((key, provider, model, blk["text"], tgt))
                                idx[bid] = key
                            self.cache.put_many(rows)
                            self.cache.index_page(job.doc_id, page_no, idx)
                        job.broadcast()

                    results, errors = self.translator.translate(
                        pending,
                        on_chunk_done=on_chunk_done,
                        should_cancel=job.cancel_event.is_set,
                    )
                    failed_ids = set()
                    for err in errors:
                        failed_ids.update(err.get("ids", []))
                        job.errors.append({"pageNo": page_no, **err})
                    st.failed += len(failed_ids)
                    job.totals["failed"] += len(failed_ids)
                    for err in errors[-2:]:
                        job.add_log(f"第 {page_no} 页翻译出错：{err.get('error', '未知错误')}", "error")

                st.seconds = time.time() - t0
                if st.failed and st.translated:
                    st.state = "partial"
                elif st.failed and not st.translated:
                    st.state = "failed"
                else:
                    st.state = "done"
                job.totals["tokens"] = self.translator.stats["prompt_tokens"] + self.translator.stats["completion_tokens"]
                job.add_log(
                    f"第 {page_no} 页完成：{st.translated}/{st.blocks} 个片段（缓存 {st.cached}，失败 {st.failed}），耗时 {st.seconds:.1f}s"
                )

            if cancelled:
                job.status = "cancelled"
                job.add_log("任务已取消（已完成的页面已写入缓存）", "warn")
            elif job.totals["failed"] and not job.totals["translated"]:
                job.status = "failed"
                first_err = job.errors[0]["error"] if job.errors else "未知错误"
                job.add_log(f"翻译失败：{first_err}", "error")
            else:
                job.status = "done"
                job.add_log(
                    f"翻译完成：{job.totals['translated']} 个片段（缓存命中 {job.totals['cached']}，失败 {job.totals['failed']}），"
                    f"API 调用 {self.translator.stats['requests']} 次"
                )
        except TranslationError as exc:
            job.status = "failed"
            job.errors.append({"pageNo": None, "error": str(exc), **exc.to_dict()})
            job.add_log(f"翻译中断：{exc}", "error")
        except Exception as exc:  # noqa: BLE001
            job.status = "failed"
            job.errors.append({"pageNo": None, "error": f"{exc.__class__.__name__}: {exc}"})
            job.add_log(f"翻译异常中断：{exc.__class__.__name__}: {exc}", "error")
        finally:
            job.finished_at = time.time()
            job.broadcast()
        return job
