"""BUGFIX_HANDOFF P1-1 / P1-2 / P1-3 / P2-3 主动聊天与社交工具回归测试。"""
import ast
import asyncio
import json
import os
import pathlib
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot import proactive_reply
from bot.social_tools import SocialContext


def _extract_main(*names):
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    nodes = [
        n for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name in set(names)
    ]
    return nodes


def _exec_main(env, *names):
    nodes = _extract_main(*names)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(ROOT / "main.py"), "exec"), env)
    return env


class P11ParserAndBackoffTests(unittest.TestCase):
    def test_proactive_extract_decision_accepts_reasoned_pass_inline_wait_and_send(self):
        env = {"re": __import__("re")}
        _exec_main(env, "_proactive_extract_decision", "_proactive_parse_reply")
        ext = env["_proactive_extract_decision"]
        parse = env["_proactive_parse_reply"]

        self.assertEqual(ext("[PASS] 目前不打扰"), ("PASS", None, ""))
        self.assertEqual(ext("先看看情况\n[PASS] 不发"), ("PASS", None, ""))
        self.assertEqual(ext("[WAIT 45] 再等一会"), ("WAIT", 45.0, ""))
        self.assertEqual(ext("先观察下 [WAIT 12.5]"), ("WAIT", 12.5, ""))
        self.assertEqual(ext("[SEND] 晚上好呀"), ("SEND", None, "晚上好呀"))
        self.assertEqual(ext("[PASS]：现在不打扰"), ("PASS", None, ""))
        self.assertEqual(ext("[SEND]晚上好"), ("SEND", None, "晚上好"))
        self.assertEqual(ext("思考里提到[PASS]\n[SEND] 晚上好"), ("SEND", None, "晚上好"))
        self.assertEqual(parse("[SEND] 晚上好呀"), (True, "晚上好呀"))
        self.assertEqual(parse("[PASS]：现在不打扰"), (False, ""))
        self.assertEqual(parse("[SEND]晚上好"), (True, "晚上好"))
        self.assertEqual(parse("[PASS]\n原因"), (False, ""))
        self.assertEqual(parse("[SEND]"), (False, ""))

    def test_cold_finish_exponential_backoff_resets_on_user_message_and_max_wakes(self):
        async def noop_planner(cid, cfg):
            return False

        cfg = proactive_reply.normalize_config({
            "private": {
                "enabled": True,
                "check_interval_seconds": 60,
                "idle_threshold_seconds": 0,
                "min_interval_between_proactive_seconds": 300,
                "max_contacts_per_day": 0,
                "quiet_hours": [],
                "observation": {"enabled": True, "trigger_probability": 1.0, "min_continuation_interval_seconds": 1},
            },
            "group": {"enabled": False},
        })
        clock = [1000.0]
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(proactive_reply, "_STATE_DIR", tmp), \
                patch.object(proactive_reply.time, "time", side_effect=lambda: clock[0]):
            sched = proactive_reply.ProactiveReplyScheduler(lambda: cfg, noop_planner, noop_planner)
            sched._account_id = "1"
            sched._bootstrap_ready = True
            sched.notify_user_message_received(False, "123", message_id="", account_id="1")
            ver = sched.get_session_state(False, "123")["activity_version"]

            sched.mark_attempt(False, "123", ver, cold_finished=True, account_id="1")
            st1 = sched.get_session_state(False, "123")
            self.assertEqual(st1["cold_finish_streak"], 1)
            self.assertEqual(st1["next_check_at"], 1000.0 + 120.0)

            sched.mark_attempt(False, "123", ver, cold_finished=True, account_id="1")
            st2 = sched.get_session_state(False, "123")
            self.assertEqual(st2["cold_finish_streak"], 2)
            self.assertEqual(st2["next_check_at"], 1000.0 + 240.0)

            # 用户发消息后退避与 next_check_at 一并清零
            sched.notify_user_message_received(False, "123", message_id="", account_id="1")
            st_reset = sched.get_session_state(False, "123")
            self.assertEqual(st_reset["cold_finish_streak"], 0)
            self.assertEqual(st_reset["next_check_at"], 0.0)

            # 再次冷场退避后，主动消息发出时 notify_bot_message_sent 也同步清零 next_check_at
            ver2 = st_reset["activity_version"]
            sched.mark_attempt(False, "123", ver2, cold_finished=True, account_id="1")
            self.assertGreater(sched.get_session_state(False, "123")["next_check_at"], 0.0)
            sched.notify_bot_message_sent(False, "123", is_proactive=True, content="bot-hi", account_id="1")
            st_sent = sched.get_session_state(False, "123")
            self.assertEqual(st_sent["cold_finish_streak"], 0)
            self.assertEqual(st_sent["next_check_at"], 0.0)

            # MAX_OBSERVATION_WAKES 限制与连续错误上限
            sched._history_allowed.add(("private", "123"))
            sched.notify_bot_message_sent(False, "123", is_proactive=True, content="hi", account_id="1")
            sched.begin_observation(False, "123", wait_seconds=10)
            key = ("private", "123")
            with sched._lock:
                ep = sched._observations[key]
                ep.initial_wait_pending = False
                ep.wait_until = 1010.0
                ep.wake_count = proactive_reply.MAX_OBSERVATION_WAKES
            clock[0] = 1050.0
            self.assertIsNone(sched._prepare_observation(key, cfg["private"], clock[0]))
            self.assertIsNone(sched.get_session_state(False, "123")["observation"])

            # error_streak 达到 MAX_OBSERVATION_ERRORS 后终止观察
            sched.notify_user_message_received(False, "123", message_id="", account_id="1")
            sched.notify_bot_message_sent(False, "123", is_proactive=True, content="hi2", account_id="1")
            sched.begin_observation(False, "123", wait_seconds=10)
            st3 = sched.get_session_state(False, "123")
            v3 = st3["activity_version"]
            g3 = st3["observation"]["generation"]
            for _ in range(proactive_reply.MAX_OBSERVATION_ERRORS - 1):
                self.assertTrue(sched.note_observation_planner_outcome(
                    False, "123", expected_activity_version=v3, expected_generation=g3, ok=False))
            self.assertFalse(sched.note_observation_planner_outcome(
                False, "123", expected_activity_version=v3, expected_generation=g3, ok=False))
            self.assertIsNone(sched.get_session_state(False, "123")["observation"])
            sched.stop()


