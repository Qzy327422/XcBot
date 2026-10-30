"""消息运行时离线测试：不使用传输层、外部服务或真实配置。"""

import asyncio
import inspect
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

from bot.message_runtime import (
    CURRENT_REQUEST, INDEX, MessageIndex, ReplyDecision, ReplySuppressed,
    RequestState, bind_request, current_request, session_key,
)


class MessageIndexTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.index = MessageIndex(clock=lambda: self.now)
        self.key = ("10", True, "20")

    def record(self, mid=1, author="30", **kwargs):
        return self.index.record(*self.key, mid, author, **kwargs)

    def resolve(self, mid=1):
        return self.index.resolve(*self.key, mid)

    def test_api_defaults_and_session_key(self):
        self.assertEqual(session_key(10, True, 20), self.key)
        self.assertIsInstance(INDEX, MessageIndex)
        self.assertEqual(self.index.max_sessions, 1000)
        self.assertEqual(self.index.max_messages, 100)
        self.assertEqual(self.index.max_bytes, 64 * 1024 * 1024)
        signature = inspect.signature(MessageIndex.record)
        self.assertEqual(signature.parameters["text"].kind, inspect.Parameter.KEYWORD_ONLY)

    def test_account_group_private_and_chat_isolation(self):
        keys = [("10", True, "20"), ("11", True, "20"),
                ("10", False, "20"), ("10", True, "21")]
        for n, key in enumerate(keys):
            self.index.record(*key, -9, str(n), text=f"session {n}")
        for n, key in enumerate(keys):
            self.assertEqual(self.index.resolve(*key, "-9")["text"], f"session {n}")
            self.assertEqual(len(self.index.snapshot(*key)), 1)
        self.index.clear(*keys[0])
        self.assertIsNone(self.index.resolve(*keys[0], -9))
        for key in keys[1:]:
            self.assertIsNotNone(self.index.resolve(*key, -9))
        self.assertIsNone(self.index.resolve("99", True, "20", -9))
        self.assertFalse(self.index.tombstone("99", True, "20", -9))

    def test_signed_ids_are_canonical_and_duplicates_are_deduplicated(self):
        for raw, canonical in [(1, "1"), ("001", "1"), (-2, "-2"), ("-0002", "-2"),
                               (0, "0"), ("-0", "0")]:
            with self.subTest(raw=raw):
                result = self.record(raw)
                self.assertEqual(result["message_id"], canonical)
                self.assertEqual(self.resolve(raw)["message_id"], canonical)
        self.assertEqual([r["message_id"] for r in self.index.snapshot(*self.key)],
                         ["1", "-2", "0"])
        self.record(4, reply_to="-0002")
        self.assertEqual(self.resolve(4)["reply_to"], "-2")

    def test_invalid_and_missing_ids_never_create_fake_records(self):
        invalid = [None, "", True, False, 1.0, -1.5, "uuid-123", "record_1", " 1", "1 ",
                   "+1", "1.0", "１２", "--1", "[CQ:reply,id=1]", {}, [], b"1"]
        for mid in invalid:
            with self.subTest(mid=mid):
                self.assertIsNone(self.record(mid))
                self.assertIsNone(self.resolve(mid))
                self.assertFalse(self.index.tombstone(*self.key, mid))
                self.assertEqual(self.index.can_recall(*self.key, mid)[1], "invalid_message_id")
        self.assertEqual(self.index.snapshot(*self.key), [])
        self.assertEqual(self.index.total_bytes, 0)
        self.record(1, reply_to="fake-id")
        self.assertIsNone(self.resolve()["reply_to"])

    def test_observation_order_pagination_and_duplicate_order(self):
        for mid in (90, -5, 20, 3):
            self.record(mid, text=str(mid), timestamp=100 - mid)
        self.record(-5, text="enriched")
        self.assertEqual([r["message_id"] for r in self.index.snapshot(*self.key, limit=3)],
                         ["-5", "20", "3"])
        self.assertEqual([r["message_id"] for r in self.index.snapshot(*self.key, before_id=20)],
                         ["90", "-5"])
        self.assertEqual(self.index.snapshot(*self.key, before_id=90), [])
        self.assertEqual(self.index.snapshot(*self.key, before_id=999), [])
        self.assertEqual(self.index.snapshot(*self.key, before_id="fake"), [])
        self.assertEqual(self.index.snapshot(*self.key, limit=1)[0]["message_id"], "3")
        for limit in (0, -1, True, "3", None):
            self.assertEqual(self.index.snapshot(*self.key, limit=limit), [])

    def test_all_outputs_are_detached_including_media(self):
        media = [{"type": "image", "url": "https://example.invalid/a", "summary": "picture"}]
        first = self.record(text="original", media=media)
        media[0]["summary"] = "caller mutated"
        first["text"] = "result mutated"
        first["media"][0]["summary"] = "result nested mutated"
        resolved = self.resolve()
        self.assertEqual(resolved["text"], "original")
        self.assertEqual(resolved["media"][0]["summary"], "picture")
        resolved["media"].clear()
        snapshot = self.index.snapshot(*self.key)
        snapshot[0]["media"][0]["url"] = "mutated"
        self.assertEqual(self.resolve()["media"][0]["url"], "https://example.invalid/a")
        self.assertEqual(self.resolve()["media"][0]["summary"], "picture")

    def test_text_nickname_and_media_are_bounded_and_private(self):
        huge = "x" * 1000000
        media = [
            {"type": "image", "id": -8, "url": "https://example.invalid/image", "summary": huge,
             "binary": b"secret", "data": huge, "base64": huge, "token": "secret"},
            {"type": huge, "url": "data:image/png;base64," + huge},
            {"id": "secret-id", "url": "file:///private/token.png"},
            {"type": b"binary", "id": "base64:" + huge, "summary": {"secret": huge}},
            {"type": "image", "url": "https://example.invalid/" + huge},
            {"type": "must-not-survive", "url": "https://example.invalid/sixth"},
        ]
        result = self.record(text="图" * 10000, nickname=huge, media=media)
        self.assertEqual(len(result["text"]), 256)
        self.assertEqual(len(result["nickname"]), 128)
        self.assertLessEqual(len(result["media"]), 5)
        self.assertEqual(result["media"][0]["id"], "-8")
        self.assertEqual(len(result["media"][0]["summary"]), 256)
        self.assertEqual(len(result["media"][1]["type"]), 64)
        for item in result["media"]:
            self.assertLessEqual(set(item), {"url", "id", "summary", "type"})
            self.assertTrue(all(isinstance(value, str) for value in item.values()))
            self.assertFalse(any("secret" in key for key in item))
        self.assertNotIn("must-not-survive", repr(result))
        self.assertNotIn("data:image", repr(result))
        self.assertNotIn("base64:", repr(result))
        self.assertNotIn("file:///", repr(result))
        self.assertLess(self.index.total_bytes, 20000)

    def test_malformed_media_and_binary_text_are_not_stringified(self):
        for media in (b"secret", "data:image/png;base64,secret", object(), 123,
                      [None, b"secret", "data:text/plain,secret"]):
            self.assertEqual(self.record(media=media, text=b"secret")["media"], [])
            self.assertEqual(self.resolve()["text"], "")
        metadata = self.record(media={"type": "image", "url": "https://[invalid", "id": True})
        self.assertEqual(metadata["media"], [{"type": "image"}])

    def test_platform_timestamp_or_self_echo_cannot_establish_trust(self):
        self.record(author="10", is_bot=True, timestamp=self.now)
        self.assertIsNone(self.resolve()["sent_at"])
        self.assertFalse(self.resolve()["known"])
        self.assertEqual(self.index.can_recall(*self.key, 1)[1], "unknown_sent_at")
        self.record(author="10", is_bot=True, timestamp=self.now + 100)
        self.assertIsNone(self.resolve()["sent_at"])
        # 已验证的出站成功回执可能晚于自身回流到达。
        self.record(author="10", is_bot=True, sent_at=self.now - 10)
        self.assertTrue(self.resolve()["known"])
        self.assertIsNone(self.index.can_recall(*self.key, 1)[1])

    def test_duplicate_echo_and_later_outbound_time_never_extend_window(self):
        self.record(author="10", is_bot=True, sent_at=800, timestamp=900, text="outbound")
        self.record(author="10", timestamp=1000)
        self.record(author="10", is_bot=True, timestamp=999999)
        self.record(author="10", is_bot=True, sent_at=999)
        record = self.resolve()
        self.assertEqual(record["sent_at"], 800)
        self.assertEqual(record["text"], "outbound")
        self.assertTrue(record["is_bot"])
        self.assertEqual(self.index.can_recall(*self.key, 1)[1], "outside_recall_window")
        self.record(author="10", is_bot=True, sent_at=790)
        self.assertEqual(self.resolve()["sent_at"], 790)

    def test_own_author_and_explicit_bot_flag_are_both_required(self):
        for mid, author, flag in [(1, "30", True), (2, "10", False), (3, "10", 1)]:
            self.record(mid, author=author, is_bot=flag, sent_at=self.now)
            record = self.resolve(mid)
            self.assertFalse(record["is_bot"])
            self.assertFalse(record["known"])
            self.assertIsNone(record["sent_at"])
            self.assertEqual(self.index.can_recall(*self.key, mid)[1], "not_bot_message")
        self.record(4, author=10, is_bot=True, sent_at=self.now)
        self.assertIsNone(self.index.can_recall(*self.key, 4)[1])

    def test_missing_identity_never_establishes_recall_authority(self):
        for own, author in (("", ""), (None, None), ("None", "None"), (" ", " "), ("10", None)):
            with self.subTest(own=own, author=author):
                index = MessageIndex(clock=lambda: 100)
                row = index.record(own, True, "20", "1", author, is_bot=True, sent_at=99)
                self.assertFalse(row["known"])
                self.assertFalse(row["is_bot"])
                self.assertIsNotNone(index.can_recall(own, True, "20", "1")[1])

    def test_duplicate_cannot_reassign_author_or_gain_own_privileges(self):
        self.record(author="30", text="someone else")
        self.record(author="10", is_bot=True, sent_at=self.now)
        self.assertEqual(self.resolve()["author_id"], "30")
        self.assertEqual(self.index.can_recall(*self.key, 1)[1], "not_bot_message")
        self.record(2, author="10", is_bot=True, sent_at=self.now)
        self.record(2, author="30", timestamp=self.now + 1)
        self.assertEqual(self.resolve(2)["author_id"], "10")
        self.assertEqual(self.resolve(2)["sent_at"], self.now)

    def test_invalid_times_never_authorize_recall(self):
        for n, value in enumerate((True, "1000", float("nan"), float("inf"), 10 ** 1000), 1):
            self.record(n, author="10", is_bot=True, sent_at=value, timestamp=value)
            self.assertIsNone(self.resolve(n)["sent_at"])
            self.assertIsNone(self.resolve(n)["timestamp"])
            self.assertEqual(self.index.can_recall(*self.key, n)[1], "unknown_sent_at")
        self.record(99, author="10", is_bot=True, sent_at=self.now)
        for now in (True, "1000", float("nan"), float("inf")):
            self.assertEqual(self.index.can_recall(*self.key, 99, now=now)[1], "invalid_now")

    def test_recall_clock_boundaries_and_detached_result(self):
        self.record(author="10", is_bot=True, sent_at=1000, media=[{"type": "image"}])
        for age in (0, 119, 120):
            with self.subTest(age=age):
                self.now = 1000 + age
                record, error = self.index.can_recall(*self.key, 1)
                self.assertIsNone(error)
                self.assertEqual(record["sent_at"], 1000)
                record["media"].clear()
                self.assertEqual(self.resolve()["media"], [{"type": "image"}])
        for now in (999, 1120.00001, 1121):
            self.assertEqual(self.index.can_recall(*self.key, 1, now=now),
                             (None, "outside_recall_window"))
        self.assertEqual(self.index.can_recall("10", False, "20", 1)[1], "message_not_found")
        self.assertEqual(self.index.can_recall("11", True, "20", 1)[1], "message_not_found")
        self.assertEqual(self.index.can_recall("10", True, "21", 1)[1], "message_not_found")

    def test_tombstones_resolve_and_remain_sticky_on_echo(self):
        self.record(author="10", is_bot=True, sent_at=self.now)
        self.assertFalse(self.index.tombstone(*self.key, 2))
        self.assertTrue(self.index.tombstone(*self.key, 1))
        self.assertTrue(self.index.tombstone(*self.key, 1))
        self.assertTrue(self.resolve()["recalled"])
        self.record(author="10", is_bot=True, sent_at=self.now + 1)
        self.assertTrue(self.resolve()["recalled"])
        self.assertTrue(self.index.snapshot(*self.key)[0]["recalled"])
        self.assertEqual(self.index.can_recall(*self.key, 1)[1], "already_recalled")

    def test_default_message_limit_and_replacement_byte_accounting(self):
        for mid in range(101):
            self.record(mid)
        records = self.index.snapshot(*self.key, limit=1000)
        self.assertEqual(len(records), 100)
        self.assertEqual(records[0]["message_id"], "1")
        self.assertIsNone(self.resolve(0))
        baseline = self.index.total_bytes
        self.record(100, text="x" * 256)
        self.assertGreater(self.index.total_bytes, baseline)
        self.record(100, text="x")
        smaller = self.index.total_bytes
        self.assertLess(smaller, baseline + 256)
        for _ in range(50):
            self.record(100, text="x")
        self.assertEqual(self.index.total_bytes, smaller)
        self.index.clear(*self.key)
        self.assertEqual(self.index.total_bytes, 0)
        self.assertEqual(self.index.session_count, 0)

    def test_clear_all_removes_every_account_session_and_recall_candidate(self):
        """全量清理按消息计数，覆盖不同账号、群私及已撤回记录。"""
        keys = [("10", True, "20"), ("11", True, "20"),
                ("10", False, "20"), ("10", True, "21")]
        for key in keys:
            self.index.record(*key, 1, key[0], is_bot=True, sent_at=self.now)
            self.index.record(*key, -2, "30", text="可见消息")
            self.index.record(*key, -2, "30", text="同 ID 补充内容")
            self.assertTrue(self.index.tombstone(*key, -2))
            self.assertIsNone(self.index.can_recall(*key, 1)[1])
        self.assertEqual(self.index.session_count, 4)
        self.assertGreater(self.index.total_bytes, 0)

        self.assertEqual(self.index.clear_all(), 8)
        self.assertEqual(self.index.session_count, 0)
        self.assertEqual(self.index.total_bytes, 0)
        for key in keys:
            with self.subTest(key=key):
                self.assertEqual(self.index.snapshot(*key), [])
                self.assertIsNone(self.index.resolve(*key, 1))
                self.assertIsNone(self.index.resolve(*key, -2))
                self.assertFalse(self.index.tombstone(*key, 1))
                self.assertEqual(self.index.can_recall(*key, 1), (None, "message_not_found"))
        self.assertEqual(self.index.clear_all(), 0)
        self.assertEqual(self.index.total_bytes, 0)
        self.assertEqual(self.index.session_count, 0)

    def test_clear_all_empty_index_and_new_records_after_clear(self):
        """空索引可重复清理，清理后新记录正常入账且不复活旧候选。"""
        self.assertEqual(self.index.clear_all(), 0)
        self.record(1, author="10", is_bot=True, sent_at=self.now)
        before = self.index.total_bytes
        self.assertEqual(self.index.clear_all(), 1)
        self.assertIsNone(self.record(2, author="10", is_bot=True, timestamp=self.now)["sent_at"])
        self.assertEqual(self.index.can_recall(*self.key, 2)[1], "unknown_sent_at")
        self.assertEqual(self.index.can_recall(*self.key, 1)[1], "message_not_found")
        self.assertGreater(self.index.total_bytes, 0)
        self.assertLessEqual(self.index.total_bytes, before)
        self.assertEqual(self.index.session_count, 1)
        self.assertEqual(self.index.clear_all(), 1)
        self.assertEqual(self.index.total_bytes, 0)

    def test_session_lru_reads_touch_but_missing_reads_do_not_create(self):
        self.index = MessageIndex(max_sessions=2)
        self.record()
        self.index.record("10", True, "21", 1, "30")
        self.assertIsNotNone(self.resolve())  # 访问会话 20 后，会话 21 成为最久未使用项。
        self.index.record("10", True, "22", 1, "30")
        self.assertIsNone(self.index.resolve("10", True, "21", 1))
        self.assertIsNotNone(self.resolve())
        self.assertEqual(self.index.snapshot("missing", False, "missing"), [])
        self.assertEqual(self.index.session_count, 2)
        self.index.snapshot("10", True, "22")
        self.index.record("10", True, "23", 1, "30")
        self.assertIsNone(self.resolve())
        self.assertIsNotNone(self.index.resolve("10", True, "22", 1))

    def test_default_session_limit(self):
        for chat in range(1001):
            self.index.record("10", True, chat, 1, "30")
        self.assertEqual(self.index.session_count, 1000)
        self.assertIsNone(self.index.resolve("10", True, 0, 1))
        self.assertIsNotNone(self.index.resolve("10", True, 1000, 1))

    def test_global_byte_budget_evicts_old_sessions_and_oversized_rejects(self):
        probe = MessageIndex()
        probe.record("10", True, "20", 1, "30", text="图" * 256)
        single = probe.total_bytes
        self.index = MessageIndex(max_bytes=single * 2)
        for chat in (20, 21, 22):
            self.index.record("10", True, chat, 1, "30", text="图" * 256)
            self.assertLessEqual(self.index.total_bytes, self.index.max_bytes)
        self.assertEqual(self.index.session_count, 2)
        self.assertIsNone(self.resolve())
        self.assertIsNotNone(self.index.resolve("10", True, 21, 1))
        before = self.index.total_bytes
        self.assertIsNone(self.index.record("10", True, 99, 1, "x" * (single * 3)))
        self.assertEqual(self.index.total_bytes, before)
        for chat in (21, 22):
            self.index.clear("10", True, chat)
        self.assertEqual(self.index.total_bytes, 0)

    def test_byte_budget_trims_single_session_and_tombstone_is_accounted(self):
        probe = MessageIndex()
        probe.record(*self.key, 1, "30", text="x" * 256)
        self.index = MessageIndex(max_bytes=probe.total_bytes + 32)
        for mid in range(20):
            self.assertIsNotNone(self.record(mid, text="x" * 256))
            self.assertLessEqual(self.index.total_bytes, self.index.max_bytes)
            self.assertEqual(len(self.index.snapshot(*self.key)), 1)
        before = self.index.total_bytes
        self.assertTrue(self.index.tombstone(*self.key, 19))
        self.assertEqual(self.index.total_bytes, before)
        self.index.clear(*self.key)
        self.assertEqual(self.index.total_bytes, 0)

    def test_enriching_old_message_under_budget_keeps_the_updated_record(self):
        probe = MessageIndex()
        probe.record(*self.key, 1, "30")
        probe.record(*self.key, 2, "30")
        self.index = MessageIndex(max_bytes=probe.total_bytes + 32)
        self.record(1)
        self.record(2)
        result = self.record(1, text="图" * 256)
        self.assertIsNotNone(result)
        self.assertEqual(self.resolve(1)["text"], "图" * 256)
        self.assertIsNone(self.resolve(2))
        self.assertLessEqual(self.index.total_bytes, self.index.max_bytes)

    def test_duplicate_platform_zero_time_is_not_treated_as_missing(self):
        self.record(timestamp=50)
        self.record(timestamp=0)
        self.assertEqual(self.resolve()["timestamp"], 0)
        self.record()
        self.assertEqual(self.resolve()["timestamp"], 0)

    def test_zero_limits_disable_storage_and_invalid_limits_reject(self):
        for name in ("max_sessions", "max_messages", "max_bytes"):
            with self.subTest(name=name):
                index = MessageIndex(**{name: 0})
                self.assertIsNone(index.record(*self.key, 1, "30"))
                self.assertEqual(index.total_bytes, 0)
                for invalid in (-1, True, 1.5, "100"):
                    with self.assertRaises(ValueError):
                        MessageIndex(**{name: invalid})

    def test_interleaved_echo_and_receipt_preserve_original_send_start(self):
        barrier = threading.Barrier(2)
        echo_done = threading.Event()

        def receipt():
            barrier.wait(timeout=3)
            self.assertTrue(echo_done.wait(timeout=3))
            self.record(author="10", is_bot=True, sent_at=800)

        def echo():
            barrier.wait(timeout=3)
            self.record(author="10", is_bot=True, timestamp=999)
            echo_done.set()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(receipt), pool.submit(echo)]
            for future in futures:
                future.result(timeout=5)
        self.assertEqual(self.resolve()["sent_at"], 800)
        self.assertEqual(self.index.can_recall(*self.key, 1)[1], "outside_recall_window")

    def test_concurrent_record_resolve_snapshot_clear_and_bounds(self):
        self.index = MessageIndex(max_sessions=4, max_messages=10, max_bytes=40000)
        barrier = threading.Barrier(4)

        def worker(worker_id):
            barrier.wait(timeout=5)
            for i in range(60):
                key = ("10", bool(worker_id % 2), str(i % 7))
                mid = worker_id * 1000 + i
                self.index.record(*key, mid, "10", is_bot=True, sent_at=800, text="图" * 1000)
                result = self.index.resolve(*key, mid)
                if result is not None:
                    self.assertEqual(result["message_id"], str(mid))
                    result["text"] = "detached"
                self.index.snapshot(*key)
                self.index.tombstone(*key, mid)
                self.index.can_recall(*key, mid, now=900)
                if i % 3 == 0:
                    self.index.clear(*key)

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(worker, n) for n in range(4)]
            for future in futures:
                future.result(timeout=10)
        self.assertLessEqual(self.index.session_count, 4)
        self.assertLessEqual(self.index.total_bytes, 40000)
        with self.index._lock:
            self.assertEqual(self.index.total_bytes, sum(s.size for s in self.index._sessions.values()))
            self.assertTrue(all(len(s.messages) <= 10 for s in self.index._sessions.values()))
        for group in (False, True):
            for chat in range(7):
                self.index.clear("10", group, chat)
        self.assertEqual(self.index.total_bytes, 0)


