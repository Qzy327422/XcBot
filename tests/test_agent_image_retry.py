"""请求级图片重试回归：真实工具循环 + AST 提取 main 片段，无 QQ/模型/网络。"""
import ast
import base64
import copy
import mimetypes
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot import agent
from bot.agent_context import AgentTurnContext
from bot.rounds import extract_agent_trail, fix_messages

MAIN_PATH = ROOT / "main.py"
MAIN_TREE = ast.parse(MAIN_PATH.read_text(encoding="utf-8"), filename=str(MAIN_PATH))
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Zl1sAAAAASUVORK5CYII="
)
DATA_URL = "data:image/png;base64," + base64.b64encode(PNG).decode("ascii")


def image_block(url=DATA_URL):
    return {"type": "image_url", "image_url": {"url": url}}


def response(calls=None, text=""):
    return SimpleNamespace(content=text, tool_calls=calls)


def read_image_call():
    return {"id": "read-once", "type": "function", "function": {
        "name": "read_image", "arguments": '{"path":"memory.png"}',
    }}


def load_media_helpers(relay):
    names = {"_adapt_agent_retry_trail", "_prepare_read_image_blocks", "_sniff_image_mime"}
    nodes = [node for node in MAIN_TREE.body if (
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names
    ) or (
        isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in {"_READ_IMAGE_SUFFIXES", "_READ_IMAGE_MAX_BYTES"}
            for target in node.targets
        )
    )]
    env = {"base64": base64, "os": os, "mimetypes": mimetypes,
           "relay_images_with_multimodal_model": relay}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(MAIN_PATH), "exec"), env)
    return env


def load_retry_flow(ctx, turn, relay):
    """执行真实 retry 播种/适配/summary 分支与 catch 提取，不导入 main。"""
    owner = next(node for node in MAIN_TREE.body if isinstance(node, ast.ClassDef)
                 and node.name == "EnhancedLimitedDeepSeekContext")
    method = next(node for node in owner.body if isinstance(node, ast.AsyncFunctionDef)
                  and node.name == "agen_content")
    setup = next(node for node in ast.walk(method) if isinstance(node, ast.Try) and any(
        isinstance(item, ast.Assign) and isinstance(item.value, ast.Call)
        and isinstance(item.value.func, ast.Name) and item.value.func.id == "AgentTurnContext"
        for item in node.body
    ))
    start = next(index for index, node in enumerate(setup.body) if isinstance(node, ast.Assign)
                 and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name)
                 and node.value.func.id == "AgentTurnContext")
    end = next(index for index in range(start, len(setup.body)) if isinstance(setup.body[index], ast.If)
               and isinstance(setup.body[index].test, ast.BoolOp))
    wrapper = ast.parse(
        "async def retry_attempt():\n"
        "    relay_total_tokens, relay_prompt_tokens, relay_completion_tokens = initial_relay_usage\n"
        "    pass\n"
        "    return result, (relay_total_tokens, relay_prompt_tokens, relay_completion_tokens)\n"
    )
    wrapper.body[0].body[1:2] = copy.deepcopy(setup.body[start:end + 1])
    env = load_media_helpers(relay)
    env.update({"self": SimpleNamespace(_current_turn=turn), "agent_ctx": ctx,
                "AgentTurnContext": AgentTurnContext, "extract_agent_trail": extract_agent_trail,
                "fix_messages": fix_messages, "reusable_agent_trail": [],
                "summary_only_retry": False, "initial_relay_usage": (0, 0, 0)})
    exec(compile(ast.fix_missing_locations(wrapper), str(MAIN_PATH), "exec"), env)
    handler = next(handler for node in ast.walk(method) if isinstance(node, ast.Try)
                   for handler in node.handlers if handler.body and any(
                       isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                       and call.func.id == "extract_agent_trail"
                       for call in ast.walk(handler.body[0])
                   ))
    env["capture_failure"] = compile(
        ast.Module(body=copy.deepcopy(handler.body[:2]), type_ignores=[]), str(MAIN_PATH), "exec"
    )
    persist = next(node for node in ast.walk(method) if isinstance(node, ast.Assign) and any(
        isinstance(target, ast.Name) and target.id == "_agent_trail" for target in node.targets
    ))
    env["persist_trail"] = compile(ast.Module(body=[persist], type_ignores=[]), str(MAIN_PATH), "exec")
    return env


