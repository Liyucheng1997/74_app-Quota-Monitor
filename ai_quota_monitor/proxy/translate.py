"""Translation between OpenAI Chat Completions and Anthropic Messages formats.

Only the request/response *shapes* are converted here; no network calls happen
in this module, which keeps it easy to unit test.
"""

from __future__ import annotations

import json
import time
from typing import Any, Iterator

# Exact identity block the Anthropic API requires as the first system block when
# authenticating with a Claude Code OAuth token. Without it the request is
# rejected as an unauthorized use of the subscription token.
CLAUDE_CODE_IDENTITY = "You are Claude Code, Anthropic's official CLI for Claude."

_FINISH_REASON = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
    "pause_turn": "stop",
}


def _fake_id(prefix: str, seed: int) -> str:
    return f"{prefix}-{seed:024d}"


# --------------------------------------------------------------------------- #
# Anthropic request shaping
# --------------------------------------------------------------------------- #

def inject_claude_code_identity(payload: dict) -> dict:
    """Ensure the Anthropic ``system`` field starts with the Claude Code block.

    The caller's own system prompt (string or block list) is preserved after the
    mandatory identity block.
    """

    system = payload.get("system")
    identity = {"type": "text", "text": CLAUDE_CODE_IDENTITY}
    blocks: list[dict[str, Any]] = [identity]
    if isinstance(system, str) and system.strip():
        blocks.append({"type": "text", "text": system})
    elif isinstance(system, list):
        for block in system:
            if isinstance(block, dict) and block.get("text") == CLAUDE_CODE_IDENTITY:
                continue
            blocks.append(block)
    payload["system"] = blocks
    return payload


def _content_to_anthropic(content: Any) -> list[dict[str, Any]] | str:
    """Convert an OpenAI message ``content`` into Anthropic content blocks."""

    if content is None:
        return ""
    if isinstance(content, str):
        return content
    blocks: list[dict[str, Any]] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind == "text":
            blocks.append({"type": "text", "text": part.get("text", "")})
        elif kind == "image_url":
            url = (part.get("image_url") or {}).get("url", "")
            if url.startswith("data:") and ";base64," in url:
                header, b64 = url.split(";base64,", 1)
                media_type = header[len("data:"):] or "image/png"
                blocks.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": b64,
                        },
                    }
                )
            elif url:
                blocks.append(
                    {"type": "image", "source": {"type": "url", "url": url}}
                )
    return blocks or ""


def openai_to_anthropic(body: dict, default_model: str, default_max_tokens: int) -> dict:
    """Translate an OpenAI Chat Completions request into an Anthropic request."""

    model = body.get("model") or default_model
    if not str(model).startswith("claude"):
        model = default_model

    system_texts: list[str] = []
    messages: list[dict[str, Any]] = []
    # Map an assistant tool_call id -> Anthropic tool_use id (they share ids here).
    for message in body.get("messages", []):
        role = message.get("role")
        if role == "system":
            content = message.get("content")
            if isinstance(content, str):
                system_texts.append(content)
            elif isinstance(content, list):
                system_texts.extend(
                    p.get("text", "") for p in content if p.get("type") == "text"
                )
            continue
        if role == "tool":
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": message.get("tool_call_id", ""),
                            "content": message.get("content") or "",
                        }
                    ],
                }
            )
            continue
        if role == "assistant" and message.get("tool_calls"):
            blocks: list[dict[str, Any]] = []
            text = message.get("content")
            if isinstance(text, str) and text:
                blocks.append({"type": "text", "text": text})
            for call in message["tool_calls"]:
                fn = call.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except (ValueError, TypeError):
                    args = {}
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call.get("id", ""),
                        "name": fn.get("name", ""),
                        "input": args,
                    }
                )
            messages.append({"role": "assistant", "content": blocks})
            continue
        messages.append(
            {"role": role or "user", "content": _content_to_anthropic(message.get("content"))}
        )

    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": int(body.get("max_tokens") or default_max_tokens),
        "stream": bool(body.get("stream")),
    }
    if system_texts:
        payload["system"] = "\n\n".join(t for t in system_texts if t)
    for src, dst in (("temperature", "temperature"), ("top_p", "top_p")):
        if body.get(src) is not None:
            payload[dst] = body[src]
    stop = body.get("stop")
    if stop:
        payload["stop_sequences"] = [stop] if isinstance(stop, str) else list(stop)
    if body.get("tools"):
        payload["tools"] = [
            {
                "name": (t.get("function") or {}).get("name", ""),
                "description": (t.get("function") or {}).get("description", ""),
                "input_schema": (t.get("function") or {}).get("parameters")
                or {"type": "object", "properties": {}},
            }
            for t in body["tools"]
            if t.get("type") == "function"
        ]
    tool_choice = body.get("tool_choice")
    if tool_choice == "auto":
        payload["tool_choice"] = {"type": "auto"}
    elif tool_choice == "required":
        payload["tool_choice"] = {"type": "any"}
    elif isinstance(tool_choice, dict) and tool_choice.get("function"):
        payload["tool_choice"] = {
            "type": "tool",
            "name": tool_choice["function"].get("name", ""),
        }
    return payload


