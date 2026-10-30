"""Offline regression tests. Run: python -m unittest discover -s tests -v"""
import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from bot import proactive_reply as proactive


async def silent_planner(chat_id, cfg):
    return False


def enabled_config():
    return {
        kind: {"enabled": True, "trigger_probability": 1,
               "observation": {"trigger_probability": 1},
               "idle_threshold_seconds": 0,
               "min_interval_between_proactive_seconds": 0}
        for kind in ("group", "private")
    }


class ProactiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.patch = mock.patch.object(proactive, "_STATE_DIR", self.temp.name)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.config = enabled_config()
        self.schedulers = []
        self.singleton = proactive._scheduler
        proactive._scheduler = None
        self.addCleanup(self.cleanup_schedulers)

    def cleanup_schedulers(self):
        for scheduler in self.schedulers:
            scheduler.stop(timeout=2)
            self.assertFalse(scheduler._thread and scheduler._thread.is_alive())
        proactive._scheduler = self.singleton

    def scheduler(self, planner=silent_planner):
        scheduler = proactive.ProactiveReplyScheduler(lambda: self.config, planner, planner)
        self.schedulers.append(scheduler)
        return scheduler

    def read_state(self, kind="group", chat_id="123"):
        return json.loads(Path(proactive._state_path(kind, chat_id)).read_text(encoding="utf-8"))

    def test_default_disabled_no_registration_no_write(self):
        self.config = {}
        scheduler = self.scheduler()
        with mock.patch.object(proactive, "atomic_write_json") as write:
            scheduler.notify_user_message_received(True, "123")
            scheduler.touch_session(False, "456")
            scheduler.notify_bot_message_sent(True, "123", is_proactive=False)
            asyncio.run(scheduler._tick_async())
            scheduler.stop()
            write.assert_not_called()
        self.assertEqual(scheduler._active_sessions, {})
        self.assertEqual(scheduler._store._cache, {})
        self.assertFalse(scheduler.can_send(True, "123", 0))

    def test_normalize_defaults_independent_and_canonical_lists(self):
        cfg = proactive.normalize_config({"group": {"whitelist": [123, " 00123 ", "456"]}})
        self.assertFalse(cfg["group"]["enabled"])
        self.assertEqual(cfg["group"]["whitelist"], ["123", "456"])
        self.assertEqual(cfg["private"]["idle_threshold_seconds"], 43200)
        cfg["private"]["whitelist"].append("1")
        self.assertEqual(proactive.DEFAULT_PRIVATE_CONFIG["whitelist"], [])

    def test_normalize_rejects_invalid_config_and_runtime_fails_closed(self):
        invalid = [None, [], {"group": None}, {"group": []}]
        for field, values in {
            "enabled": ["false", 0, 1, None],
            "trigger_probability": [True, "0.5", float("nan"), float("inf"), -0.1, 1.1, 30],
            "check_interval_seconds": [0, 4, float("-inf")],
            "idle_threshold_seconds": [-1, "900"],
            "min_interval_between_proactive_seconds": [False, float("nan")],
            "max_consecutive_bot_messages": [0, -1, True, 1.5, 3.0],
            "whitelist": ["123", None, {}, (123,), [True], [None], [""], ["abc"], [0], [-1]],
            "blacklist": ["123", [False], ["１２３"], [1.5]],
        }.items():
            invalid.extend({"group": {field: value}} for value in values)
        scheduler = self.scheduler()
        scheduler.notify_user_message_received(True, "123")
        for raw in invalid:
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    proactive.normalize_config(raw)
                self.config = raw
                self.assertFalse(scheduler.can_send(True, "123", 1))
        with self.assertRaises(ValueError):
            proactive.normalize_config({"private": {"max_unanswered_messages": 0}})

    def test_lists_blacklist_wins_and_disallowed_not_registered(self):
        self.config["group"].update(whitelist=[123, 456], blacklist=[123])
        scheduler = self.scheduler()
        for cid in ("123", "456", "789"):
            scheduler.notify_user_message_received(True, cid)
        self.assertEqual(set(scheduler._active_sessions), {("group", "456")})
        self.assertTrue(scheduler.can_send(True, "456", 1))
        self.assertFalse(scheduler.can_send(True, "123", 0))
        self.assertFalse(scheduler.can_send(True, "789", 0))
        self.config["group"]["blacklist"].append(456)
        self.assertFalse(scheduler.can_send(True, "456", 1, True))

    def test_probability_zero_one_and_no_second_roll(self):
        scheduler = self.scheduler()
        scheduler.notify_user_message_received(True, "123")
        cfg = proactive.normalize_config(self.config)["group"]
        with mock.patch.object(proactive.random, "random", return_value=0):
            cfg["trigger_probability"] = 0
            self.assertFalse(scheduler._should_trigger("group", "123", cfg, 10000000000))
        cfg["trigger_probability"] = 1
        next_check = 10000000000 + cfg["check_interval_seconds"]
        with mock.patch.object(proactive.random, "random", return_value=0.999999):
            self.assertTrue(scheduler._should_trigger("group", "123", cfg, next_check))
            self.assertFalse(scheduler._should_trigger("group", "123", cfg, next_check + 1))
        with mock.patch.object(proactive.random, "random", side_effect=AssertionError("reroll")):
            self.assertTrue(scheduler.can_send(True, "123", 1))

    def test_user_memory_only_tick_and_final_flush(self):
        scheduler = self.scheduler()
        scheduler.notify_user_message_received(True, "123")
        scheduler.notify_user_message_received(True, "123")
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])
        self.config = {}  # 关闭后仍须刷出之前已接受的 dirty 状态。
        asyncio.run(scheduler._tick_async())
        # 热关闭同时撤销旧 planner 资格，活动版本需再递增一次。
        self.assertEqual(self.read_state()["activity_version"], 3)
        self.config = enabled_config()
        scheduler.notify_user_message_received(False, "456")
        scheduler.stop()
        # stop 同时失效旧活动版本，但不丢弃之前的 dirty 用户状态。
        self.assertEqual(self.read_state("private", "456")["activity_version"], 2)

    def test_per_message_fuses_recent_content_and_real_user_reset(self):
        scheduler = self.scheduler()
        for group in (True, False):
            scheduler.notify_user_message_received(group, "123")
            for i in range(3):
                self.assertTrue(scheduler.can_send(group, "123", 1, continuation=i > 0))
                scheduler.notify_bot_message_sent(group, "123", is_proactive=True, content=f"part {i}")
                disk = self.read_state("group" if group else "private")
                self.assertEqual(disk["total_proactive_sent"], i + 1)
            self.assertFalse(scheduler.can_send(group, "123", 1, True))
            scheduler.notify_bot_message_sent(group, "123", is_proactive=False)
            self.assertFalse(scheduler.can_send(group, "123", 1, True))
            scheduler.notify_user_message_received(group, "123")
            state = scheduler.get_session_state(group, "123")
            self.assertEqual(state["activity_version"], 2)
            self.assertEqual(state["unanswered_count"], 0)
            self.assertEqual(state["consecutive_bot_count"], 0)
            self.assertTrue(scheduler.can_send(group, "123", 2))
            self.assertEqual(len(state["recent_proactive_messages"]), 3)

    def test_reset_clears_state_persists_version_and_invalidates_old_send(self):
        scheduler = self.scheduler()
        proactive._scheduler = scheduler
        for group in (True, False):
            with self.subTest(is_group=group):
                kind = "group" if group else "private"
                scheduler.notify_user_message_received(group, "123")
                scheduler.notify_bot_message_sent(group, "123", is_proactive=True, content="old topic")
                old_version = scheduler.get_session_state(group, "123")["activity_version"]
                self.assertTrue(proactive.can_send(group, "123", old_version, True))
                scheduler._last_check_at[(kind, "123")] = 10
                proactive.reset_session(group, "123")
                expected = proactive._default_state()
                expected["activity_version"] = old_version + 1
                self.assertEqual(scheduler.get_session_state(group, "123"), {**expected, "observation": None})
                self.assertEqual(self.read_state(kind), expected)
                self.assertNotIn((kind, "123"), scheduler._active_sessions)
                self.assertNotIn((kind, "123"), scheduler._last_check_at)
                self.assertFalse(proactive.can_send(group, "123", old_version, True))
                self.assertNotIn((kind, "123"), scheduler._store._dirty)
                # 即使再次 touch 注册，也不能把 reset 当新话题或复活旧版本。
                scheduler.touch_session(group, "123")
                self.assertFalse(proactive.can_send(group, "123", old_version, True))
                self.assertFalse(proactive.can_send(group, "123", old_version + 1))
                scheduler.notify_user_message_received(group, "123")
                self.assertFalse(proactive.can_send(group, "123", old_version, True))
                self.assertTrue(proactive.can_send(group, "123", old_version + 2))
                self.assertEqual(scheduler.get_session_state(group, "123")["recent_proactive_messages"], [])

    def test_reset_reloads_evicted_state_when_cache_all_dirty_even_disabled_stopped(self):
        scheduler = self.scheduler()
        scheduler._store = proactive._StateStore(max_entries=1)
        scheduler.notify_user_message_received(False, "123")
        scheduler.notify_bot_message_sent(False, "123", is_proactive=True, content="forget me")
        scheduler.notify_user_message_received(False, "456")
        self.assertNotIn(("private", "123"), scheduler._store._cache)
        self.assertTrue(scheduler._store._dirty)
        self.config = {}
        scheduler._stop.set()
        scheduler.reset_session(False, "123")
        expected = proactive._default_state()
        expected["activity_version"] = 2
        self.assertEqual(self.read_state("private"), expected)
        self.assertEqual(proactive._StateStore().load("private", "123"), expected)
        self.assertNotIn(("private", "123"), scheduler._active_sessions)
        self.assertLessEqual(len(scheduler._store._cache), 1)
        self.assertEqual(self.read_state("private", "456")["activity_version"], 1)

    def test_reset_flush_failure_still_invalidates_and_keeps_dirty_for_retry(self):
        scheduler = self.scheduler()
        scheduler.notify_user_message_received(True, "123")
        scheduler.notify_bot_message_sent(True, "123", is_proactive=True, content="old")
        with mock.patch.object(proactive, "atomic_write_json", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                scheduler.reset_session(True, "123")
        self.assertFalse(scheduler.can_send(True, "123", 1, True))
        self.assertEqual(scheduler.get_session_state(True, "123")["recent_proactive_messages"], [])
        self.assertTrue(scheduler._store._dirty)
        scheduler._store.flush()
        self.assertEqual(self.read_state()["activity_version"], 2)
        self.assertEqual(self.read_state()["recent_proactive_messages"], [])

    def test_concurrent_resets_preserve_each_version_increment(self):
        scheduler = self.scheduler()
        scheduler.notify_user_message_received(True, "123")
        barrier = threading.Barrier(5)
        failures = []
        def resetter():
            try:
                barrier.wait()
                for _ in range(5):
                    scheduler.reset_session(True, "123")
            except Exception as exc:
                failures.append(exc)
        threads = [threading.Thread(target=resetter) for _ in range(4)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(self.read_state()["activity_version"], 21)
        self.assertFalse(scheduler.can_send(True, "123", 1, True))
        self.assertEqual(self.read_state()["last_user_message_at"], 0)

    def test_version_idle_gap_continuation_and_stop(self):
        scheduler = self.scheduler()
        with mock.patch.object(proactive.time, "time", return_value=100):
            scheduler.notify_user_message_received(True, "123")
        self.config["group"].update(idle_threshold_seconds=10, min_interval_between_proactive_seconds=100)
        with mock.patch.object(proactive.time, "time", return_value=105):
            self.assertFalse(scheduler.can_send(True, "123", 1))
        with mock.patch.object(proactive.time, "time", return_value=111):
            self.assertTrue(scheduler.can_send(True, "123", 1))
            scheduler.notify_bot_message_sent(True, "123", is_proactive=True)
            self.assertFalse(scheduler.can_send(True, "123", 1))
            self.assertTrue(scheduler.can_send(True, "123", 1, True))
            scheduler.notify_user_message_received(True, "123")
            self.assertFalse(scheduler.can_send(True, "123", 1, True))
            self.assertFalse(scheduler.can_send(True, "123", True, True))
            self.assertTrue(scheduler.can_send(True, "123", 2, True))
            self.config["group"]["enabled"] = False
            self.assertFalse(scheduler.can_send(True, "123", 2, True))
        scheduler.stop()
        self.assertFalse(scheduler.can_send(True, "123", 2, True))

    def test_group_passive_bot_counts_private_passive_does_not_reset(self):
        scheduler = self.scheduler()
        for group in (True, False):
            scheduler.notify_user_message_received(group, "123")
            scheduler.notify_bot_message_sent(group, "123", is_proactive=True)
            scheduler.notify_bot_message_sent(group, "123", is_proactive=False)
        self.assertEqual(scheduler.get_session_state(True, "123")["consecutive_bot_count"], 2)
        self.assertEqual(scheduler.get_session_state(False, "123")["unanswered_count"], 1)

    def test_recent_content_bounded_and_snapshot_isolated(self):
        scheduler = self.scheduler()
        for _ in range(15):
            scheduler.notify_bot_message_sent(True, "123", is_proactive=True, content="x" * 5000)
        state = scheduler.get_session_state(True, "123")
        self.assertEqual(len(state["recent_proactive_messages"]), proactive.RECENT_PROACTIVE_LIMIT)
        self.assertEqual(len(state["recent_proactive_messages"][0]["content"]), proactive.RECENT_CONTENT_LIMIT)
        state["recent_proactive_messages"].clear()
        self.assertEqual(len(scheduler.get_session_state(True, "123")["recent_proactive_messages"]), 10)
        reloaded = proactive._StateStore().load("group", "123")
        self.assertEqual(len(reloaded["recent_proactive_messages"]), 10)

    def test_atomic_concurrent_updates(self):
        scheduler = self.scheduler()
        barrier = threading.Barrier(9)
        def worker():
            barrier.wait()
            for _ in range(100):
                scheduler.notify_user_message_received(True, "123")
        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(scheduler.get_session_state(True, "123")["activity_version"], 800)
        scheduler._store.flush()
        self.assertEqual(self.read_state()["activity_version"], 800)

    def test_concurrent_successful_message_accounting(self):
        scheduler = self.scheduler()
        scheduler.notify_user_message_received(False, "123")
        barrier = threading.Barrier(5)
        failures = []
        def sender():
            try:
                barrier.wait()
                for _ in range(10):
                    scheduler.notify_bot_message_sent(False, "123", is_proactive=True, content="sent")
            except Exception as exc:
                failures.append(exc)
        threads = [threading.Thread(target=sender) for _ in range(4)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        state = scheduler.get_session_state(False, "123")
        self.assertEqual(state["total_proactive_sent"], 40)
        self.assertEqual(state["unanswered_count"], 40)
        self.assertEqual(self.read_state("private")["total_proactive_sent"], 40)

    def test_flush_preserves_racing_update_and_serializes_writers(self):
        store = proactive._StateStore()
        def increment(state):
            state["activity_version"] += 1
        store.update("group", "123", increment)
        writing, release = threading.Event(), threading.Event()
        actual_write = proactive.atomic_write_json
        def blocked_write(path, state, **kwargs):
            writing.set()
            self.assertTrue(release.wait(2))
            actual_write(path, state, **kwargs)
        with mock.patch.object(proactive, "atomic_write_json", side_effect=blocked_write):
            writer = threading.Thread(target=store.flush)
            writer.start()
            try:
                self.assertTrue(writing.wait(2))
                store.update("group", "123", increment)
                self.assertEqual(store.load("group", "123")["activity_version"], 2)
            finally:
                release.set()
                writer.join(2)
        self.assertTrue(store._dirty)
        self.assertEqual(self.read_state()["activity_version"], 1)
        store.flush()
        self.assertFalse(store._dirty)
        self.assertEqual(self.read_state()["activity_version"], 2)

    def test_failed_flush_retains_dirty_and_retries(self):
        scheduler = self.scheduler()
        scheduler.notify_user_message_received(True, "123")
        with mock.patch.object(proactive, "atomic_write_json", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                scheduler._store.flush()
        self.assertTrue(scheduler._store._dirty)
        scheduler._store.flush()
        self.assertFalse(scheduler._store._dirty)
        self.assertEqual(self.read_state()["activity_version"], 1)

    def test_tick_fresh_time_and_config_and_exception_isolation(self):
        clock = [100]
        seen = []
        async def planner(cid, cfg):
            seen.append(cid)
            clock[0] = 200
            if cid == "1":
                self.config["private"]["enabled"] = False
                raise RuntimeError("one broken session")
            return False
        scheduler = self.scheduler(planner)
        with mock.patch.object(proactive.time, "time", side_effect=lambda: clock[0]):
            for group, cid in ((True, "1"), (True, "2"), (False, "3")):
                scheduler.notify_user_message_received(group, cid)
            scheduler._store.update("group", "2", lambda s: s.update(last_user_message_at=150))
            self.config["group"]["idle_threshold_seconds"] = 20
            clock[0] = 125
            asyncio.run(scheduler._tick_async())
        self.assertEqual(seen, ["1", "2"])
        self.assertFalse(scheduler._store._dirty)

    def test_capacity_evicts_fused_and_cleans_last_check_without_ttl(self):
        scheduler = self.scheduler()
        scheduler.MAX_ACTIVE_SESSIONS = 2
        scheduler._store = proactive._StateStore(max_entries=2)
        scheduler.notify_user_message_received(False, "1")
        scheduler.notify_user_message_received(False, "2")
        scheduler._store.flush()
        scheduler._last_check_at[("private", "2")] = 1
        scheduler._store.update("private", "2", lambda s: s.update(unanswered_count=3))
        scheduler._store.flush()
        scheduler.notify_user_message_received(False, "3")
        self.assertNotIn(("private", "2"), scheduler._active_sessions)
        self.assertNotIn(("private", "2"), scheduler._last_check_at)
        self.assertTrue(Path(proactive._state_path("private", "2")).is_file())
        scheduler._store.flush()
        for i in range(4, 15):
            scheduler.notify_user_message_received(False, str(i))
            scheduler._store.flush()
        self.assertLessEqual(len(scheduler._active_sessions), 2)
        self.assertLessEqual(len(scheduler._store._cache), 2)
        self.assertLessEqual(len(scheduler._last_check_at), 2)
        # No TTL: a seven-day-idle session remains eligible.
        self.config["private"]["idle_threshold_seconds"] = 604800
        with mock.patch.object(proactive.time, "time", return_value=100):
            scheduler.notify_user_message_received(False, "14")
        with mock.patch.object(proactive.time, "time", return_value=604901):
            self.assertTrue(scheduler.can_send(False, "14", 2))

    def test_all_dirty_capacity_never_writes_from_user_entry(self):
        scheduler = self.scheduler()
        scheduler._store = proactive._StateStore(max_entries=2)
        with mock.patch.object(proactive, "atomic_write_json") as write:
            for cid in ("1", "2", "3"):
                scheduler.notify_user_message_received(True, cid)
            write.assert_not_called()
        self.assertEqual(len(scheduler._store._cache), 2)
        self.assertNotIn(("group", "3"), scheduler._active_sessions)
        scheduler._store.flush()
        scheduler.notify_user_message_received(True, "3")
        self.assertEqual(scheduler.get_session_state(True, "3")["activity_version"], 1)

    def test_stop_cancels_inflight_no_next_planner_and_finally_flush(self):
        entered, cancelled = threading.Event(), threading.Event()
        calls = []
        async def planner(cid, cfg):
            calls.append(cid)
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        scheduler = self.scheduler(planner)
        scheduler.TICK_SECONDS = 0.01
        scheduler.notify_user_message_received(True, "1")
        scheduler.notify_user_message_received(True, "2")
        with mock.patch.object(asyncio, "to_thread", side_effect=AssertionError("executor forbidden")):
            scheduler.start()
            self.assertTrue(entered.wait(2))
            thread = scheduler._thread
            scheduler.stop(timeout=2)
        self.assertTrue(cancelled.is_set())
        self.assertFalse(thread.is_alive())
        self.assertEqual(calls, ["1"])
        self.assertIsNone(scheduler._task)
        self.assertIsNone(scheduler._event_loop)
        self.assertEqual(self.read_state(chat_id="1")["activity_version"], 2)
        self.assertEqual(self.read_state(chat_id="2")["activity_version"], 2)
        self.assertEqual(scheduler._active_sessions, {})
        self.assertEqual(scheduler._observations, {})

    def test_stop_closes_async_generators(self):
        entered, closed = threading.Event(), threading.Event()
        generators = []
        async def stream():
            try:
                yield "ready"
                await asyncio.Event().wait()
            finally:
                closed.set()
        async def planner(cid, cfg):
            generator = stream()
            generators.append(generator)  # 保留引用，让 shutdown_asyncgens 负责关闭。
            await generator.__anext__()
            entered.set()
            await asyncio.Event().wait()
        scheduler = self.scheduler(planner)
        scheduler.TICK_SECONDS = 0.01
        scheduler.notify_user_message_received(True, "123")
        scheduler.start()
        self.assertTrue(entered.wait(2))
        scheduler.stop(2)
        self.assertTrue(closed.is_set())

    def test_stop_wakes_sleep_without_executor(self):
        scheduler = self.scheduler()
        scheduler.TICK_SECONDS = 3600
        with mock.patch.object(asyncio, "to_thread", side_effect=AssertionError("executor forbidden")):
            scheduler.start()
            thread = scheduler._thread
            scheduler.stop(timeout=2)
        self.assertFalse(thread.is_alive())

    def test_stop_timeout_keeps_thread_and_singleton_until_dead(self):
        entered, cancelling, release = threading.Event(), threading.Event(), threading.Event()
        calls = []
        async def planner(cid, cfg):
            calls.append(cid)
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelling.set()
                # 模拟阻塞/暂不响应取消的外部 planner，仅用于 stop timeout 测试。
                release.wait(3)
            return False
        scheduler = self.scheduler(planner)
        scheduler.TICK_SECONDS = 0.01
        proactive._scheduler = scheduler
        scheduler.notify_user_message_received(True, "1")
        scheduler.notify_user_message_received(True, "2")
        scheduler.start()
        self.assertTrue(entered.wait(2))
        thread = scheduler._thread
        try:
            scheduler.stop(timeout=0)
            self.assertTrue(cancelling.wait(2))
            self.assertIs(scheduler._thread, thread)
            self.assertTrue(thread.is_alive())
            with mock.patch.object(scheduler, "stop", wraps=lambda: None):
                proactive.stop_proactive_scheduler()
            self.assertIs(proactive._scheduler, scheduler)
            other = proactive.start_proactive_scheduler(lambda: self.config, silent_planner, silent_planner)
            self.assertIs(other, scheduler)
            self.assertFalse(proactive.can_send(True, "1", 1, True))
        finally:
            release.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(calls, ["1"])
        proactive.stop_proactive_scheduler()
        self.assertIsNone(proactive._scheduler)

    def test_public_hooks_without_and_with_singleton(self):
        self.assertFalse(proactive.can_send(True, "123", 0))
        self.assertEqual(proactive.get_session_state(True, "123")["activity_version"], 0)
        scheduler = self.scheduler()
        proactive._scheduler = scheduler
        proactive.notify_user_message_received(True, "123")
        self.assertTrue(proactive.can_send(True, "123", 1))
        proactive.notify_bot_message_sent(True, "123", is_proactive=True, content="hello")
        self.assertEqual(proactive.get_session_state(True, "123")["recent_proactive_messages"][0]["content"], "hello")


if __name__ == "__main__":
    unittest.main()
