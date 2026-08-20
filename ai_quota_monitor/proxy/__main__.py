from __future__ import annotations

import argparse

from .config import ProxyConfig
from .server import serve


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m ai_quota_monitor.proxy",
        description="把 Claude Code / Codex 订阅反代成本地 API（仅供个人自用）。",
    )
    parser.add_argument("--host", help="监听地址，默认 127.0.0.1")
    parser.add_argument("--port", type=int, help="监听端口，默认 8787")
    parser.add_argument("--key", help="客户端需携带的 API Key（默认无鉴权，仅本机）")
    parser.add_argument("--model", help="非 Claude 模型名时使用的默认模型")
    args = parser.parse_args()

    config = ProxyConfig()
    if args.host:
        config.host = args.host
    if args.port:
        config.port = args.port
    if args.key is not None:
        config.api_key = args.key
    if args.model:
        config.default_claude_model = args.model
    serve(config)


if __name__ == "__main__":
    main()
