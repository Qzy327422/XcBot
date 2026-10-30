"""真实工具循环的离线回归；所有 handler 只修改内存。"""
import asyncio
import json
import types
import unittest
from unittest.mock import patch

from bot import agent


DSML = '''<｜｜DSML｜｜ calls>
<｜｜DSML｜｜ invoke name="execute_shell">
<｜｜DSML｜｜ parameter name="command" string="true">这段文本绝不会执行</｜｜DSML｜｜ parameter>
</｜｜DSML｜｜ invoke>
</｜｜DSML｜｜ calls>'''


def native(name="execute_shell", args=None, call_id="call_1"):
    return {"id": call_id, "type": "function", "function": {
        "name": name, "arguments": json.dumps(args if args is not None else {"command": "内存操作"}, ensure_ascii=False)
    }}


def response(calls=None, text=""):
    return types.SimpleNamespace(content=text, tool_calls=calls)


class AgentProtocolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.registry = agent.ToolRegistry()
        self.patch = patch.object(agent, "REGISTRY", self.registry)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.effects = []

        async def handler(args, ctx):
            self.effects.append(dict(args))
            await asyncio.sleep(0)
            return "内存测试完成"

        self.registry.register(agent.ToolSpec(
            name="execute_shell", description="仅测试的内存 handler", parameters={
                "type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"],
            }, handler=handler, level="admin", side_effect=True,
        ))
        self.ctx = agent.AgentContext(user_id="123", user_level="admin")
        self.settings = agent.AgentSettings(enabled=True, max_rounds=5, retry_attempts=0,
                                            enabled_tools={"execute_shell": True})
        self.requests = []

    async def run_responses(self, responses, **kwargs):
        queue = list(responses)

        async def complete(messages, tools):
            self.requests.append((json.loads(json.dumps(messages)), tools))
            if not queue:
                raise AssertionError("不应额外调用模型")
            item = queue.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item, {"total": 1, "prompt": 1, "completion": 0}

        history = [{"role": "user", "content": "测试"}]
        output = await agent.run_tool_loop(complete, history, self.ctx, self.settings, **kwargs)
        return output, history

    async def test_dsml_correction_then_native_executes_once(self):
        (text, _, _), history = await self.run_responses([
            response(text=DSML), response([native()], "开始检查"), response(text="已经完成"),
        ])
        self.assertEqual(text, "已经完成")
        self.assertEqual(len(self.effects), 1)
        self.assertEqual(len(self.requests), 3)
        self.assertIn("上次只在正文", self.requests[1][0][-1]["content"])
        self.assertNotIn(DSML, json.dumps(history, ensure_ascii=False))
        self.assertTrue(self.ctx.extra["tool_format_correction_used"])

    async def test_repeated_dsml_is_explicit_nonempty_failure(self):
        (text, _, _), _ = await self.run_responses([response(text=DSML), response(text=DSML)])
        self.assertIn("没有执行", text)
        self.assertFalse(self.effects)
        self.assertEqual(len(self.requests), 2)
        self.assertNotIn("这段文本绝不会执行", text)

    async def test_correction_budget_shared_by_provider_attempts(self):
        self.ctx.extra["tool_format_correction_used"] = True
        (text, _, _), _ = await self.run_responses([response(text=DSML)])
        self.assertIn("没有执行", text)
        self.assertEqual(len(self.requests), 1)

    async def test_no_tools_never_corrects_or_executes(self):
        self.settings.enabled_tools = {}
        (text, _, _), _ = await self.run_responses([response(text=DSML)])
        self.assertIn("没有开放工具", text)
        self.assertFalse(self.effects)
        self.assertEqual(len(self.requests), 1)

    async def test_final_round_markup_never_executes(self):
        self.settings.max_rounds = 1
        (text, _, _), _ = await self.run_responses([response([native()]), response(text=DSML)])
        self.assertIn("用完工具轮数", text)
        self.assertEqual(len(self.effects), 1)
        self.assertIsNone(self.requests[-1][1])

    async def test_quoted_and_code_examples_remain_text(self):
        example = "这是格式示例：\n```xml\n" + DSML + "\n```"
        (text, _, _), _ = await self.run_responses([response(text=example)])
        self.assertIn(DSML, text)
        self.assertFalse(self.effects)
        self.assertEqual(len(self.requests), 1)

    async def test_native_has_priority_no_body_reexecution(self):
        (text, _, _), _ = await self.run_responses([response([native()], DSML), response(text="完成")])
        self.assertEqual(len(self.effects), 1)
        self.assertNotIn("tool_format_correction_used", self.ctx.extra)

    async def test_same_batch_equivalent_side_effect_reuses_result(self):
        second = native(call_id="call_2")
        second["function"]["arguments"] = '{"arguments":{"command":"内存操作"}}'
        (_, _, _), history = await self.run_responses([
            response([native(), second]), response(text="完成"),
        ])
        self.assertEqual(len(self.effects), 1)
        results = [m for m in history if m["role"] == "tool"]
        self.assertEqual(len(results), 2)
        self.assertNotEqual(results[0]["tool_call_id"], results[1]["tool_call_id"])
        self.assertEqual(results[0]["content"], results[1]["content"])
        self.assertEqual(self.ctx.extra["trace_calls"][1]["effect_state"], "reused")

    async def test_sequential_batch_dedup_also_applies(self):
        self.settings.parallel_tools = False
        await self.run_responses([response([native(), native(call_id="call_2")]), response(text="完成")])
        self.assertEqual(len(self.effects), 1)

    async def test_changed_parameters_and_later_round_are_not_globally_cached(self):
        await self.run_responses([
            response([native(), native(args={"command": "不同操作"}, call_id="call_2")]),
            response([native(call_id="call_3")]), response(text="完成"),
        ])
        self.assertEqual(len(self.effects), 3)

    async def test_required_or_bad_json_never_enters_handler(self):
        malformed = native(call_id="call_2")
        malformed["function"]["arguments"] = '{broken'
        await self.run_responses([response([native(args={}), malformed]), response(text="参数有误")])
        self.assertFalse(self.effects)
        self.assertNotIn("side_effects_fired", self.ctx.extra)
        self.assertTrue(all(t["effect_state"] == "none" for t in self.ctx.extra["trace_calls"]))

    async def test_missing_duplicate_ids_dicts_and_invalid_calls_are_safe(self):
        malformed = {"id": "same", "type": "function", "function": None}
        good = native(call_id="same")
        badname = native(name=" ", call_id="same")
        (_, _, _), history = await self.run_responses([response([malformed, good, badname]), response(text="已处理")])
        self.assertEqual(len(self.effects), 1)
        calls = next(m["tool_calls"] for m in history if m.get("tool_calls"))
        ids = [c["id"] for c in calls]
        self.assertEqual(len(set(ids)), 3)
        self.assertEqual(len([m for m in history if m["role"] == "tool"]), 3)

    async def test_unknown_tool_is_feedback_not_crash(self):
        (_, _, _), history = await self.run_responses([response([native(name="unknown_tool")]), response(text="没有工具")])
        self.assertFalse(self.effects)
        self.assertIn("不存在", next(m["content"] for m in history if m["role"] == "tool"))

    async def test_dict_response_is_supported(self):
        await self.run_responses([{"content": "", "tool_calls": [native()]}, {"content": "完成", "tool_calls": []}])
        self.assertEqual(len(self.effects), 1)

    async def test_normalized_readonly_streak_not_raw_json(self):
        async def readonly(args, ctx):
            return "检查结果"
        self.registry.register(agent.ToolSpec("read", "只读测试", {
            "type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"],
        }, readonly))
        self.settings.enabled_tools["read"] = True
        calls = [native(name="read", args={"value": 1}, call_id=str(i)) for i in range(3)]
        calls[1]["function"]["arguments"] = '{ "value" : 1 }'
        calls[2]["function"]["arguments"] = '{"params":{"value":1}}'
        await self.run_responses([response([c]) for c in calls] + [response(text="完成")])
        trace = self.ctx.extra["trace_calls"]
        self.assertEqual([t["streak"] for t in trace], [1, 2, 3])
        self.assertTrue(trace[-1]["notice"])

    async def test_request_layer_self_cancellation_on_stop_returns_abort_notice(self):
        import threading
        abort = threading.Event()

        async def complete(messages, tools):
            abort.set()
            raise asyncio.CancelledError()

        with patch.object(agent, "ABORT_POLL_SECONDS", 0.005):
            text, _, _ = await agent.run_tool_loop(complete, [{"role": "user", "content": "测试"}], self.ctx, self.settings, abort=abort)
        self.assertIn("停", text)
        self.assertFalse(self.effects)

    async def test_external_cancellation_cancels_inner_waiter(self):
        import threading
        started, stopped = asyncio.Event(), asyncio.Event()

        async def blocking():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        task = asyncio.create_task(agent._await_or_abort(blocking(), threading.Event()))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(stopped.is_set())

    async def test_materialize_duplicate_result_only_once(self):
        calls = []

        def materialize(*args):
            calls.append(args)
            return args[0], {"mode": "raw"}

        with patch.object(agent, "materialize_large_result", materialize):
            await self.run_responses([response([native(), native(call_id="call_2")]), response(text="完成")])
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(self.effects), 1)

    async def test_effect_timeout_duplicate_is_not_reexecuted(self):
        async def slow(args, ctx):
            self.effects.append(args)
            await asyncio.Event().wait()
        self.registry.get("execute_shell").handler = slow
        self.registry.get("execute_shell").timeout = 0.01
        await self.run_responses([response([native(), native(call_id="call_2")]), response(text="未能确认执行结果")])
        self.assertEqual(len(self.effects), 1)
        trace = self.ctx.extra["trace_calls"]
        self.assertTrue(all(t["effect_state"] == "unknown" for t in trace))

    async def test_unauthorized_native_call_never_reaches_parameter_precheck(self):
        async def safe(args, ctx):
            return "只读"
        self.registry.register(agent.ToolSpec("safe", "无参数只读", {"type": "object", "properties": {}}, safe))
        self.settings.enabled_tools["safe"] = True
        self.ctx.user_level = "user"
        with patch.object(agent, "prepare_tool_args", side_effect=AssertionError("无权工具不能预检")):
            (_, _, _), history = await self.run_responses([response([native()]), response(text="权限不足")])
        self.assertFalse(self.effects)
        self.assertIn("不在本次对话可用", next(m["content"] for m in history if m["role"] == "tool"))

    async def test_progress_plain_text_still_uses_callback(self):
        progress = []
        self.ctx.extra["progress_callback"] = progress.append
        await self.run_responses([response([native()], "我先检查一下"), response(text="最终结果")])
        self.assertEqual(progress, ["我先检查一下"])


if __name__ == "__main__":
    unittest.main()