class ReplyDecisionTests(unittest.TestCase):
    def decision(self, **kwargs):
        return ReplyDecision("10", True, "20", actor_id="30", **kwargs)

    def test_disabled_outside_weak_and_self_do_not_consume_random(self):
        random_fn = Mock(side_effect=AssertionError("此处不应抽取随机数"))
        decision = self.decision(random_fn=random_fn)
        self.assertTrue(decision.check(False, ["30"], 0))
        self.assertTrue(decision.check(True, ["99"], 0))
        self.assertTrue(decision.check(False, [], float("nan")))
        self.assertTrue(decision.check(True, [], float("nan")))
        self.assertIsNone(decision.sample)
        for group in (False, True):
            own = ReplyDecision("10", group, "20", actor_id="10", random_fn=random_fn)
            self.assertTrue(own.check(True, ["10"], 0))
        random_fn.assert_not_called()

    def test_probability_zero_and_one_are_exact_without_random(self):
        random_fn = Mock(side_effect=AssertionError("概率为 0 或 1 时无需抽样"))
        zero = self.decision(sample=0, random_fn=random_fn)
        self.assertFalse(zero.check(True, [30], 0))
        one = self.decision(random_fn=random_fn)
        self.assertTrue(one.check(True, ["30"], 1))
        self.assertIsNone(one.sample)
        random_fn.assert_not_called()

    def test_lazy_sample_is_drawn_only_once_across_configuration_rechecks(self):
        random_fn = Mock(return_value=0.4)
        decision = self.decision(random_fn=random_fn)
        for _ in range(10):
            self.assertTrue(decision.check(False, ["30"], 0))
            self.assertTrue(decision.check(True, [], 0))
        random_fn.assert_not_called()
        for probability in (0.8, 0.6, 0.9, 0.5):
            self.assertTrue(decision.check(True, ["30"], probability))
        self.assertEqual(decision.sample, 0.4)
        self.assertFalse(decision.check(True, ["30"], 0.4))
        self.assertTrue(decision.denied)
        self.assertFalse(decision.check(True, ["30"], 1))
        random_fn.assert_called_once_with()

    def test_supplied_sample_boundaries_and_invalid_samples(self):
        for sample, probability, expected in [(0, 0.001, True), (0.5, 0.5, False),
                                               (0.4999, 0.5, True), (0.999999, 1, True)]:
            random_fn = Mock(side_effect=AssertionError("已有样本时不得重新抽样"))
            decision = self.decision(sample=sample, random_fn=random_fn)
            self.assertEqual(decision.check(True, ["30"], probability), expected)
            random_fn.assert_not_called()
        for sample in (-0.1, 1, True, "0.5", float("inf"), float("nan")):
            with self.subTest(sample=sample), self.assertRaises(ValueError):
                self.decision(sample=sample)

    def test_denial_sticky_after_actor_switch_disable_or_list_removal(self):
        decision = self.decision(sample=0.9)
        self.assertFalse(decision.check(True, ["30"], 0.5))
        for enabled, users, probability, actor in [
            (False, [], 1, None), (True, [], 1, None), (True, ["30"], 1, "99"),
            (False, [], 1, "10"), (True, [], 1, ""),
        ]:
            self.assertFalse(decision.check(enabled, users, probability, actor_id=actor))
        self.assertFalse(hasattr(decision, "clear"))

    def test_normal_actor_is_fixed_even_when_switch_is_disabled(self):
        decision = self.decision(sample=0.1)
        self.assertTrue(decision.actor_frozen)
        for actor in ("99", "10", ""):
            self.assertFalse(decision.check(False, [], 1, actor_id=actor, freeze_actor=True))
            self.assertEqual(decision.actor_id, "30")
        self.assertTrue(decision.check(True, ["30"], 0.2))
        self.assertTrue(decision.check(True, ["30"], 0.2, actor_id=30))

    def test_proactive_group_starts_without_actor_bot_reference_does_not_bind(self):
        random_fn = Mock(side_effect=AssertionError("群主动请求未绑定主体时不得触发弱名单抽样"))
        decision = ReplyDecision("10", True, "20", source="proactive", random_fn=random_fn)
        self.assertTrue(decision.check(True, ["20", "10"], 0))
        self.assertTrue(decision.check(True, ["10"], 0, actor_id="10", freeze_actor=True))
        self.assertFalse(decision.actor_frozen)
        self.assertEqual(decision.actor_id, "")
        self.assertFalse(decision.check(True, [], 1, actor_id="30", freeze_actor=False))
        self.assertFalse(decision.actor_frozen)
        self.assertTrue(decision.check(True, ["30"], 1, actor_id="30", freeze_actor=True))
        self.assertTrue(decision.actor_frozen)
        random_fn.assert_not_called()

    def test_proactive_group_successful_freeze_cannot_switch_or_self_exempt(self):
        decision = ReplyDecision("10", True, "20", source="proactive", sample=0.4)
        self.assertTrue(decision.check(True, [], 0, actor_id="30", freeze_actor=True))
        self.assertEqual(decision.actor_id, "30")
        self.assertFalse(decision.check(True, [], 1, actor_id="99", freeze_actor=True))
        self.assertFalse(decision.check(False, [], 1, actor_id="99", freeze_actor=True))
        self.assertEqual(decision.actor_id, "30")
        self.assertFalse(decision.check(True, ["30"], 0.2, actor_id="10", freeze_actor=True))
        self.assertTrue(decision.denied)
        self.assertFalse(decision.check(False, [], 1))

    def test_proactive_group_rejection_freezes_and_cannot_drop_reference(self):
        decision = ReplyDecision("10", True, "20", source="proactive", sample=0.8)
        self.assertFalse(decision.check(True, ["30"], 0.5, actor_id="30", freeze_actor=True))
        self.assertTrue(decision.actor_frozen)
        self.assertEqual(decision.actor_id, "30")
        self.assertFalse(decision.check(True, [], 1, actor_id="99", freeze_actor=True))
        self.assertFalse(decision.check(True, [], 1, actor_id="10", freeze_actor=True))
        self.assertFalse(decision.check(True, [], 1, actor_id=""))
        self.assertFalse(decision.check(False, [], 1))

    def test_freeze_while_disabled_still_binds_actor_for_later_recheck(self):
        decision = ReplyDecision("10", True, "20", source="proactive", sample=0.8)
        self.assertTrue(decision.check(False, ["30"], 0, actor_id="30", freeze_actor=True))
        self.assertEqual(decision.actor_id, "30")
        self.assertTrue(decision.actor_frozen)
        self.assertFalse(decision.check(True, ["30"], 0.5))

    def test_proactive_private_uses_target_without_group_id_confusion(self):
        decision = ReplyDecision("10", False, "30", source="proactive", sample=0.8)
        self.assertEqual(decision.actor_id, "30")
        self.assertFalse(decision.check(True, [30], 0.5))
        self.assertFalse(decision.check(False, [], 1, actor_id="99", freeze_actor=True))

    def test_invalid_active_probability_fails_closed_without_sampling(self):
        for probability in (True, "0.5", None, -0.1, 1.1, float("nan"), float("inf")):
            random_fn = Mock(side_effect=AssertionError("策略无效时不得抽样"))
            decision = self.decision(random_fn=random_fn)
            self.assertFalse(decision.check(True, ["30"], probability))
            self.assertFalse(decision.check(False, [], 1))
            random_fn.assert_not_called()

    def test_weak_membership_supports_integer_and_string_ids_without_bool(self):
        for users in ([30], {"30"}, "30", 30, ("30",)):
            self.assertFalse(self.decision().check(True, users, 0))
        decision = ReplyDecision("10", False, "20", actor_id="True")
        self.assertTrue(decision.check(True, [True], 0))

    def test_parallel_checks_take_only_one_random_sample(self):
        barrier = threading.Barrier(8)
        random_fn = Mock(return_value=0.3)
        decision = self.decision(random_fn=random_fn)

        def check():
            barrier.wait(timeout=5)
            return [decision.check(True, ["30"], 0.5) for _ in range(20)]

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(check) for _ in range(8)]
            self.assertTrue(all(all(future.result(timeout=10)) for future in futures))
        random_fn.assert_called_once_with()

    def test_interleaved_actor_freeze_has_exactly_one_winner(self):
        barrier = threading.Barrier(2)
        decision = ReplyDecision("10", True, "20", source="proactive")

        def check(actor):
            barrier.wait(timeout=5)
            return decision.check(True, [], 1, actor_id=actor, freeze_actor=True)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(check, actor) for actor in ("30", "31")]
            results = [future.result(timeout=5) for future in futures]
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(decision.actor_id, "30" if results[0] else "31")
        self.assertTrue(decision.actor_frozen)


