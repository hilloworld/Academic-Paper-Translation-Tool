"""FastAPI 服务：文档解析 / 翻译任务 / 进度推送 / 静态前端。"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import queue
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .cache import TranslationCache
from .config import (
    PROVIDER_PRESETS,
    clear_runtime_override,
    get_settings,
    save_runtime_override,
    ui_payload_to_fields,
)
from .jobs import JobManager
from .pdf_parser import draw_debug_overlay
from .service import TranslationService
from .storage import DocNotFound, DocumentStore
from .translator import Translator, TranslationError

settings = get_settings()
store = DocumentStore(settings)
cache = TranslationCache(settings.cache_dir / "translations.db")
service = TranslationService(settings, store, cache)
jobs = JobManager()
executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="job")

app = FastAPI(title="论文 PDF 翻译 · 中英对照阅读器", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _err(status: int, message: str, detail: str = "") -> HTTPException:
    return HTTPException(status_code=status, detail={"error": message, "detail": detail[:800]})


def _to_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")


def _normalize(fields: dict) -> dict:
    """把界面传来的值规整为 Settings 期望的类型，类型错误时给出明确提示。"""
    out = dict(fields)
    casters = {
        "temperature": ("温度", lambda v: float(v)),
        "render_zoom": ("渲染倍率", lambda v: float(v)),
        "max_concurrency": ("并发数", lambda v: int(float(v))),
        "skip_reference_blocks": ("跳过参考文献", _to_bool),
        "translate_formula_blocks": ("翻译公式块", _to_bool),
        "translate_tables": ("翻译表格内容", _to_bool),
        "translate_captions": ("翻译图表标题", _to_bool),
        "inline_math_image": ("行内公式保留原图", _to_bool),
        "mock": ("模拟模式", _to_bool),
        "provider": ("服务商", lambda v: str(v).strip().lower()),
        "base_url": ("Base URL", lambda v: str(v).strip()),
        "model": ("模型名", lambda v: str(v).strip()),
        "api_key": ("API Key", lambda v: str(v).strip()),
        "source_lang": ("源语言", lambda v: str(v).strip()),
        "target_lang": ("目标语言", lambda v: str(v).strip()),
    }
    for key, (label, cast) in casters.items():
        if key not in out:
            continue
        try:
            out[key] = cast(out[key])
        except (TypeError, ValueError) as exc:
            raise _err(400, f"「{label}」的取值不合法：{out[key]!r}") from exc
    return out


@app.exception_handler(DocNotFound)
async def _doc_not_found(_: Request, exc: DocNotFound):
    return JSONResponse(status_code=404, content={"error": f"文档不存在：{exc}"})


# ----------------------------------------------------------------------
# 基础信息
# ----------------------------------------------------------------------
def _refresh_settings() -> None:
    """重新加载配置并「就地」写回既有 settings 对象。

    这样所有已持有 ``settings`` 引用的模块（store / cache / service）都能同步看到新值，
    同时丢弃缓存的翻译客户端，使新的 base_url / model / key 立刻生效。
    """
    fresh = get_settings(reload=True)
    for f in dataclasses.fields(fresh):
        setattr(settings, f.name, getattr(fresh, f.name))
    settings.ensure_dirs()
    service.reset_translator()


@app.get("/api/config")
def api_config():
    return {
        **settings.public(),
        "editable": settings.editable_public(),
        "cache": cache.stats(),
        "docsDir": str(settings.data_path),
    }


@app.post("/api/config/test")
async def api_config_test(request: Request):
    """连通性自检。可带待测试的临时配置（{provider,baseUrl,model,apiKey}）先试后存。"""
    probe = settings
    raw = await request.body()
    if raw:
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError as exc:
            raise _err(400, f"请求体不是合法 JSON：{exc}") from exc
        if isinstance(body, dict):
            fields, _ = ui_payload_to_fields(body)
            if fields:
                try:
                    probe = dataclasses.replace(settings, **_normalize(fields))
                except TypeError as exc:
                    raise _err(400, f"测试参数不合法：{exc}") from exc
    result = Translator(probe).test_connection()
    return JSONResponse(status_code=200 if result.get("ok") else 502, content=result)


@app.put("/api/config")
async def api_config_update(request: Request):
    """保存界面设置到 <DATA_DIR>/runtime-config.json（优先级最高），并立即生效。"""
    raw = await request.body()
    try:
        body = json.loads(raw.decode("utf-8") or "{}")
    except json.JSONDecodeError as exc:
        raise _err(400, f"请求体不是合法 JSON：{exc}") from exc
    if not isinstance(body, dict):
        raise _err(400, "请求体必须是 JSON 对象")

    fields, file_values = ui_payload_to_fields(body)
    if not fields:
        raise _err(400, "没有需要更新的配置项")

    provider = str(fields.get("provider") or settings.provider).strip().lower()
    if provider not in PROVIDER_PRESETS:
        raise _err(400, f"未知的服务商：{provider}（可选：{'、'.join(PROVIDER_PRESETS)}）")
    preset = PROVIDER_PRESETS.get(provider, {})
    # provider 变化时，未显式给出 base_url / model 则套用该服务商预设
    base_url = str(body.get("baseUrl") or "").strip() or preset.get("base_url", "")
    model = str(body.get("model") or "").strip() or preset.get("model", "")
    fields["provider"] = provider
    fields["base_url"] = base_url
    fields["model"] = model
    file_values["LLM_PROVIDER"] = provider
    file_values["LLM_BASE_URL"] = base_url
    file_values["LLM_MODEL"] = model

    try:
        probe = dataclasses.replace(settings, **_normalize(fields))
    except TypeError as exc:
        raise _err(400, f"配置项不合法：{exc}") from exc
    if not probe.base_url:
        raise _err(400, "Base URL 不能为空（服务商预设为空时需手动填写）")
    if not probe.model:
        raise _err(400, "模型名不能为空")
    if not probe.api_key and not probe.mock:
        raise _err(400, "还没有可用的 API Key：请在「API Key」中填写，或勾选模拟模式（LLM_MOCK=1）先跑通链路。")

    provider_changed = provider != settings.provider
    save_runtime_override(file_values)
    _refresh_settings()

    # 容易踩坑的两种情况，明确提醒（不阻塞保存）
    extra: list[str] = []
    if provider_changed and "api_key" not in fields and settings.api_key:
        extra.append(
            f"服务商已切换为 {provider}，但「API Key」留空，仍在沿用之前保存的密钥，"
            "请确认它对新的服务商有效，否则会报 401 鉴权失败。"
        )
    if "render_zoom" in fields:
        extra.append("页面渲染倍率已变更：需要重新导入（或重新解析）该 PDF，才会按新倍率重新出图。")

    return {
        "ok": True,
        "savedTo": settings.override_file,
        "overriddenKeys": settings.overridden_keys,
        "config": settings.public(),
        "warnings": settings.warnings + extra,
    }


@app.delete("/api/config/override")
def api_config_override_clear():
    """清除界面设置，回落到 .env / config.json / 环境变量。"""
    removed = clear_runtime_override()
    _refresh_settings()
    return {
        "ok": True,
        "removed": removed,
        "overrideFile": settings.override_file,
        "config": settings.public(),
        "warnings": settings.warnings,
    }


@app.get("/api/health")
def api_health():
    return {"ok": True, "pageCount": 0}


# ----------------------------------------------------------------------
# 文档
# ----------------------------------------------------------------------
@app.post("/api/documents")
async def api_upload(request: Request, filename: str = Query(default=""), force: bool = Query(default=False)):
    """直接以二进制 body 上传 PDF：`curl --data-binary @x.pdf "…/api/documents?filename=x.pdf"`"""
    data = await request.body()
    if not data:
        raise _err(400, "请求体为空，请以二进制方式上传 PDF 内容（Content-Type: application/pdf）")
    try:
        return store.import_pdf(filename or "document.pdf", data, force_reparse=force)
    except ValueError as exc:
        raise _err(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise _err(500, f"PDF 解析失败：{exc.__class__.__name__}: {exc}") from exc


@app.get("/api/documents")
def api_documents():
    return {"documents": store.list_docs()}


@app.get("/api/documents/{doc_id}")
def api_document(doc_id: str):
    try:
        return store.summary(doc_id)
    except DocNotFound as exc:
        raise _err(404, f"文档不存在：{doc_id}") from exc


@app.post("/api/documents/{doc_id}/reparse")
def api_reparse(doc_id: str):
    """用当前解析器重新解析已导入的 PDF（解析逻辑升级后需要）。"""
    try:
        return {"ok": True, "document": store.reparse(doc_id)}
    except DocNotFound as exc:
        raise _err(404, f"文档不存在：{doc_id}") from exc
    except ValueError as exc:
        raise _err(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise _err(500, f"重新解析失败：{exc.__class__.__name__}: {exc}") from exc


@app.delete("/api/documents/{doc_id}")
def api_delete_document(doc_id: str, drop_cache: bool = Query(default=False)):
    store.delete_doc(doc_id)
    removed = cache.clear(doc_id) if drop_cache else 0
    return {"ok": True, "removedCacheRows": removed}


@app.get("/api/documents/{doc_id}/pages/{page_no}")
def api_page(doc_id: str, page_no: int):
    try:
        return service.page_payload(doc_id, page_no)
    except DocNotFound as exc:
        raise _err(404, f"文档不存在：{doc_id}") from exc
    except IndexError as exc:
        raise _err(400, str(exc)) from exc


@app.get("/api/documents/{doc_id}/plan")
def api_plan(doc_id: str, pages: str = Query(default="all"), force: bool = Query(default=False)):
    try:
        page_list = _parse_pages(pages, store.meta(doc_id)["pageCount"])
    except DocNotFound as exc:
        raise _err(404, f"文档不存在：{doc_id}") from exc
    except ValueError as exc:
        raise _err(400, str(exc)) from exc
    return service.plan(doc_id, page_list, force)


@app.get("/api/debug/overlay/{doc_id}/{page_no}")
def api_overlay(doc_id: str, page_no: int):
    """生成带块框与阅读顺序编号的调试图，用于人工校验坐标。"""
    try:
        doc_dir = store.doc_dir(doc_id)
        if not (doc_dir / "meta.json").exists():
            raise DocNotFound(f"文档不存在：{doc_id}")
        out = settings.debug_dir / f"{doc_id}-p{page_no:04d}-overlay.png"
        draw_debug_overlay(doc_dir, page_no, out)
        return FileResponse(out, media_type="image/png")
    except DocNotFound as exc:
        raise _err(404, str(exc)) from exc


# ----------------------------------------------------------------------
# 翻译任务
# ----------------------------------------------------------------------
@app.post("/api/documents/{doc_id}/translate")
async def api_translate(doc_id: str, request: Request):
    body = {}
    raw = await request.body()
    if raw:
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError as exc:
            raise _err(400, f"请求体不是合法 JSON：{exc}") from exc

    try:
        meta = store.meta(doc_id)
    except DocNotFound as exc:
        raise _err(404, f"文档不存在：{doc_id}") from exc

    page_spec = body.get("pages", "all")
    force = bool(body.get("force", False))
    try:
        page_list = _parse_pages(page_spec, meta["pageCount"])
    except ValueError as exc:
        raise _err(400, str(exc)) from exc
    if not page_list:
        raise _err(400, "没有需要翻译的页面")

    if not settings.mock and not settings.api_key:
        raise _err(
            400,
            "未配置 LLM_API_KEY，无法调用翻译服务。请在项目根目录复制 .env.example 为 .env 并填写密钥"
            "（或设置 LLM_MOCK=1 使用模拟译文先跑通链路）。",
        )
    if not settings.base_url or not settings.model:
        raise _err(400, "未配置 LLM_BASE_URL / LLM_MODEL，无法调用翻译服务。")

    plan = service.plan(doc_id, page_list, force)
    job = jobs.create(doc_id, page_list, force)
    job.add_log(f"任务已创建（待翻译页 {len(page_list)}，其中 {len(plan['needsApi'])} 页需要请求模型）")
    executor.submit(service.run_job, job)
    return {"jobId": job.id, "plan": plan, "snapshot": job.snapshot()}


@app.get("/api/jobs")
def api_jobs():
    return {"jobs": jobs.list()}


@app.get("/api/jobs/{job_id}")
def api_job(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise _err(404, f"任务不存在：{job_id}")
    return job.snapshot()


@app.post("/api/jobs/{job_id}/cancel")
def api_job_cancel(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise _err(404, f"任务不存在：{job_id}")
    job.cancel()
    return {"ok": True, "snapshot": job.snapshot()}


@app.get("/api/jobs/{job_id}/events")
async def api_job_events(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise _err(404, f"任务不存在：{job_id}")

    async def gen():
        q = job.subscribe()
        try:
            while True:
                try:
                    snap = await asyncio.to_thread(q.get, True, 15.0)
                except queue.Empty:
                    yield ": keep-alive\n\n"
                    continue
                yield f"data: {json.dumps(snap, ensure_ascii=False)}\n\n"
                if snap["status"] in ("done", "failed", "cancelled"):
                    break
        finally:
            job.unsubscribe(q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


# ----------------------------------------------------------------------
# 缓存
# ----------------------------------------------------------------------
@app.get("/api/cache/stats")
def api_cache_stats():
    return cache.stats()


@app.delete("/api/cache")
def api_cache_clear(doc_id: str | None = Query(default=None)):
    return {"ok": True, "removed": cache.clear(doc_id)}


# ----------------------------------------------------------------------
# 静态资源
# ----------------------------------------------------------------------
def _parse_pages(spec, total: int) -> list[int]:
    """支持 "all" / "3" / "1-14" / "1,3,5-7" 。"""
    if spec is None:
        return list(range(1, total + 1))
    if isinstance(spec, list):
        pages = {int(p) for p in spec}
    else:
        text = str(spec).strip().lower()
        if text in ("", "all", "*"):
            return list(range(1, total + 1))
        pages = set()
        for part in text.replace("，", ",").split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                a, _, b = part.partition("-")
                try:
                    start, end = int(a), int(b)
                except ValueError as exc:
                    raise ValueError(f"页码范围格式错误：'{part}'，示例 1-14 或 1,3,5-7") from exc
                if start > end:
                    start, end = end, start
                pages.update(range(start, end + 1))
            else:
                try:
                    pages.add(int(part))
                except ValueError as exc:
                    raise ValueError(f"页码格式错误：'{part}'") from exc
    bad = sorted(p for p in pages if p < 1 or p > total)
    if bad:
        raise ValueError(f"页码超出范围（1-{total}）：{bad}")
    return sorted(pages)


if settings.frontend_dir.exists():
    app.mount("/static", StaticFiles(directory=str(settings.frontend_dir)), name="static")
app.mount("/files", StaticFiles(directory=str(settings.data_path)), name="files")


@app.get("/")
def index():
    idx = settings.frontend_dir / "index.html"
    if not idx.exists():
        return JSONResponse(status_code=500, content={"error": f"前端文件缺失：{idx}"})
    return FileResponse(idx, media_type="text/html")