class P12SplitContinuationTests(unittest.TestCase):
    def test_split_proactive_three_segments_not_cut_off_when_max_unanswered_is_one(self):
        async def noop(cid, cfg):
            return False

        cfg = proactive_reply.normalize_config({
            "private": {
                "enabled": True,
                "max_unanswered_messages": 1,
                "idle_threshold_seconds": 0,
                "min_interval_between_proactive_seconds": 0,
                "quiet_hours": [],
            },
            "group": {"enabled": False},
        })
        with tempfile.TemporaryDirectory() as tmp, patch.object(proactive_reply, "_STATE_DIR", tmp):
            sched = proactive_reply.ProactiveReplyScheduler(lambda: cfg, noop, noop)
            sched._account_id = "1"
            sched._bootstrap_ready = True
            sched._history_allowed.add(("private", "77"))
            sched.notify_user_message_received(False, "77", message_id="", account_id="1")
            ver = sched.get_session_state(False, "77")["activity_version"]
            batch_id = "batch-abc"

            self.assertTrue(sched.can_send(False, "77", ver, continuation=False, contact_id=batch_id))
            sched.notify_bot_message_sent(False, "77", is_proactive=True, content="段1",
                                          contact_id=batch_id, account_id="1")
            # 第 1 段发出后 unanswered_count == 1，默认 contact_id="" 会被拦截
            self.assertFalse(sched.can_send(False, "77", ver, continuation=True))
            # 同批次 contact_id 的第 2、3 段继续放行，且 unanswered_count 只计 1 次
            self.assertTrue(sched.can_send(False, "77", ver, continuation=True, contact_id=batch_id))
            sched.notify_bot_message_sent(False, "77", is_proactive=True, content="段2",
                                          contact_id=batch_id, account_id="1")
            self.assertTrue(sched.can_send(False, "77", ver, continuation=True, contact_id=batch_id))
            sched.notify_bot_message_sent(False, "77", is_proactive=True, content="段3",
                                          contact_id=batch_id, account_id="1")
            st = sched.get_session_state(False, "77")
            self.assertEqual(st["unanswered_count"], 1)
            # 下一轮新 batch 必须被熔断
            self.assertFalse(sched.can_send(False, "77", ver, continuation=False, contact_id="other-batch"))
            sched.stop()


