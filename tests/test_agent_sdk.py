"""通过内存 HTTP 传输验证真实公开 SDK 流聚合，不建立网络连接。"""
import json
import unittest

import httpx
from openai import OpenAI

from bot.agent import AgentContext, ToolSpec, prepare_tool_args
from bot.agent_protocol import normalize_tool_calls
from bot.agent_stream import request_chat_completion


class SDKStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_sdk_assembles_native_tool_arguments(self):
        chunks = [
            {"role": "assistant", "content": "我来看看文件。"},
            {"tool_calls": [{"index": 0, "id": "call_test", "type": "function", "function": {"name": "read_test", "arguments": '{"path":'}}]},
            {"tool_calls": [{"index": 0, "function": {"arguments": '"example.txt"}'}}]},
        ]
        payloads = []
        for index, delta in enumerate(chunks + [{}]):
            payloads.append({"id": "chat_test", "object": "chat.completion.chunk", "created": 1,
                             "model": "test-model", "choices": [{"index": 0, "delta": delta,
                             "finish_reason": "tool_calls" if index == len(chunks) else None}]})
        body = "".join("data: " + json.dumps(item, ensure_ascii=False) + "\n\n" for item in payloads) + "data: [DONE]\n\n"
        requests = []

        def handle(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode())

        async def handler(args, ctx):
            raise AssertionError("此测试只验证协议，不执行 handler")

        spec = ToolSpec("read_test", "测试", {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}, handler)
        visible = []
        with OpenAI(api_key="offline-fake-key", base_url="https://offline.invalid/v1",
                    http_client=httpx.Client(transport=httpx.MockTransport(handle), trust_env=False)) as client:
            result = await request_chat_completion(client, {"model": "test-model", "messages": [{"role": "user", "content": "测试"}],
                "tools": [spec.openai_schema()], "tool_choice": "auto", "stream": False}, timeout=2,
                stream_callback=visible.append)
        calls = normalize_tool_calls(result.choices[0].message.tool_calls)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].function.name, "read_test")
        self.assertEqual(calls[0].id, "call_test")
        self.assertFalse(calls[0].error)
        args, error, _ = prepare_tool_args(spec, calls[0].raw_arguments, AgentContext())
        self.assertFalse(error)
        self.assertEqual(args, {"path": "example.txt"})
        self.assertEqual("".join(visible), "我来看看文件。")
        self.assertEqual(len(requests), 1)
        self.assertTrue(requests[0]["stream"])
        self.assertEqual(requests[0]["tools"][0]["function"]["name"], "read_test")


if __name__ == "__main__":
    unittest.main()
