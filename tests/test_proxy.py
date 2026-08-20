import json
import tempfile
import time
import unittest
from pathlib import Path

from ai_quota_monitor.proxy import translate
from ai_quota_monitor.proxy.auth import ClaudeTokenStore


class IdentityInjectionTests(unittest.TestCase):
    def test_prepends_identity_to_string_system(self):
        payload = {"system": "Be terse."}
        translate.inject_claude_code_identity(payload)
        self.assertEqual(payload["system"][0]["text"], translate.CLAUDE_CODE_IDENTITY)
        self.assertEqual(payload["system"][1]["text"], "Be terse.")

    def test_missing_system_still_gets_identity(self):
        payload = {}
        translate.inject_claude_code_identity(payload)
        self.assertEqual(len(payload["system"]), 1)
        self.assertEqual(payload["system"][0]["text"], translate.CLAUDE_CODE_IDENTITY)

    def test_identity_not_duplicated(self):
        payload = {"system": [{"type": "text", "text": translate.CLAUDE_CODE_IDENTITY}]}
        translate.inject_claude_code_identity(payload)
        self.assertEqual(len(payload["system"]), 1)


class OpenAIToAnthropicTests(unittest.TestCase):
    def test_system_and_messages_split(self):
        body = {
            "model": "gpt-4o",
            "messages": [
                {"role": "system", "content": "You help."},
                {"role": "user", "content": "Hi"},
            ],
        }
        out = translate.openai_to_anthropic(body, "claude-sonnet-4-5", 4096)
        self.assertEqual(out["model"], "claude-sonnet-4-5")  # non-claude mapped to default
        self.assertEqual(out["system"], "You help.")
        self.assertEqual(out["messages"], [{"role": "user", "content": "Hi"}])
        self.assertEqual(out["max_tokens"], 4096)

    def test_claude_model_passthrough(self):
        body = {"model": "claude-opus-4-1", "messages": [{"role": "user", "content": "Hi"}]}
        out = translate.openai_to_anthropic(body, "claude-sonnet-4-5", 4096)
        self.assertEqual(out["model"], "claude-opus-4-1")

    def test_tool_call_roundtrip_shapes(self):
        body = {
            "model": "claude-sonnet-4-5",
            "messages": [
                {"role": "user", "content": "weather?"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "get_weather", "arguments": '{"city":"SF"}'},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call_1", "content": "sunny"},
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "description": "gets weather",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
        }
        out = translate.openai_to_anthropic(body, "claude-sonnet-4-5", 4096)
        assistant = out["messages"][1]
        self.assertEqual(assistant["role"], "assistant")
        self.assertEqual(assistant["content"][0]["type"], "tool_use")
        self.assertEqual(assistant["content"][0]["input"], {"city": "SF"})
        tool_msg = out["messages"][2]
        self.assertEqual(tool_msg["content"][0]["type"], "tool_result")
        self.assertEqual(tool_msg["content"][0]["tool_use_id"], "call_1")
        self.assertEqual(out["tools"][0]["name"], "get_weather")

    def test_image_content_translation(self):
        body = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "what is this"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64,AAAA"},
                        },
                    ],
                }
            ]
        }
        out = translate.openai_to_anthropic(body, "claude-sonnet-4-5", 4096)
        blocks = out["messages"][0]["content"]
        self.assertEqual(blocks[0]["type"], "text")
        self.assertEqual(blocks[1]["type"], "image")
        self.assertEqual(blocks[1]["source"]["media_type"], "image/png")
        self.assertEqual(blocks[1]["source"]["data"], "AAAA")

    def test_stop_and_sampling_params(self):
        body = {
            "messages": [{"role": "user", "content": "hi"}],
            "temperature": 0.3,
            "top_p": 0.9,
            "stop": "END",
        }
        out = translate.openai_to_anthropic(body, "claude-sonnet-4-5", 4096)
        self.assertEqual(out["temperature"], 0.3)
        self.assertEqual(out["top_p"], 0.9)
        self.assertEqual(out["stop_sequences"], ["END"])


class AnthropicToOpenAITests(unittest.TestCase):
    def test_text_response(self):
        resp = {
            "id": "msg_1",
            "content": [{"type": "text", "text": "hello"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 3},
        }
        out = translate.anthropic_to_openai_response(resp, "claude-sonnet-4-5", 1000)
        self.assertEqual(out["choices"][0]["message"]["content"], "hello")
        self.assertEqual(out["choices"][0]["finish_reason"], "stop")
        self.assertEqual(out["usage"]["total_tokens"], 13)

    def test_tool_use_response(self):
        resp = {
            "id": "msg_2",
            "content": [
                {"type": "tool_use", "id": "toolu_1", "name": "f", "input": {"a": 1}}
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 5, "output_tokens": 5},
        }
        out = translate.anthropic_to_openai_response(resp, "claude-sonnet-4-5", 1000)
        call = out["choices"][0]["message"]["tool_calls"][0]
        self.assertEqual(call["function"]["name"], "f")
        self.assertEqual(json.loads(call["function"]["arguments"]), {"a": 1})
        self.assertEqual(out["choices"][0]["finish_reason"], "tool_calls")


class StreamTranslationTests(unittest.TestCase):
    def _events(self, events):
        lines = []
        for event in events:
            lines.append(f"event: {event['type']}\n".encode())
            lines.append(("data: " + json.dumps(event) + "\n").encode())
            lines.append(b"\n")
        return iter(lines)

    def test_text_stream_to_openai_chunks(self):
        events = [
            {"type": "message_start", "message": {"id": "msg"}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "text_delta", "text": "Hel"}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "text_delta", "text": "lo"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
            {"type": "message_stop"},
        ]
        chunks = list(
            translate.anthropic_stream_to_openai(self._events(events), "claude-sonnet-4-5", 1000)
        )
        self.assertEqual(chunks[-1], "data: [DONE]\n\n")
        texts = []
        finish = None
        for chunk in chunks:
            if chunk.strip() == "data: [DONE]":
                continue
            data = json.loads(chunk[len("data: "):])
            delta = data["choices"][0]["delta"]
            if delta.get("content"):
                texts.append(delta["content"])
            if data["choices"][0]["finish_reason"]:
                finish = data["choices"][0]["finish_reason"]
        self.assertEqual("".join(texts), "Hello")
        self.assertEqual(finish, "stop")


class ClaudeTokenRefreshTests(unittest.TestCase):
    def test_returns_token_without_refresh_when_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            creds = home / ".claude" / ".credentials.json"
            creds.parent.mkdir(parents=True)
            creds.write_text(
                json.dumps(
                    {
                        "claudeAiOauth": {
                            "accessToken": "valid-token",
                            "refreshToken": "r",
                            "expiresAt": int((time.time() + 3600) * 1000),
                        }
                    }
                ),
                encoding="utf-8",
            )
            store = ClaudeTokenStore(home)
            self.assertEqual(store.access_token(), "valid-token")


if __name__ == "__main__":
    unittest.main()
