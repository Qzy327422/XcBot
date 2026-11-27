"""BUGFIX_HANDOFF P0 & P2-1 回归测试：Agent follow-up/Timeout/emoji/Any插件/RoleLife外围/LoggedActions引用/引用开关迁移。"""
import ast
import asyncio
import json
import pathlib
import sys
import tempfile
import threading
import types
import unittest
from collections import OrderedDict

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot import agent as bot_agent
from bot import message_runtime
from config_migrate import ensure_config_up_to_date
from webui_core.features import FEATURE_META


def _extract_main_nodes(*names):
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    selected = []
    wanted = set(names)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name in wanted:
            selected.append(node)
    return source, selected


def _exec_main_nodes(env, *names):
    _source, nodes = _extract_main_nodes(*names)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(ROOT / "main.py"), "exec"), env)
    return env


class P01FollowUpAndP02TimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_text_return_with_follow_up_skips_draft_assistant_and_unregister_race_guarded(self):
        session_id = "private_test_p01"
        mock_state = {"_lock": threading.Lock(), "_pending_follow_ups": ["pending-item"]}
        bot_agent.register_session(session_id, mock_state)
        try:
            self.assertFalse(bot_agent.try_unregister_session_if_idle(session_id, mock_state))
            self.assertTrue(bot_agent.has_active_session(session_id))
            mock_state["_pending_follow_ups"].clear()
            self.assertTrue(bot_agent.try_unregister_session_if_idle(session_id, mock_state))
            self.assertFalse(bot_agent.has_active_session(session_id))
        finally:
            bot_agent.unregister_session(session_id)

        dummy_name = "_p0_test_dummy_tool"
        dummy_spec = bot_agent.ToolSpec(
            name=dummy_name,
            description="dummy",
            parameters={"type": "object", "properties": {}},
            handler=lambda args, ctx: "ok",
            level="user",
        )
        bot_agent.REGISTRY._tools[dummy_name] = dummy_spec
        try:
            messages = [{"role": "user", "content": "先干活"}]
            ctx = bot_agent.AgentContext(user_id="100", is_group=False, user_level="user", extra={})
            settings = bot_agent.AgentSettings(
                enabled=True, max_rounds=4, retry_attempts=2,
                enabled_tools={dummy_name: True}, allowed_names={dummy_name},
            )
            seen_msgs_per_call = []
            calls = 0

            async def complete(msgs, tools):
                nonlocal calls
                calls += 1
                seen_msgs_per_call.append([dict(m) for m in msgs])
                if calls == 1:
                    bot_agent.follow_up_session(session_id, "等下帮我再加一句")
                    return types.SimpleNamespace(content="这是尚未发出的草稿", tool_calls=None), {
                        "total": 10, "prompt": 6, "completion": 4, "cached": 0
                    }
                return types.SimpleNamespace(content="整合后的最终回复", tool_calls=None), {
                    "total": 10, "prompt": 6, "completion": 4, "cached": 0
                }

            result, _usage, _tool_count = await bot_agent.run_tool_loop(
                complete, messages, ctx, settings, session_id=session_id
            )
            self.assertEqual(result, "整合后的最终回复")
            self.assertEqual(calls, 2)
            second_call_msgs = seen_msgs_per_call[1]
            assistant_msgs = [m for m in second_call_msgs if m.get("role") == "assistant"]
            self.assertEqual(assistant_msgs, [])
            notice_msgs = [
                m for m in second_call_msgs
                if m.get("role") == "user" and 'type="follow_up"' in str(m.get("content", ""))
            ]
            self.assertEqual(len(notice_msgs), 1)
            self.assertIn("等下帮我再加一句", notice_msgs[0]["content"])
            self.assertFalse(bot_agent.has_active_session(session_id))
        finally:
            bot_agent.REGISTRY._tools.pop(dummy_name, None)

    async def test_timeout_error_skips_in_place_retry_when_fallback_exists_and_formats_nonempty(self):
        self.assertEqual(bot_agent._format_exc(TimeoutError()), "TimeoutError")
        self.assertIn("TimeoutError", bot_agent._safe_error_text(TimeoutError()))

        dummy_name = "_p0_test_tmo_tool"
        bot_agent.REGISTRY._tools[dummy_name] = bot_agent.ToolSpec(
            name=dummy_name,
            description="dummy",
            parameters={"type": "object", "properties": {}},
            handler=lambda args, ctx: "ok",
            level="user",
        )
        try:
            for has_fallback, expected_calls in ((True, 1), (False, 2)):
                with self.subTest(has_fallback=has_fallback):
                    messages = [{"role": "user", "content": "hi"}]
                    ctx = bot_agent.AgentContext(
                        user_id="100",
                        is_group=False,
                        user_level="user",
                        extra={"has_fallback_candidates": has_fallback},
                    )
                    settings = bot_agent.AgentSettings(
                        enabled=True, max_rounds=2, retry_attempts=3,
                        enabled_tools={dummy_name: True}, allowed_names={dummy_name},
                    )
                    calls = 0

                    class APITimeoutError(Exception):
                        pass

                    async def complete(msgs, tools):
                        nonlocal calls
                        calls += 1
                        raise APITimeoutError()

                    orig_sleep = asyncio.sleep
                    asyncio.sleep = lambda *_a, **_kw: orig_sleep(0)
                    try:
                        with self.assertRaises(APITimeoutError):
                            await bot_agent.run_tool_loop(
                                complete, messages, ctx, settings, session_id=f"private_tmo_{has_fallback}"
                            )
                    finally:
                        asyncio.sleep = orig_sleep
                    self.assertEqual(calls, expected_calls)

            env_fmt = {}
            _exec_main_nodes(env_fmt, "_format_exc_text", "log_api_failure")
            self.assertEqual(env_fmt["_format_exc_text"](TimeoutError()), "TimeoutError")
            self.assertTrue(bot_agent._is_timeout_error(TimeoutError()))
            logged_errs = []
            env_fmt["log_console"] = lambda tag, msg: logged_errs.append((tag, msg))
            env_fmt["log_api_failure"]("s", "m", "k-123456", TimeoutError())
            self.assertTrue(logged_errs and "err=TimeoutError" in logged_errs[0][1])
        finally:
            bot_agent.REGISTRY._tools.pop(dummy_name, None)

    async def test_max_rounds_keeps_follow_up_undelivered_and_timeout_carries_over_to_fallback(self):
        dummy_name = "_p0_test_round_fu_tool"
        bot_agent.REGISTRY._tools[dummy_name] = bot_agent.ToolSpec(
            name=dummy_name,
            description="dummy",
            parameters={"type": "object", "properties": {}},
            handler=lambda args, ctx: "tool-ok",
            level="user",
        )
        try:
            # 1) round_index == max_rounds 时结束，不得把未给模型看过的补充消息记为已投递
            sid = "private_max_rounds_fu"
            ctx = bot_agent.AgentContext(user_id="100", is_group=False, user_level="user", extra={})
            settings = bot_agent.AgentSettings(
                enabled=True, max_rounds=1, retry_attempts=1,
                enabled_tools={dummy_name: True}, allowed_names={dummy_name},
            )
            calls = 0

            async def complete_max(msgs, tools):
                nonlocal calls
                calls += 1
                if calls == 1:
                    return types.SimpleNamespace(
                        content="",
                        tool_calls=[{"id": "t1", "type": "function", "function": {"name": dummy_name, "arguments": "{}"}}],
                    ), {"total": 5, "prompt": 3, "completion": 2, "cached": 0}
                bot_agent.follow_up_session(sid, "来不及看的新消息")
                return types.SimpleNamespace(content="到达上限回复", tool_calls=None), {
                    "total": 5, "prompt": 3, "completion": 2, "cached": 0
                }

            res, _, _ = await bot_agent.run_tool_loop(
                complete_max, [{"role": "user", "content": "start"}], ctx, settings, session_id=sid
            )
            self.assertEqual(res, "到达上限回复")
            self.assertFalse(ctx.extra.get("follow_ups_delivered"))

            # 2) Attempt 1 TimeoutError 退出的未投递 Follow-Up 必须由 Attempt 2 继承并立即送入模型
            sid_fail = "private_tmo_carry"
            ctx1 = bot_agent.AgentContext(user_id="100", is_group=False, user_level="user", extra={"has_fallback_candidates": True})
            settings_tmo = bot_agent.AgentSettings(
                enabled=True, max_rounds=3, retry_attempts=1,
                enabled_tools={dummy_name: True}, allowed_names={dummy_name},
            )

            async def complete_tmo(msgs, tools):
                bot_agent.follow_up_session(sid_fail, "在不在")
                raise TimeoutError()

            with self.assertRaises(TimeoutError):
                await bot_agent.run_tool_loop(
                    complete_tmo, [{"role": "user", "content": "first"}], ctx1, settings_tmo, session_id=sid_fail
                )
            carried = list(ctx1.extra.get("carry_over_follow_ups") or [])
            self.assertEqual(len(carried), 1)

            seen_attempt2 = []

            async def complete_ok(msgs, tools):
                seen_attempt2.append([dict(m) for m in msgs])
                return types.SimpleNamespace(content="我在的", tool_calls=None), {
                    "total": 5, "prompt": 3, "completion": 2, "cached": 0
                }

            ctx2 = bot_agent.AgentContext(
                user_id="100", is_group=False, user_level="user",
                extra={"carry_over_follow_ups": carried},
            )
            res2, _, _ = await bot_agent.run_tool_loop(
                complete_ok, [{"role": "user", "content": "first"}], ctx2, settings_tmo, session_id=sid_fail
            )
            self.assertEqual(res2, "我在的")
            self.assertEqual(ctx2.extra.get("follow_ups_delivered"), ["在不在"])
            self.assertTrue(any("在不在" in str(m.get("content", "")) for m in seen_attempt2[0]))
        finally:
            bot_agent.REGISTRY._tools.pop(dummy_name, None)

    async def test_agen_content_no_double_stream_no_draft_leak_and_keeps_mixed_followups(self):
        import copy as _copy
        import re as _re
        import time as _time
        from bot.agent_context import AgentTurnContext
        from bot.rounds import extract_agent_trail, fix_messages, split_into_rounds

        dummy_name = "_p0_test_stream_fu_tool"
        bot_agent.REGISTRY._tools[dummy_name] = bot_agent.ToolSpec(
            name=dummy_name,
            description="dummy",
            parameters={"type": "object", "properties": {}},
            handler=lambda args, ctx: "tool-done",
            level="user",
        )
        try:
            _src, nodes = _extract_main_nodes("_format_exc_text", "EnhancedLimitedDeepSeekContext")
            cls_node = next(n for n in nodes if isinstance(n, ast.ClassDef))

            class _FakeKM:
                def get_attempt_count(self):
                    return 2

                def get_next_for_request(self, **kw):
                    return ("https://mock/v1", "KEY-TEST-1234", "mock-model", False, 1, None, "", {})

                def make_attempt_identity(self, *args):
                    return args

                def mark_success(self, *args, **kw):
                    pass

                def mark_failure(self, *args, **kw):
                    pass

                def exclude_model_slot(self, *args, **kw):
                    pass

            class _FailoverRefusal(Exception):
                pass

            attempt_no = 0
            loop_step = 0

            async def fake_req_completion(client, kwargs, timeout=None, stream_callback=None, reasoning_callback=None, abort=None):
                nonlocal attempt_no, loop_step
                loop_step += 1
                if loop_step == 1:
                    attempt_no = 1
                    if callable(stream_callback):
                        stream_callback("不能透露的系统草稿")
                    return types.SimpleNamespace(
                        choices=[types.SimpleNamespace(message=types.SimpleNamespace(content="不能透露的系统草稿", tool_calls=None))],
                        usage=types.SimpleNamespace(total_tokens=10, prompt_tokens=5, completion_tokens=5),
                    )
                if loop_step == 2:
                    attempt_no = 2
                    bot_agent.follow_up_session("private_stream_fu", "工具轮插话")
                    return types.SimpleNamespace(
                        choices=[types.SimpleNamespace(message=types.SimpleNamespace(
                            content="",
                            tool_calls=[{"id": "tc1", "type": "function", "function": {"name": dummy_name, "arguments": "{}"}}],
                        ))],
                        usage=types.SimpleNamespace(total_tokens=10, prompt_tokens=5, completion_tokens=5),
                    )
                if loop_step == 3:
                    bot_agent.follow_up_session("private_stream_fu", "文本轮插话")
                    if callable(stream_callback):
                        stream_callback("未合并草稿")
                    return types.SimpleNamespace(
                        choices=[types.SimpleNamespace(message=types.SimpleNamespace(content="未合并草稿", tool_calls=None))],
                        usage=types.SimpleNamespace(total_tokens=10, prompt_tokens=5, completion_tokens=5),
                    )
                if callable(stream_callback):
                    stream_callback("最终合流回复")
                return types.SimpleNamespace(
                    choices=[types.SimpleNamespace(message=types.SimpleNamespace(content="最终合流回复", tool_calls=None))],
                    usage=types.SimpleNamespace(total_tokens=10, prompt_tokens=5, completion_tokens=5),
                )

            class _BaseCtx:
                def __init__(self, system_prompt="", *args, **kwargs):
                    self.system_prompt = system_prompt
                    self.history = []
                    self.max_rounds = 10
                    self.max_context_tokens = 0
                    self._history_lock = threading.Lock()
                    self.total_tokens = 0
                    self.total_calls = 0
                    self.auto_compress_enabled = False
                    self.compressor = None
                    self.session_id = "private_stream_fu"
                    self.context_type = "private"
                    self.chat_id = "100"

                def _clean_content(self, c):
                    return str(c or "")

                def _get_client(self, *a, **kw):
                    return object()

                def _build_messages(self, user_prompt=None, **kw):
                    msgs = [{"role": "system", "content": "sys"}]
                    if user_prompt is not None:
                        msgs.append({"role": "user", "content": user_prompt})
                    return msgs

                def _enforce_message_limit(self):
                    pass

                def get_message_count(self):
                    return 0

            async def _acquire_lock(lock, timeout=None):
                lock.acquire()
                return True

            async def _fake_prepare_turn_images(ctx_s, msg, supports_mm, direct=False):
                return str(msg.get("text", "")), [], [], (0, 0, 0)

            import bot.agent_stream as _agent_stream_mod
            import datetime as _datetime
            env = {
                "asyncio": asyncio,
                "datetime": _datetime,
                "threading": threading,
                "re": _re,
                "time": _time,
                "copy": _copy,
                "ContextCompressor": object,
                "LimitedDeepSeekContext": _BaseCtx,
                "_normal_ai_state": lambda: None,
                "key_manager": _FakeKM(),
                "is_agent_enabled_for": lambda is_grp, cid: True,
                "get_agent_settings": lambda: bot_agent.AgentSettings(
                    enabled=True, max_rounds=5, retry_attempts=1,
                    enabled_tools={dummy_name: True}, allowed_names={dummy_name},
                ),
                "build_agent_context": lambda **kw: bot_agent.AgentContext(
                    user_id="100", is_group=False, user_level="user", extra={}
                ),
                "_agent_fs_tools": types.SimpleNamespace(primary_root=lambda ctx: "."),
                "AGENT_REGISTRY": bot_agent.REGISTRY,
                "is_trace_recording": lambda: False,
                "AgentTurnContext": AgentTurnContext,
                "extract_agent_trail": extract_agent_trail,
                "fix_messages": fix_messages,
                "split_into_rounds": split_into_rounds,
                "get_runtime_setting": lambda *a, **kw: {},
                "get_runtime_others": lambda: {},
                "normalize_bool_config": lambda v, default=False: bool(v),
                "get_multimodal_image_mode": lambda: "relay",
                "get_configured_multimodal_model": lambda: "",
                "_prepare_history_turn_images": _fake_prepare_turn_images,
                "_merge_extra_user_suffix": lambda a, b: a + ("\n" + b if b else ""),
                "build_llm_user_message": lambda s: s,
                "build_openai_message_content": lambda text, **kw: text,
                "user_cfg": {"auto_compress_after_messages": 40},
                "clamp_compression_trigger_rounds": lambda a, b: a,
                "log_api_request": lambda *a, **kw: None,
                "log_api_success": lambda *a, **kw: None,
                "log_api_failure": lambda *a, **kw: None,
                "add_token_usage": lambda *a, **kw: None,
                "split_reasoning_output": lambda msg: (msg.content, ""),
                "_extract_tool_calls": lambda msg: msg.tool_calls,
                "tool_call_names": lambda calls: [c["function"]["name"] for c in calls],
                "check_final_reply_for_failover": lambda text: (_ for _ in ()).throw(_FailoverRefusal("refused")) if "不能透露" in text else None,
                "filter_sensitive_content": lambda s: str(s or ""),
                "_history_user_message": lambda ctx, content, refs, msgs: {"role": "user", "content": content},
                "_format_exc_text": lambda e: f"{type(e).__name__}: {e}",
                "acquire_history_lock": _acquire_lock,
                "_HISTORY_LOCK_TIMEOUT": 5.0,
                "AGENT_ABORTS": types.SimpleNamespace(begin=lambda sid: None, end=lambda sid: None),
                "ReplySuppressed": type("ReplySuppressed", (Exception,), {}),
                "traceback": __import__("traceback"),
                "ReplyKeywordFailure": _FailoverRefusal,
                "AgentContext": bot_agent.AgentContext,
                "run_tool_loop": bot_agent.run_tool_loop,
                "_run_tool_loop": bot_agent.run_tool_loop,
                "print": lambda *a, **kw: None,
            }
            exec(compile(ast.Module(body=[cls_node], type_ignores=[]), str(ROOT / "main.py"), "exec"), env)

            ctx_obj = env["EnhancedLimitedDeepSeekContext"](system_prompt="sys", session_id="private_stream_fu")
            ctx_obj._save_memory = lambda: True
            ctx_obj._prune_history_images = lambda: None
            streamed = []
            orig_fn = _agent_stream_mod.request_chat_completion
            _agent_stream_mod.request_chat_completion = fake_req_completion
            try:
                out, *_ = await ctx_obj.agen_content(
                    {"text": "主问题"},
                    agent_meta={"stream_callback": streamed.append, "user_id": "100"},
                )
            finally:
                _agent_stream_mod.request_chat_completion = orig_fn

            self.assertEqual(out, "最终合流回复")
            self.assertEqual(streamed, ["最终合流回复"])
            user_hist = next(m["content"] for m in ctx_obj.history if m["role"] == "user")
            self.assertIn("文本轮插话", user_hist)

            # P0-1 补充：Attempt 2 注入了结转 Follow-Up 但未调任何工具就失败时，Attempt 3 仍必须保留 Agent tools；
            # 且 summary-only 重试若 _complete 失败不得提前消费 carry_over_follow_ups，成功时须透传 image_sources。
            seen_tools_per_call = []
            builder_calls = []
            fu_src = bot_agent.FollowUpTicket(
                seq=1,
                text="跨三跳插话",
                image_urls=("https://img/carry.png",),
                image_sources=(types.SimpleNamespace(url="https://img/carry.png", message_id="10", author_id="100"),),
            )
            hop = 0

            async def multi_hop_req(client, kwargs, timeout=None, stream_callback=None, reasoning_callback=None, abort=None):
                nonlocal hop
                hop += 1
                seen_tools_per_call.append(bool(kwargs.get("tools")))
                if hop == 1:
                    raise RuntimeError("attempt2 fail without tool")
                if callable(stream_callback):
                    stream_callback("第三跳成功")
                return types.SimpleNamespace(
                    choices=[types.SimpleNamespace(message=types.SimpleNamespace(content="第三跳成功", tool_calls=None))],
                    usage=types.SimpleNamespace(total_tokens=8, prompt_tokens=4, completion_tokens=4),
                )

            class _FakeKM3(_FakeKM):
                def get_attempt_count(self):
                    return 3

            async def _fake_builder(urls, sources=()):
                builder_calls.append((list(urls), tuple(sources)))
                return [{"type": "text", "text": "[图]"}]

            base_build_ctx = env["build_agent_context"]
            shared_ctx = bot_agent.AgentContext(
                user_id="100", is_group=False, user_level="user",
                extra={"carry_over_follow_ups": [fu_src]},
            )
            env["key_manager"] = _FakeKM3()
            env["build_agent_context"] = lambda **kw: shared_ctx
            orig_get_settings = env["get_agent_settings"]

            def _settings_with_builder():
                s = orig_get_settings()
                s.follow_up_image_builder = _fake_builder
                return s

            env["get_agent_settings"] = _settings_with_builder
            _agent_stream_mod.request_chat_completion = multi_hop_req
            try:
                out_hop, *_ = await ctx_obj.agen_content(
                    {"text": "再试一次"},
                    agent_meta={"user_id": "100"},
                )
            finally:
                _agent_stream_mod.request_chat_completion = orig_fn
                env["build_agent_context"] = base_build_ctx
                env["get_agent_settings"] = orig_get_settings

            self.assertEqual(out_hop, "第三跳成功")
            self.assertEqual(seen_tools_per_call, [True, True])
        finally:
            bot_agent.REGISTRY._tools.pop(dummy_name, None)


