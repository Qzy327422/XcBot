"""离线主动恢复回归：只读源码 AST；fake actions/model/clock，不导入 main。"""
import ast
import asyncio
import copy
import json
import math
import pathlib
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

from bot import proactive_reply as proactive

ROOT = pathlib.Path(__file__).resolve().parents[1]
TREE = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))


def extract(names, env):
    nodes = [copy.deepcopy(n) for n in TREE.body
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name in names]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(ROOT / "main.py"), "exec"), env)
    return env


async def silent(*args):
    return False


def config():
    return {kind: {"enabled": True, "trigger_probability": 1,
                   "idle_threshold_seconds": 0, "min_interval_between_proactive_seconds": 0,
                   "observation": {"enabled": True, "trigger_probability": 1}}
            for kind in ("group", "private")}


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.patcher = patch.object(proactive, "_STATE_DIR", self.tmp.name)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.cfg = config()
        self.clock = [1000.0]
        clock_patch = patch.object(proactive.time, "time", side_effect=lambda: self.clock[0])
        clock_patch.start()
        self.addCleanup(clock_patch.stop)

    def scheduler(self, runner=silent):
        return proactive.ProactiveReplyScheduler(lambda: self.cfg, runner, runner)

    def tick(self, scheduler):
        asyncio.run(scheduler._tick_async())
        self.clock[0] += 120

    def test_hot_whitelist_new_target_can_make_first_contact(self):
        calls = []
        async def planner(cid, cfg):
            self.assertTrue(s.history_allowed(False, cid))
            state = s.get_session_state(False, cid)
            self.assertTrue(s.can_send(False, cid, state["activity_version"]))
            self.assertFalse((pathlib.Path(self.tmp.name) / "history_owners.json").exists())
            calls.append(cid)
            return False
        s = self.scheduler(planner)
        self.assertTrue(s.bootstrap("999", lambda: {}))
        self.tick(s)
        self.assertFalse(calls)
        self.cfg["private"]["whitelist"] = [456]
        self.cfg["private"]["check_interval_seconds"] = 120
        for _ in range(3):
            self.tick(s)
        self.assertEqual(calls, ["456"] * 3)
        self.assertIn(("private", "456"), s._active_sessions)
        self.assertIn(("private", "456"), s._restored_candidates)
        state = s.get_session_state(False, "456")
        self.assertEqual(state["last_user_message_at"], 0)
        self.assertIsNone(state["observation"])

    def test_hot_group_whitelist_works_seamlessly_alongside_private_history(self):
        calls = []
        async def planner(cid, cfg):
            calls.append(cid)
            return False
        s = self.scheduler(planner)
        self.assertTrue(s.bootstrap("999", lambda: {"private": [456]}))
        self.cfg["group"]["whitelist"] = ["00456"]
        self.tick(s)
        # 既有私聊历史和热新增群聊白名单均可独立进入主动检查，互不阻塞
        self.assertEqual(calls, ["456", "456"])
        self.assertTrue(s.history_allowed(True, "456"))
        self.assertTrue(s.history_allowed(False, "456"))
        version = s.get_session_state(True, "456")["activity_version"]
        self.assertTrue(s.can_send(True, "456", version))

    def test_hot_whitelist_accepts_existing_history_without_legacy_gate(self):
        s = self.scheduler()
        self.assertTrue(s.bootstrap("999", lambda: {"private": [456]}))
        self.cfg["private"]["whitelist"] = [456]
        for _ in range(3):
            self.tick(s)
        self.assertTrue(s.history_allowed(False, "456"))
        self.assertIn(("private", "456"), s._active_sessions)

    def test_hot_whitelist_works_regardless_of_previous_account_session(self):
        self.cfg["private"]["whitelist"] = [456]
        previous = self.scheduler()
        self.assertTrue(previous.bootstrap("888", lambda: {}))
        self.cfg["private"]["whitelist"] = []
        s = self.scheduler()
        self.assertTrue(s.bootstrap("999", lambda: {}))
        self.cfg["private"]["whitelist"] = [456]
        self.tick(s)
        self.assertTrue(s.history_allowed(False, "456"))
        self.assertIn(("private", "456"), s._active_sessions)

    def test_hot_whitelist_blacklist_disable_and_removal_close_gate(self):
        s = self.scheduler()
        self.assertTrue(s.bootstrap("999", lambda: {}))
        self.cfg["private"].update(whitelist=[456], blacklist=[456])
        self.tick(s)
        self.assertFalse(s.history_allowed(False, "456"))
        self.assertNotIn(("private", "456"), s._active_sessions)
        self.cfg["private"].update(blacklist=[], enabled=False)
        self.tick(s)
        self.assertFalse(s.history_allowed(False, "456"))
        self.cfg["private"]["enabled"] = True
        self.tick(s)
        version = s.get_session_state(False, "456")["activity_version"]
        self.assertTrue(s.can_send(False, "456", version))
        for change in ({"whitelist": []}, {"whitelist": [123]},
                       {"blacklist": [456]}, {"enabled": False}):
            with self.subTest(change=change):
                self.cfg["private"].update(whitelist=[456], blacklist=[], enabled=True)
                self.cfg["private"].update(change)
                self.assertFalse(s.can_send(False, "456", version))
                self.assertFalse(s.can_send(False, "456", version, continuation=True))
        self.tick(s)
        self.assertNotIn(("private", "456"), s._active_sessions)

    def test_hot_whitelist_waits_for_current_bootstrapped_connection(self):
        current = [True]
        s = self.scheduler()
        self.assertTrue(s.bootstrap("999", lambda: {}, is_current=lambda: current[0]))
        self.cfg["private"]["whitelist"] = [456]
        current[0] = False
        self.tick(s)
        self.assertFalse(s.history_allowed(False, "456"))
        self.assertNotIn(("private", "456"), s._active_sessions)
        current[0] = True
        s.cancel_observations("disconnect")
        self.tick(s)
        self.assertFalse(s.history_allowed(False, "456"))
        self.assertNotIn(("private", "456"), s._active_sessions)

    def test_hot_whitelist_same_account_reconnect_restores(self):
        s = self.scheduler()
        self.assertTrue(s.bootstrap("999", lambda: {}))
        self.cfg["private"]["whitelist"] = [456]
        self.tick(s)
        self.assertTrue(s.history_allowed(False, "456"))
        s.notify_bot_message_sent(False, "456", is_proactive=True, content="fake", contact_id="one")
        s.cancel_observations("disconnect")
        self.assertTrue(s.bootstrap("999", lambda: {}))
        self.assertIn(("private", "456"), s._active_sessions)
        self.assertEqual(s.get_session_state(False, "456")["unanswered_count"], 1)
        fresh = self.scheduler()
        self.assertTrue(fresh.bootstrap("999", lambda: {}))
        self.assertTrue(fresh.history_allowed(False, "456"))
        self.assertIn(("private", "456"), fresh._active_sessions)
        self.assertEqual(fresh.get_session_state(False, "456")["unanswered_count"], 1)

    def test_hot_whitelist_does_not_reanimate_reset_history(self):
        self.cfg["private"]["whitelist"] = [456]
        s = self.scheduler()
        self.assertTrue(s.bootstrap("999", lambda: {}))
        s.reset_session(False, "456")
        for _ in range(3):
            self.tick(s)
        self.assertNotIn(("private", "456"), s._active_sessions)
        self.assertNotIn(("private", "456"), s._restored_candidates)

    def test_hot_whitelist_does_not_restore_deleted_history_from_state(self):
        self.cfg["private"]["whitelist"] = [123]
        old = self.scheduler()
        self.assertTrue(old.bootstrap("999", lambda: {}))
        old.notify_bot_message_sent(False, "123", is_proactive=True, contact_id="old")
        self.cfg["private"]["whitelist"] = []
        fresh = self.scheduler()
        self.assertTrue(fresh.bootstrap("999", lambda: {}))
        self.cfg["private"]["whitelist"] = [456]
        self.tick(fresh)
        self.assertEqual(set(fresh._active_sessions), {("private", "456")})

    def test_whitelist_without_history_and_blacklist_switches(self):
        self.cfg["private"].update(whitelist=[123, 456], blacklist=[456])
        self.cfg["group"].update(enabled=False, whitelist=[789])
        s = self.scheduler()
        self.assertTrue(s.bootstrap("999", lambda: {}))
        self.assertEqual(set(s._active_sessions), {("private", "123")})
        self.assertTrue(s.can_send(False, "123", 1))
        self.assertFalse(s.can_send(False, "456", 0))
        self.assertFalse(s.can_send(True, "789", 0))
        self.assertEqual(s.get_session_state(False, "123")["last_user_message_at"], 0)

    def test_restart_restores_long_term_but_not_observation_ticket_or_draft(self):
        clock = [100.0]
        histories = lambda: {"private": [123]}
        with patch.object(proactive.time, "time", side_effect=lambda: clock[0]):
            s = self.scheduler()
            self.assertTrue(s.bootstrap("999", histories))
            s.notify_user_message_received(False, "123", message_id="old")
            s.notify_bot_message_sent(False, "123", is_proactive=True, content="old", contact_id="contact")
            s.begin_observation(False, "123")
            old = s.get_session_state(False, "123")
            self.assertIsNotNone(old["observation"])
            s.record_plan(False, "123", old["activity_version"], {"angle": "爵士和声"}, "opportunity")
            s._store.update("private", "123", lambda v: v.update(next_check_at=200))
            s._flush()
            fresh = self.scheduler()
            self.assertTrue(fresh.bootstrap("999", histories))
            state = fresh.get_session_state(False, "123")
            self.assertIsNone(state["observation"])
            self.assertFalse(fresh._reply_tickets)
            self.assertFalse(fresh._begin_eligible)
            self.assertEqual(state["unanswered_count"], 1)
            self.assertEqual(state["recent_angles"], ["爵士和声"])
            self.assertEqual(state["recent_opportunity_ids"], ["opportunity"])
            self.assertEqual(state["next_check_at"], 200)
            self.assertFalse(fresh._should_trigger("private", "123", fresh._config()["private"], 199))
            self.assertTrue(fresh._should_trigger("private", "123", fresh._config()["private"], 201))

    def test_v4_flat_legacy_state_loads_automatically_without_legacy_account_id(self):
        legacy = pathlib.Path(self.tmp.name) / "private_123.json"
        legacy.write_text(json.dumps({**proactive._default_state(), "last_user_message_at": 100}), encoding="utf-8")
        history = lambda: {"private": [123]}
        s = self.scheduler()
        self.assertTrue(s.bootstrap("999", history))
        self.assertTrue(s.history_allowed(False, "123"))
        self.assertIn(("private", "123"), s._active_sessions)
        self.assertEqual(s.get_session_state(False, "123")["last_user_message_at"], 100)
        # 配置里残留 legacy_account_id 也不会报错，且显式标记了错误 account_id 的文件仍会被隔离拒绝
        proactive.normalize_config({"legacy_account_id": "../bad"})
        other = self.scheduler()
        self.assertTrue(other.bootstrap("888", history))
        self.assertTrue(other.history_allowed(False, "123"))
        self.assertEqual(other.get_session_state(False, "123")["last_user_message_at"], 100)

    def test_stale_connection_cannot_bootstrap_or_send(self):
        self.cfg["private"]["whitelist"] = [123]
        s = self.scheduler()
        current = [True]
        self.assertTrue(s.bootstrap("999", lambda: {}, is_current=lambda: current[0]))
        self.assertTrue(s.can_send(False, "123", 1))
        current[0] = False
        self.assertFalse(s.can_send(False, "123", 1, True))
        self.assertFalse(s.bootstrap("888", lambda: {}, is_current=lambda: False))
        self.assertEqual(s._account_id, "999")

    def test_contact_segments_once_skip_and_unknown_do_not_count_unanswered(self):
        s = self.scheduler()
        with patch.object(proactive.time, "time", return_value=100):
            s.notify_user_message_received(False, "123")
            s.mark_attempt(False, "123", 1)
            self.assertEqual(s.get_session_state(False, "123")["unanswered_count"], 0)
            self.assertFalse(s.can_send(False, "123", 1))
        with patch.object(proactive.time, "time", return_value=300):
            for part in ("a", "b", "c"):
                s.notify_bot_message_sent(False, "123", is_proactive=True, content=part, contact_id="one")
            state = s.get_session_state(False, "123")
            self.assertEqual(state["unanswered_count"], 1)
            self.assertEqual(state["total_proactive_sent"], 3)
            self.assertEqual(len(state["contact_times"]), 1)
            s.mark_attempt(False, "123", 1, unknown=True)
            self.assertFalse(s.can_send(False, "123", 1, continuation=True))

    def test_budgets_quiet_hours_and_unanswered_backoff(self):
        self.cfg["private"].update(max_background_calls_per_day=2, max_contacts_per_day=1,
                                   min_interval_between_proactive_seconds=10)
        s = self.scheduler()
        with patch.object(proactive.time, "time", return_value=100):
            s.notify_user_message_received(False, "123")
            # 废弃的 max_background_calls_per_day=2 不再限制 reserve_background_call
            for _ in range(4):
                self.assertTrue(s.reserve_background_call(False, "123", 1))
            s.notify_bot_message_sent(False, "123", is_proactive=True, contact_id="one")
            self.assertFalse(s.reserve_background_call(False, "123", 1))
        with patch.object(proactive.time, "time", return_value=1000):
            self.assertFalse(s.can_send(False, "123", 1))
            self.cfg["private"]["max_contacts_per_day"] = 0
            self.assertTrue(s.can_send(False, "123", 1))
            self.assertTrue(s.reserve_background_call(False, "123", 1))
            self.cfg["private"]["quiet_hours"] = [[0, 0]]
            self.assertFalse(s.can_send(False, "123", 1, True))
        self.cfg["private"]["quiet_hours"] = []
        with patch.object(proactive.time, "time", return_value=115):
            self.assertFalse(s.can_send(False, "123", 1))
        with patch.object(proactive.time, "time", return_value=121):
            self.assertTrue(s.can_send(False, "123", 1))

    def test_account_and_session_background_call_caps_removed_while_contact_limit_stays_scoped(self):
        # 全局 192 与单会话 200 次后台调用上限均已取消；显式配置 max_contacts_per_day 仍按会话隔离生效。
        self.cfg["max_account_background_calls_per_day"] = 1
        self.cfg["private"]["max_background_calls_per_day"] = 1
        self.cfg["private"]["max_contacts_per_day"] = 1
        self.cfg["private"]["whitelist"] = [123]
        self.cfg["group"]["whitelist"] = [456]
        clock = [100.0]
        with patch.object(proactive.time, "time", side_effect=lambda: clock[0]):
            s = self.scheduler()
            self.assertTrue(s.bootstrap("999", lambda: {}))
            self.assertTrue(s.reserve_background_call(False, "123", 1, "999"))
            self.assertTrue(s.reserve_background_call(False, "123", 1, "999"))
            self.assertTrue(s.reserve_background_call(True, "456", 1, "999"))
            self.assertTrue(proactive.reserve_account_call_once("999", 1, clock[0]))
            self.assertFalse(s.reserve_background_call(False, "123", 1, "888"))
            self.assertFalse(s.reserve_background_call(False, "123", 0, "999"))
            s.notify_bot_message_sent(False, "123", is_proactive=True, contact_id="one", account_id="999")
            # 私聊会话 123 达到当日联系上限 1 后拦截，但不影响其他会话
            self.assertFalse(s.reserve_background_call(False, "123", 1, "999"))
            self.assertTrue(s.reserve_background_call(True, "456", 1, "999"))
            clock[0] += 86401
            self.assertTrue(s.reserve_background_call(False, "123", 1, "999"))

    def test_per_session_reset_storage_error_fails_closed(self):
        self.cfg["private"]["whitelist"] = [123]
        s = self.scheduler()
        self.assertTrue(s.bootstrap("999", lambda: {}))
        with patch.object(proactive, "atomic_write_json", side_effect=OSError("fake disk full")):
            with self.assertRaises(OSError):
                s.reset_session(False, "123")

    def test_follow_up_persists_updates_and_stays_account_session_scoped(self):
        self.cfg["private"]["whitelist"] = [123, 456]
        s = self.scheduler()
        self.assertTrue(s.bootstrap("999", lambda: {}))
        sources = [{"id": "msg-1", "author": "123", "text": "我明天考试", "is_bot": False}]
        item = json.loads(s.record_follow_up(False, "123", "999", "用户提到考试", sources, version=1))
        self.assertEqual(item["status"], "open")
        self.assertTrue(s.record_follow_up(False, "456", "999", "不能跨会话", sources,
            follow_up_id=item["id"], version=1).startswith("error:"))
        self.assertTrue(s.record_follow_up(False, "123", "888", "不能跨账号", sources,
            version=1).startswith("error:"))
        self.assertTrue(s.record_follow_up(False, "123", "999", "旧回合", sources,
            version=0).startswith("error:"))
        fresh = self.scheduler()
        self.assertTrue(fresh.bootstrap("999", lambda: {}))
        state = fresh.get_session_state(False, "123")
        self.assertEqual(state["follow_ups"][0]["id"], item["id"])
        evidence = [{"id": "msg-2", "author": "123", "text": "考完了", "is_bot": False}]
        updated = json.loads(fresh.record_follow_up(False, "123", "999", "考试已结束（用户明说）",
            evidence, follow_up_id=item["id"], status="resolved", version=state["activity_version"]))
        self.assertEqual(updated["status"], "resolved")
        self.assertEqual(updated["sources"][0]["id"], "msg-2")
        self.assertEqual(fresh.get_session_state(False, "123")["last_proactive_at"], 0)

    def test_late_success_is_charged_to_original_account(self):
        s = self.scheduler()
        self.assertTrue(s.bootstrap("999", lambda: {}))
        self.assertTrue(s.bootstrap("888", lambda: {}))
        s.notify_bot_message_sent(False, "123", is_proactive=True, content="old receipt",
                                  contact_id="old", account_id="999")
        self.assertEqual(s.get_session_state(False, "123")["total_proactive_sent"], 0)
        old = proactive._StateStore(account_id="999").load("private", "123")
        self.assertEqual(old["total_proactive_sent"], 1)
        self.assertEqual(old["unanswered_count"], 1)

    def test_empty_whitelist_does_not_reanimate_deleted_history(self):
        self.cfg["private"]["whitelist"] = [123]
        s = self.scheduler()
        self.assertTrue(s.bootstrap("999", lambda: {}))
        s.notify_bot_message_sent(False, "123", is_proactive=True, contact_id="old")
        self.cfg["private"]["whitelist"] = []
        fresh = self.scheduler()
        self.assertTrue(fresh.bootstrap("999", lambda: {}))
        self.assertFalse(fresh._active_sessions)

    def test_bound_state_rejects_wrong_account_field(self):
        directory = pathlib.Path(self.tmp.name) / "999"
        directory.mkdir()
        (directory / "private_123.json").write_text(json.dumps(
            {"account_id": "888", "last_user_message_at": 100}), encoding="utf-8")
        store = proactive._StateStore(account_id="999")
        self.assertEqual(store.load("private", "123")["last_user_message_at"], 0)

    def test_disabled_bootstrap_hot_enable_recovers_without_new_chat(self):
        self.cfg["private"]["enabled"] = False
        s = self.scheduler()
        self.assertTrue(s.bootstrap("999", lambda: {"private": [123]}))
        self.assertFalse(s._active_sessions)
        self.cfg["private"]["enabled"] = True
        asyncio.run(s._tick_async())
        self.assertIn(("private", "123"), s._active_sessions)
        self.assertEqual(s.get_session_state(False, "123")["last_user_message_at"], 0)

    def test_new_incoming_session_owner_survives_restart(self):
        s = self.scheduler()
        self.assertTrue(s.bootstrap("999", lambda: {}))
        s.notify_user_message_received(False, "123")
        s._flush()
        new = self.scheduler()
        self.assertTrue(new.bootstrap("999", lambda: {"private": [123]}))
        self.assertIn(("private", "123"), new._active_sessions)
        self.assertTrue(new.history_allowed(False, "123"))


class ConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_login_receipt_does_not_block_loop(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        async def login():
            calls.append("get_login_info")
            return "echo"
        def receipt(echo, timeout):
            entered.set()
            if not release.wait(2):
                raise TimeoutError()
            self.assertEqual(echo, "echo")
            self.assertEqual(timeout, 5.0)
            return {"status": "ok", "retcode": 0, "data": {"user_id": 999}}
        env = extract({"_proactive_query_login", "_receipt_succeeded"}, {
            "asyncio": asyncio, "Manager": types.SimpleNamespace(reports=types.SimpleNamespace(get=receipt))})
        task = asyncio.create_task(env["_proactive_query_login"](
            types.SimpleNamespace(custom=types.SimpleNamespace(get_login_info=login))))
        try:
            for _ in range(100):
                await asyncio.sleep(0.001)
                if entered.is_set():
                    break
            self.assertTrue(entered.is_set())
            self.assertFalse(task.done())
        finally:
            release.set()
        self.assertEqual(await task, 999)
        self.assertEqual(calls, ["get_login_info"])

    async def test_listener_start_thread_and_stale_generation(self):
        threads = []
        class FakeThread:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                threads.append(self)
            def start(self):
                pass
        env = extract({"_maintain_event_runtime", "_proactive_connection_current"}, {
            "threading": types.SimpleNamespace(Thread=FakeThread), "_qq_actions_lock": threading.RLock(),
            "_current_qq_actions": None, "_current_bot_self_id": None,
            "_agent_startup_tasks_sync": lambda: None,
            "_proactive_bootstrap_connection": lambda *args: None})
        event = types.SimpleNamespace(notice_type="listener_start")
        a, b = object(), object()
        with patch.object(proactive, "cancel_observations"):
            env["_maintain_event_runtime"](event, a)
            old = threads[-1].kwargs["args"][1]
            env["_maintain_event_runtime"](types.SimpleNamespace(), a)
            self.assertEqual(len(threads), 1)
            # 同一底层actions被重用的新listener_start仍必须换代。
            env["_maintain_event_runtime"](event, a)
            self.assertEqual(len(threads), 2)
            self.assertFalse(env["_proactive_connection_current"](a, old))
            env["_maintain_event_runtime"](event, b)
            self.assertEqual(len(threads), 3)
            self.assertFalse(env["_proactive_connection_current"](a, old))
            self.assertTrue(env["_proactive_connection_current"](b, threads[-1].kwargs["args"][1]))

    async def test_transport_charges_every_actual_model_attempt(self):
        import sys
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
                return types.SimpleNamespace(choices=[])
        env = extract({"_ProactiveContext"}, {"LimitedDeepSeekContext": object,
                       "asyncio": asyncio, "normalize_proxy_url": lambda value: value})
        ctx = env["_ProactiveContext"]()
        ctx.proactive_budget = (False, "123", 7)
        with patch.dict(sys.modules, {
                "httpx": types.SimpleNamespace(AsyncClient=lambda **kw: object()),
                "openai": types.SimpleNamespace(AsyncOpenAI=Client)}), patch.object(
                proactive, "reserve_background_call", side_effect=[True, False]) as reserve:
            await ctx._request_completion("https://offline.invalid", "fake", 1, "", {})
            with self.assertRaises(RuntimeError):
                await ctx._request_completion("https://offline.invalid", "fake", 1, "", {})
            self.assertEqual(reserve.call_count, 2)
            self.assertEqual(len(captured), 1)

    async def test_bootstrap_worker_ignores_old_reply_and_publishes_current_account(self):
        own = object()
        generation = object()
        env = extract({"_proactive_bootstrap_connection", "_proactive_connection_current"}, {
            "asyncio": asyncio, "_qq_actions_lock": threading.RLock(),
            "_current_qq_actions": own, "_current_bot_self_id": None,
            "_proactive_connection_generation": generation,
            "chat_memory": types.SimpleNamespace(get_all_sessions=lambda: {}),
            "_get_proactive_runtime_config": lambda: {}})
        bootstrap_calls = []
        scheduler = types.SimpleNamespace(bootstrap=lambda account, history, **kwargs:
            bootstrap_calls.append(account) or kwargs["is_current"]())
        async def login(actions):
            return 999
        env["_proactive_query_login"] = login
        with patch.object(proactive, "_scheduler", scheduler):
            # 调用工作函数的是独立线程，不在当前运行loop上asyncio.run。
            worker = threading.Thread(target=env["_proactive_bootstrap_connection"],
                                      args=(own, generation, None))
            worker.start()
            await asyncio.to_thread(worker.join, 2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(env["_current_bot_self_id"], 999)
            self.assertEqual(bootstrap_calls, ["999"])
            env["_current_qq_actions"] = object()
            worker = threading.Thread(target=env["_proactive_bootstrap_connection"],
                                      args=(own, generation, None))
            worker.start()
            await asyncio.to_thread(worker.join, 2)
            self.assertEqual(bootstrap_calls, ["999"])

    async def test_bootstrap_worker_does_not_rebind_global_role_life(self):
        own = object()
        generation = object()
        life_binds = []
        env = extract({"_proactive_bootstrap_connection", "_proactive_connection_current"}, {
            "asyncio": asyncio, "_qq_actions_lock": threading.RLock(),
            "_current_qq_actions": own, "_current_bot_self_id": None,
            "_proactive_connection_generation": generation,
            "chat_memory": types.SimpleNamespace(get_all_sessions=lambda: {}),
            "_get_proactive_runtime_config": lambda: {},
            "_role_life_bind_account": lambda acct, gen: life_binds.append((acct, gen))})
        async def login(actions):
            return 999
        env["_proactive_query_login"] = login
        # 角色生活已独立于 QQ 连接运行：QQ 连接初始化不再绑定或打断全局角色生活
        with patch.object(proactive, "_scheduler", None):
            worker = threading.Thread(target=env["_proactive_bootstrap_connection"],
                                      args=(own, generation, None))
            worker.start()
            await asyncio.to_thread(worker.join, 2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(life_binds, [])
            self.assertEqual(env["_current_bot_self_id"], 999)

    async def test_whitelist_empty_history_writer_contract_and_network_lock(self):
        from test_proactive_integration import IntegrationTests
        helper = IntegrationTests()
        def cleanup():
            while helper._cleanups:
                function, args, kwargs = helper._cleanups.pop()
                function(*args, **kwargs)
        self.addCleanup(cleanup)
        env, ctx, calls, sends, notes, _ = helper.environment(["[SEND]\n旧reason兼容", "我最近对爵士和声很着迷"])
        ctx.history = []
        original = env["_current_qq_actions"].send
        async def send(**kwargs):
            self.assertFalse(ctx._history_lock.locked())
            return await original(**kwargs)
        env["_current_qq_actions"].send = send
        self.assertTrue(await env["_proactive_run_planner"](False, "123", {"whitelist": [123]}))
        self.assertEqual(len(calls), 2)
        self.assertIn("persona", calls[0][1])
        self.assertIn("无事件也可chat", calls[1][1])
        self.assertIn("不强迫问句结尾", calls[1][1])
        self.assertNotIn("近期对话：", calls[1][1])
        self.assertTrue(notes[0]["contact_id"])
        self.assertEqual(len(sends), 1)

    async def test_main_follow_up_callback_rechecks_live_source_and_session(self):
        from bot.message_runtime import INDEX, ReplyDecision, RequestState
        from bot.social_tools import register_social_tools
        from bot import agent
        with tempfile.TemporaryDirectory() as directory, patch.object(proactive, "_STATE_DIR", directory):
            cfg = config()
            cfg["private"]["whitelist"] = [123]
            scheduler = proactive.ProactiveReplyScheduler(lambda: cfg, silent, silent)
            self.assertTrue(scheduler.bootstrap("999", lambda: {}))
            request = RequestState("999", False, "123", ReplyDecision("999", False, "123", source="normal"))
            INDEX.record("999", False, "123", "7001", "123", text="明天考试", timestamp=100)
            self.addCleanup(INDEX.clear, "999", False, "123")
            env = extract({"_make_social_context"}, {
                "normalize_bool_config": lambda value, default=False: bool(value),
                "get_runtime_setting": lambda key, default=None: True if key.endswith("enabled") else default,
                "_proactive_target_allowed": lambda *args: True, "_ai_reply_allowed": lambda *args, **kw: True,
                "_hard_blacklisted_author": lambda *args: False})
            registry = agent.ToolRegistry()
            register_social_tools(registry)
            with patch.object(proactive, "_scheduler", scheduler):
                social = env["_make_social_context"](request)
                request.social = social
                ctx = agent.AgentContext(user_id="123", is_group=False,
                    extra={"self_id": "999", "social": social})
                result = await registry.get("record_follow_up").handler(
                    {"summary": "用户提到考试", "source_ids": ["7001"]}, ctx)
                self.assertFalse(result.startswith("error:"), result)
                self.assertEqual(json.loads(result)["sources"][0]["id"], "7001")
                INDEX.tombstone("999", False, "123", "7001")
                result = await social.record_follow_up("来源已撤回", ["7001"])
                self.assertTrue(result.startswith("error:"))
                self.assertEqual(len(scheduler.get_session_state(False, "123")["follow_ups"]), 1)

    async def test_partial_delivery_never_begins_or_rearms_observation(self):
        from test_proactive_integration import IntegrationTests
        for observation in ({}, {"generation": 7}):
            for failure in ("gate", "exception", "unknown"):
                with self.subTest(observation=observation, failure=failure):
                    helper = IntegrationTests()
                    try:
                        env, ctx, calls, sends, notes, _ = helper.environment(
                            ["[SEND]\nchat", "first<split>second", "[WAIT 30]"],
                            fail_at=1 if failure == "exception" else None)
                        original = env["_current_qq_actions"].send
                        async def send(**kwargs):
                            if sends and failure == "unknown":
                                return {"status": "unknown"}
                            return await original(**kwargs)
                        env["_current_qq_actions"].send = send
                        with patch.object(proactive, "can_send", side_effect=lambda *a, **kw:
                                not (failure == "gate" and len(notes) >= 1)), \
                             patch.object(proactive, "begin_observation") as begin, \
                             patch.object(proactive, "set_observation_wait") as wait:
                            self.assertTrue(await env["_proactive_run_planner"](
                                False, "123", {"_observation": observation}))
                        self.assertEqual(len(notes), 1)
                        self.assertEqual(len(calls), 2)
                        self.assertEqual(ctx.history[-1]["content"], "first")
                        begin.assert_not_called()
                        wait.assert_not_called()
                    finally:
                        while helper._cleanups:
                            function, args, kwargs = helper._cleanups.pop()
                            function(*args, **kwargs)

    async def test_planner_reads_persisted_follow_up_with_source_and_status(self):
        from test_proactive_integration import IntegrationTests
        helper = IntegrationTests()
        def cleanup():
            while helper._cleanups:
                function, args, kwargs = helper._cleanups.pop()
                function(*args, **kwargs)
        self.addCleanup(cleanup)
        env, _, calls, _, _, _ = helper.environment(["[PASS]"])
        item = {"id": "follow-1", "summary": "用户提到考试", "status": "open",
                "sources": [{"id": "msg-1", "author": "123", "text": "明天考试", "is_bot": False}]}
        with patch.object(proactive, "get_session_state", return_value={"activity_version": 1, "follow_ups": [item]}):
            self.assertFalse(await env["_proactive_run_planner"](False, "123", {}))
        self.assertIn("follow-1", calls[0][1])
        self.assertIn("msg-1", calls[0][1])
        self.assertIn('"status": "open"', calls[0][1])

    async def test_fully_sent_but_invalidated_request_does_not_observe(self):
        from test_proactive_integration import IntegrationTests
        helper = IntegrationTests()
        def cleanup():
            while helper._cleanups:
                function, args, kwargs = helper._cleanups.pop()
                function(*args, **kwargs)
        self.addCleanup(cleanup)
        env, _, calls, _, notes, _ = helper.environment(["[SEND]\nchat", "one"])
        with patch.object(proactive, "can_send", side_effect=lambda *a, **kw: not notes), \
             patch.object(proactive, "begin_observation") as begin, \
             patch.object(proactive, "set_observation_wait") as wait:
            self.assertTrue(await env["_proactive_run_planner"](False, "123", {}))
        self.assertEqual(len(notes), 1)
        self.assertEqual(len(calls), 2)
        begin.assert_not_called()
        wait.assert_not_called()

    async def test_unknown_receipt_has_attempt_cooldown_without_unanswered(self):
        from test_proactive_integration import IntegrationTests
        helper = IntegrationTests()
        def cleanup():
            while helper._cleanups:
                function, args, kwargs = helper._cleanups.pop()
                function(*args, **kwargs)
        self.addCleanup(cleanup)
        env, ctx, _, sends, notes, _ = helper.environment(["[SEND]\nchat", "正文"])
        async def unknown(**kwargs):
            self.assertFalse(ctx._history_lock.locked())
            raise TimeoutError("fake unknown")
        env["_current_qq_actions"].send = unknown
        attempts = []
        with patch.object(proactive, "mark_attempt", side_effect=lambda *a, **kw: attempts.append(kw)):
            self.assertFalse(await env["_proactive_run_planner"](False, "123", {}))
        self.assertTrue(attempts[0]["unknown"])
        self.assertFalse(notes)
        self.assertFalse(sends)
        self.assertEqual(ctx.saved, 0)

    def test_topic_native_contract_validates_types_and_keeps_old_reason(self):
        env = extract({"_planner_tools", "_planner_tool_decision"}, {"json": json, "math": math})
        def message(args):
            return types.SimpleNamespace(tool_calls=[types.SimpleNamespace(function=types.SimpleNamespace(
                name="reply", arguments=json.dumps(args)))])
        result = env["_planner_tool_decision"](message({"reason": "old", "angle": "爵士和声",
            "fact_ids": ["history:0"], "avoid": ["催回复"], "purpose": "chat"}), 10, 60)
        self.assertEqual(result["angle"], "爵士和声")
        self.assertEqual(result["purpose"], "chat")
        for field in ("fact_ids", "avoid"):
            with self.assertRaises(ValueError):
                env["_planner_tool_decision"](message({field: "not-list"}), 10, 60)
        self.assertEqual(env["_planner_tool_decision"](message({"reason": "old"}), 10, 60)["reason"], "old")

    def test_mismatched_passive_notify_bot_message_sent_returns_false_without_polluting(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(proactive, "_STATE_DIR", directory):
            scheduler = proactive.ProactiveReplyScheduler(lambda: config(), silent, silent)
            self.assertTrue(scheduler.bootstrap("999", lambda: {}))
            self.assertFalse(scheduler.notify_bot_message_sent(
                True, "123", is_proactive=False, content="old-acct", account_id="111",
            ))
            self.assertEqual(scheduler.get_session_state(True, "123")["consecutive_bot_count"], 0)
            self.assertTrue(scheduler.notify_bot_message_sent(
                True, "123", is_proactive=False, content="new-acct", account_id="999",
            ))
            self.assertEqual(scheduler.get_session_state(True, "123")["consecutive_bot_count"], 1)

    def test_maintain_event_runtime_stale_generation_guard_prevents_late_disconnect_cancel(self):
        captured = []
        old_gen = object()
        env = extract({"_maintain_event_runtime"}, {
            "threading": threading,
            "_agent_startup_done": True,
            "_qq_actions_lock": threading.RLock(),
            "_current_qq_actions": object(),
            "_current_bot_self_id": "111",
            "_proactive_connection_generation": old_gen,
            "_proactive_bootstrap_pending": False,
            "_proactive_bootstrap_ready": old_gen,
            "_proactive_bootstrap_connection": lambda *a, **k: None,
        })
        fake_thread = types.SimpleNamespace(start=lambda: None)
        event = types.SimpleNamespace(self_id="222", notice_type=None)
        with patch.object(threading, "Thread", return_value=fake_thread), \
             patch.object(proactive, "cancel_observations",
                          side_effect=lambda *a, **kw: captured.append(kw.get("guard"))):
            env["_maintain_event_runtime"](event, object())
        self.assertEqual(len(captured), 1)
        guard = captured[0]
        self.assertTrue(callable(guard))
        self.assertTrue(guard())
        # 新连接接管并旋转 generation 后，旧调用的 guard 必须失效
        env["_proactive_connection_generation"] = object()
        self.assertFalse(guard())


if __name__ == "__main__":
    unittest.main()