class ExtractAgentTrailTests(unittest.TestCase):
    def test_identity_only_opt_in_and_default_history_has_no_images(self):
        registered = {"role": "user", "content": [image_block()]}
        equal_but_unregistered = copy.deepcopy(registered)
        spoofed = {**copy.deepcopy(registered), "request_user_message": True}
        assistant = {"role": "assistant", "content": None, "tool_calls": [read_image_call()]}
        tool = {"role": "tool", "tool_call_id": "read-once", "content": "图片已读取"}
        messages = [{"role": "user", "content": "原提问"}, equal_but_unregistered,
                    assistant, tool, spoofed, registered,
                    {"role": "assistant", "content": "结束"}]
        before = copy.deepcopy(messages)
        ordinary = extract_agent_trail(messages)
        self.assertEqual(ordinary, [assistant, tool])
        self.assertNotIn("data:image", str(ordinary))
        trail = extract_agent_trail(messages, request_user_messages=[registered])
        self.assertEqual(trail, [assistant, tool, registered])
        self.assertIsNot(trail[-1], registered)
        self.assertEqual(messages, before)
        self.assertEqual(extract_agent_trail(messages, request_user_messages=[]), ordinary)
        with self.assertRaises(TypeError):
            extract_agent_trail(messages, [registered])


class RetryMediaAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_multimodal_preserves_original_images_on_deep_copy(self):
        relay = AsyncMock(side_effect=AssertionError("不应请求转述"))
        adapt = load_media_helpers(relay)["_adapt_agent_retry_trail"]
        raw = [{"role": "user", "content": [{"type": "text", "text": "原文字"}, image_block()]}]
        adapted, *usage = await adapt(object(), raw, True)
        self.assertEqual(adapted, raw)
        self.assertEqual(usage, [0, 0, 0])
        adapted[0]["content"][1]["image_url"]["url"] = "changed"
        self.assertEqual(raw[0]["content"][1]["image_url"]["url"], DATA_URL)
        relay.assert_not_awaited()

    async def test_text_relay_preserves_text_sums_usage_and_skips_already_relayed(self):
        relay = AsyncMock(side_effect=[("红色标记", 7, 5, 2), ("蓝色标记", 11, 8, 3)])
        adapt = load_media_helpers(relay)["_adapt_agent_retry_trail"]
        context = object()
        original_text = {"type": "text", "text": "保留原文字", "cache_control": {"type": "ephemeral"}}
        raw = [{"role": "tool", "tool_call_id": "read-once", "content": "工具返回"},
               {"role": "user", "content": [original_text, image_block(), image_block("https://example.invalid/a.png")]},
               {"role": "user", "content": [image_block()]},
               {"role": "user", "content": "纯文字"}]
        before = copy.deepcopy(raw)
        adapted, *usage = await adapt(context, raw, False)
        self.assertEqual(usage, [18, 13, 5])
        self.assertNotIn("image_url", str(adapted))
        self.assertEqual(adapted[1]["content"][0], original_text)
        self.assertIn("红色标记", adapted[1]["content"][-1]["text"])
        self.assertEqual(raw, before)
        self.assertEqual(relay.await_args_list[0].args,
                         (context, "保留原文字", [DATA_URL, "https://example.invalid/a.png"]))
        self.assertEqual(relay.await_args_list[1].args, (context, "", [DATA_URL]))
        again, *usage = await adapt(context, adapted, False)
        self.assertEqual(again, adapted)
        self.assertEqual(usage, [0, 0, 0])
        self.assertEqual(relay.await_count, 2)
        self.assertEqual(await adapt(context, [], False), ([], 0, 0, 0))

    async def test_relay_failure_or_invalid_image_is_explicit_and_never_leaks_data(self):
        for result in (RuntimeError(DATA_URL), ("", 4, 3, 1), ("   ", 0, 0, 0)):
            with self.subTest(result=type(result).__name__):
                relay = AsyncMock(side_effect=[result])
                adapt = load_media_helpers(relay)["_adapt_agent_retry_trail"]
                raw = [{"role": "user", "content": [{"type": "text", "text": "保留文字"}, image_block()]}]
                adapted, total, prompt, completion = await adapt(None, raw, False)
                self.assertNotIn("image_url", str(adapted))
                self.assertNotIn(DATA_URL, str(adapted))
                self.assertEqual(adapted[0]["content"][0]["text"], "保留文字")
                self.assertEqual(adapted[0]["content"][-1]["text"], "[图片]")
                self.assertEqual((total, prompt, completion), (0, 0, 0) if isinstance(result, Exception) else result[1:])
                self.assertIn(DATA_URL, str(raw))
        relay = AsyncMock(side_effect=AssertionError("没有有效 URL 时不能请求"))
        adapt = load_media_helpers(relay)["_adapt_agent_retry_trail"]
        adapted, *_ = await adapt(None, [{"role": "user", "content": [image_block("")]}], False)
        self.assertEqual(adapted[0]["content"][-1]["text"], "[图片]")
        relay.assert_not_awaited()

    async def test_actual_read_image_helper_with_temporary_one_pixel_png(self):
        relay = AsyncMock(return_value=("一像素图片的离线转述", 3, 2, 1))
        prepare = load_media_helpers(relay)["_prepare_read_image_blocks"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pixel.png"
            path.write_bytes(PNG)
            blocks, error = await prepare(path, True)
            self.assertEqual((blocks, error), ([image_block()], ""))
            relay.assert_not_awaited()
            blocks, error = await prepare(path, False)
            self.assertEqual(error, "")
            self.assertEqual(blocks, [{"type": "text", "text": "[图片内容]\n一像素图片的离线转述"}])
            relay.assert_awaited_once_with(None, "", [DATA_URL])


class AgentImageRetryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.registry = agent.ToolRegistry()
        patcher = patch.object(agent, "REGISTRY", self.registry)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tool_runs = []
        self.ctx = agent.AgentContext(user_id="offline-image-retry", user_level="admin")
        self.settings = agent.AgentSettings(enabled=True, max_rounds=5, retry_attempts=0,
                                            enabled_tools={"read_image": True})
        self.blocks = [{"type": "text", "text": "工具产出的图片说明"}, image_block()]

        async def read_image(args, ctx):
            self.tool_runs.append(dict(args))
            ctx.extra.setdefault("pending_user_images", []).extend(copy.deepcopy(self.blocks))
            return "图片已读取，内容见随后的图片消息"

        self.registry.register(agent.ToolSpec(
            name="read_image", description="只修改内存的图片 handler", parameters={
                "type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"],
            }, handler=read_image, level="admin",
        ))
        self.seed = [{"role": "system", "content": "离线测试"}, {"role": "user", "content": "请读取并分析图片"}]
        self.turn = AgentTurnContext()
        self.turn.seed(copy.deepcopy(self.seed))

    async def test_real_loop_summary_fail_text_retry_fail_then_multimodal_retry(self):
        requests = []

        async def complete(messages, tools):
            requests.append((copy.deepcopy(messages), tools))
            if len(requests) == 1:
                return response([read_image_call()]), {"total": 1, "prompt": 1, "completion": 0}
            raise RuntimeError("初次整理失败")

        with self.assertRaisesRegex(RuntimeError, "初次整理失败"):
            await agent.run_tool_loop(complete, self.turn.messages, self.ctx, self.settings)
        self.assertEqual(len(self.tool_runs), 1)
        injected = self.turn.new_messages()[-1]
        self.assertIs(self.ctx.extra["request_user_messages"][0], injected)
        self.assertNotIn("pending_user_images", self.ctx.extra)
        self.assertIn(DATA_URL, str(requests[-1][0]))

        relay = AsyncMock(return_value=("红色按钮", 9, 6, 3))
        env = load_retry_flow(self.ctx, self.turn, relay)
        exec(env["capture_failure"], env)
        raw = env["reusable_agent_trail"]
        self.assertEqual([message["role"] for message in raw], ["assistant", "tool", "user"])
        before = copy.deepcopy(raw)
        self.assertNotIn(self.seed[-1], raw)
        env.update({"messages": copy.deepcopy(self.seed), "supports_multimodal": False})

        async def fail_summary(messages, tools):
            requests.append((copy.deepcopy(messages), tools))
            self.assertIsNone(tools)
            self.assertNotIn("image_url", str(messages))
            self.assertIn("红色按钮", str(messages))
            raise RuntimeError("第二次整理失败")

        env["_complete"] = fail_summary
        with self.assertRaisesRegex(RuntimeError, "第二次整理失败"):
            await env["retry_attempt"]()
        request_user = env["self"]._current_turn.new_messages()[-1]
        self.assertTrue(any(item is request_user for item in self.ctx.extra["request_user_messages"]))
        extracted = extract_agent_trail(env["self"]._current_turn.new_messages(),
                                        request_user_messages=self.ctx.extra["request_user_messages"])
        self.assertEqual([message["role"] for message in extracted], ["assistant", "tool", "user"])
        exec(env["capture_failure"], env)
        self.assertIs(env["reusable_agent_trail"], raw)
        self.assertEqual(raw, before)

        # 即使新 attempt 尚未追加任何可复用内容，也不能覆盖旧 trail。
        env["self"]._current_turn = AgentTurnContext()
        env["self"]._current_turn.seed(copy.deepcopy(self.seed))
        exec(env["capture_failure"], env)
        self.assertIs(env["reusable_agent_trail"], raw)
        env["supports_multimodal"] = True

        async def fail_multimodal_summary(messages, tools):
            requests.append((copy.deepcopy(messages), tools))
            self.assertIsNone(tools)
            self.assertIn(DATA_URL, str(messages))
            raise RuntimeError("第三次整理失败")

        env["_complete"] = fail_multimodal_summary
        with self.assertRaisesRegex(RuntimeError, "第三次整理失败"):
            await env["retry_attempt"]()
        request_user = env["self"]._current_turn.new_messages()[-1]
        self.assertTrue(any(item is request_user for item in self.ctx.extra["request_user_messages"]))
        extracted = extract_agent_trail(env["self"]._current_turn.new_messages(),
                                        request_user_messages=self.ctx.extra["request_user_messages"])
        self.assertIn(DATA_URL, str(extracted))
        exec(env["capture_failure"], env)
        self.assertIs(env["reusable_agent_trail"], raw)
        self.assertEqual(raw, before)

        async def finish_summary(messages, tools):
            requests.append((copy.deepcopy(messages), tools))
            self.assertIsNone(tools)
            self.assertIn(DATA_URL, str(messages))
            return response(text="图片分析完成"), {"total": 2, "prompt": 1, "completion": 1, "cached": 0}

        env["_complete"] = finish_summary
        text, usage = await env["retry_attempt"]()
        self.assertEqual((text, usage), ("图片分析完成", (0, 0, 0)))
        relay.assert_awaited_once_with(env["self"], "工具产出的图片说明", [DATA_URL])
        self.assertEqual(len(self.tool_runs), 1)
        for messages, _ in requests:
            self.assertEqual(messages.count(self.seed[-1]), 1)
        exec(env["persist_trail"], env)
        self.assertEqual([message["role"] for message in env["_agent_trail"]], ["assistant", "tool"])
        self.assertNotIn("data:image", str(env["_agent_trail"]))
        self.assertEqual(raw, before)

    async def test_actual_retry_setup_adds_relay_usage_to_attempt_totals(self):
        relay = AsyncMock(return_value=("离线图片转述", 9, 6, 3))
        env = load_retry_flow(self.ctx, self.turn, relay)
        env.update({"messages": copy.deepcopy(self.seed), "supports_multimodal": False,
                    "initial_relay_usage": (5, 4, 1),
                    "reusable_agent_trail": [{"role": "user", "content": [image_block()]}],
                    "_complete": AsyncMock(return_value=(response(text="完成"),
                                                         {"total": 1, "prompt": 1, "completion": 0, "cached": 0}))})
        self.assertEqual(await env["retry_attempt"](), ("完成", (14, 10, 4)))
        self.assertIsNone(env["_complete"].await_args.args[1])
        self.assertIn("离线图片转述", str(env["_complete"].await_args.args[0]))

    async def test_follow_up_injection_points_register_actual_user_objects(self):
        for after_tool in (False, True):
            with self.subTest(after_tool=after_tool):
                ctx = agent.AgentContext(user_id=f"follow-up-{after_tool}", user_level="admin")
                messages = copy.deepcopy(self.seed)
                settings = copy.copy(self.settings)
                settings.follow_up_image_builder = AsyncMock(return_value=[image_block()])
                requests = []
                session_id = f"offline-follow-up-{after_tool}"

                async def complete(current, tools):
                    requests.append(copy.deepcopy(current))
                    if len(requests) == 1:
                        self.assertTrue(agent.follow_up_session(session_id, "补充看这张图", image_urls=[DATA_URL]))
                        return response([read_image_call()] if after_tool else None, "正在检查"), {}
                    raise RuntimeError("补充消息后的整理失败")

                with self.assertRaisesRegex(RuntimeError, "补充消息后的整理失败"):
                    await agent.run_tool_loop(complete, messages, ctx, settings, session_id=session_id)
                registered = ctx.extra["request_user_messages"]
                injected = [item for item in messages[len(self.seed):] if item.get("role") == "user"]
                self.assertEqual(len(injected), 2 if after_tool else 1)
                self.assertEqual(len(registered), len(injected))
                for item in injected:
                    self.assertTrue(any(entry is item for entry in registered))
                retry = fix_messages(extract_agent_trail(messages, request_user_messages=registered))
                self.assertEqual(sum(item["role"] == "user" for item in retry), len(injected))
                self.assertNotIn("data:image", str(extract_agent_trail(messages)))

    async def test_new_loop_attempt_does_not_clear_request_user_identities(self):
        requests = 0

        async def complete(messages, tools):
            nonlocal requests
            requests += 1
            return (response([read_image_call()]) if requests == 1 else response(text="完成")), {}

        await agent.run_tool_loop(complete, self.turn.messages, self.ctx, self.settings)
        registered = self.ctx.extra["request_user_messages"]
        injected = registered[0]
        fail = AsyncMock(side_effect=RuntimeError("新 attempt 失败"))
        with self.assertRaisesRegex(RuntimeError, "新 attempt 失败"):
            await agent.run_tool_loop(fail, self.turn.messages, self.ctx, self.settings)
        self.assertIs(self.ctx.extra["request_user_messages"], registered)
        self.assertIs(self.ctx.extra["request_user_messages"][0], injected)
        self.assertEqual(extract_agent_trail([injected], request_user_messages=registered), [injected])
        self.assertEqual(len(self.tool_runs), 1)
        self.assertNotIn("data:image", str(extract_agent_trail(self.turn.new_messages())))


if __name__ == "__main__":
    unittest.main()