class P03EmojiBusyTests(unittest.TestCase):
    def test_is_session_busy_for_emoji_checks_int_and_str_keys_with_agent_busy_and_lock(self):
        lock = threading.Lock()
        ctx_idle = types.SimpleNamespace(_busy=0, _history_lock=lock)
        ctx_busy = types.SimpleNamespace(_busy=1, _history_lock=threading.Lock())
        ctx_str = types.SimpleNamespace(_busy=1, _history_lock=threading.Lock())
        cmc = types.SimpleNamespace(
            _manager_lock=threading.Lock(),
            groups={1001: ctx_idle, 1002: ctx_busy, "3003": ctx_str},
            private_chats={2001: ctx_idle},
        )
        active = set()
        env = {
            "cmc": cmc,
            "_has_active_session": lambda sid: sid in active,
        }
        _exec_main_nodes(env, "_is_session_busy_for_emoji")
        fn = env["_is_session_busy_for_emoji"]

        self.assertFalse(fn(True, "1001"))
        self.assertFalse(fn(True, 1001))
        self.assertTrue(fn(True, "1002"))
        self.assertTrue(fn(True, 1002))
        self.assertTrue(fn(True, "3003"))
        active.add("private_2001")
        self.assertTrue(fn(False, "2001"))
        self.assertTrue(fn(False, 2001))
        active.clear()
        with lock:
            self.assertTrue(fn(True, "1001"))
            self.assertTrue(fn(True, 1001))