# --------------------------------------------------------------------------- #
# Anthropic response -> OpenAI (non-streaming)
# --------------------------------------------------------------------------- #

def anthropic_to_openai_response(resp: dict, model: str, created: int) -> dict:
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for block in resp.get("content", []):
        if block.get("type") == "text":
            text_parts.append(block.get("text", ""))
        elif block.get("type") == "tool_use":
            tool_calls.append(
                {
                    "id": block.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": json.dumps(block.get("input") or {}),
                    },
                }
            )
    message: dict[str, Any] = {"role": "assistant", "content": "".join(text_parts) or None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    usage = resp.get("usage") or {}
    prompt = usage.get("input_tokens", 0) or 0
    completion = usage.get("output_tokens", 0) or 0
    return {
        "id": "chatcmpl-" + resp.get("id", _fake_id("gen", created)),
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": _FINISH_REASON.get(resp.get("stop_reason"), "stop"),
            }
        ],
        "usage": {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
        },
    }


# --------------------------------------------------------------------------- #
# Anthropic SSE stream -> OpenAI SSE chunks
# --------------------------------------------------------------------------- #

def _sse_events(lines: Iterator[bytes]) -> Iterator[dict]:
    """Parse an Anthropic ``text/event-stream`` byte iterator into JSON events."""

    data_buffer: list[str] = []
    for raw in lines:
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if line.startswith("data:"):
            data_buffer.append(line[5:].lstrip())
        elif line == "":
            if data_buffer:
                blob = "\n".join(data_buffer)
                data_buffer = []
                if blob and blob != "[DONE]":
                    try:
                        yield json.loads(blob)
                    except ValueError:
                        continue
    if data_buffer:
        blob = "\n".join(data_buffer)
        if blob and blob != "[DONE]":
            try:
                yield json.loads(blob)
            except ValueError:
                pass


def anthropic_stream_to_openai(
    lines: Iterator[bytes], model: str, created: int
) -> Iterator[str]:
    """Yield OpenAI-formatted ``data: ...\\n\\n`` SSE strings from Anthropic events."""

    chunk_id = "chatcmpl-" + _fake_id("gen", created)
    base = {"id": chunk_id, "object": "chat.completion.chunk", "created": created, "model": model}
    # Track which content block index maps to which tool call slot.
    tool_index: dict[int, int] = {}
    tool_count = 0
    role_sent = False
    finish_reason = "stop"

    def chunk(delta: dict, finish: str | None = None) -> str:
        payload = dict(base)
        payload["choices"] = [{"index": 0, "delta": delta, "finish_reason": finish}]
        return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"

    for event in _sse_events(lines):
        etype = event.get("type")
        if etype == "content_block_start":
            block = event.get("content_block") or {}
            if block.get("type") == "tool_use":
                slot = tool_count
                tool_index[event.get("index", 0)] = slot
                tool_count += 1
                if not role_sent:
                    role_sent = True
                yield chunk(
                    {
                        "tool_calls": [
                            {
                                "index": slot,
                                "id": block.get("id", ""),
                                "type": "function",
                                "function": {"name": block.get("name", ""), "arguments": ""},
                            }
                        ]
                    }
                )
        elif etype == "content_block_delta":
            delta = event.get("delta") or {}
            if delta.get("type") == "text_delta":
                text = delta.get("text", "")
                if not text:
                    continue
                out: dict[str, Any] = {"content": text}
                if not role_sent:
                    out["role"] = "assistant"
                    role_sent = True
                yield chunk(out)
            elif delta.get("type") == "input_json_delta":
                slot = tool_index.get(event.get("index", 0), 0)
                yield chunk(
                    {
                        "tool_calls": [
                            {
                                "index": slot,
                                "function": {"arguments": delta.get("partial_json", "")},
                            }
                        ]
                    }
                )
        elif etype == "message_delta":
            reason = (event.get("delta") or {}).get("stop_reason")
            if reason:
                finish_reason = _FINISH_REASON.get(reason, "stop")
        elif etype == "message_stop":
            break

    yield chunk({}, finish=finish_reason)
    yield "data: [DONE]\n\n"


def error_chunk(message: str, created: int, model: str) -> str:
    payload = {
        "error": {"message": message, "type": "proxy_error"},
    }
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\ndata: [DONE]\n\n"
