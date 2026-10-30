"""从入口 AST 提取真实函数做离线集成测试，不执行入口、抢锁或连接 QQ。"""
import ast
import asyncio
import pathlib
import threading
import types
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
TREE = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))


def extracted(names, env):
    nodes = [node for node in TREE.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name in names]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "main.py", "exec"), env)
    return env


class FakeContext:
    def __init__(self):
        self.history = [{"role": "user", "content": "明天考试"}]
        self._history_lock = threading.Lock()
        self._busy = 0
        self.total_tokens = 0
        self.saved = 0

    def _enforce_message_limit(self):
        pass

    def _save_memory(self):
        self.saved += 1


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    def environment(self, outputs, fail_at=None):
        import json
        from bot import proactive_reply
        context = FakeContext()
        calls, sends, notifications = [], [], []
        state = {"activity_version": 1}
        permit = {"value": True}

        class Model:
            def __init__(self, prompt):
                self.prompt = prompt

            async def agen_content(self, prompt, **kwargs):
                calls.append((self.prompt, prompt, kwargs))
                return outputs.pop(0), 10, 6, 4

        class Actions:
            async def send(self, **kwargs):
                if fail_at is not None and len(sends) == fail_at:
                    raise RuntimeError("模拟发送失败")
                sends.append(kwargs)
                return {"status": "ok", "retcode": 0}

        async def sleep(_):
            return None

        async def generate(writer, prompt, request):
            text, total, _, _ = await writer.agen_content(prompt)
            writer.last_trace_id = "writer-offline"
            return text, total

        def social(request):
            return types.SimpleNamespace(finished=False, wait_seconds=None, consume_reply=lambda: None)

        env = {
            "asyncio": types.SimpleNamespace(sleep=sleep), "json": json,
            "time": __import__("time"), "random": __import__("random"), "re": __import__("re"),
            "_make_social_context": social, "_generate_proactive_content": generate,
            "_ai_reply_allowed": lambda *args, **kwargs: permit["value"],
            "_current_bot_self_id": "999",
            "_PROACTIVE_GROUP_SYSTEM": "group judge", "_PROACTIVE_PRIVATE_SYSTEM": "private judge",
            "_ProactiveContext": Model, "_get_planner_manager": lambda: "planner-manager",
            "get_runtime_setting": lambda *args: {},
            "_proactive_target_allowed": lambda *args: permit["value"],
            "_qq_actions_lock": threading.Lock(), "_current_qq_actions": Actions(),
            "cmc": types.SimpleNamespace(get_context=lambda *args: context, _get_system_prompt=lambda *args: "persona"),
            "proactive_group_chat_context": types.SimpleNamespace(tail=lambda *args, **kwargs: ["群友: 明天考试"]),
            "get_all_blacklist": lambda: set(),
            "get_group_chat_context_max_messages": lambda: 30,
            "find_llm_reply_failover_keyword": lambda text: "blocked" if "blocked" in text else "",
            "build_final_send_parts": lambda text: text.split("<split>"),
            "Manager": types.SimpleNamespace(Message=lambda x: x),
            "Segments": types.SimpleNamespace(Text=lambda x: x),
            "trace_sends": [],

        }
        env["attach_trace_send"] = lambda trace_id, parts, ids: env["trace_sends"].append((trace_id, parts, ids)) or True
        initial_prompt = next(node for node in TREE.body if isinstance(node, ast.Assign)
                              and any(isinstance(target, ast.Name) and target.id == "_PROACTIVE_INITIAL_WAIT_SYSTEM"
                                      for target in node.targets))
        env["_PROACTIVE_INITIAL_WAIT_SYSTEM"] = ast.literal_eval(initial_prompt.value)
        extracted({"is_user_blacklisted", "_hard_blacklisted_author", "_memory_author_visible",
                   "_receipt_succeeded", "_extract_send_message_id", "_proactive_parse_reply", "_proactive_run_planner"}, env)
        patches = [
            patch.object(proactive_reply, "get_session_state", lambda *args: state),
            patch.object(proactive_reply, "can_send", lambda *args, **kwargs: permit["value"] and len(notifications) < 2),
            patch.object(proactive_reply, "notify_bot_message_sent", lambda *args, **kwargs: notifications.append(kwargs)),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        return env, context, calls, sends, notifications, permit

    async def test_group_planner_filters_structured_history_and_dynamic_tail(self):
        from bot.group_chat_context import GroupChatContextManager
        env, ctx, calls, sends, _, _ = self.environment(["[PASS]"])
        blacklist = {"8,备注"}
        env["get_all_blacklist"] = lambda: blacklist
        ctx.history = [{"role": "user", "author_id": "8", "content": "旧黑作者历史"},
                       {"role": "user", "content": "无作者摘要保留 8"}]
        manager = GroupChatContextManager()
        manager.record("123", "旧黑作者旁听", author_id="8")
        manager.record("123", "正常旁听含数字 8", author_id="9")
        env["proactive_group_chat_context"] = manager
        self.assertFalse(await env["_proactive_run_planner"](True, "123", {}))
        self.assertEqual(len(calls), 1)
        self.assertNotIn("旧黑作者", calls[0][1])
        self.assertIn("无作者摘要保留 8", calls[0][1])
        self.assertIn("正常旁听含数字 8", calls[0][1])
        self.assertEqual(ctx.history[0]["content"], "旧黑作者历史")
        self.assertEqual(manager.size("123"), 2)
        self.assertFalse(sends)

    async def test_pass_does_not_call_writer(self):
        env, ctx, calls, sends, _, _ = self.environment(["[PASS]\n先不打扰"])
        self.assertFalse(await env["_proactive_run_planner"](False, "123", {}))
        self.assertEqual(len(calls), 1)
        self.assertFalse(sends)
        self.assertEqual(ctx.saved, 0)
        self.assertFalse(ctx._history_lock.locked())

    async def test_two_stages_and_message_limit(self):
        env, ctx, calls, sends, notes, _ = self.environment(["[SEND]\n关心考试", "加油<split>早点休息<split>第三条"])
        self.assertTrue(await env["_proactive_run_planner"](False, "123", {}))
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][2]["model_manager"], "planner-manager")
        self.assertEqual(calls[1][0], "persona")
        self.assertEqual(calls[1][2], {})
        self.assertEqual(len(sends), 2)
        self.assertEqual(len(notes), 2)
        self.assertEqual([m["content"] for m in ctx.history if m["role"] == "assistant"], ["加油", "早点休息"])
        self.assertEqual(ctx.saved, 2)

    async def test_observation_reply_runs_restricted_followup_wait_without_writer(self):
        from bot import proactive_reply
        env, _, calls, sends, _, _ = self.environment(["[SEND]\n话题", "正文", "[WAIT 42]"])
        waits = []
        with patch.object(proactive_reply, "set_observation_wait",
                          side_effect=lambda *args, **kwargs: waits.append((args, kwargs)) or True):
            self.assertTrue(await env["_proactive_run_planner"](
                False, "123", {"_observation": {"generation": 7}}))
        self.assertEqual(len(sends), 1)
        self.assertEqual([calls[0][0], calls[1][0]], ["private judge", "persona"])
        self.assertIn("只能选择 wait 或 finish", calls[2][1])
        self.assertIn("只能选择 wait 或 finish", calls[2][1])
        self.assertNotIn("调用主模型生成回复", calls[2][1])
        self.assertEqual(len(waits), 1)
        self.assertEqual(waits[0][1], {"expected_activity_version": 1,
                                      "expected_generation": 7, "planner_choice": True})
        self.assertEqual(waits[0][0][2], 42.0)

    async def test_observation_reply_followup_finish_and_invalid_fallback(self):
        from bot import proactive_reply
        for followup, expected in (("[PASS]", "finish"), ("[SEND]\n禁止正文", "wait")):
            with self.subTest(followup=followup):
                env, _, calls, sends, _, _ = self.environment(["[SEND]\n话题", "正文", followup])
                waits, finishes = [], []
                with patch.object(proactive_reply, "set_observation_wait",
                                  side_effect=lambda *args, **kwargs: waits.append((args, kwargs)) or True), \
                     patch.object(proactive_reply, "finish_observation",
                                  side_effect=lambda *args, **kwargs: finishes.append((args, kwargs)) or True):
                    self.assertTrue(await env["_proactive_run_planner"](
                        True, "123", {"_observation": {"generation": 8}}))
                self.assertEqual(len(calls), 3)
                if expected == "finish":
                    self.assertEqual(len(finishes), 1)
                    self.assertFalse(waits)
                else:
                    self.assertEqual(len(waits), 1)
                    self.assertIsNone(waits[0][0][2])
                    self.assertFalse(finishes)
                self.assertEqual(len(sends), 1)

    async def test_observation_reply_followup_exception_uses_default_wait(self):
        from bot import proactive_reply
        env, _, _, sends, _, _ = self.environment(["[SEND]\n话题", "正文", "ignored"])
        original = env["_ProactiveContext"].agen_content
        count = [0]
        async def failing_followup(instance, prompt, **kwargs):
            count[0] += 1
            if count[0] == 3:
                raise RuntimeError("二次规划失败")
            return await original(instance, prompt, **kwargs)
        env["_ProactiveContext"].agen_content = failing_followup
        waits = []
        with patch.object(proactive_reply, "set_observation_wait",
                          side_effect=lambda *args, **kwargs: waits.append((args, kwargs)) or True):
            self.assertTrue(await env["_proactive_run_planner"](
                False, "123", {"_observation": {"generation": 9}}))
        self.assertEqual(len(sends), 1)
        self.assertEqual(len(waits), 1)
        self.assertIsNone(waits[0][0][2])
        self.assertTrue(waits[0][1]["planner_choice"])

    async def test_observation_reply_new_activity_skips_followup_safely(self):
        env, _, calls, sends, _, permit = self.environment(["[SEND]\n话题", "正文", "[WAIT 42]"])
        original_send = env["_current_qq_actions"].send
        async def send_and_invalidate(**kwargs):
            result = await original_send(**kwargs)
            permit["value"] = False
            return result
        env["_current_qq_actions"].send = send_and_invalidate
        self.assertTrue(await env["_proactive_run_planner"](
            False, "123", {"_observation": {"generation": 10}}))
        self.assertEqual(len(sends), 1)
        self.assertEqual(len(calls), 2)

    async def test_partial_failure_keeps_sent_memory(self):
        env, ctx, _, sends, notes, _ = self.environment(["[SEND]\n关心考试", "第一条<split>第二条"], fail_at=1)
        self.assertTrue(await env["_proactive_run_planner"](True, "123", {}))
        self.assertEqual(len(sends), 1)
        self.assertEqual(len(notes), 1)
        self.assertEqual(ctx.history[-1]["content"], "第一条")
        self.assertEqual(env["trace_sends"], [("writer-offline", 1, [])])

    async def test_sdk_rejection_does_not_count_as_success(self):
        env, ctx, _, sends, notes, _ = self.environment(["[SEND]\n关心考试", "加油"])

        class Rejected:
            async def send(self, **kwargs):
                return types.SimpleNamespace(status="failed", ret_code=100)

        env["_current_qq_actions"] = Rejected()
        self.assertFalse(await env["_proactive_run_planner"](False, "123", {}))
        self.assertFalse(notes)
        self.assertEqual(ctx.saved, 0)

    async def test_state_flush_failure_does_not_lose_sent_history(self):
        from bot import proactive_reply
        env, ctx, _, sends, _, _ = self.environment(["[SEND]\n关心考试", "加油"])
        with patch.object(proactive_reply, "notify_bot_message_sent", side_effect=OSError("模拟磁盘失败")):
            self.assertTrue(await env["_proactive_run_planner"](False, "123", {}))
        self.assertEqual(len(sends), 1)
        self.assertEqual(ctx.history[-1]["content"], "加油")
        self.assertEqual(ctx.saved, 1)

    async def test_stale_result_is_not_sent(self):
        env, ctx, calls, sends, _, permit = self.environment(["[SEND]\n关心考试", "加油"])
        original = env["_ProactiveContext"].agen_content

        async def request(instance, prompt, **kwargs):
            result = await original(instance, prompt, **kwargs)
            permit["value"] = False
            return result

        env["_ProactiveContext"].agen_content = request
        self.assertFalse(await env["_proactive_run_planner"](False, "123", {}))
        self.assertEqual(len(calls), 1)
        self.assertFalse(sends)
        self.assertEqual(ctx.saved, 0)

    async def test_filter_blocks_writer_output(self):
        env, _, _, sends, _, _ = self.environment(["[SEND]\n话题", "blocked"])
        self.assertFalse(await env["_proactive_run_planner"](False, "123", {}))
        self.assertFalse(sends)

    async def test_planner_wait_pass_send_logs_complete_output_and_decision(self):
        from bot import proactive_reply
        for decision in ("[WAIT 45]", "[PASS]\nPRIVATE_RAW secret", "[SEND]\nPRIVATE_RAW secret"):
            with self.subTest(decision=decision):
                planner_outputs = [decision, "reply", "[PASS]"] if decision.startswith("[SEND]") else [decision, "reply"]
                env, _, _, _, _, _ = self.environment(planner_outputs)
                lines, waits = [], []
                env["print"] = lambda line: lines.append(line)
                with patch.object(proactive_reply, "set_observation_wait", side_effect=lambda *args, **kwargs: waits.append((args, kwargs)) or True):
                    await env["_proactive_run_planner"](False, "123", {"_observation": {"generation": 7}})
                results = [line for line in lines if "规划结果：" in line]
                expected = "WAIT" if decision.startswith("[WAIT") else "PASS" if decision.startswith("[PASS") else "SEND"
                self.assertEqual(len(results), 2 if expected == "SEND" else 1)
                self.assertIn({"WAIT": "等待 45 秒", "PASS": "不发送", "SEND": "调用主模型"}[expected], results[0])
                self.assertTrue(any(decision in line and "完整输出" in line and "[规划器]" in line for line in lines))
                self.assertEqual(len(waits), int(expected == "WAIT"))
                if waits:
                    self.assertEqual(waits[0][1], {"expected_activity_version": 1, "expected_generation": 7, "planner_choice": True})

    async def test_normal_success_drives_real_scheduler_wait_send_pass_offline(self):
        import tempfile
        from bot import proactive_reply as proactive
        env, ctx, _, sends, _, _ = self.environment(["[WAIT 45]", "[SEND]\nPRIVATE_TOPIC", "reply", "[PASS]"])
        logs, stage, clock = [], [0], [100.0]
        def social(request):
            stage[0] += 1
            return types.SimpleNamespace(finished=False, wait_seconds=45 if stage[0] == 2 else None,
                                         consume_reply=lambda: None)
        env["_make_social_context"] = social
        env["print"] = logs.append
        async def planner(cid, cfg):
            return await env["_proactive_run_planner"](False, cid, cfg)
        config = {"private": {"enabled": True, "trigger_probability": 0, "observation": {"trigger_probability": 1}}}
        scheduler = proactive.ProactiveReplyScheduler(lambda: config, planner, planner, logs.append)
        with tempfile.TemporaryDirectory() as directory, patch.object(proactive, "_STATE_DIR", directory), \
                patch.object(proactive, "_scheduler", scheduler), \
                patch.object(proactive.time, "time", side_effect=lambda: clock[0]), \
                patch.object(proactive, "get_session_state", scheduler.get_session_state), \
                patch.object(proactive, "can_send", scheduler.can_send), \
                patch.object(proactive, "notify_bot_message_sent", scheduler.notify_bot_message_sent):
            ticket = scheduler.notify_user_message_received(False, "123", message_id="normal")
            self.assertTrue(scheduler.notify_bot_message_sent(False, "123", is_proactive=False))
            self.assertTrue(scheduler.complete_normal_reply(False, "123", message_id="normal",
                normal_handled=True, ticket=ticket, reply_sent=True))
            for moment in (100, 145, 190):
                clock[0] = moment
                await scheduler._tick_async(include_cold=False)
                await scheduler._tick_async(include_cold=False)
            state = scheduler.get_session_state(False, "123")
            self.assertIsNone(state["observation"])
            self.assertEqual(state["total_proactive_sent"], 1)
            self.assertEqual(state["unanswered_count"], 1)
            self.assertEqual(len(sends), 1)
            self.assertEqual(ctx.history[-1]["content"], "reply")
            self.assertEqual(sum("[规划器]" in line and "开始判断" in line for line in logs), 3)
            self.assertEqual(sum("首次观察：等待" in line for line in logs), 1)
            self.assertEqual(sum("调用主模型生成回复" in line for line in logs), 1)
            self.assertEqual(sum("规划结果：不发送" in line for line in logs), 1)
            self.assertIn("PRIVATE_TOPIC", " ".join(logs))
            scheduler.stop()

    async def test_group_normal_success_waits_then_planner_sends_and_logs(self):
        import tempfile
        from bot import proactive_reply as proactive
        env, _, _, sends, _, _ = self.environment(["[WAIT 25]", "[SEND]\n继续当前话题", "群聊续聊"])
        logs, clock = [], [100.0]
        env["print"] = logs.append
        async def planner(cid, cfg):
            return await env["_proactive_run_planner"](True, cid, cfg)
        config = {"group": {"enabled": True, "trigger_probability": 0, "observation": {"trigger_probability": 1}}}
        scheduler = proactive.ProactiveReplyScheduler(lambda: config, planner, planner, logs.append)
        with tempfile.TemporaryDirectory() as directory, patch.object(proactive, "_STATE_DIR", directory), \
                patch.object(proactive, "_scheduler", scheduler), \
                patch.object(proactive.time, "time", side_effect=lambda: clock[0]), \
                patch.object(proactive, "get_session_state", scheduler.get_session_state), \
                patch.object(proactive, "can_send", scheduler.can_send), \
                patch.object(proactive, "notify_bot_message_sent", scheduler.notify_bot_message_sent):
            ticket = scheduler.notify_user_message_received(True, "123", message_id="normal")
            scheduler.notify_bot_message_sent(True, "123", is_proactive=False)
            self.assertTrue(scheduler.complete_normal_reply(True, "123", normal_handled=True,
                message_id="normal", ticket=ticket, reply_sent=True))
            self.assertFalse(sends)
            await scheduler._tick_async(include_cold=False)
            self.assertFalse(sends)
            self.assertEqual(scheduler.get_session_state(True, "123")["observation"]["wait_until"], 125)
            clock[0] = 125
            await scheduler._tick_async(include_cold=False)
            self.assertEqual(len(sends), 1)
            self.assertEqual(scheduler.get_session_state(True, "123")["total_proactive_sent"], 1)
            self.assertTrue(any("进入持续观察" in line and "普通回复" in line for line in logs))
            self.assertTrue(any("观察唤醒" in line for line in logs))
            self.assertTrue(any("主动回复发送完成" in line and "群聊 123" in line for line in logs))
            scheduler.stop()

    async def test_planner_preflight_skip_logs_explain_missing_output(self):
        from bot import proactive_reply
        for cause in ("connection", "policy", "busy", "history"):
            with self.subTest(cause=cause):
                env, context, calls, sends, _, permit = self.environment([])
                logs = []
                env["print"] = logs.append
                if cause == "connection":
                    env["_current_qq_actions"] = None
                elif cause == "policy":
                    permit["value"] = False
                elif cause == "busy":
                    context._history_lock.acquire()
                else:
                    context.history = []
                with patch.object(proactive_reply, "set_observation_wait", return_value=True):
                    self.assertFalse(await env["_proactive_run_planner"](False, "123",
                        {"_observation": {"generation": 7, "initial_wait": True}}))
                if cause == "busy":
                    context._history_lock.release()
                self.assertFalse(calls)
                self.assertFalse(sends)
                self.assertTrue(any("未调用规划器" in line and "首次观察时间规划" in line for line in logs))
                self.assertTrue(any("已回退默认等待" in line for line in logs))
                self.assertFalse(any("完整输出" in line for line in logs))

    async def test_initial_wait_invalid_actions_and_model_error_use_default_without_writer(self):
        import tempfile
        from bot import proactive_reply as proactive
        for decision in ("[WAIT 20]", "[WAIT 90]", "[WAIT nope]", "[PASS]", "[SEND]\n不应发送", None):
            with self.subTest(decision=decision):
                env, _, calls, sends, _, _ = self.environment([decision])
                logs, clock = [], [100.0]
                env["print"] = logs.append
                if decision is None:
                    async def failed(*args, **kwargs):
                        raise RuntimeError("模拟模型失败")
                    env["_ProactiveContext"].agen_content = failed
                async def planner(cid, cfg):
                    return await env["_proactive_run_planner"](False, cid, cfg)
                scheduler = proactive.ProactiveReplyScheduler(lambda: {"private": {"enabled": True, "observation": {"trigger_probability": 1}}}, planner, planner)
                with tempfile.TemporaryDirectory() as directory, patch.object(proactive, "_STATE_DIR", directory), \
                        patch.object(proactive, "_scheduler", scheduler), \
                        patch.object(proactive.time, "time", side_effect=lambda: clock[0]), \
                        patch.object(proactive, "get_session_state", scheduler.get_session_state), \
                        patch.object(proactive, "can_send", scheduler.can_send), \
                        patch.object(proactive, "notify_bot_message_sent", scheduler.notify_bot_message_sent):
                    ticket = scheduler.notify_user_message_received(False, "123", message_id="normal")
                    self.assertTrue(scheduler.complete_normal_reply(False, "123", normal_handled=True,
                        message_id="normal", ticket=ticket, reply_sent=True))
                    await scheduler._tick_async(include_cold=False)
                    self.assertEqual(scheduler.get_session_state(False, "123")["observation"]["wait_until"],
                                     120 if decision == "[WAIT 20]" else 160)
                    self.assertFalse(sends)
                    if calls:
                        self.assertEqual(len(calls), 1)
                        self.assertIn("首次观察时间规划", calls[0][0])
                        self.assertIn("自主选择", calls[0][1])
                    scheduler.stop()

    async def test_stale_writer_finally_cannot_wait_or_finish_new_normal_episode(self):
        import tempfile
        from bot import proactive_reply as proactive
        for intent in ("wait", "finish", "cold_finish"):
            with self.subTest(intent=intent):
                env, _, _, sends, _, _ = self.environment((["[WAIT 60]"] if intent != "cold_finish" else []) + ["[SEND]\nold topic", "stale reply"])
                clock, replacement = [100.0], []
                env["print"] = lambda *args: None
                async def planner(cid, cfg):
                    return await env["_proactive_run_planner"](False, cid, cfg)
                scheduler = proactive.ProactiveReplyScheduler(lambda: {"private": {"enabled": True,
                    "trigger_probability": 1, "observation": {"trigger_probability": 1},
                    "idle_threshold_seconds": 0, "min_interval_between_proactive_seconds": 0}}, planner, planner)
                async def generate(writer, prompt, request):
                    ticket = scheduler.notify_user_message_received(False, "123", message_id="new-normal")
                    scheduler.notify_bot_message_sent(False, "123", is_proactive=False)
                    self.assertTrue(scheduler.complete_normal_reply(False, "123", normal_handled=True,
                        message_id="new-normal", ticket=ticket, reply_sent=True))
                    replacement.append(scheduler.get_session_state(False, "123")["observation"])
                    request.social.wait_seconds = 500 if intent == "wait" else None
                    request.social.finished = intent != "wait"
                    return "stale reply", 1
                env["_generate_proactive_content"] = generate
                with tempfile.TemporaryDirectory() as directory, patch.object(proactive, "_STATE_DIR", directory), \
                        patch.object(proactive, "_scheduler", scheduler), \
                        patch.object(proactive.time, "time", side_effect=lambda: clock[0]), \
                        patch.object(proactive, "get_session_state", scheduler.get_session_state), \
                        patch.object(proactive, "can_send", scheduler.can_send), \
                        patch.object(proactive, "notify_bot_message_sent", scheduler.notify_bot_message_sent):
                    ticket = scheduler.notify_user_message_received(False, "123", message_id="old")
                    if intent == "cold_finish":
                        scheduler.note_incoming(False, "123", normal_handled=True, message_id="old")
                    else:
                        self.assertTrue(scheduler.complete_normal_reply(False, "123", normal_handled=True,
                            message_id="old", ticket=ticket, reply_sent=True))
                        await scheduler._tick_async(include_cold=False)
                    clock[0] = 160
                    if intent == "cold_finish":
                        await planner("123", {})
                    else:
                        await scheduler._tick_async(include_cold=False)
                    self.assertEqual(len(replacement), 1)
                    actual = scheduler.get_session_state(False, "123")["observation"]
                    self.assertEqual(actual, replacement[0])
                    self.assertEqual(actual["wait_until"], 220)
                    self.assertFalse(sends)
                    scheduler.stop()

    def test_plugin_reads_top_level_and_defaults(self):
        env = {"get_runtime_setting": lambda *args: {"weather": {"default_city": "上海"}},
               "_plugin_config_schemas": {"weather": {"cache_minutes": {"default": 30}}}}
        extracted({"get_plugin_config", "get_all_plugin_config_schemas"}, env)
        self.assertEqual(env["get_plugin_config"]("weather", "default_city"), "上海")
        self.assertEqual(env["get_plugin_config"]("weather", "cache_minutes"), 30)
        self.assertEqual(env["get_all_plugin_config_schemas"]()["weather"]["values"]["cache_minutes"], 30)

    async def test_actual_base_generation_accepts_independent_manager(self):
        class Manager:
            def __init__(self):
                self.succeeded = []

            def get_attempt_count(self):
                return 1

            def get_next_for_request(self, **kwargs):
                return ("https://example.invalid/v1", "fake-key", "judge-only", False, 17, "p/judge-only", "", {})

            def make_attempt_identity(self, *args):
                return args

            def mark_success(self, *args, **kwargs):
                self.succeeded.append((args, kwargs))

        manager = Manager()
        env = extracted({"LimitedDeepSeekContext"}, {
            "threading": threading, "asyncio": asyncio, "re": __import__("re"),
            "key_manager": object(), "_normal_ai_state": lambda: None,
            "split_reasoning_output": lambda message: (message.content, ""),
            "check_final_reply_for_failover": lambda text: None,
            "filter_sensitive_content": lambda text: text,
            "build_llm_user_message": lambda text: text,
            "_merge_extra_user_suffix": lambda text, extra: text,
            "log_api_request": lambda **kwargs: None,
            "log_api_success": lambda **kwargs: None,
        })
        base = env["LimitedDeepSeekContext"]
        received = []

        class Actual(base):
            def __init__(self):
                self.history = []
                self.max_context_tokens = 0

            def _enforce_message_limit(self):
                pass

            def _build_messages(self, message=None):
                return [{"role": "user", "content": message}]

            async def _request_completion(self, url, key, timeout, proxy, kwargs):
                received.append((url, timeout, kwargs))
                return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content="[PASS]"))], usage=None)

        actual = Actual()
        result = await actual.agen_content("测试", model_manager=manager)
        self.assertEqual(result[0], "[PASS]")
        self.assertEqual(received[0][1], 17)
        self.assertEqual(received[0][2]["model"], "judge-only")
        self.assertEqual(len(manager.succeeded), 1)

    async def test_proactive_keyword_retry_summarizes_without_replaying_tools(self):
        from bot import agent
        from bot.rounds import fix_messages
        from key_manager import SiliconFlowKeyManager
        manager = object.__new__(SiliconFlowKeyManager)
        manager._initialized = False
        manager.__init__([dict(provider_id=p, model=p, base_url=f"https://{p}.invalid", keys=["fake1", "fake2"])
                          for p in ("first", "second")])
        calls, tool_runs = [], []
        async def completion(base, key, timeout, proxy, kwargs):
            calls.append((kwargs["model"], bool(kwargs.get("tools"))))
            text = "bad-body" if kwargs["model"] == "first" else "safe-body"
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=text))],
                                         usage=types.SimpleNamespace(total_tokens=7, prompt_tokens=4, completion_tokens=3))
        async def loop(complete, messages, ctx, settings):
            tool_runs.append("executed")
            msg, usage = await complete(messages, [{"type": "function"}])
            return msg.content, [usage], 1
        env = extracted({"_generate_proactive_content", "ReplyKeywordFailure", "check_final_reply_for_failover"}, {
            "time": __import__("time"), "key_manager": manager, "_ai_reply_allowed": lambda request: True,
            "split_reasoning_output": lambda msg: (msg.content, "bad-body reasoning"),
            "build_final_send_parts": lambda text: [text],
            "find_llm_reply_failover_keyword": lambda text: "bad" if "bad-body" in text else "",
            "fix_messages": fix_messages, "is_trace_recording": lambda: False})
        writer = types.SimpleNamespace(history=[], _build_messages=lambda prompt: [{"role": "user", "content": prompt}],
                                       _request_completion=completion)
        request = types.SimpleNamespace(chat_id="123", is_group=False, self_id="999", social=None)
        with patch.object(agent, "run_tool_loop", loop):
            self.assertEqual(await env["_generate_proactive_content"](writer, "offline", request), ("safe-body", 14))
        self.assertEqual(calls, [("first", True), ("second", False)])
        self.assertEqual(tool_runs, ["executed"])

    async def test_proactive_writer_records_full_trace_without_changing_return(self):
        from bot import agent
        recorded = []
        full = "主动回复全文" * 1000
        manager = types.SimpleNamespace(get_attempt_count=lambda: 1,
            get_next_for_request=lambda **kw: ("https://offline.invalid", "fake", "model", False, 10, "test/model", "", {}),
            make_attempt_identity=lambda *args: "one", mark_success=lambda *args, **kw: None,
            mark_failure=lambda *args, **kw: None)
        async def completion(*args):
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=full))],
                usage=types.SimpleNamespace(total_tokens=9, prompt_tokens=5, completion_tokens=4))
        writer = types.SimpleNamespace(history=[{"role": "user", "content": "历史全文"}],
            _build_messages=lambda prompt: [{"role": "system", "content": "系统提示"}, {"role": "user", "content": prompt}],
            _request_completion=completion)
        request = types.SimpleNamespace(chat_id="123", is_group=False, self_id="999", social=None)
        async def loop(complete, messages, ctx, settings):
            message, usage = await complete(messages, [])
            ctx.extra["trace_calls"] = [{"name": "wait_for_messages", "arguments": "{}", "result": "ok"}]
            return message.content, usage, 1
        env = extracted({"_generate_proactive_content"}, {"time": __import__("time"), "key_manager": manager,
            "_ai_reply_allowed": lambda req: True, "split_reasoning_output": lambda msg: (msg.content, "完整推理"),
            "check_final_reply_for_failover": lambda text: None,
            "is_trace_recording": lambda: True, "add_trace_record": lambda record: recorded.append(record) or "trace-writer"})
        with patch.object(agent, "run_tool_loop", loop):
            self.assertEqual(await env["_generate_proactive_content"](writer, "主动生成提示", request), (full, 9))
        self.assertEqual(writer.last_trace_id, "trace-writer")
        self.assertEqual(recorded[0]["reply"], full)
        self.assertEqual(recorded[0]["reasoning"], "完整推理")
        self.assertEqual(recorded[0]["session_id"], "private_123")
        self.assertEqual(recorded[0]["source"], "proactive")
        self.assertEqual(recorded[0]["tool_calls"][0]["name"], "wait_for_messages")
        class BrokenHistory:
            def __iter__(self):
                raise RuntimeError("trace snapshot failure")
        writer.history = BrokenHistory()
        with patch.object(agent, "run_tool_loop", loop):
            self.assertEqual(await env["_generate_proactive_content"](writer, "提示", request), (full, 9))
        async def cancelled(*args):
            raise asyncio.CancelledError()
        with patch.object(agent, "run_tool_loop", cancelled), self.assertRaises(asyncio.CancelledError):
            await env["_generate_proactive_content"](writer, "提示", request)

    async def test_native_actions_route_reply_wait_finish_and_free_text(self):
        from bot import proactive_reply
        for action in ("reply", "wait", "finish", "invalid"):
            with self.subTest(action=action):
                env, _, calls, sends, _, _ = self.environment(["自由文字说明", "最终回复"])
                original = env["_ProactiveContext"].agen_content
                async def generate(instance, prompt, **kwargs):
                    result = await original(instance, prompt, **kwargs)
                    if kwargs.get("model_manager"):
                        instance.planner_raw_output = "自由文字说明"
                        instance.planner_decision = {"action": action, "reason": "", "seconds": 37 if action == "wait" else None}
                    return result
                env["_ProactiveContext"].agen_content = generate
                waits, logs = [], []
                env["print"] = logs.append
                with patch.object(proactive_reply, "set_observation_wait", side_effect=lambda *a, **kw: waits.append((a, kw)) or True):
                    sent = await env["_proactive_run_planner"](False, "123", {})
                self.assertEqual(sent, action == "reply")
                self.assertEqual(len(calls), 2 if action == "reply" else 1)
                self.assertEqual(len(waits), int(action in ("wait", "invalid")))
                if waits:
                    self.assertEqual(waits[0][0][2], 37 if action == "wait" else None)
                    self.assertTrue(waits[0][1]["allow_start"])
                if action == "invalid":
                    self.assertTrue(any("回退默认等待" in line for line in logs))

    def test_native_planner_tools_allow_optional_text_and_validate_wait(self):
        import math
        env = extracted({"_planner_tools", "_planner_tool_decision", "_proactive_parse_reply"},
                        {"json": __import__("json"), "math": math})
        tools = env["_planner_tools"](10, 600)
        self.assertEqual([t["function"]["name"] for t in tools], ["reply", "wait", "finish"])
        def message(name, args):
            return types.SimpleNamespace(content="自由说明文本", tool_calls=[types.SimpleNamespace(
                function=types.SimpleNamespace(name=name, arguments=args))])
        self.assertEqual(env["_planner_tool_decision"](message("reply", "{}"), 10, 600)["action"], "reply")
        self.assertEqual(env["_planner_tool_decision"](message("wait", '{"seconds":37.5}'), 10, 600)["seconds"], 37.5)
        for args in ('{"seconds":true}', '{"seconds":0}', '{"seconds":601}', '{"seconds":NaN}'):
            with self.assertRaises(ValueError):
                env["_planner_tool_decision"](message("wait", args), 10, 600)
        self.assertIsNone(env["_planner_tool_decision"](types.SimpleNamespace(content="自由文本"), 10, 600))
        self.assertTrue(env["_proactive_parse_reply"]("[PSEND]\n接话理由")[0])

    async def test_native_planner_transport_preserves_text_and_auto_tools(self):
        import sys, math
        captured = []
        class Client:
            def __init__(self, **kwargs):
                self.chat = types.SimpleNamespace(completions=self)
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            async def create(self, **kwargs):
                captured.append(kwargs)
                return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(
                    content="我想再观察37秒", tool_calls=[types.SimpleNamespace(function=types.SimpleNamespace(
                        name="wait", arguments='{"seconds":37,"reason":"等话题明确"}'))]))])
        env = extracted({"_planner_tools", "_planner_tool_decision", "_ProactiveContext"}, {
            "LimitedDeepSeekContext": object, "asyncio": asyncio, "json": __import__("json"), "math": math,
            "normalize_proxy_url": lambda x: x})
        with patch.dict(sys.modules, {"httpx": types.SimpleNamespace(AsyncClient=lambda **kw: object()),
                                      "openai": types.SimpleNamespace(AsyncOpenAI=Client)}):
            ctx = env["_ProactiveContext"]()
            ctx.planner_wait_bounds = (10, 600)
            result = await ctx._request_completion("https://offline.invalid", "fake", 12, "", {"model": "judge"})
        self.assertEqual(captured[0]["tool_choice"], "auto")
        self.assertFalse(captured[0]["parallel_tool_calls"])
        self.assertEqual(ctx.planner_raw_output, "我想再观察37秒")
        self.assertEqual(ctx.planner_decision["seconds"], 37)
        self.assertEqual(result.choices[0].message.content, "[WAIT 37]")

    async def test_real_async_transport_arguments_and_cancellation_cleanup(self):
        import sys
        transport_options, client_options, requests, closed = [], [], [], []
        wait = asyncio.Event()

        class Transport:
            def __init__(self, **kwargs):
                transport_options.append(kwargs)

        class Client:
            def __init__(self, **kwargs):
                client_options.append(kwargs)
                self.chat = types.SimpleNamespace(completions=self)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                closed.append(True)

            async def create(self, **kwargs):
                requests.append(kwargs)
                await wait.wait()
                return "response"

        env = extracted({"_ProactiveContext"}, {
            "LimitedDeepSeekContext": object, "asyncio": asyncio,
            "normalize_proxy_url": lambda x: x,
        })
        with patch.dict(sys.modules, {"httpx": types.SimpleNamespace(AsyncClient=Transport),
                                      "openai": types.SimpleNamespace(AsyncOpenAI=Client)}):
            ctx = env["_ProactiveContext"]()
            kwargs = {"model": "judge", "messages": [], "stream": False, "timeout": 12}
            task = asyncio.create_task(ctx._request_completion("https://example.invalid/v1", "fake-key", 12, "http://localhost:9999", kwargs))
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(transport_options[0]["proxy"], "http://localhost:9999")
        self.assertFalse(transport_options[0]["trust_env"])
        self.assertEqual(client_options[0]["max_retries"], 0)
        self.assertEqual(requests[0], kwargs)
        self.assertTrue(closed)

    def test_planner_manager_is_independent_and_uses_planner_slots(self):
        from key_manager import key_manager
        from bot.llm_config import build_planner_endpoints
        env = extracted({"_PlannerKeyManager", "_get_planner_manager"}, {
            "key_manager": key_manager, "threading": threading,
            "_planner_endpoints_snapshot": None,
            "_planner_manager_lock": threading.Lock(),
            "get_runtime_others": lambda: {"llm_providers": [{
                "id": "judge", "base_url": "https://example.invalid/v1", "keys": ["sk-" + "a" * 32],
                "models": [], "planner_models": [{"name": "judge-only", "enabled": True, "timeout_seconds": 17}],
            }]},
        })
        manager = env["_PlannerKeyManager"]([])
        env["_planner_key_manager"] = manager
        self.assertIsNot(manager, key_manager)
        before = list(key_manager.endpoints)
        self.assertIs(env["_get_planner_manager"](), manager)
        selected = manager.get_next_for_request()
        self.assertEqual(selected[2], "judge-only")
        self.assertEqual(selected[4], 17)
        self.assertEqual(key_manager.endpoints, before)

    def test_runtime_config_overlays_response_switch_without_mutating_old_keys(self):
        import copy
        raw = {"group": {"enabled": True, "extra": 1}, "private": {"enabled": True}}
        original = copy.deepcopy(raw)
        flags = {"ai_chat": False, "group_chat": False, "private_chat": True}
        env = extracted({"_get_proactive_runtime_config"}, {
            "get_runtime_setting": lambda *args: raw,
            "is_feature_enabled": lambda key, default: flags.get(key, default),
        })
        config = env["_get_proactive_runtime_config"]()
        self.assertFalse(config["group"]["enabled"])
        self.assertTrue(config["private"]["enabled"])
        self.assertEqual(config["group"]["extra"], 1)
        self.assertEqual(raw, original)
        startup = next(node for node in TREE.body if isinstance(node, ast.FunctionDef)
                       and node.name == "_agent_startup_tasks_sync")
        self.assertIn("get_config=_get_proactive_runtime_config", ast.unparse(startup))

    async def test_closed_type_skips_initial_and_regular_planner_before_models(self):
        for group in (True, False):
            for observation in ({}, {"generation": 7, "initial_wait": True}, {"generation": 8, "timeout": True}):
                env, _, calls, sends, _, _ = self.environment([])
                env["is_feature_enabled"] = lambda key, default: key not in ("group_chat", "private_chat")
                extracted({"_proactive_target_allowed"}, env)
                self.assertFalse(await env["_proactive_run_planner"](group, "123", {"_observation": observation}))
                self.assertFalse(calls)
                self.assertFalse(sends)

    def test_global_feature_and_blacklist_policy(self):
        flags = {"ai_chat": True, "group_chat": True, "private_chat": True}
        blacklist = {"456"}
        env = extracted({"_proactive_target_allowed"}, {
            "is_feature_enabled": lambda key, default: flags.get(key, default),
            "get_all_blacklist": lambda: blacklist,
            "is_user_blacklisted": lambda target, blocked: target in blocked,
        })
        allowed = env["_proactive_target_allowed"]
        self.assertTrue(allowed(True, "123"))
        self.assertFalse(allowed(False, "456"))
        flags["ai_chat"] = False
        self.assertTrue(allowed(True, "123"))
        flags["ai_chat"] = True
        flags["private_chat"] = False
        self.assertFalse(allowed(False, "123"))
        self.assertTrue(allowed(True, "123"))
        self.assertFalse(allowed(True, "not-a-number"))

    async def test_cancel_releases_history_lock_without_sending(self):
        env, ctx, _, sends, _, _ = self.environment(["[SEND]\n话题"])
        started = asyncio.Event()

        async def blocked(instance, *args, **kwargs):
            started.set()
            await asyncio.Event().wait()

        env["_ProactiveContext"].agen_content = blocked
        task = asyncio.create_task(env["_proactive_run_planner"](False, "123", {}))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(ctx._history_lock.locked())
        self.assertEqual(ctx._busy, 0)
        self.assertFalse(sends)
        self.assertEqual(ctx.saved, 0)

    def test_parser_fails_closed(self):
        env = extracted({"_proactive_parse_reply"}, {})
        for text in ("", "send anything", "[PASS]\n原因", "[SEND]"):
            self.assertFalse(env["_proactive_parse_reply"](text)[0])


if __name__ == "__main__":
    unittest.main()