class P04AnyPluginTests(unittest.IsolatedAsyncioTestCase):
    async def test_any_plugin_listed_called_without_prefix_and_not_double_executed(self):
        import copy as _copy
        import importlib.util as _iu

        spec = _iu.spec_from_file_location("real_simple_like", str(ROOT / "plugins" / "simple_like" / "setup.py"))
        like_plugin = _iu.module_from_spec(spec)
        like_plugin.PLUGIN_NAME = "simple_like"
        spec.loader.exec_module(like_plugin)
        _tmp_like = tempfile.TemporaryDirectory()
        self.addCleanup(_tmp_like.cleanup)
        like_plugin._STATS_FILE = str(pathlib.Path(_tmp_like.name) / "simple_like_stats.json")

        weather_calls = []
        weather_plugin = types.ModuleType("weather_plugin")
        weather_plugin.PLUGIN_NAME = "weather"
        weather_plugin.TRIGGHT_KEYWORD = "天气"
        weather_plugin.HELP_MESSAGE = "天气 城市"

        async def weather_on_message(order="", user_message="", event=None):
            weather_calls.append({"order": order, "user_message": user_message, "event": event})
            return True

        weather_plugin.on_message = weather_on_message

        class FakeEvent:
            def __init__(self):
                self.user_id = 12345
                self.message = "原本的消息"
                self.raw_message = "原本的消息"
                self.msg_str = "原本的消息"

        fake_event = FakeEvent()
        sent_replies = []

        class _Actions:
            def __init__(self):
                self.custom = self

            async def send(self, **kw):
                sent_replies.append(kw)

            async def send_like(self, **kw):
                sent_replies.append(("like", kw))

        _manager = types.SimpleNamespace(Message=lambda *a: list(a))
        _segments = types.SimpleNamespace(Text=lambda t: types.SimpleNamespace(text=t))
        env = {
            "asyncio": asyncio,
            "copy": _copy,
            "inspect": __import__("inspect"),
            "re": __import__("re"),
            "plugins": [like_plugin, weather_plugin],
            "reminder": "/",
            "ROOT_User": ["10000"],
            "Manager": _manager,
            "Segments": _segments,
            "is_feature_enabled": lambda name, default=True: True,
            "render_plugin_help": lambda msg, kw: str(msg or "").replace("{kw}", kw),
            "_agent_plugin_keyword_matches": lambda kw, cmd: cmd == kw or cmd.startswith(kw + " "),
            "agent_tool": lambda **kw: (lambda fn: fn),
            "_agent_actions": lambda ctx: _Actions(),
            "build_plugin_base_context": lambda actions, event, admins, supers: {
                "actions": actions, "event": event, "Manager": _manager, "Segments": _segments, "is_group": False
            },
            "_make_plugin_logger": lambda name: (lambda *a, **kw: None),
            "get_plugin_config": lambda name, key, default=None: default,
            "_ensure_chat_response_allowed": lambda req: None,
            "CURRENT_REQUEST": message_runtime.CURRENT_REQUEST,
            "ReplySuppressed": type("ReplySuppressed", (Exception,), {}),
            "traceback": __import__("traceback"),
        }
        _exec_main_nodes(
            env,
            "_extract_any_plugin_triggers",
            "_PluginCommandEventProxy",
            "_make_plugin_command_event",
            "_agent_plugin_entries",
            "_agent_list_plugins",
            "_agent_call_plugin",
            "execute_plugins",
        )

        # execute_plugins(False, ...) 对真实 /Any 以及 spy Any 插件均不得执行（不双重触发）
        spy_any_calls = []
        spy_any = types.ModuleType("spy_any")
        spy_any.PLUGIN_NAME = "spy_any"
        spy_any.TRIGGHT_KEYWORD = "Any"

        async def spy_on_message(order="", user_message="", event=None):
            spy_any_calls.append((order, user_message))
            return True

        spy_any.on_message = spy_on_message
        env["plugins"] = [like_plugin, weather_plugin, spy_any]
        _base_kw = {"actions": _Actions(), "Manager": _manager, "Segments": _segments, "is_group": False}
        self.assertFalse(await env["execute_plugins"](False, order="Any", user_message="/Any", event=fake_event, **_base_kw))
        self.assertFalse(await env["execute_plugins"](False, order="赞我", user_message="/赞我", event=fake_event, **_base_kw))
        self.assertEqual(spy_any_calls, [])
        env["plugins"] = [like_plugin, weather_plugin]
        self.assertFalse(await env["execute_plugins"](True, order="Any", user_message="/Any", event=fake_event, **_base_kw))

        # _make_plugin_command_event 保留原类 isinstance 与 msg_str 且不污染原对象
        cloned_ev = env["_make_plugin_command_event"](fake_event, "新命令")
        self.assertIsInstance(cloned_ev, FakeEvent)
        self.assertEqual(cloned_ev.msg_str, "新命令")
        self.assertEqual(cloned_ev.raw_message, "新命令")
        self.assertEqual(fake_event.msg_str, "原本的消息")

        listing = await env["_agent_list_plugins"]({}, None)
        self.assertIn("simple_like：无前缀触发（Any）", listing)
        self.assertIn("weather：触发词「天气」", listing)

        ctx = types.SimpleNamespace(
            event=fake_event,
            user_id="12345",
            group_id=None,
            is_group=False,
            say=lambda *a, **kw: None,
        )
        res_like = await env["_agent_call_plugin"]({"command": "赞我"}, ctx)
        self.assertIn("执行", res_like)
        self.assertTrue(any(item[0] == "like" for item in sent_replies if isinstance(item, tuple)))
        # 通过插件名触发也可命中同一插件并由插件自身校验入参（无双执行、无参数错误）
        res_by_name = await env["_agent_call_plugin"]({"command": "simple_like"}, ctx)
        self.assertIn("simple_like", res_by_name)
        res_prefix = await env["_agent_call_plugin"]({"command": "天气 北京"}, ctx)
        self.assertIn("执行", res_prefix)
        self.assertEqual(weather_calls[-1]["order"], "天气 北京")
        self.assertEqual(weather_calls[-1]["user_message"], "/天气 北京")
        self.assertEqual(fake_event.message, "原本的消息")
        self.assertEqual(fake_event.raw_message, "原本的消息")

    def test_load_plugins_rejects_empty_and_whitespace_keyword(self):
        import os as _os
        import sys as _sys
        import uuid as _uuid
        import importlib.util as _iu

        with tempfile.TemporaryDirectory() as tmp:
            pdir = pathlib.Path(tmp)
            bad_dir = pdir / "blank_kw_plugin"
            bad_dir.mkdir()
            (bad_dir / "setup.py").write_text(
                'TRIGGHT_KEYWORD = "   "\nasync def on_message(**kw):\n    return False\n',
                encoding="utf-8",
            )
            env = {
                "os": _os,
                "sys": _sys,
                "uuid": _uuid,
                "importlib": __import__("importlib"),
                "loaded_plugins": [],
                "disabled_plugins": [],
                "failed_plugins": [],
                "plugins": [],
                "plugins_help": "",
                "reminder": "/",
                "bot_name": "XcBot",
                "PLUGIN_FOLDER": str(pdir),
                "version_name": "test",
                "_plugin_config_schemas": {},
                "_register_plugin_config_schema": lambda *a, **kw: None,
                "print": lambda *a, **kw: None,
            }
            env["importlib"].util = _iu
            _exec_main_nodes(env, "load_plugins")
            env["load_plugins"](verbose=False)
            self.assertEqual(env["plugins"], [])
            self.assertTrue(any("blank_kw_plugin" in item for item in env["failed_plugins"]))