class P13FollowUpLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_follow_ups_context_injection_post_restart_close_eviction_and_rollback(self):
        async def noop(cid, cfg):
            return False

        cfg = {"private": {"enabled": True}, "group": {"enabled": False}}
        with tempfile.TemporaryDirectory() as tmp, patch.object(proactive_reply, "_STATE_DIR", tmp):
            sched = proactive_reply.ProactiveReplyScheduler(lambda: cfg, noop, noop)
            sched._account_id = "1"
            sched._bootstrap_ready = True
            sched.notify_user_message_received(False, "88", message_id="m0", account_id="1")

            # 1) 连续写入 21 条 open 事项不死锁（满 20 条滚动淘汰最旧项）
            created_ids = []
            for idx in range(21):
                res = sched.record_follow_up(
                    False, "88", "1", f"事项 {idx}",
                    [{"id": f"m{idx}", "author": "88", "text": "内容", "is_bot": False}],
                )
                self.assertFalse(res.startswith("error:"), res)
                created_ids.append(json.loads(res)["id"])
            st = sched.get_session_state(False, "88")
            self.assertEqual(len(st["follow_ups"]), 20)
            self.assertNotIn(created_ids[0], [x["id"] for x in st["follow_ups"]])

            # 2) flush() 抛 OSError 时回滚 _store._cache[key] 与 _dirty
            key = ("private", "88")
            with patch.object(sched._store, "flush", side_effect=OSError("disk full")):
                err = sched.record_follow_up(
                    False, "88", "1", "写入失败测试",
                    [{"id": "m99", "author": "88", "text": "内容", "is_bot": False}],
                )
            self.assertTrue(err.startswith("error:"))
            self.assertNotIn("写入失败测试", [x["summary"] for x in sched._store.load("private", "88")["follow_ups"]])
            self.assertNotIn(key, sched._store._dirty)

            # 3) _follow_ups_context_suffix 注入 open 事项；重启/快照缺失时关闭既有 follow_up_id
            with patch.object(proactive_reply, "_scheduler", sched):
                env = {"_hard_blacklisted_author": lambda a, s: False}
                _exec_main(env, "_follow_ups_context_suffix")
                ctx_obj = types.SimpleNamespace(context_type="private", chat_id="88")
                suffix = env["_follow_ups_context_suffix"](ctx_obj)
                self.assertIn(created_ids[-1], suffix)
                self.assertIn("事项 20", suffix)

                # SocialContext 快照里不含历史 source_id，依然可以关闭既有 follow_up_id
                social = SocialContext(
                    key=("1", False, "88"),
                    candidates=[],
                    proactive=False,
                    enabled=lambda: True,
                    follow_up_ready=lambda: True,
                    follow_up_callback=lambda summary, sources, follow_up_id="", status="open": sched.record_follow_up(
                        False, "88", "1", summary, sources, follow_up_id=follow_up_id, status=status
                    ),
                )
                closed = await social.record_follow_up(
                    "事项 20", [], follow_up_id=created_ids[-1], status="resolved"
                )
                self.assertFalse(closed.startswith("error:"), closed)
                self.assertEqual(json.loads(closed)["status"], "resolved")
            sched.stop()