class RequestStateTests(unittest.TestCase):
    def state(self, chat="20", **kwargs):
        decision = ReplyDecision("10", True, chat, actor_id="30")
        return RequestState("10", True, chat, decision, **kwargs)

    def test_envelope_api_and_key_validation(self):
        event, actions, social = object(), object(), object()
        state = self.state(event=event, actions=actions, social=social)
        self.assertEqual(state.key, ("10", True, "20"))
        self.assertIs(state.event, event)
        self.assertIs(state.actions, actions)
        self.assertIs(state.social, social)
        self.assertFalse(state.handled)
        self.assertIsNone(state.wait_seconds)
        self.assertFalse(state.finish)
        self.assertIsNone(state.pending_reply)
        with self.assertRaises(ValueError):
            RequestState("99", True, "20", state.decision)

    def test_pending_reply_consumed_only_once_and_records_not_shared(self):
        allowed = {1, "-2"}
        records = {"1": {"media": [{"type": "image"}]}}
        images = ["https://example.invalid/a"]
        first = self.state(allowed_reply_ids=allowed, records=records, image_urls=images)
        second = self.state(allowed_reply_ids=allowed, records=records, image_urls=images)
        first.records["1"]["media"][0]["type"] = "changed"
        first.allowed_reply_ids.clear()
        first.image_urls.clear()
        self.assertEqual(second.records["1"]["media"][0]["type"], "image")
        self.assertEqual(records["1"]["media"][0]["type"], "image")
        self.assertEqual(second.allowed_reply_ids, {"1", "-2"})
        self.assertEqual(second.image_urls, images)
        self.assertFalse(first.select_reply(1))
        self.assertTrue(second.select_reply(-2))
        self.assertEqual(second.pending_reply, "-2")
        self.assertEqual(second.consume_once(), "-2")
        self.assertIsNone(second.consume_once())
        self.assertIsNone(first.consume_once())
        second.pending_reply = 1
        self.assertEqual(second.consume_once(), "1")
        second.pending_reply = None
        with self.assertRaises(ValueError):
            second.pending_reply = True
        self.assertFalse(second.select_reply("fake"))

    def test_default_mutable_fields_and_decisions_are_request_private(self):
        first, second = self.state(), self.state()
        first.allowed_reply_ids.add("1")
        first.image_urls.append("https://example.invalid/a")
        first.records["1"] = {"text": "a"}
        first.decision.check(True, ["30"], 0)
        self.assertEqual(second.allowed_reply_ids, set())
        self.assertEqual(second.image_urls, [])
        self.assertEqual(second.records, {})
        self.assertFalse(second.decision.denied)

    def test_concurrent_consume_once_has_exactly_one_consumer(self):
        state = self.state(reply_id="-1")
        barrier = threading.Barrier(8)

        def consume():
            barrier.wait(timeout=5)
            return state.consume_once()

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(consume) for _ in range(8)]
            results = [future.result(timeout=5) for future in futures]
        self.assertEqual(results.count("-1"), 1)
        self.assertEqual(results.count(None), 7)

    def test_context_binding_nesting_and_exception_restoration(self):
        first, second = self.state(), self.state("21")
        self.assertIs(CURRENT_REQUEST, current_request)
        self.assertIsNone(CURRENT_REQUEST.get())
        with bind_request(first) as bound:
            self.assertIs(bound, first)
            self.assertIs(CURRENT_REQUEST.get(), first)
            with self.assertRaises(ReplySuppressed):
                with bind_request(second):
                    self.assertIs(CURRENT_REQUEST.get(), second)
                    raise ReplySuppressed("weak_denied")
            self.assertIs(CURRENT_REQUEST.get(), first)
            with bind_request(None):
                self.assertIsNone(CURRENT_REQUEST.get())
            self.assertIs(CURRENT_REQUEST.get(), first)
        self.assertIsNone(CURRENT_REQUEST.get())
        suppressed = ReplySuppressed("weak_denied")
        self.assertTrue(suppressed.silent)
        self.assertEqual(suppressed.reason, "weak_denied")

    def test_threads_require_explicit_binding_and_restore_on_exit(self):
        state = self.state(reply_id=1)

        def worker(explicit_state):
            self.assertIsNone(CURRENT_REQUEST.get())
            with bind_request(explicit_state):
                self.assertIs(CURRENT_REQUEST.get(), state)
                result = CURRENT_REQUEST.get().consume_once()
            self.assertIsNone(CURRENT_REQUEST.get())
            return result

        with bind_request(state), ThreadPoolExecutor(max_workers=1) as pool:
            self.assertEqual(pool.submit(worker, state).result(timeout=5), "1")
            self.assertIs(CURRENT_REQUEST.get(), state)
            self.assertIsNone(state.consume_once())
        self.assertIsNone(CURRENT_REQUEST.get())


class AsyncRequestStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_tasks_are_isolated_and_inherit_only_their_request(self):
        first = RequestState("10", True, "20", ReplyDecision("10", True, "20"), reply_id=1)
        second = RequestState("10", False, "20", ReplyDecision("10", False, "20"), reply_id=2)
        ready = [asyncio.Event(), asyncio.Event()]

        async def child(state):
            self.assertIs(CURRENT_REQUEST.get(), state)
            await asyncio.sleep(0)
            self.assertIs(CURRENT_REQUEST.get(), state)

        async def request(state, index):
            self.assertIsNone(CURRENT_REQUEST.get())
            with bind_request(state):
                ready[index].set()
                await ready[1 - index].wait()
                await asyncio.create_task(child(state))
                self.assertIs(CURRENT_REQUEST.get(), state)
                result = state.consume_once()
            self.assertIsNone(CURRENT_REQUEST.get())
            return result

        results = await asyncio.gather(request(first, 0), request(second, 1))
        self.assertEqual(results, ["1", "2"])
        self.assertIsNone(CURRENT_REQUEST.get())

    async def test_cancelled_task_restores_its_binding(self):
        state = RequestState("10", True, "20", ReplyDecision("10", True, "20"))
        entered = asyncio.Event()

        async def request():
            try:
                with bind_request(state):
                    entered.set()
                    await asyncio.Event().wait()
            finally:
                self.assertIsNone(CURRENT_REQUEST.get())

        task = asyncio.create_task(request())
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIsNone(CURRENT_REQUEST.get())


if __name__ == "__main__":
    unittest.main()
