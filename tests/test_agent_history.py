"""工具历史修复离线回归：不重放、不猜 ID、不修改原消息。"""
import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot.rounds import fix_messages, keep_recent_rounds


def call(call_id, arguments=None):
    return {"id": call_id, "type": "function", "function": {
        "name": "memory", "arguments": "{}" if arguments is None else arguments}}


def assistant(*calls, content=None):
    return {"role": "assistant", "content": content, "tool_calls": list(calls)}


def result(call_id, content="ok"):
    return {"role": "tool", "tool_call_id": call_id, "content": content}


class HistoryTests(unittest.TestCase):
    def assert_legal(self, messages):
        seen = set()
        index = 0
        while index < len(messages):
            message = messages[index]
            self.assertNotEqual(message.get("role"), "tool", "孤立 tool_result")
            if message.get("role") != "assistant" or not message.get("tool_calls"):
                index += 1
                continue
            calls = message["tool_calls"]
            expected = {item["id"] for item in calls}
            self.assertEqual(len(expected), len(calls))
            self.assertFalse(expected & seen)
            seen.update(expected)
            for item in calls:
                self.assertIsInstance(item["id"], str)
                self.assertTrue(item["id"].strip())
                self.assertEqual(item["type"], "function")
                self.assertIsInstance(item["function"]["arguments"], str)
            index += 1
            actual = []
            while index < len(messages) and messages[index].get("role") == "tool":
                actual.append(messages[index]["tool_call_id"])
                index += 1
            self.assertEqual(set(actual), expected)
            self.assertEqual(len(actual), len(expected))

    def test_complete_batch_exact_ids_and_result_order(self):
        messages = [{"role": "user", "content": "test"}, assistant(call("first"), call("second")),
                    result("second", "2"), result("first", "1"), {"role": "assistant", "content": "done"}]
        self.assertEqual(fix_messages(messages), messages)
        self.assert_legal(fix_messages(messages))

    def test_duplicate_results_keep_first_only(self):
        messages = [assistant(call("a")), result("a", "first"), result("a", "duplicate")]
        fixed = fix_messages(messages)
        self.assertEqual(fixed, messages[:2])
        self.assert_legal(fixed)

    def test_orphans_are_not_paired_by_position_name_or_prefix(self):
        messages = [result("orphan"), assistant(call("call_full_suffix"), content="普通说明"),
                    result("call_full"), {"role": "assistant", "content": "next"}, result("call_full_suffix")]
        fixed = fix_messages(messages)
        self.assertEqual(fixed, [{"role": "assistant", "content": "普通说明"}, {"role": "assistant", "content": "next"}])
        self.assert_legal(fixed)
        # 完整 ID 即使前 40 字一样也不可被截断或合并。
        left, right = "x" * 80 + "left", "x" * 80 + "right"
        fixed = fix_messages([assistant(call(left), call(right)), result(right), result(left)])
        self.assertEqual(fixed[0]["tool_calls"][0]["id"], left)
        self.assert_legal(fixed)

    def test_incomplete_group_strips_all_tool_protocol_but_keeps_body(self):
        messages = [assistant(call("a"), call("b"), content="已开始检查，但工具未确认"), result("a"),
                    {"role": "user", "content": "stop"}, result("b")]
        fixed = fix_messages(messages)
        self.assertEqual(fixed, [{"role": "assistant", "content": "已开始检查，但工具未确认"}, messages[2]])
        self.assert_legal(fixed)
        self.assertEqual(fix_messages([assistant(call("a")), result("other")]), [])

    def test_duplicate_ids_within_batch_make_entire_group_invalid(self):
        messages = [assistant(call("same"), call("same"), content="保留正文"), result("same")]
        self.assertEqual(fix_messages(messages), [{"role": "assistant", "content": "保留正文"}])
        self.assert_legal(fix_messages(messages))

    def test_reused_ids_across_groups_are_ambiguous(self):
        messages = [assistant(call("same"), content="first"), result("same"),
                    {"role": "user", "content": "next"},
                    assistant(call("same"), content="second"), result("same")]
        fixed = fix_messages(messages)
        self.assertEqual(fixed, [{"role": "assistant", "content": "first"}, messages[2],
                                 {"role": "assistant", "content": "second"}])
        self.assert_legal(fixed)

    def test_bad_call_shape_keeps_only_assistant_body(self):
        invalid = [call(""), call("   "), call(7), {"type": "function", "function": {"name": "memory", "arguments": "{}"}},
                   {"id": "a", "type": "function", "function": {}},
                   {"id": "a", "type": "other", "function": {"name": "memory", "arguments": "{}"}},
                   {"id": "a", "type": "function", "function": {"name": "memory", "arguments": []}},
                   {"id": "a", "type": "function", "function": {"name": "", "arguments": "{}"}}, "bad"]
        for item in invalid:
            with self.subTest(item=item):
                self.assertEqual(fix_messages([assistant(item, content="正文"), result("a")]),
                                 [{"role": "assistant", "content": "正文"}])
        self.assertEqual(fix_messages([{"role": "assistant", "content": "正文", "tool_calls": "bad"}]),
                         [{"role": "assistant", "content": "正文"}])

    def test_dict_arguments_are_serialized_on_copy(self):
        messages = [assistant(call("a", {"text": "中文", "nested": {"items": [1, None]}})), result("a")]
        original = copy.deepcopy(messages)
        fixed = fix_messages(messages)
        arguments = fixed[0]["tool_calls"][0]["function"]["arguments"]
        self.assertEqual(json.loads(arguments), original[0]["tool_calls"][0]["function"]["arguments"])
        self.assertEqual(messages, original)
        self.assertIsNot(fixed[0], messages[0])
        self.assertIsNot(fixed[0]["tool_calls"], messages[0]["tool_calls"])
        self.assertIsNot(fixed[1], messages[1])
        self.assert_legal(fixed)

    def test_nonserializable_dict_args_never_leave_tool_group(self):
        for args in ({"bad": float("nan")}, {"bad": {1, 2}}, {"bad": object()}):
            with self.subTest(args=args):
                self.assertEqual(fix_messages([assistant(call("a", args), content="普通正文"), result("a")]),
                                 [{"role": "assistant", "content": "普通正文"}])

    def test_kept_and_invalid_body_messages_are_deep_copies(self):
        messages = [{"role": "user", "content": [{"type": "text", "text": "原始"}]},
                    assistant(call("missing"), content=[{"type": "text", "text": "助手正文"}])]
        snapshot = copy.deepcopy(messages)
        fixed = fix_messages(messages)
        self.assertEqual(messages, snapshot)
        fixed[0]["content"][0]["text"] = "changed"
        fixed[1]["content"][0]["text"] = "changed"
        self.assertEqual(messages, snapshot)
        self.assert_legal(fixed)

    def test_intervening_assistant_does_not_receive_previous_result(self):
        messages = [assistant(call("a"), content="first"), assistant(call("b"), content="second"),
                    result("a"), result("b")]
        fixed = fix_messages(messages)
        self.assertEqual(fixed, [{"role": "assistant", "content": "first"}, messages[1], result("b")])
        self.assert_legal(fixed)

    def test_user_boundary_does_not_guess_late_pair(self):
        messages = [assistant(call("a"), content="first"), {"role": "user", "content": "next"}, result("a")]
        fixed = fix_messages(messages)
        self.assertEqual(fixed, [{"role": "assistant", "content": "first"}, messages[1]])
        self.assert_legal(fixed)

    def test_unknown_results_removed_without_losing_complete_batch(self):
        messages = [assistant(call("a")), result("unknown"), result("a"), result("unknown")]
        fixed = fix_messages(messages)
        self.assertEqual(fixed, [messages[0], messages[2]])
        self.assert_legal(fixed)

    def test_empty_tool_calls_and_plain_history_are_copied(self):
        messages = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello", "tool_calls": []}]
        fixed = fix_messages(messages)
        self.assertEqual(fixed, [messages[0], {"role": "assistant", "content": "hello"}])
        self.assertIn("tool_calls", messages[1])
        self.assert_legal(fixed)
        self.assertEqual(fix_messages([None, "bad", result(None)]), [])

    def test_keep_recent_rounds_preserves_summary_and_valid_chain(self):
        summary = {"role": "assistant", "content": "[历史摘要，压缩了 10 轮] earlier"}
        messages = [summary, {"role": "user", "content": "old"}, assistant(call("old")), result("old"),
                    {"role": "user", "content": "new"}, assistant(call("new"), content="checking"), result("new")]
        original = copy.deepcopy(messages)
        fixed = keep_recent_rounds(messages, 2)
        self.assertEqual(fixed, [summary] + messages[4:])
        self.assert_legal(fixed)
        self.assertEqual(messages, original)

    def test_fix_is_idempotent(self):
        messages = [result("orphan"), assistant(call("a", {"x": 1})), result("a"), result("a"),
                    assistant(call("missing"), content="body")]
        fixed = fix_messages(messages)
        self.assertEqual(fix_messages(fixed), fixed)
        self.assert_legal(fixed)


if __name__ == "__main__":
    unittest.main()