class P06QuoteRuntimeAndLoggedActionsTests(unittest.IsolatedAsyncioTestCase):
    async def test_logged_actions_shares_sent_scopes_across_instances_and_fetch_ret_timeout(self):
        env = {
            "asyncio": asyncio,
            "threading": threading,
            "OrderedDict": OrderedDict,
            "CURRENT_REQUEST": message_runtime.CURRENT_REQUEST,
            "INDEX": message_runtime.MessageIndex(),
            "quote_id": message_runtime.quote_id,
            "quote_receipt_data": message_runtime.quote_receipt_data,
            "_receipt_succeeded": lambda ret: isinstance(ret, dict) and ret.get("retcode") == 0,
            "_extract_send_message_id": lambda ret: (ret.get("data") or {}).get("message_id"),
            "_message_facts": lambda msg, media_limit=100: (str(msg or ""), [], None),
            "log_console": lambda *a, **kw: None,
            "_agent_action_receipt_meta": {},
            "_agent_action_receipt_lock": threading.Lock(),
            "_current_qq_actions": None,
            "_current_bot_self_id": "10086",
            "_qq_actions_lock": threading.Lock(),
        }
        got_timeouts = []

        class FakeReports:
            @staticmethod
            def get(echo, timeout=None):
                got_timeouts.append(timeout)
                return {"echo": echo, "retcode": 0, "data": {"message_id": 7}}

        env["Manager"] = types.SimpleNamespace(reports=FakeReports())
        _exec_main_nodes(env, "_agent_fetch_ret", "LoggedActions", "clear_current_qq_actions")

        ret = await env["_agent_fetch_ret"]("echo-1", timeout=3.0)
        self.assertEqual(ret["retcode"], 0)
        self.assertEqual(got_timeouts, [3.0])

        LoggedActions = env["LoggedActions"]
        a1 = LoggedActions(object(), self_id="10086")
        a1._record_receipt({"retcode": 0, "data": {"message_id": 999}}, ("10086", True, "55555"), "hi", 1.0)
        a2 = LoggedActions(object(), self_id="10086")
        self.assertEqual(a2.get_quote_sent_scope(999), ("10086", True, "55555"))

        a_other = LoggedActions(object(), self_id="99999")
        self.assertIsNone(a_other.get_quote_sent_scope(999))

        env["_current_qq_actions"] = a2
        env["clear_current_qq_actions"](a2)
        self.assertIsNone(a1.get_quote_sent_scope(999))

        # delete_with_receipt 传入 timeout=10.0
        async def _fake_del_msg(message_id=None):
            return "del-echo"

        a_del = LoggedActions(
            types.SimpleNamespace(custom=types.SimpleNamespace(delete_msg=_fake_del_msg)),
            self_id="20001",
        )
        got_timeouts.clear()
        del_ret = await a_del.delete_with_receipt("77")
        self.assertEqual(del_ret["retcode"], 0)
        self.assertEqual(got_timeouts, [10.0])

        # _maintain_event_runtime 切号时清空上一账号的 sent scopes
        a_old = LoggedActions(object(), self_id="10086")
        a_old._record_receipt({"retcode": 0, "data": {"message_id": 888}}, ("10086", True, "55555"), "hi", 1.0)
        rt_env = {
            "threading": threading,
            "_qq_actions_lock": threading.RLock(),
            "_agent_startup_lock": threading.Lock(),
            "_current_qq_actions": a_old,
            "_current_bot_self_id": "10086",
            "_proactive_connection_generation": None,
            "_proactive_bootstrap_connection": lambda *a, **kw: None,
            "_agent_startup_tasks_sync": lambda: None,
            "LoggedActions": LoggedActions,
        }
        _exec_main_nodes(rt_env, "_maintain_event_runtime")
        from unittest.mock import patch as _patch
        from bot import proactive_reply as _pr
        with _patch.object(_pr, "cancel_observations"):
            rt_env["_maintain_event_runtime"](types.SimpleNamespace(self_id="20260"), object())
        self.assertIsNone(LoggedActions(object(), self_id="10086").get_quote_sent_scope(888))

        # P0-6：clear_sent_scopes 之后，clear 前创建的 LoggedActions 仍指向动态桶，且 del_message 支持 quote_id 查表撤回
        async def _ok_del(*_a, **_kw):
            return {"status": "ok", "retcode": 0}

        a_live = LoggedActions(
            types.SimpleNamespace(del_message=_ok_del),
            self_id="30001",
        )
        LoggedActions.clear_sent_scopes("30001")
        a_live._record_receipt({"status": "ok", "retcode": 0, "data": {"message_id": 654}}, ("30001", True, "888"), "hello", 1.0)
        self.assertEqual(a_live.get_quote_sent_scope(654), ("30001", True, "888"))
        self.assertEqual(LoggedActions(object(), self_id="30001").get_quote_sent_scope(654), ("30001", True, "888"))
        await a_live.del_message(654)
        self.assertTrue(env["INDEX"].resolve("30001", True, "888", "654")["recalled"])

    async def test_find_quote_in_history_skips_malformed_items_and_forwards_message_seq(self):
        seen_calls = []

        class Actions:
            async def get_group_msg_history(self, **kwargs):
                seen_calls.append(kwargs)
                return {
                    "status": "ok",
                    "retcode": 0,
                    "data": {
                        "messages": [
                            "not-a-dict",
                            {"message_id": 10, "message_type": "group", "group_id": 888},
                            {
                                "message_id": 42,
                                "message_type": "group",
                                "group_id": 3,
                                "sender": {"user_id": 8},
                                "message": [{"type": "text", "data": {"text": "目标消息"}}],
                            },
                        ]
                    },
                }

        state = message_runtime.RequestState(
            self_id="1",
            is_group=True,
            chat_id="3",
            decision=message_runtime.ReplyDecision("1", True, "3", source="normal"),
        )
        found, reason = await message_runtime.find_quote_in_history(
            Actions(), state, "42", message_seq=777
        )
        self.assertIsNone(reason)
        self.assertEqual(found["message_id"], 42)
        self.assertEqual(seen_calls[0].get("message_seq"), 777)

        seen_calls.clear()
        found0, reason0 = await message_runtime.find_quote_in_history(
            Actions(), state, "42", message_seq=0
        )
        self.assertIsNone(reason0)
        self.assertEqual(found0["message_id"], 42)
        self.assertEqual(len(seen_calls), 1)
        self.assertNotIn("message_seq", seen_calls[0])

        proven, scope_reason = message_runtime.quote_scope(
            {"message_type": "private", "user_id": 0, "target_id": "", "sender": {"user_id": 9}},
            "1", False, "9", author="9",
        )
        self.assertIsNone(scope_reason)
        self.assertTrue(proven)


class P21QuoteSwitchTests(unittest.TestCase):
    def test_migrate_split_reply_quote_and_feature_meta(self):
        quote_meta = next(item for item in FEATURE_META if item["key"] == "split_reply_quote")
        self.assertIn("仅群聊", quote_meta["desc"])
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            cfg_path = root / "config.json"
            tpl_path = root / "config.default.json"
            bak_dir = root / "bak"
            tpl_data = json.loads((ROOT / "config.default.json").read_text(encoding="utf-8"))
            tpl_path.write_text(json.dumps(tpl_data, ensure_ascii=False), encoding="utf-8")
            old_cfg = {
                "config_version": 1,
                "split_reply_quote": {"default_enabled": False},
                "Others": {},
            }
            cfg_path.write_text(json.dumps(old_cfg, ensure_ascii=False), encoding="utf-8")
            changed = ensure_config_up_to_date(str(cfg_path), str(tpl_path), str(bak_dir))
            self.assertTrue(changed)
            migrated = json.loads(cfg_path.read_text(encoding="utf-8"))
            self.assertIs(migrated["FeatureSwitches"]["split_reply_quote"], False)


if __name__ == "__main__":
    unittest.main()
