"""云端大模型翻译客户端（OpenAI 兼容 /chat/completions 协议）。

* 默认服务商 DeepSeek，可通过环境变量或配置文件切换到任意同类服务；
* 按字符预算把文本片段分块，块内携带 id，便于把译文精确放回原文坐标；
* 429 / 5xx / 网络超时自动指数退避重试，其它错误抛出带明确提示的 TranslationError。
"""
from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from .config import Settings

USER_TEMPLATE_HINT = (
    '只输出 JSON，格式为 {"translations":[{"id":"片段id","text":"中文译文"}]}，'
    "id 必须与输入一一对应且不能缺失，不要输出任何解释性文字。"
)


class TranslationError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False, detail: str = ""):
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        self.detail = detail

    def to_dict(self) -> dict:
        return {"message": str(self), "status": self.status, "detail": self.detail[:500]}


# ----------------------------------------------------------------------
def chunk_segments(segments: list[dict], max_chars: int, max_segments: int) -> list[list[dict]]:
    """按字符预算与片段数上限切块，保持阅读顺序。"""
    chunks: list[list[dict]] = []
    cur: list[dict] = []
    size = 0
    for seg in segments:
        text = seg["text"]
        n = len(text)
        if cur and (size + n > max_chars or len(cur) >= max_segments):
            chunks.append(cur)
            cur, size = [], 0
        cur.append(seg)
        size += n
    if cur:
        chunks.append(cur)
    return chunks


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"```\s*$", "", text).strip()
    return text


def parse_translation_payload(content) -> dict[str, str]:
    """把模型返回的内容解析成 {id: 译文}，容忍代码围栏与多余前后缀。"""
    if isinstance(content, list):  # 部分服务商返回分段数组
        content = "".join(part.get("text", "") if isinstance(part, dict) else str(part) for part in content)
    raw = _strip_fences(str(content or ""))
    data = None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        for opener, closer in (("{", "}"), ("[", "]")):
            i, j = raw.find(opener), raw.rfind(closer)
            if i != -1 and j > i:
                try:
                    data = json.loads(raw[i : j + 1])
                    break
                except json.JSONDecodeError:
                    continue
    if data is None:
        raise TranslationError(
            "模型返回内容不是合法 JSON，无法解析译文。",
            detail=raw[:500],
        )

    items = data.get("translations") if isinstance(data, dict) else data
    if isinstance(items, dict):
        items = [{"id": k, "text": v} for k, v in items.items()]
    if not isinstance(items, list):
        raise TranslationError("模型返回的 JSON 结构不符合约定（缺少 translations 数组）。", detail=raw[:500])

    out: dict[str, str] = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        sid = it.get("id") or it.get("segment_id") or it.get("key")
        txt = it.get("text")
        if txt is None:
            txt = it.get("translation") or it.get("target") or ""
        if sid is None:
            continue
        out[str(sid).strip()] = str(txt).strip()
    if not out:
        raise TranslationError("模型返回的 JSON 中没有任何有效译文。", detail=raw[:500])
    return out


