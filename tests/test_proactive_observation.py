"""Offline observation tests with a controlled clock; no QQ/model/network access."""
import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from bot import proactive_reply as proactive


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patch = mock.patch.object(proactive, "_STATE_DIR", self.temp.name)
        patch.start()
        self.addCleanup(patch.stop)
        self.now = 100.0
        patch = mock.patch.object(proactive.time, "time", side_effect=lambda: self.now)
        patch.start()
        self.addCleanup(patch.stop)
        self.config = {kind: {"enabled": True, "trigger_probability": 0,
            "observation": {"trigger_probability": 1},
            "idle_threshold_seconds": 900, "min_interval_between_proactive_seconds": 86400}
            for kind in ("group", "private")}
        self.calls = []
        self.scheduler = proactive.ProactiveReplyScheduler(lambda: self.config, self.planner, self.planner)
        self.old_singleton = proactive._scheduler
        proactive._scheduler = self.scheduler
        self.addCleanup(self.cleanup_scheduler)
        self.on_wake = None

    def cleanup_scheduler(self):
        self.scheduler.stop(2)
        self.assertFalse(self.scheduler._thread and self.scheduler._thread.is_alive())
        proactive._scheduler = self.old_singleton

    async def planner(self, cid, cfg):
        self.calls.append((cid, cfg))
        if self.on_wake is not None:
            return await self.on_wake(cid, cfg)
        return False

    def state(self, group=True, cid="123"):
        return self.scheduler.get_session_state(group, cid)

    def seed(self, group=True, cid="123", wait=None):
        self.scheduler.notify_user_message_received(group, cid)
        self.scheduler.notify_bot_message_sent(group, cid, is_proactive=True, content="first")
        self.scheduler.begin_observation(group, cid, wait_seconds=wait)
        self.assertIsNotNone(self.state(group, cid)["observation"])

    def incoming(self, mid="m1", handled=False, group=True, cid="123"):
        self.scheduler.note_incoming(group, cid, normal_handled=handled, message_id=mid)

    def tick(self):
        asyncio.run(self.scheduler._tick_async(include_cold=False))

    def test_type_hot_close_invalidates_tickets_wakes_and_registration_without_reviving(self):
        self.seed(True, "123")
        self.seed(False, "456")
        private_generation = self.state(False, "456")["observation"]["generation"]
        self.incoming("pending", handled=False, group=True)
        version = self.state()["activity_version"]
        # WebUI 热应用走同一个取消方法，不必等下一次 tick。
        proactive.cancel_observations("response_disabled", kind="group")
        self.assertIsNone(self.state()["observation"])
        self.assertNotIn(("group", "123"), self.scheduler._active_sessions)
        self.assertGreater(self.state()["activity_version"], version)
        self.assertEqual(self.state(False, "456")["observation"]["generation"], private_generation)
        self.assertFalse(self.scheduler.can_send(True, "123", version))
        self.config["group"]["enabled"] = False
        self.assertIsNone(self.scheduler.notify_user_message_received(True, "123", message_id="closed"))
        self.scheduler.touch_session(True, "123")
        self.scheduler.begin_observation(True, "123")
        self.assertNotIn(("group", "123"), self.scheduler._active_sessions)
        self.config["group"]["enabled"] = True
        self.now += 10000
        self.tick()
        self.assertFalse(any(cid == "123" for cid, _ in self.calls))
        self.assertIsNone(self.state()["observation"])
        # 旧 ticket 的 finally 不能将关闭前消息重新注册回来。
        ticket = self.scheduler.notify_user_message_received(True, "789", message_id="old")
        self.scheduler.cancel_observations("response_disabled", kind="group")
        self.assertFalse(self.scheduler.complete_normal_reply(True, "789", normal_handled=True,
            message_id="old", ticket=ticket, reply_sent=True))
        self.assertNotIn(("group", "789"), self.scheduler._active_sessions)

    def test_config_close_prevents_first_planning_and_timeout_wake_after_reopen(self):
        for group in (True, False):
            kind, cid = ("group", "123") if group else ("private", "456")
            ticket = self.scheduler.notify_user_message_received(group, cid, message_id="first")
            self.assertTrue(self.scheduler.complete_normal_reply(group, cid, normal_handled=True,
                message_id="first", ticket=ticket, reply_sent=True))
            self.config[kind]["enabled"] = False
            self.tick()
            self.assertFalse(self.calls)
            self.assertIsNone(self.state(group, cid)["observation"])
            self.config[kind]["enabled"] = True
            self.now += 1000
            self.tick()
            self.assertFalse(self.calls)
            # 新活动才能重新开始；等待中的关闭也撤销代次。
            self.seed(group, cid)
            generation = self.state(group, cid)["observation"]["generation"]
            version = self.state(group, cid)["activity_version"]
            self.config[kind]["enabled"] = False
            self.assertFalse(self.scheduler.can_send(group, cid, version, observation_generation=generation))
            self.config[kind]["enabled"] = True
            self.tick()
            self.assertFalse(self.calls)

    def test_initial_planner_can_choose_wait_without_sending(self):
        self.scheduler.notify_user_message_received(False, "123", message_id="m0")
        self.scheduler.note_incoming(False, "123", normal_handled=False, message_id="m0")
        self.now = 1100
        state = self.state(False)
        self.assertTrue(self.scheduler.set_observation_wait(False, "123", 37,
            expected_activity_version=state["activity_version"], allow_start=True))
        self.assertEqual(self.state(False)["observation"]["wait_until"], 1137)
        self.assertEqual(self.state(False)["total_proactive_sent"], 0)
        self.scheduler.notify_user_message_received(False, "123", message_id="m1")
        self.assertFalse(self.scheduler.set_observation_wait(False, "123", 50,
            expected_activity_version=state["activity_version"], allow_start=True))
        self.assertIsNone(self.state(False)["observation"])

    def test_defaults_independent_unknown_fields_and_obsolete_limits(self):
        cfg = proactive.normalize_config({"group": {"extra": 9, "observation": {
            "future": {"value": []}, "window_seconds": 1, "max_continuation_rounds": 1}}})
        self.assertEqual(cfg["private"]["observation"], proactive.DEFAULT_OBSERVATION_CONFIG)
        self.assertTrue(cfg["group"]["observation"]["enabled"])
        self.assertFalse(cfg["group"]["enabled"])
        self.assertNotIn("window_seconds", cfg["group"]["observation"])
        self.assertNotIn("max_continuation_rounds", cfg["group"]["observation"])
        cfg["group"]["observation"]["default_wait_seconds"] = 20
        self.assertEqual(cfg["private"]["observation"]["default_wait_seconds"], 60)
        self.assertEqual(proactive.DEFAULT_GROUP_CONFIG["observation"]["default_wait_seconds"], 60)
        self.assertEqual(cfg["group"]["extra"], 9)

    def test_strict_observation_validation_and_fail_closed(self):
        invalid = [None, False, [], "on"]
        for name in proactive._OBSERVATION_BOUNDS:
            for value in (True, "10", float("nan"), float("inf"), -1, 10 ** 1000):
                invalid.append({name: value})
        invalid += [{"enabled": value} for value in (0, 1, None, "false")]
        invalid += [{"min_wait_seconds": 100}, {"default_wait_seconds": 601},
                    {"min_wait_seconds": 0}, {"max_wait_seconds": 3601},
                    {"min_continuation_interval_seconds": 0},
                    {"debounce_seconds": 21, "max_debounce_seconds": 20}]
        self.seed()
        for value in invalid:
            with self.subTest(value=str(value)[:120]):
                with self.assertRaises(ValueError):
                    proactive.normalize_config({"group": {"observation": value}})
                self.config["group"]["observation"] = value
                self.assertFalse(self.scheduler.can_send(True, "123", 1,
                    observation_generation=1))
        self.assertIsNone(self.state()["observation"])

    def test_begin_requires_local_success_and_does_not_extend_or_resume(self):
        self.scheduler.notify_user_message_received(True, "123")
        self.scheduler.begin_observation(True, "123")
        self.assertIsNone(self.state()["observation"])
        self.scheduler.notify_bot_message_sent(True, "123", is_proactive=True)
        self.scheduler.begin_observation(True, "123")
        snapshot = self.state()["observation"]
        self.now = 120
        self.scheduler.begin_observation(True, "123", wait_seconds=500)
        self.assertEqual(self.state()["observation"], snapshot)
        other = proactive.ProactiveReplyScheduler(lambda: self.config, self.planner, self.planner)
        other.touch_session(True, "123")
        other.begin_observation(True, "123")
        self.assertIsNone(other.get_session_state(True, "123")["observation"])
        other.stop()

    def test_new_message_debounce_no_probability_roll_and_single_consumption(self):
        self.seed()
        self.now = 130
        self.incoming()
        self.now = 137
        self.tick()
        self.assertEqual(self.calls, [])
        checks = []
        async def check(cid, cfg):
            metadata = cfg["_observation"]
            checks.append(self.scheduler.can_send(True, cid, self.state()["activity_version"],
                observation_generation=metadata["generation"]))
            self.assertEqual(metadata["new_messages"], True)
            self.assertEqual(metadata["timeout"], False)
            self.assertFalse(self.scheduler.can_send(True, cid, self.state()["activity_version"]))
            return False
        self.on_wake = check
        self.now = 138
        with mock.patch.object(proactive.random, "random", side_effect=AssertionError("reroll")):
            self.tick()
            for _ in range(3):
                self.tick()
        self.assertEqual(checks, [True])
        self.assertEqual(len(self.calls), 1)
        self.assertIsNone(self.state()["observation"])

    def test_max_debounce_does_not_move_forever_and_ignores_elapsed_timeout(self):
        self.seed(wait=10)
        for now, mid in ((130, "1"), (137, "2"), (144, "3"), (149, "4")):
            self.now = now
            self.incoming(mid)
            self.tick()
            self.assertEqual(self.calls, [])
        self.assertEqual(self.state()["observation"]["message_due_at"], 150)
        self.now = 150
        self.tick()
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(self.calls[0][1]["_observation"]["new_messages"])
        self.assertTrue(self.calls[0][1]["_observation"]["timeout"])

    def test_timeout_can_send_once_and_no_wait_ends_episode(self):
        self.seed()
        checks = []
        async def timeout(cid, cfg):
            obs = cfg["_observation"]
            checks.append(self.scheduler.can_send(True, cid, self.state()["activity_version"],
                observation_generation=obs["generation"]))
            self.assertEqual((obs["new_messages"], obs["timeout"]), (False, True))
            return False
        self.on_wake = timeout
        self.now = 159
        self.tick()
        self.assertEqual(self.calls, [])
        self.now = 160
        self.tick()
        self.now = 1000
        self.tick()
        self.assertEqual(checks, [True])
        self.assertIsNone(self.state()["observation"])
        self.assertFalse(self.scheduler.set_observation_wait(True, "123", 10))

    def test_wait_on_pass_can_continue_beyond_five_minutes_and_two_wakes(self):
        self.seed()
        generations = []
        async def waiting(cid, cfg):
            generation = cfg["_observation"]["generation"]
            generations.append(generation)
            self.assertTrue(self.scheduler.set_observation_wait(True, cid, 100))
            return False
        self.on_wake = waiting
        for now in (160, 260, 360, 460, 560):
            self.now = now
            self.tick()
            self.tick()
        self.assertEqual(len(self.calls), 5)
        self.assertEqual(len(set(generations)), 5)
        self.assertEqual(self.state()["observation"]["wake_count"], 5)
        self.assertEqual(self.state()["observation"]["started_at"], 100)
        self.assertEqual(self.state()["consecutive_bot_count"], 1)
        self.scheduler.finish_observation(True, "123")
        self.now = 700
        self.tick()
        self.assertEqual(len(self.calls), 5)
        self.scheduler.begin_observation(True, "123")
        self.assertIsNone(self.state()["observation"])

    def test_single_wait_bounds_and_invalid_intents(self):
        self.seed()
        for seconds, expected in ((0, 110), (5, 110), (15.5, 115.5), (10000, 700)):
            self.assertTrue(proactive.set_observation_wait(True, "123", seconds))
            self.assertEqual(self.state()["observation"]["wait_until"], expected)
        previous = self.state()["observation"]
        for seconds in (True, None, "10", -1, float("nan"), float("inf"), 10 ** 1000):
            self.assertFalse(self.scheduler.set_observation_wait(True, "123", seconds))
            self.assertEqual(self.state()["observation"], previous)

    def test_two_stage_activity_dedup_blocks_wake_until_route_resolved(self):
        self.seed()
        self.now = 160
        with mock.patch.object(proactive, "atomic_write_json") as write:
            proactive.notify_user_message_received(True, "123", message_id="inflight")
            write.assert_not_called()
        self.assertEqual(self.state()["activity_version"], 2)
        self.tick()
        self.assertEqual(self.calls, [])
        proactive.note_incoming(True, "123", normal_handled=False, message_id="inflight")
        self.assertEqual(self.state()["activity_version"], 2)
        self.assertEqual(self.state()["observation"]["message_due_at"], 168)
        proactive.note_incoming(True, "123", normal_handled=False, message_id="inflight")
        self.assertEqual(self.scheduler._observations[("group", "123")].message_sequence, 1)
        self.now = 168
        self.tick()
        self.assertEqual(len(self.calls), 1)

    def test_normal_handled_cancels_even_after_false_and_duplicate_cannot_reset_fuse(self):
        self.seed()
        self.now = 130
        self.incoming()
        self.scheduler.notify_bot_message_sent(True, "123", is_proactive=True)
        self.incoming()
        self.assertEqual(self.state()["consecutive_bot_count"], 1)
        self.assertEqual(self.state()["activity_version"], 2)
        self.incoming(handled=True)
        self.assertIsNone(self.state()["observation"])
        version = self.state()["activity_version"]
        self.incoming(handled=True)
        self.assertEqual(self.state()["activity_version"], version)
        self.now = 300
        self.tick()
        self.assertEqual(self.calls, [])

    def test_private_cancels_immediately_and_legacy_notify_never_schedules(self):
        self.seed(group=False)
        proactive.notify_user_message_received(False, "123", message_id="private")
        self.assertIsNone(self.state(False)["observation"])
        proactive.note_incoming(False, "123", normal_handled=False, message_id="private")
        self.assertEqual(self.state(False)["activity_version"], 2)
        self.seed()
        self.now = 130
        self.scheduler.notify_user_message_received(True, "123")
        snapshot = self.state()["observation"]
        self.assertFalse(snapshot["new_messages"])
        self.assertIsNone(snapshot["message_due_at"])
        self.tick()
        self.assertEqual(self.calls, [])

    def test_observation_send_still_uses_fuses_and_split_interval_exception(self):
        for group in (True, False):
            cid = "123" if group else "456"
            self.now = 100
            self.seed(group=group, cid=cid, wait=10)
            checks = []
            async def send(chat_id, cfg):
                version = self.state(group, cid)["activity_version"]
                generation = cfg["_observation"]["generation"]
                gate = lambda continuation=False: self.scheduler.can_send(group, cid, version,
                    continuation, observation_generation=generation)
                self.assertTrue(gate())
                self.scheduler.notify_bot_message_sent(group, cid, is_proactive=True)
                self.assertFalse(gate())
                self.assertTrue(gate(True))
                self.scheduler.notify_bot_message_sent(group, cid, is_proactive=True)
                checks.append(gate(True))
                self.scheduler.set_observation_wait(group, cid, 10)
                return True
            self.on_wake = send
            self.now = 129
            self.tick()
            self.now = 130
            self.tick()
            self.assertEqual(checks, [False])
            state = self.state(group, cid)
            counter = "consecutive_bot_count" if group else "unanswered_count"
            self.assertEqual(state[counter], 3)
            self.now = 160
            self.tick()
            self.assertIsNone(self.state(group, cid)["observation"])
            self.assertEqual(self.state(group, cid)[counter], 3)

    def test_min_wake_interval_limits_pass_wait_cost(self):
        self.seed(wait=10)
        async def waiting(cid, cfg):
            self.scheduler.set_observation_wait(True, cid, 10)
            return False
        self.on_wake = waiting
        for now, expected in ((129, 0), (130, 1), (140, 1), (159, 1), (160, 2)):
            self.now = now
            self.tick()
            self.assertEqual(len(self.calls), expected)

    def test_new_message_during_runner_invalidates_old_result_and_keeps_pending(self):
        self.seed()
        generations = []
        async def racing(cid, cfg):
            version = self.state()["activity_version"]
            generation = cfg["_observation"]["generation"]
            generations.append(generation)
            if len(generations) == 1:
                self.scheduler.notify_user_message_received(True, cid, message_id="racing")
                self.assertFalse(self.scheduler.can_send(True, cid, version,
                    observation_generation=generation))
                self.assertFalse(self.scheduler.set_observation_wait(True, cid, 20))
            else:
                self.assertFalse(self.scheduler.can_send(True, cid, version,
                    observation_generation=generations[0]))
                self.assertTrue(self.scheduler.can_send(True, cid, version,
                    observation_generation=generation))
            return False
        self.on_wake = racing
        self.now = 160
        self.tick()
        self.assertIsNotNone(self.state()["observation"])
        self.now = 180
        self.incoming("racing")
        self.assertEqual(self.state()["activity_version"], 2)
        self.now = 190
        self.tick()
        self.assertEqual(len(generations), 2)
        self.assertNotEqual(*generations)

    def test_disable_lists_invalid_config_and_finish_fail_closed_during_runner(self):
        changes = [lambda: self.config["group"].update(enabled=False),
            lambda: self.config["group"].update(observation={"enabled": False}),
            lambda: self.config["group"].update(blacklist=[123]),
            lambda: self.config["group"].update(whitelist=[456]),
            lambda: self.config["group"].update(observation={"enabled": "bad"}),
            lambda: proactive.finish_observation(True, "123")]
        for change in changes:
            self.config["group"] = {"enabled": True, "trigger_probability": 0,
                "observation": {"trigger_probability": 1},
                "idle_threshold_seconds": 900, "min_interval_between_proactive_seconds": 86400}
            self.now = 100
            self.seed()
            async def changing(cid, cfg):
                version = self.state()["activity_version"]
                generation = cfg["_observation"]["generation"]
                change()
                self.assertFalse(self.scheduler.can_send(True, cid, version, True,
                    observation_generation=generation))
                self.assertFalse(self.scheduler.set_observation_wait(True, cid, 60))
                return False
            self.on_wake = changing
            self.now = 160
            self.tick()
            self.assertIsNone(self.state()["observation"])

    def test_reset_disconnect_stop_restart_do_not_resume_or_clear_fuses(self):
        self.seed(group=False)
        self.now = 130
        self.scheduler.notify_bot_message_sent(False, "123", is_proactive=True)
        old_version = self.state(False)["activity_version"]
        proactive.cancel_observations()
        self.assertGreater(self.state(False)["activity_version"], old_version)
        self.assertEqual(self.state(False)["unanswered_count"], 2)
        self.assertEqual(self.scheduler._active_sessions, {})
        self.scheduler.begin_observation(False, "123")
        self.assertIsNone(self.state(False)["observation"])
        self.seed()
        proactive.reset_session(True, "123")
        self.assertIsNone(self.state()["observation"])
        self.assertEqual(self.state()["consecutive_bot_count"], 0)
        self.seed()
        self.scheduler.stop()
        version = self.state()["activity_version"]
        self.assertIsNone(self.state()["observation"])
        self.assertEqual(self.state()["consecutive_bot_count"], 1)
        self.scheduler.start()
        self.scheduler.begin_observation(True, "123")
        self.assertIsNone(self.state()["observation"])
        self.assertEqual(self.state()["activity_version"], version)
        self.scheduler.stop()
        disk = json.loads(Path(proactive._state_path("group", "123")).read_text(encoding="utf-8"))
        self.assertNotIn("observation", disk)

    def test_priority_serial_runner_exception_cleanup_and_fresh_config(self):
        self.seed(cid="observed")
        self.scheduler.notify_user_message_received(True, "cold")
        self.config["group"].update(trigger_probability=1, idle_threshold_seconds=0,
            min_interval_between_proactive_seconds=0)
        self.now = 160
        async def changing(cid, cfg):
            if cid == "observed":
                self.config["group"]["enabled"] = False
                raise RuntimeError("judge failed")
            self.fail("cold runner must see fresh disabled configuration")
        self.on_wake = changing
        asyncio.run(self.scheduler._tick_async())
        self.assertEqual([cid for cid, _ in self.calls], ["observed"])
        self.assertIsNone(self.state(cid="observed")["observation"])
        self.assertFalse(self.scheduler._store._dirty)

    def test_capacity_and_snapshots_are_bounded_and_isolated(self):
        self.scheduler.MAX_ACTIVE_SESSIONS = 3
        for i in range(8):
            self.seed(cid=str(i))
            self.scheduler._store.flush()
        self.assertLessEqual(len(self.scheduler._observations), 3)
        self.assertLessEqual(len(self.scheduler._begin_eligible), 3)
        for i in range(proactive.RECENT_INCOMING_LIMIT + 30):
            self.incoming(str(i), cid="7")
        self.assertLessEqual(len(self.scheduler._recent_incoming[("group", "7")]),
            proactive.RECENT_INCOMING_LIMIT)
        self.assertLessEqual(len(self.scheduler._recent_incoming), 3)
        self.assertLessEqual(len(self.scheduler._store._cache), 1000)
        snapshot = self.state(cid="7")["observation"]
        snapshot["wait_until"] = -1
        self.assertGreater(self.state(cid="7")["observation"]["wait_until"], 0)
        self.assertNotIn("message_sequence", snapshot)

    def test_route_pending_blocks_cold_and_continuation_and_bool_inputs_are_strict(self):
        self.scheduler.notify_user_message_received(True, "123", message_id="pending")
        self.config["group"].update(trigger_probability=1, idle_threshold_seconds=0,
            min_interval_between_proactive_seconds=0)
        with mock.patch.object(proactive.random, "random", side_effect=AssertionError("premature roll")):
            asyncio.run(self.scheduler._tick_async())
        self.assertEqual(self.calls, [])
        self.assertFalse(self.scheduler.can_send(True, "123", 1, True))
        for handled in (None, 0, 1, "false"):
            with self.assertRaises(ValueError):
                self.scheduler.note_incoming(True, "123", normal_handled=handled)
        self.scheduler.note_incoming(True, "123", normal_handled=True, message_id="pending")
        self.assertTrue(self.scheduler.can_send(True, "123", 1, True))

    def test_disconnect_during_runner_invalidates_generation_preserves_fuse(self):
        self.seed()
        async def disconnecting(cid, cfg):
            generation = cfg["_observation"]["generation"]
            version = self.state()["activity_version"]
            self.assertTrue(self.scheduler.can_send(True, cid, version,
                observation_generation=generation))
            self.scheduler.cancel_observations()
            self.assertFalse(self.scheduler.can_send(True, cid, version, True,
                observation_generation=generation))
            self.assertFalse(self.scheduler.set_observation_wait(True, cid, 20))
            self.scheduler.touch_session(True, cid)
            self.assertFalse(self.scheduler.can_send(True, cid, version, True))
            return False
        self.on_wake = disconnecting
        self.now = 160
        self.tick()
        self.assertEqual(self.state()["consecutive_bot_count"], 1)
        self.assertIsNone(self.state()["observation"])
        self.assertEqual(self.scheduler._begin_eligible, {})

    def test_capacity_failure_cancels_episode_without_user_entry_disk_write(self):
        self.scheduler._store = proactive._StateStore(max_entries=1)
        self.seed()
        self.scheduler.notify_user_message_received(True, "456")
        self.assertNotIn(("group", "123"), self.scheduler._store._cache)
        self.assertTrue(self.scheduler._store._dirty)
        with mock.patch.object(proactive, "atomic_write_json") as write:
            self.incoming()
            write.assert_not_called()
        self.assertNotIn(("group", "123"), self.scheduler._observations)
        self.assertNotIn(("group", "123"), self.scheduler._active_sessions)
        self.assertFalse(self.scheduler.can_send(True, "123", 1, True))

    def test_short_observation_poll_does_not_increase_cold_tick_rate(self):
        intervals, cold_flags = [], []
        clock = [0.0]
        self.seed()
        async def wait(seconds):
            intervals.append(seconds)
            clock[0] += 28 if len(intervals) == 3 else 1
        async def tick(*, include_cold=True):
            cold_flags.append(include_cold)
            if len(cold_flags) == 3:
                self.scheduler._stop.set()
        with mock.patch.object(self.scheduler, "_wait_or_stop", side_effect=wait), \
                mock.patch.object(self.scheduler, "_tick_async", side_effect=tick), \
                mock.patch.object(proactive.time, "monotonic", side_effect=lambda: clock[0]):
            asyncio.run(self.scheduler._async_loop())
        self.assertEqual(cold_flags, [False, False, True])
        self.assertTrue(all(0 <= seconds <= 1 for seconds in intervals))
        self.scheduler._stop.clear()

    def test_stop_cancels_observation_planner_and_wakes_without_executor(self):
        self.seed()
        self.now = 160
        entered, cancelled = threading.Event(), threading.Event()
        async def blocking(cid, cfg):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        self.on_wake = blocking
        self.scheduler.TICK_SECONDS = 0.01
        with mock.patch.object(asyncio, "to_thread", side_effect=AssertionError("executor forbidden")):
            self.scheduler.start()
            self.assertTrue(entered.wait(2))
            self.scheduler.stop(2)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(self.scheduler._observations, {})
        self.assertEqual(self.scheduler._active_sessions, {})

    def test_group_normal_success_replaces_observation_without_probability_roll(self):
        logs = []
        self.scheduler._log = logs.append
        self.seed()
        previous = self.state()["observation"]
        self.now = 120
        ticket = self.scheduler.notify_user_message_received(True, "123", message_id="normal")
        self.assertIsNotNone(ticket)
        self.scheduler.note_incoming(True, "123", normal_handled=True, message_id="normal")
        self.assertIsNone(self.state()["observation"])
        self.scheduler.notify_bot_message_sent(True, "123", is_proactive=False)
        with mock.patch.object(proactive.random, "random", return_value=0):
            self.assertTrue(self.scheduler.complete_normal_reply(True, "123", normal_handled=True,
                message_id="normal", ticket=ticket, reply_sent=True))
            self.assertFalse(self.scheduler.complete_normal_reply(True, "123", normal_handled=True,
                message_id="normal", ticket=ticket, reply_sent=True))
            episode = self.state()["observation"]
            self.assertNotEqual(episode["generation"], previous["generation"])
            self.assertEqual(episode["wait_until"], 180)
            self.tick()
            self.assertTrue(self.calls[0][1]["_observation"]["initial_wait"])
            self.now = 179
            self.tick()
            self.assertEqual(len(self.calls), 1)
            self.now = 180
            self.tick()
            self.tick()
        self.assertEqual(len(self.calls), 2)
        self.assertIsNone(self.state()["observation"])
        self.assertTrue(any("进入持续观察" in line and "普通回复" in line
                            and "群聊 123" in line for line in logs))

    def test_group_normal_observation_requires_current_success_and_enabled_policy(self):
        for scenario in ("failed", "unhandled", "stale", "disabled", "observation_disabled", "fused"):
            with self.subTest(scenario=scenario):
                self.scheduler.reset_session(True, "123")
                self.config["group"] = {"enabled": True, "trigger_probability": 0}
                ticket = self.scheduler.notify_user_message_received(True, "123", message_id="normal")
                if scenario == "stale":
                    self.scheduler.notify_user_message_received(True, "123", message_id="new")
                elif scenario == "disabled":
                    self.config["group"]["enabled"] = False
                elif scenario == "observation_disabled":
                    self.config["group"]["observation"] = {"enabled": False}
                elif scenario == "fused":
                    self.config["group"]["max_consecutive_bot_messages"] = 1
                    self.scheduler.notify_bot_message_sent(True, "123", is_proactive=False)
                with mock.patch.object(proactive.random, "random", return_value=0):
                    self.assertFalse(self.scheduler.complete_normal_reply(True, "123",
                        normal_handled=scenario != "unhandled", message_id="normal", ticket=ticket,
                        reply_sent=scenario != "failed"))
                self.assertIsNone(self.state()["observation"])

    def test_private_normal_reply_observation_ignores_probability_and_respects_switch(self):
        for probability in (0, 0.3, 1):
            for enabled in (False, True):
                with self.subTest(probability=probability, enabled=enabled):
                    self.now = 100
                    self.calls.clear()
                    self.scheduler.reset_session(False, "123")
                    self.config["private"].update(trigger_probability=probability,
                        observation={"enabled": enabled, "trigger_probability": 1})
                    ticket = self.scheduler.notify_user_message_received(False, "123", message_id="normal")
                    self.scheduler.notify_bot_message_sent(False, "123", is_proactive=False)
                    with mock.patch.object(proactive.random, "random", return_value=0):
                        self.assertEqual(self.scheduler.complete_normal_reply(False, "123",
                            normal_handled=True, message_id="normal", ticket=ticket, reply_sent=True), enabled)
                        self.assertFalse(self.scheduler.complete_normal_reply(False, "123",
                            normal_handled=True, message_id="normal", ticket=ticket, reply_sent=True))
                        self.assertEqual(self.state(False)["observation"] is not None, enabled)
                        self.assertEqual(self.calls, [])
                        self.tick()
                        self.assertEqual(len(self.calls), int(enabled))
                        if enabled:
                            self.assertTrue(self.calls[0][1]["_observation"]["initial_wait"])
                        self.now = 159
                        self.tick()
                        self.assertEqual(len(self.calls), int(enabled))
                        self.now = 160
                        self.tick()
                        self.assertEqual(len(self.calls), 2 * int(enabled))

    def test_observation_probability_applies_once_to_successful_normal_and_proactive_starts(self):
        for group in (False, True):
            kind = "group" if group else "private"
            for source in ("normal", "proactive"):
                for probability, sample, expected in ((0, 0, False), (1, 0.99, True),
                                                       (0.5, 0.49, True), (0.5, 0.5, False)):
                    with self.subTest(group=group, source=source, probability=probability, sample=sample):
                        self.scheduler.reset_session(group, "123")
                        self.config[kind]["observation"] = {"trigger_probability": probability}
                        logs = []
                        self.scheduler._log = logs.append
                        ticket = self.scheduler.notify_user_message_received(group, "123", message_id="normal")
                        self.scheduler.notify_bot_message_sent(group, "123", is_proactive=source == "proactive")
                        with mock.patch.object(proactive.random, "random", return_value=sample) as roll:
                            if source == "normal":
                                self.assertEqual(self.scheduler.complete_normal_reply(group, "123", normal_handled=True,
                                    message_id="normal", ticket=ticket, reply_sent=True), expected)
                                self.assertFalse(self.scheduler.complete_normal_reply(group, "123", normal_handled=True,
                                    message_id="normal", ticket=ticket, reply_sent=True))
                            else:
                                self.scheduler.note_incoming(group, "123", normal_handled=False, message_id="normal")
                                self.scheduler.begin_observation(group, "123")
                                self.scheduler.begin_observation(group, "123")
                            self.assertEqual(self.state(group)["observation"] is not None, expected)
                            self.assertEqual(roll.call_count, int(probability > 0))
                            if expected:
                                self.assertTrue(self.scheduler.set_observation_wait(group, "123", 20))
                                self.scheduler.notify_bot_message_sent(group, "123", is_proactive=True)
                                self.scheduler.begin_observation(group, "123")
                                self.assertEqual(roll.call_count, 1)
                        if not expected:
                            self.assertEqual(sum("未命中观察概率" in line for line in logs), 1)

    def test_unhandled_message_is_silent_and_incomplete_reply_is_not_reported_as_send_failure(self):
        logs = []
        self.scheduler._log = logs.append
        ticket = self.scheduler.notify_user_message_received(False, "123", message_id="unhandled")
        with mock.patch.object(proactive.random, "random", side_effect=AssertionError("不应抽取概率")):
            self.assertFalse(self.scheduler.complete_normal_reply(False, "123", normal_handled=False,
                message_id="unhandled", ticket=ticket, reply_sent=False))
        self.assertEqual(logs, [])
        ticket = self.scheduler.notify_user_message_received(False, "123", message_id="incomplete")
        self.assertFalse(self.scheduler.complete_normal_reply(False, "123", normal_handled=True,
            message_id="incomplete", ticket=ticket, reply_sent=False))
        self.assertTrue(any("未确认完整回复" in line for line in logs))
        self.assertFalse(any("最终回复未完整发送成功" in line or "普通回复发送成功" in line for line in logs))

    def test_initial_wait_choice_fallback_deadline_and_stale_guard(self):
        for seconds, expected in ((25, 125), (60, 160), (61, 160), (None, 160),
                                  (True, 160), (float("nan"), 160), (float("inf"), 160),
                                  (5, 160), (-1, 160), (10 ** 400, 160)):
            with self.subTest(seconds=seconds):
                self.scheduler.reset_session(False, "123")
                self.now = 100
                ticket = self.scheduler.notify_user_message_received(False, "123", message_id="normal")
                self.assertTrue(self.scheduler.complete_normal_reply(False, "123", normal_handled=True,
                    message_id="normal", ticket=ticket, reply_sent=True))
                async def choose(cid, cfg):
                    self.assertTrue(cfg["_observation"]["initial_wait"])
                    self.now = 110
                    self.assertTrue(self.scheduler.set_observation_wait(False, cid, seconds,
                        expected_activity_version=self.state(False)["activity_version"],
                        expected_generation=cfg["_observation"]["generation"], planner_choice=True))
                    return False
                self.on_wake = choose
                self.tick()
                self.assertEqual(self.state(False)["observation"]["wait_until"], expected)
        self.scheduler.reset_session(False, "123")
        self.now = 100
        ticket = self.scheduler.notify_user_message_received(False, "123", message_id="old")
        self.scheduler.complete_normal_reply(False, "123", normal_handled=True,
            message_id="old", ticket=ticket, reply_sent=True)
        async def stale(cid, cfg):
            version = self.state(False)["activity_version"]
            self.scheduler.notify_user_message_received(False, cid, message_id="new")
            self.assertFalse(self.scheduler.set_observation_wait(False, cid, 20,
                expected_activity_version=version, expected_generation=cfg["_observation"]["generation"],
                planner_choice=True))
            return False
        self.on_wake = stale
        self.tick()
        self.assertIsNone(self.state(False)["observation"])

    def test_normal_ticket_success_route_dedup_and_pass_wait_lifecycle(self):
        self.config["private"]["trigger_probability"] = 1
        logs = []
        self.scheduler._log = logs.append
        with mock.patch.object(proactive, "atomic_write_json") as write:
            ticket = proactive.notify_user_message_received(False, "123", message_id="normal")
            self.assertIsNotNone(ticket)
            self.assertIsNone(proactive.notify_user_message_received(False, "123", message_id="normal"))
            self.assertTrue(proactive.notify_bot_message_sent(False, "123", is_proactive=False))
            self.assertTrue(proactive.complete_normal_reply(False, "123", normal_handled=True,
                message_id="normal", ticket=ticket, reply_sent=True))
            write.assert_not_called()
        snapshot = self.state(False)
        self.assertEqual(snapshot["activity_version"], 1)
        self.assertEqual(snapshot["unanswered_count"], 0)
        self.assertEqual(snapshot["total_proactive_sent"], 0)
        self.assertEqual(snapshot["last_proactive_at"], 0)
        self.now = 120
        self.assertFalse(proactive.complete_normal_reply(False, "123", normal_handled=True,
            message_id="normal", ticket=ticket, reply_sent=True))
        self.assertEqual(self.state(False)["observation"], snapshot["observation"])
        for _ in range(3):
            self.tick()
        self.assertEqual(len(logs), 2)
        async def waiting(cid, cfg):
            if len(self.calls) == 2:
                self.scheduler.set_observation_wait(False, cid, 30)
            return False
        self.on_wake = waiting
        self.now = 160
        self.tick()
        self.tick()
        self.now = 190
        self.tick()
        self.tick()
        self.assertIsNone(self.state(False)["observation"])
        self.assertEqual(sum("进入持续观察" in line for line in logs), 1)
        self.assertEqual(sum("观察唤醒" in line for line in logs), 3)
        self.assertEqual(sum("调整观察等待" in line for line in logs), 1)
        self.assertEqual(sum("观察任务结束" in line for line in logs), 1)

    def test_normal_ticket_invalidated_by_activity_reset_lifecycle_and_configuration(self):
        mutations = [lambda: self.scheduler.notify_user_message_received(False, "123", message_id="new"),
            lambda: self.scheduler.reset_session(False, "123"),
            lambda: self.scheduler.cancel_observations(),
            lambda: self.scheduler.stop(),
            lambda: self.config["private"].update(enabled=False),
            lambda: self.config["private"].update(blacklist=[123]),
            lambda: self.config["private"].update(whitelist=[456]),
            lambda: self.config["private"].update(observation={"enabled": False})]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                self.scheduler._stop.clear()
                self.config["private"] = {"enabled": True}
                ticket = self.scheduler.notify_user_message_received(False, "123", message_id=f"old{index}")
                self.assertIsNotNone(ticket)
                mutate()
                self.assertFalse(self.scheduler.complete_normal_reply(False, "123", normal_handled=True,
                    message_id=f"old{index}", ticket=ticket, reply_sent=True))
                self.assertIsNone(self.state(False)["observation"])
                if index in (1, 2, 3):
                    self.assertNotIn(("private", "123"), self.scheduler._active_sessions)

    def test_normal_ticket_missing_failed_wrong_session_and_new_scheduler(self):
        ticket = self.scheduler.notify_user_message_received(False, "123", message_id="normal")
        self.assertFalse(self.scheduler.complete_normal_reply(False, "123", normal_handled=True,
            message_id="normal", ticket=ticket, reply_sent=False))
        self.assertFalse(self.scheduler.complete_normal_reply(False, "123", normal_handled=True,
            message_id="normal", ticket=ticket, reply_sent=True))
        ticket = self.scheduler.notify_user_message_received(False, "123", message_id="other")
        self.assertFalse(self.scheduler.complete_normal_reply(False, "456", normal_handled=True,
            message_id="other", ticket=ticket, reply_sent=True))
        other = proactive.ProactiveReplyScheduler(lambda: self.config, self.planner, self.planner)
        self.assertFalse(other.complete_normal_reply(False, "123", normal_handled=True,
            message_id="other", ticket=ticket, reply_sent=True))
        self.assertIsNotNone(self.scheduler.notify_user_message_received(True, "123", message_id="group"))
        self.assertIsNone(self.state(False)["observation"])

    def test_normal_capacity_failure_and_duplicate_route_cannot_revalidate_ticket(self):
        self.config["private"]["trigger_probability"] = 1
        self.scheduler._store = proactive._StateStore(max_entries=1)
        ticket = self.scheduler.notify_user_message_received(False, "123", message_id="first")
        self.assertIsNotNone(ticket)
        self.assertIsNone(self.scheduler.notify_user_message_received(False, "456", message_id="full"))
        self.assertFalse(self.scheduler.notify_bot_message_sent(False, "456", is_proactive=False))
        self.assertIsNone(self.scheduler.notify_user_message_received(False, "123", message_id="first"))
        self.scheduler.note_incoming(False, "123", normal_handled=True, message_id="first")
        self.assertTrue(self.scheduler.complete_normal_reply(False, "123", normal_handled=True,
            message_id="first", ticket=ticket, reply_sent=True))
        newer = self.scheduler.notify_user_message_received(False, "123", message_id="second")
        self.assertIsNotNone(newer)
        self.scheduler.note_incoming(False, "123", normal_handled=True, message_id="first")
        self.assertFalse(self.scheduler.complete_normal_reply(False, "123", normal_handled=True,
            message_id="first", ticket=ticket, reply_sent=True))
        self.assertIsNone(self.state(False)["observation"])

    def test_same_wake_rearm_intent_does_not_extend_or_repeat_log(self):
        logs = []
        self.scheduler._log = logs.append
        self.seed(group=False)
        async def waiting(cid, cfg):
            self.assertTrue(self.scheduler.set_observation_wait(False, cid, 45))
            deadline = self.state(False)["observation"]["wait_until"]
            self.now += 1
            self.assertTrue(self.scheduler.set_observation_wait(False, cid, 45))
            self.assertEqual(self.state(False)["observation"]["wait_until"], deadline)
            return False
        self.on_wake = waiting
        self.now = 160
        self.tick()
        self.assertEqual(sum("调整观察等待" in line for line in logs), 1)

    def test_bound_wait_and_finish_reject_wrong_generation_even_same_activity(self):
        self.seed(group=False)
        snapshot = self.state(False)
        version = snapshot["activity_version"]
        generation = snapshot["observation"]["generation"]
        for expected in (generation + 1, None):
            self.assertFalse(proactive.set_observation_wait(False, "123", 500,
                expected_activity_version=version, expected_generation=expected))
            self.assertFalse(proactive.finish_observation(False, "123",
                expected_activity_version=version, expected_generation=expected))
            self.assertEqual(self.state(False)["observation"], snapshot["observation"])
        self.assertTrue(proactive.set_observation_wait(False, "123", 25,
            expected_activity_version=version, expected_generation=generation))
        self.assertEqual(self.state(False)["observation"]["wait_until"], 125)
        self.assertTrue(proactive.finish_observation(False, "123",
            expected_activity_version=version, expected_generation=generation))
        self.assertIsNone(self.state(False)["observation"])

    def test_observation_error_logs_are_sanitized(self):
        logs = []
        self.scheduler._log = logs.append
        self.seed(group=False)
        async def broken(cid, cfg):
            raise RuntimeError("PRIVATE_RAW token=secret topic=hidden")
        self.on_wake = broken
        self.now = 160
        self.tick()
        self.assertTrue(any("reason=observation_error" in line for line in logs))
        self.assertNotIn("PRIVATE_RAW", " ".join(logs))
        self.assertNotIn("secret", " ".join(logs))

    def test_public_apis_without_singleton(self):
        proactive._scheduler = None
        proactive.notify_user_message_received(True, "123", message_id="1")
        proactive.note_incoming(True, "123", normal_handled=False, message_id="1")
        proactive.begin_observation(True, "123", wait_seconds=30)
        self.assertFalse(proactive.set_observation_wait(True, "123", 30))
        proactive.finish_observation(True, "123")
        proactive.cancel_observations()
        self.assertIsNone(proactive.get_session_state(True, "123")["observation"])
        self.assertFalse(proactive.can_send(True, "123", 0, observation_generation=1))


if __name__ == "__main__":
    unittest.main()
