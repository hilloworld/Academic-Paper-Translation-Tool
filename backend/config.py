"""配置加载：环境变量 / .env / config.json / 界面设置 四级注入，绝不硬编码密钥。

优先级（后者覆盖前者）：
    内置默认值 -> config.json -> .env -> 系统环境变量 -> 界面设置(runtime-config.json)

最后一级「界面设置」由 Web 界面的「⚙ 设置」窗口写入 ``<DATA_DIR>/runtime-config.json``，
刻意放在最高优先级：用户在界面上显式改动过一次，就应该立刻生效，而不是被 .env 里
的旧值默默盖掉。点界面上的「清除界面设置」即可删除该文件，重新回到由 .env 控制。
该文件位于 ``data/``（已在 .gitignore 内），不会随代码提交。
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 提示词版本号：变更翻译提示词时递增，可让旧缓存自然失效
# v2：系统提示词新增「⟦k⟧ 是行内公式占位符，必须原样原位保留」这条规则。
# ★ 不要回退或再随意递增：缓存键含版本号，一改就等于把用户已翻译好的全文作废、重花一次额度。
#   真要再改，请先想清楚受影响的只是"含占位符的块"还是全部文本。
PROMPT_VERSION = "v2"

# 界面设置文件名（位于 DATA_DIR 下），优先级最高
RUNTIME_OVERRIDE_NAME = "runtime-config.json"

# 允许在「⚙ 设置」窗口中编辑的配置项
#   key   : 前端使用的字段名（camelCase）
#   env   : 写入配置文件/环境变量时使用的键名
#   field : backend.config.Settings 的字段名
#   label : 界面标签
UI_FIELDS: dict[str, dict[str, str]] = {
    "provider": {"env": "LLM_PROVIDER", "field": "provider", "label": "服务商"},
    "baseUrl": {"env": "LLM_BASE_URL", "field": "base_url", "label": "Base URL"},
    "model": {"env": "LLM_MODEL", "field": "model", "label": "模型名"},
    "apiKey": {"env": "LLM_API_KEY", "field": "api_key", "label": "API Key"},
    "mock": {"env": "LLM_MOCK", "field": "mock", "label": "模拟模式"},
    "temperature": {"env": "LLM_TEMPERATURE", "field": "temperature", "label": "温度"},
    "maxConcurrency": {"env": "LLM_MAX_CONCURRENCY", "field": "max_concurrency", "label": "并发数"},
    "sourceLang": {"env": "SOURCE_LANG", "field": "source_lang", "label": "源语言"},
    "targetLang": {"env": "TARGET_LANG", "field": "target_lang", "label": "目标语言"},
    "renderZoom": {"env": "RENDER_ZOOM", "field": "render_zoom", "label": "渲染倍率"},
    "skipReferenceBlocks": {
        "env": "SKIP_REFERENCE_BLOCKS",
        "field": "skip_reference_blocks",
        "label": "跳过参考文献",
    },
    "translateTables": {
        "env": "TRANSLATE_TABLES",
        "field": "translate_tables",
        "label": "翻译表格内容",
    },
    "translateCaptions": {
        "env": "TRANSLATE_CAPTIONS",
        "field": "translate_captions",
        "label": "翻译图表标题",
    },
    "inlineMathImage": {
        "env": "INLINE_MATH_IMAGE",
        "field": "inline_math_image",
        "label": "行内公式保留原图",
    },
    "translateFormulaBlocks": {
        "env": "TRANSLATE_FORMULA_BLOCKS",
        "field": "translate_formula_blocks",
        "label": "翻译公式块",
    },
}

# 常见 OpenAI 兼容服务商预设（可被显式配置覆盖）
PROVIDER_PRESETS: dict[str, dict[str, str]] = {
    "deepseek": {"base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
    "openai": {"base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini"},
    "moonshot": {"base_url": "https://api.moonshot.cn/v1", "model": "moonshot-v1-8k"},
    "qwen": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
    },
    "zhipu": {"base_url": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4-flash"},
    "siliconflow": {
        "base_url": "https://api.siliconflow.cn/v1",
        "model": "Qwen/Qwen2.5-72B-Instruct",
    },
    "ollama": {"base_url": "http://127.0.0.1:11434/v1", "model": "qwen2.5:14b"},
    "custom": {"base_url": "", "model": ""},
}

DEFAULT_SYSTEM_PROMPT = """你是一名资深的学术论文翻译专家，擅长把英文计算机/工程类论文翻译成准确、通顺的中文。