class P23ConcurrencyBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_late_listener_start_preserves_self_id_and_restored_candidates(self):
        threads = []

        class FakeThread:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                threads.append(self)

            def start(self):
                pass

        env = {
            "threading": types.SimpleNamespace(Thread=FakeThread, Lock=threading.Lock),
            "_qq_actions_lock": threading.RLock(),
            "_agent_startup_lock": threading.Lock(),
            "_current_qq_actions": None,
            "_current_bot_self_id": None,
            "_agent_startup_tasks_sync": lambda: None,
            "_proactive_bootstrap_connection": lambda *args: None,
        }
        _exec_main(env, "_maintain_event_runtime", "_proactive_connection_current")
        actions = object()
        with patch.object(proactive_reply, "cancel_observations") as cancel_mock:
            # 先来首条带 self_id 的真实消息
            env["_maintain_event_runtime"](types.SimpleNamespace(self_id="10086"), actions)
            first_gen = env["_proactive_connection_generation"]
            self.assertEqual(env["_current_bot_self_id"], "10086")
            cancel_mock.assert_called_once()
            # 后到的同连接 HyperListenerStartNotify 不得清掉 self_id 或换代取消观察
            late = types.SimpleNamespace(notice_type="listener_start", self_id=None)
            env["_maintain_event_runtime"](late, actions)
            self.assertEqual(env["_current_bot_self_id"], "10086")
            self.assertIs(env["_proactive_connection_generation"], first_gen)
            self.assertEqual(cancel_mock.call_count, 1)

        # 热开关恢复 _restored_candidates 允许启动后曾收消息的会话
        async def noop(cid, cfg):
            return False

        cfg = {"private": {"enabled": True, "observation": {"enabled": False}}, "group": {"enabled": False}}
        with tempfile.TemporaryDirectory() as tmp, patch.object(proactive_reply, "_STATE_DIR", tmp):
            sched = proactive_reply.ProactiveReplyScheduler(lambda: cfg, noop, noop)
            sched._account_id = "1"
            sched._bootstrap_ready = True
            sched._store.update("private", "555", lambda s: s.update(last_user_message_at=100.0))
            sched._restored_candidates.add(("private", "555"))
            await sched._tick_async(include_cold=False)
            # tick(include_cold=True) 时从 _restored_candidates 恢复进 _active_sessions
            await sched._tick_async(include_cold=True)
            self.assertIn(("private", "555"), sched._active_sessions)
            sched.stop()

    async def test_proactive_after_judge_error_records_outcome_and_busy_deferred_write_guards(self):
        # 1) 验证 after_judge 异常分支会调用 note_observation_planner_outcome(..., ok=False)
        fn_node = next(n for n in _extract_main("_proactive_run_planner"))
        after_try = next(
            n for n in ast.walk(fn_node)
            if isinstance(n, ast.Try) and n.handlers
            and any(isinstance(h.name, str) and h.name == "after_error" for h in n.handlers)
        )
        outcomes = []
        waits = []
        wrapper_after = ast.parse(
            "async def run_after(after_judge):\n"
            "    with bind_request(request):\n"
            "        pass\n"
        )
        wrapper_after.body[0].body[0].body = [after_try]
        after_env = {
            "bind_request": lambda req: __import__("contextlib").nullcontext(),
            "request": object(),
            "_get_planner_manager": lambda: None,
            "after_prompt": "p",
            "json": json,
            "time": time,
            "is_group": False,
            "chat_id": "123",
            "after_phase": "发送后观察判断",
            "after_started": 0.0,
            "after_result": "ERROR",
            "after_min": 10.0,
            "after_default": 60.0,
            "after_version": 1,
            "generation": 2,
            "proactive_reply": types.SimpleNamespace(
                note_observation_planner_outcome=lambda *a, **kw: outcomes.append((a, kw)) or True,
                set_observation_wait=lambda *a, **kw: waits.append((a, kw)) or True,
                finish_observation=lambda *a, **kw: None,
            ),
            "print": lambda *a, **kw: None,
        }
        exec(compile(ast.Module(body=wrapper_after.body, type_ignores=[]), "<after-judge>", "exec"), after_env)
        bad_judge = types.SimpleNamespace(agen_content=lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
        await after_env["run_after"](bad_judge)
        self.assertEqual(len(outcomes), 1)
        self.assertIs(outcomes[0][1]["ok"], False)
        self.assertEqual(len(waits), 1)

        # 2) 验证 _deferred_write 等待锁、检查 _discarded、释放 chat_memory.image_history 及归零 _busy；
        #    同时验证持锁分支释放 chat_memory.image_history(f"context:{id(context)}") 与 _prune_history_images()
        defer_node = next(
            n for n in ast.walk(fn_node)
            if isinstance(n, ast.FunctionDef) and n.name == "_deferred_write"
        )
        defer_env = {
            "time": time,
            "asyncio": asyncio,
            "chat_memory": None,
            "context": None,
            "history_user_anchor": "",
            "part": "",
            "total": 0,
            "is_first_segment": True,
            "handoff_busy": True,
        }
        exec(compile(ast.Module(body=[defer_node], type_ignores=[]), "<defer-write>", "exec"), defer_env)
        released_keys = []
        fake_ih = types.SimpleNamespace(release=lambda key: released_keys.append(key))
        defer_env["chat_memory"] = types.SimpleNamespace(image_history=fake_ih)

        saved_calls = []
        pruned_calls = []
        ctx_normal = types.SimpleNamespace(
            _busy=1,
            _history_lock=threading.Lock(),
            _discarded=False,
            history=[],
            total_tokens=0,
            _enforce_message_limit=lambda: None,
            _save_memory=lambda: saved_calls.append("saved") or True,
            _prune_history_images=lambda: pruned_calls.append("pruned"),
        )
        ctx_normal._history_lock.acquire()
        t = threading.Thread(target=defer_env["_deferred_write"], args=(ctx_normal, "anchor", "hi", 7, True, True))
        t.start()
        time.sleep(0.05)
        ctx_normal._history_lock.release()
        t.join(timeout=2.0)
        self.assertEqual(ctx_normal._busy, 0)
        self.assertEqual(saved_calls, ["saved"])
        self.assertEqual(pruned_calls, ["pruned"])
        self.assertIn(f"context:{id(ctx_normal)}", released_keys)

        # _discarded=True 不得把已 /reset 会话重新落盘或覆盖 history
        saved_calls.clear()
        pruned_calls.clear()
        released_keys.clear()
        ctx_disc = types.SimpleNamespace(
            _busy=1,
            _history_lock=threading.Lock(),
            _discarded=True,
            history=[],
            total_tokens=0,
            _enforce_message_limit=lambda: None,
            _save_memory=lambda: saved_calls.append("saved") or True,
            _prune_history_images=lambda: pruned_calls.append("pruned"),
        )
        defer_env["_deferred_write"](ctx_disc, "anchor", "hi", 7, True, True)
        self.assertEqual(ctx_disc._busy, 0)
        self.assertEqual(ctx_disc.history, [])
        self.assertEqual(saved_calls, [])
        self.assertIn(f"context:{id(ctx_disc)}", released_keys)

        # 直连释放分支（planner 自己的 try/finally）必须使用 chat_memory.image_history.release(f"context:{id(context)}")
        planner_finally = next(
            n for n in ast.walk(fn_node)
            if isinstance(n, ast.Try) and n.finalbody
            and any("chat_memory" in ast.dump(stmt) for stmt in n.finalbody)
        )
        busy_release_if = planner_finally.finalbody[-1]
        wrapper_fin = ast.parse("async def _run_fin():\n    pass\n")
        wrapper_fin.body[0].body = [busy_release_if]
        fin_env = {
            "asyncio": asyncio,
            "chat_memory": types.SimpleNamespace(image_history=fake_ih),
            "context": ctx_normal,
            "busy": True,
        }
        released_keys.clear()
        pruned_calls.clear()
        ctx_normal._busy = 1
        ctx_normal._discarded = False
        exec(compile(ast.Module(body=wrapper_fin.body, type_ignores=[]), "<planner-fin>", "exec"), fin_env)
        await fin_env["_run_fin"]()
        self.assertEqual(ctx_normal._busy, 0)
        self.assertEqual(released_keys, [f"context:{id(ctx_normal)}"])
        self.assertEqual(pruned_calls, ["pruned"])

        # _save_memory() 抛异常以及锁等待超时分支下，owns_busy 都必须安全归零 _busy 并执行 cleanup_downloads
        cleanup_calls = []
        fake_ih.cleanup_downloads = lambda mem, refs, hist: cleanup_calls.append((list(refs), hist))
        ctx_err = types.SimpleNamespace(
            _busy=1,
            _history_lock=threading.Lock(),
            _discarded=False,
            history=[],
            total_tokens=0,
            _image_download_refs=[{"scope": "s", "sha256": "deadbeef"}],
            _image_pending_refs=[{"scope": "s", "sha256": "deadbeef"}],
            _enforce_message_limit=lambda: None,
            _save_memory=lambda: (_ for _ in ()).throw(OSError("disk error")),
            _prune_history_images=lambda: pruned_calls.append("pruned_err"),
        )
        defer_env["_deferred_write"](ctx_err, "anchor", "hi", 7, True, True)
        self.assertEqual(ctx_err._busy, 0)
        self.assertEqual(ctx_err._image_download_refs, [])
        self.assertEqual(ctx_err._image_pending_refs, [])
        self.assertTrue(cleanup_calls and cleanup_calls[-1][1] == ())


if __name__ == "__main__":
    unittest.main()
