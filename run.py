"""一键启动：python run.py

* 从 .env / config.json / 环境变量读取配置（无需修改代码）
* 启动本地 Web 服务并自动打开浏览器
"""
from __future__ import annotations

import argparse
import sys
import threading
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    import uvicorn
except ImportError:  # pragma: no cover
    print("缺少依赖 uvicorn。请先执行：pip install -r requirements.txt", file=sys.stderr)
    raise SystemExit(1)

from backend.config import get_settings  # noqa: E402


BANNER = r"""
 ┌──────────────────────────────────────────────────────────────┐
 │  论文 PDF 翻译 · 中英对照阅读器                              │
 └──────────────────────────────────────────────────────────────┘
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="启动论文对照阅读器")
    parser.add_argument("--host", default=None, help="监听地址，默认取 HOST 配置")
    parser.add_argument("--port", type=int, default=None, help="监听端口，默认取 PORT 配置")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--reload", action="store_true", help="开发模式：代码变更自动重载")
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()

    settings = get_settings(reload=True)
    host = args.host or settings.host
    port = args.port or settings.port

    print(BANNER)
    print(f"  翻译服务 : {settings.provider}  |  模型 {settings.model or '（未配置）'}")
    print(f"  Base URL : {settings.base_url or '（未配置）'}")
    print(f"  API Key  : {settings.api_key_masked() or '（未配置）'}")
    print(f"  数据目录 : {settings.data_path}")
    if settings.config_file:
        print(f"  配置文件 : {settings.config_file}")
    if settings.env_file:
        print(f"  .env    : {settings.env_file}")
    for w in settings.warnings:
        print(f"  ⚠  {w}")
    if settings.mock:
        print("  ⚠  当前为模拟翻译模式（LLM_MOCK=1），不会调用真实 API")
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}/"
    print(f"\n  请在浏览器打开：{url}\n  按 Ctrl+C 停止服务\n")

    if settings.open_browser and not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    uvicorn.run(
        "backend.main:app",
        host=host,
        port=port,
        reload=args.reload,
        log_level=args.log_level,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
