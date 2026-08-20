"""Local reverse proxy that exposes Claude Code / Codex subscriptions as APIs.

This package turns the OAuth session tokens that the desktop clients already
store on disk into a local HTTP API that other projects can call. It reuses the
same credential files the Monitor reads, adds automatic token refresh, and
translates between the OpenAI Chat Completions format and the Anthropic Messages
format.

Warning: using a subscription's OAuth token to serve a general-purpose API is a
violation of the Anthropic / OpenAI terms of service and can get the account
banned. It is intended here only for personal, low-volume, single-user use.
"""

from .config import ProxyConfig
from .server import ProxyServer, serve

__all__ = ["ProxyConfig", "ProxyServer", "serve"]