class Translator:
    def __init__(self, settings: Settings):
        self.s = settings
        self.base_url = (settings.base_url or "").rstrip("/")
        self.model = settings.model
        self._json_mode = settings.json_mode
        self._lock = threading.Lock()
        self.stats = {"requests": 0, "failures": 0, "retries": 0, "prompt_tokens": 0, "completion_tokens": 0}

    # ------------------------------------------------------------------
    @property
    def chat_url(self) -> str:
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return f"{self.base_url}/chat/completions"

    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.s.api_key}",
        }

    def _build_messages(self, segments: list[dict]) -> list[dict]:
        payload = {
            "task": "把下列论文文本片段翻译成目标语言",
            "source_language": self.s.source_lang,
            "target_language": self.s.target_lang,
            "segments": [{"id": s["id"], "text": s["text"]} for s in segments],
        }
        return [
            {"role": "system", "content": self.s.system_prompt_text},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False) + "\n\n" + USER_TEMPLATE_HINT},
        ]

    # ------------------------------------------------------------------
    def _post(self, body: dict) -> dict:
        try:
            resp = requests.post(
                self.chat_url,
                headers=self._headers(),
                data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                timeout=(15, self.s.timeout),
            )
        except requests.exceptions.ConnectTimeout as exc:
            raise TranslationError(
                f"连接 {self.base_url} 超时，请检查网络或代理设置。", retryable=True, detail=str(exc)
            ) from exc
        except requests.exceptions.ReadTimeout as exc:
            raise TranslationError(
                f"等待模型响应超时（>{self.s.timeout:.0f}s），可调大 LLM_TIMEOUT 或减小 LLM_BATCH_CHARS。",
                retryable=True,
                detail=str(exc),
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise TranslationError(
                f"无法连接 {self.base_url}：{exc.__class__.__name__}。请检查 Base URL、网络或代理。",
                retryable=True,
                detail=str(exc),
            ) from exc

        if resp.status_code >= 400:
            raise self._http_error(resp)
        try:
            return resp.json()
        except ValueError as exc:
            raise TranslationError("接口返回的不是 JSON。", status=resp.status_code, detail=resp.text[:500]) from exc

    def _http_error(self, resp: requests.Response) -> TranslationError:
        code = resp.status_code
        body = resp.text or ""
        try:
            err = resp.json()
            msg = err.get("error", {}).get("message") if isinstance(err.get("error"), dict) else err.get("message")
            if msg:
                body = f"{msg} | {body}"
        except Exception:  # noqa: BLE001
            pass
        hints = {
            400: "请求被拒绝（400），常见原因：模型名不支持该参数、消息过长。可尝试调小 LLM_BATCH_CHARS / 关闭 LLM_JSON_MODE。",
            401: "鉴权失败（401）：LLM_API_KEY 无效或已过期，请重新填写。",
            402: "余额不足（402）：请到服务商控制台充值。",
            403: "无权限（403）：该 Key 无权访问此模型。",
            404: "接口不存在（404）：请检查 LLM_BASE_URL 与 LLM_MODEL 是否正确。",
            413: "请求体过大（413）：请调小 LLM_BATCH_CHARS。",
            429: "触发限流（429）：已自动退避重试，可持续降低 LLM_MAX_CONCURRENCY。",
            500: "服务端错误（500）：稍后重试。",
            502: "网关错误（502）：稍后重试。",
            503: "服务暂不可用（503）：稍后重试。",
            504: "网关超时（504）：稍后重试。",
        }
        retryable = code in (408, 409, 425, 429) or code >= 500
        return TranslationError(
            f"{hints.get(code, f'接口返回 {code}。')}", status=code, retryable=retryable, detail=body
        )

    # ------------------------------------------------------------------
    def _call_once(self, segments: list[dict], json_mode: bool) -> dict[str, str]:
        body: dict = {
            "model": self.model,
            "messages": self._build_messages(segments),
            "temperature": self.s.temperature,
            "max_tokens": self.s.max_tokens,
            "stream": False,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        data = self._post(body)
        choices = data.get("choices") or []
        if not choices:
            raise TranslationError("接口返回中没有 choices 字段。", detail=json.dumps(data, ensure_ascii=False)[:500])
        message = choices[0].get("message") or {}
        content = message.get("content")
        if not content and message.get("reasoning_content"):
            content = message.get("reasoning_content")
        usage = data.get("usage") or {}
        with self._lock:
            self.stats["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
            self.stats["completion_tokens"] += int(usage.get("completion_tokens") or 0)
        return parse_translation_payload(content)

    def translate_chunk(self, segments: list[dict]) -> dict[str, str]:
        """翻译一个分块，含重试。"""
        want_json = self._json_mode in ("auto", "on")
        attempt = 0
        last_error: TranslationError | None = None
        while attempt <= self.s.retries:
            attempt += 1
            try:
                with self._lock:
                    self.stats["requests"] += 1
                return self._call_once(segments, want_json)
            except TranslationError as exc:
                last_error = exc
                # JSON 模式不被支持时自动降级，只降级一次
                if want_json and exc.status == 400 and "response_format" in (exc.detail or "").lower():
                    want_json = False
                    with self._lock:
                        self._json_mode = "off"
                    continue
                if not exc.retryable or attempt > self.s.retries:
                    raise
                with self._lock:
                    self.stats["retries"] += 1
                time.sleep(min(8.0, 1.5 * (2 ** (attempt - 1))))
        raise last_error or TranslationError("翻译失败：未知错误")

    # ------------------------------------------------------------------
    def translate(
        self,
        segments: list[dict],
        on_chunk_done=None,
        should_cancel=None,
    ) -> tuple[dict[str, str], list[dict]]:
        """并发翻译全部片段。

        返回 (译文映射, 错误列表)。可以在 mock 模式下不联网跑通全链路。
        """
        chunks = chunk_segments(segments, self.s.batch_chars, self.s.batch_segments)
        results: dict[str, str] = {}
        errors: list[dict] = []
        if not chunks:
            return results, errors

        if self.s.mock:
            for i, ch in enumerate(chunks):
                if should_cancel and should_cancel():
                    break
                partial = {seg["id"]: f"【模拟译文·未调用API】{seg['text']}" for seg in ch}
                results.update(partial)
                if on_chunk_done:
                    on_chunk_done(i + 1, len(chunks), partial)
            return results, errors

        workers = max(1, min(self.s.max_concurrency, len(chunks)))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="translate") as pool:
            futures = {pool.submit(self.translate_chunk, ch): ch for ch in chunks}
            for i, fut in enumerate(as_completed(futures)):
                if should_cancel and should_cancel():
                    for f in futures:
                        f.cancel()
                    break
                ch = futures[fut]
                partial: dict[str, str] = {}
                try:
                    partial = fut.result()
                    missing = [s["id"] for s in ch if s["id"] not in partial]
                    if missing:
                        errors.append({"ids": missing, "error": "模型漏译了部分片段（返回 id 不完整）"})
                    results.update(partial)
                except TranslationError as exc:
                    with self._lock:
                        self.stats["failures"] += 1
                    errors.append({"ids": [s["id"] for s in ch], "error": str(exc), **exc.to_dict()})
                except Exception as exc:  # noqa: BLE001
                    with self._lock:
                        self.stats["failures"] += 1
                    errors.append({"ids": [s["id"] for s in ch], "error": f"{exc.__class__.__name__}: {exc}"})
                if on_chunk_done:
                    on_chunk_done(i + 1, len(chunks), partial)
        return results, errors

    # ------------------------------------------------------------------
    def test_connection(self) -> dict:
        """连通性自检：发一条极短请求。"""
        t0 = time.time()
        sample = "Gaussian Splatting is a novel scene representation."
        try:
            payload = [{"id": "t1", "text": sample}]
            out = self.translate_chunk(payload) if not self.s.mock else {"t1": "【模拟模式】不发起真实请求"}
            return {
                "ok": True,
                "provider": self.s.provider,
                "model": self.model,
                "baseUrl": self.base_url,
                "latencyMs": int((time.time() - t0) * 1000),
                "sample": out.get("t1", ""),
                "mock": self.s.mock,
                "usage": dict(self.stats),
            }
        except TranslationError as exc:
            return {
                "ok": False,
                "provider": self.s.provider,
                "model": self.model,
                "baseUrl": self.base_url,
                "latencyMs": int((time.time() - t0) * 1000),
                "error": str(exc),
                "detail": exc.detail[:500],
                "status": exc.status,
            }