翻译要求：
1. 忠实准确：完整传达原文含义，不增删、不解释、不总结、不添加任何评注。
2. 术语规范：使用学术界通用中文译名；首次出现的专业术语可保留英文缩写于括号内，例如"三维高斯泼溅(3DGS)"。
3. 公式与符号：行内公式、变量名、数学符号、单位、代码、URL 一律原样保留，不要翻译、不要改写。
   若出现 ⟦1⟧、⟦2⟧ 这样的占位符，表示此处有一段公式已由原图替代：必须把占位符**原样、原位**保留在译文里
   （包括编号），既不要翻译它、不要删除它，也不要在它前后添加解释。
4. 引用编号：形如 [12]、(Smith et al., 2021) 的引用标记原样保留。
5. 风格一致：保持学术书面语，避免口语化；同一术语在全文中译法保持一致。
6. 段落结构：一段输入对应一段输出，不要合并或拆分段落。
7. 只输出译文本身，不要输出原文、不要输出任何前后缀说明。"""


def _parse_env_file(path: Path) -> dict[str, str]:
    """极简 .env 解析，避免额外依赖。"""
    data: dict[str, str] = {}
    if not path.exists():
        return data
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if key.lower().startswith("export "):
            key = key[7:].strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        data[key] = val
    return data


def _as_bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")


def _as_float(value, default: float) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _as_int(value, default: int) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


@dataclass
class Settings:
    # ---- 翻译服务 ----
    provider: str = "deepseek"
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    temperature: float = 0.2
    max_tokens: int = 4096
    timeout: float = 120.0
    max_concurrency: int = 4
    batch_chars: int = 1800
    batch_segments: int = 24
    retries: int = 3
    json_mode: str = "auto"
    mock: bool = False
    source_lang: str = "英语"
    target_lang: str = "简体中文"
    system_prompt: str = ""
    skip_reference_blocks: bool = True
    translate_formula_blocks: bool = False
    # 表格与图表标题默认**不翻译**：右栏原样显示原表/原图，避免中文盖住版面
    translate_tables: bool = False
    translate_captions: bool = True
    # 行内公式（嵌在正文里的公式）默认按原图贴回，不让模型重写
    inline_math_image: bool = True
    # ---- 服务 ----
    host: str = "127.0.0.1"
    port: int = 8000
    open_browser: bool = True
    render_zoom: float = 2.0
    data_dir: str = "data"
    # ---- 运行期字段 ----
    config_file: str = ""
    env_file: str = ""
    override_file: str = ""
    overridden_keys: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------
    @classmethod
    def load(cls, root: Path | None = None) -> "Settings":
        root = root or PROJECT_ROOT
        merged: dict[str, object] = {}

        cfg_path = root / "config.json"
        if cfg_path.exists():
            try:
                merged.update(json.loads(cfg_path.read_text(encoding="utf-8-sig")))
            except Exception as exc:  # noqa: BLE001
                raise ValueError(f"config.json 解析失败：{exc}") from exc

        env_path = root / ".env"
        merged.update(_parse_env_file(env_path))
        merged.update({k: v for k, v in os.environ.items() if k in _ENV_KEYS})

        s = cls()
        s.config_file = str(cfg_path) if cfg_path.exists() else ""
        s.env_file = str(env_path) if env_path.exists() else ""

        # 先定位 DATA_DIR，才能找到「界面设置」文件（它自己也可能覆写 DATA_DIR，故先读一次原始值）
        raw_data_dir = str(merged.get("DATA_DIR", s.data_dir) or "data").strip() or "data"
        data_path = Path(raw_data_dir)
        if not data_path.is_absolute():
            data_path = root / data_path
        override_path = data_path / RUNTIME_OVERRIDE_NAME
        override_data: dict[str, object] = {}
        if override_path.exists():
            try:
                loaded = json.loads(override_path.read_text(encoding="utf-8-sig")) or {}
                if not isinstance(loaded, dict):
                    raise ValueError("顶层必须是 JSON 对象")
                override_data = loaded
            except Exception as exc:  # noqa: BLE001
                s.warnings.append(f"界面设置文件无法解析，已忽略：{override_path}（{exc}）")
                override_data = {}
            else:
                # 界面设置优先级最高，覆盖前面所有来源
                merged.update(override_data)
        s.override_file = str(override_path)
        s.overridden_keys = sorted(k for k in override_data if k in _ENV_KEYS)

        s.provider = str(merged.get("LLM_PROVIDER", s.provider) or "deepseek").strip().lower()
        preset = PROVIDER_PRESETS.get(s.provider, {})
        s.base_url = str(merged.get("LLM_BASE_URL") or preset.get("base_url", "")).strip()
        s.model = str(merged.get("LLM_MODEL") or preset.get("model", "")).strip()
        s.api_key = str(merged.get("LLM_API_KEY") or "").strip()
        s.temperature = _as_float(merged.get("LLM_TEMPERATURE"), s.temperature)
        s.max_tokens = _as_int(merged.get("LLM_MAX_TOKENS"), s.max_tokens)
        s.timeout = _as_float(merged.get("LLM_TIMEOUT"), s.timeout)
        s.max_concurrency = max(1, _as_int(merged.get("LLM_MAX_CONCURRENCY"), s.max_concurrency))
        s.batch_chars = max(200, _as_int(merged.get("LLM_BATCH_CHARS"), s.batch_chars))
        s.batch_segments = max(1, _as_int(merged.get("LLM_BATCH_SEGMENTS"), s.batch_segments))
        s.retries = max(0, _as_int(merged.get("LLM_RETRIES"), s.retries))
        s.json_mode = str(merged.get("LLM_JSON_MODE", s.json_mode)).strip().lower()
        s.mock = _as_bool(merged.get("LLM_MOCK"), s.mock)
        s.source_lang = str(merged.get("SOURCE_LANG", s.source_lang)).strip() or "英语"
        s.target_lang = str(merged.get("TARGET_LANG", s.target_lang)).strip() or "简体中文"
        s.system_prompt = str(merged.get("LLM_SYSTEM_PROMPT") or "").strip()
        s.skip_reference_blocks = _as_bool(
            merged.get("SKIP_REFERENCE_BLOCKS"), s.skip_reference_blocks
        )
        s.translate_formula_blocks = _as_bool(
            merged.get("TRANSLATE_FORMULA_BLOCKS"), s.translate_formula_blocks
        )
        s.translate_tables = _as_bool(merged.get("TRANSLATE_TABLES"), s.translate_tables)
        s.translate_captions = _as_bool(merged.get("TRANSLATE_CAPTIONS"), s.translate_captions)
        s.inline_math_image = _as_bool(merged.get("INLINE_MATH_IMAGE"), s.inline_math_image)
        s.host = str(merged.get("HOST", s.host)).strip() or "127.0.0.1"
        s.port = _as_int(merged.get("PORT"), s.port)
        s.open_browser = _as_bool(merged.get("OPEN_BROWSER"), s.open_browser)
        s.render_zoom = min(4.0, max(1.0, _as_float(merged.get("RENDER_ZOOM"), s.render_zoom)))
        s.data_dir = str(merged.get("DATA_DIR", s.data_dir)).strip() or "data"

        if s.provider not in PROVIDER_PRESETS:
            s.warnings.append(
                f"未知的 LLM_PROVIDER='{s.provider}'，已按 custom 处理，请显式配置 LLM_BASE_URL 与 LLM_MODEL。"
            )
            preset = PROVIDER_PRESETS["custom"]
        if not s.base_url:
            s.warnings.append("未配置 LLM_BASE_URL，且 provider 预设为空，翻译将无法调用。")
        if not s.model:
            s.warnings.append("未配置 LLM_MODEL，翻译将无法调用。")
        if not s.api_key and not s.mock:
            s.warnings.append("未配置 LLM_API_KEY，翻译将无法调用（可设置 LLM_MOCK=1 先跑通链路）。")
        return s

    # ------------------------------------------------------------------
    @property
    def root(self) -> Path:
        return PROJECT_ROOT

    @property
    def data_path(self) -> Path:
        p = Path(self.data_dir)
        return p if p.is_absolute() else (PROJECT_ROOT / p)

    @property
    def override_path(self) -> Path:
        """界面设置的落盘位置（始终位于 DATA_DIR 下）。"""
        return self.data_path / RUNTIME_OVERRIDE_NAME

    @property
    def uploads_dir(self) -> Path:
        return self.data_path / "uploads"

    @property
    def docs_dir(self) -> Path:
        return self.data_path / "docs"

    @property
    def cache_dir(self) -> Path:
        return self.data_path / "cache"

    @property
    def frontend_dir(self) -> Path:
        return PROJECT_ROOT / "frontend"

    @property
    def debug_dir(self) -> Path:
        return self.data_path / "debug"

    @property
    def system_prompt_text(self) -> str:
        return self.system_prompt or DEFAULT_SYSTEM_PROMPT

    def ensure_dirs(self) -> None:
        for p in (self.uploads_dir, self.docs_dir, self.cache_dir, self.debug_dir):
            p.mkdir(parents=True, exist_ok=True)

    def api_key_masked(self) -> str:
        if not self.api_key:
            return ""
        if len(self.api_key) <= 8:
            return "*" * len(self.api_key)
        return f"{self.api_key[:4]}{'*' * 8}{self.api_key[-4:]}"

    def public(self) -> dict:
        """对外暴露的配置（不含密钥明文）。"""
        return {
            "provider": self.provider,
            "baseUrl": self.base_url,
            "model": self.model,
            "apiKeySet": bool(self.api_key),
            "apiKeyMasked": self.api_key_masked(),
            "mock": bool(self.mock),
            "temperature": self.temperature,
            "maxConcurrency": self.max_concurrency,
            "sourceLang": self.source_lang,
            "targetLang": self.target_lang,
            "renderZoom": self.render_zoom,
            "skipReferenceBlocks": self.skip_reference_blocks,
            "translateFormulaBlocks": self.translate_formula_blocks,
            "translateTables": self.translate_tables,
            "translateCaptions": self.translate_captions,
            "inlineMathImage": self.inline_math_image,
            "configFile": self.config_file,
            "envFile": self.env_file,
            "overrideFile": self.override_file,
            "overrideActive": bool(self.overridden_keys),
            "overriddenKeys": self.overridden_keys,
            "providers": {
                name: {"baseUrl": preset.get("base_url", ""), "model": preset.get("model", "")}
                for name, preset in PROVIDER_PRESETS.items()
            },
            "fields": {
                key: {"env": spec["env"], "label": spec["label"]} for key, spec in UI_FIELDS.items()
            },
            "warnings": self.warnings,
        }

    def editable_public(self) -> dict:
        """供「⚙ 设置」窗口回填表单的值。"""
        return {
            "provider": self.provider,
            "baseUrl": self.base_url,
            "model": self.model,
            "apiKeySet": bool(self.api_key),
            "apiKeyMasked": self.api_key_masked(),
            "mock": bool(self.mock),
            "temperature": self.temperature,
            "maxConcurrency": self.max_concurrency,
            "sourceLang": self.source_lang,
            "targetLang": self.target_lang,
            "renderZoom": self.render_zoom,
            "skipReferenceBlocks": self.skip_reference_blocks,
            "translateFormulaBlocks": self.translate_formula_blocks,
            "translateTables": self.translate_tables,
            "translateCaptions": self.translate_captions,
            "inlineMathImage": self.inline_math_image,
        }

    def to_dict(self) -> dict:
        d = asdict(self)
        d["api_key"] = self.api_key_masked()
        return d


# 允许从系统环境变量读取的键（避免把整机 env 全量灌入）
_ENV_KEYS = {
    "LLM_PROVIDER",
    "LLM_BASE_URL",
    "LLM_MODEL",
    "LLM_API_KEY",
    "LLM_TEMPERATURE",
    "LLM_MAX_TOKENS",
    "LLM_TIMEOUT",
    "LLM_MAX_CONCURRENCY",
    "LLM_BATCH_CHARS",
    "LLM_BATCH_SEGMENTS",
    "LLM_RETRIES",
    "LLM_JSON_MODE",
    "LLM_MOCK",
    "LLM_SYSTEM_PROMPT",
    "SOURCE_LANG",
    "TARGET_LANG",
    "SKIP_REFERENCE_BLOCKS",
    "TRANSLATE_FORMULA_BLOCKS",
    "TRANSLATE_TABLES",
    "TRANSLATE_CAPTIONS",
    "INLINE_MATH_IMAGE",
    "HOST",
    "PORT",
    "OPEN_BROWSER",
    "RENDER_ZOOM",
    "DATA_DIR",
}

_settings: Settings | None = None


def get_settings(reload: bool = False) -> Settings:
    global _settings
    if _settings is None or reload:
        _settings = Settings.load()
        _settings.ensure_dirs()
    return _settings


# ----------------------------------------------------------------------
# 界面设置（runtime-config.json）读写
# ----------------------------------------------------------------------
def save_runtime_override(values: dict[str, object]) -> Path:
    """把界面提交的配置合并进 runtime-config.json（只覆盖提交的键）。"""
    path = get_settings().override_path
    path.parent.mkdir(parents=True, exist_ok=True)
    current: dict[str, object] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8-sig")) or {}
            if isinstance(loaded, dict):
                current = loaded
        except Exception:  # noqa: BLE001
            current = {}
    current.update(values)
    path.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def clear_runtime_override() -> bool:
    """删除界面设置文件，配置回落到 .env / config.json / 环境变量。返回是否真的删掉了。"""
    path = get_settings().override_path
    existed = path.exists()
    path.unlink(missing_ok=True)
    return existed


def ui_payload_to_fields(body: dict) -> tuple[dict[str, object], dict[str, object]]:
    """把界面上提交的 JSON 拆成 (Settings 字段, 配置文件键值)。

    * ``apiKey`` 为空字符串/None 时视为「不修改」，避免把已保存的密钥抹掉；
    * 其它未提交的字段一律不动。
    """
    fields: dict[str, object] = {}
    file_values: dict[str, object] = {}
    for ui_key, spec in UI_FIELDS.items():
        if ui_key not in body:
            continue
        raw = body[ui_key]
        if ui_key == "apiKey":
            key = str(raw or "").strip()
            if not key:
                continue
            fields[spec["field"]] = key
            file_values[spec["env"]] = key
            continue
        fields[spec["field"]] = raw
        file_values[spec["env"]] = raw
    return fields, file_values

