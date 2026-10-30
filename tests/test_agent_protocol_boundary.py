"""离线边界测试：不使用 bot 执行器、网络或子进程。"""
import copy
import dataclasses
import json
import unittest
import uuid
from types import SimpleNamespace
from unittest import mock

from bot.agent_protocol import (
    NormalizedToolCall,
    inspect_tool_markup,
    markup_prefix_pending,
    normalize_tool_calls,
)


def call(arguments="{}", name="safe_tool", call_id="call_1", **extra):
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": arguments}, **extra}


def dsml(body="SECRET_COMMAND", pipes="｜｜", word="calls"):
    return (f"<{pipes}DSML{pipes} {word}>"
            f'<{pipes}DSML{pipes}invoke name="not_executed">'
            f'<{pipes}DSML{pipes}parameter name="command">{body}'
            f'</{pipes}DSML{pipes}parameter>'
            f'</{pipes}DSML{pipes}invoke></{pipes}DSML{pipes}{word}>')


def native(body="SECRET_COMMAND", pipes="｜"):
    return (f"<{pipes}tool▁calls▁begin{pipes}>"
            f"<{pipes}tool▁call▁begin{pipes}>function"
            f"<{pipes}tool▁sep{pipes}>not_executed\n{body}"
            f"<{pipes}tool▁call▁end{pipes}>"
            f"<{pipes}tool▁calls▁end{pipes}>")


class NormalizeToolCallsTests(unittest.TestCase):
    def test_standard_dict_and_sdk_shape(self):
        sdk = SimpleNamespace(
            id="sdk-id", type="function",
            function=SimpleNamespace(name="  unknown_legal-name  ",
                                     arguments='{"value": 1}'),
        )
        source = call({"text": "中文", "nested": {"items": [1, 2]}})
        before = copy.deepcopy(source)
        normalized = normalize_tool_calls([source, sdk])
        first, second = normalized
        self.assertTrue(dataclasses.is_dataclass(first))
        self.assertIsInstance(first, NormalizedToolCall)
        self.assertEqual(first.id, "call_1")
        self.assertEqual(first.type, "function")
        self.assertEqual(first.function.name, "safe_tool")
        self.assertEqual(first.error, "")
        self.assertEqual(json.loads(first.function.arguments), source["function"]["arguments"])
        self.assertIsNot(first.raw_arguments, source["function"]["arguments"])
        first.raw_arguments["nested"]["items"].append(3)
        self.assertEqual(source, before)
        self.assertEqual(second.function.name, "unknown_legal-name")
        self.assertEqual(second.function.arguments, '{"value": 1}')
        self.assertEqual(second.error, "")
        self.assertEqual(sdk.function.name, "  unknown_legal-name  ")
        self.assertEqual(sdk.function.arguments, '{"value": 1}')

    def test_actual_openai_sdk_if_installed(self):
        try:
            from openai.types.chat.chat_completion_message_tool_call import (
                ChatCompletionMessageToolCall,
            )
        except ImportError:
            self.skipTest("optional openai SDK is not installed")
        sdk = ChatCompletionMessageToolCall(
            id="sdk-call", type="function",
            function={"name": "safe_tool", "arguments": '{"x": 1}'},
        )
        before = sdk.model_dump()
        result = normalize_tool_calls([sdk])[0]
        self.assertEqual(result.error, "")
        self.assertEqual(result.id, "sdk-call")
        self.assertEqual(result.function.arguments, '{"x": 1}')
        self.assertEqual(sdk.model_dump(), before)

    def test_empty_and_tuple_batch(self):
        self.assertEqual(normalize_tool_calls([]), [])
        self.assertEqual(normalize_tool_calls(()), [])
        self.assertEqual(len(normalize_tool_calls((call(),))), 1)

    def test_ids_are_unique_and_shared_set_is_updated(self):
        used = {"historic-id"}
        originals = [call(call_id=value) for value in (
            None, "", "   ", 123, [], "x" * 65, "historic-id", "same", "same",
        )]
        originals.append({"type": "function", "function": {"name": "safe_tool"}})
        before = copy.deepcopy(originals)
        results = normalize_tool_calls(originals, used)
        ids = [item.id for item in results]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(used, {"historic-id", *ids})
        self.assertEqual(ids[7], "same")
        for index in (0, 1, 2, 3, 4, 5, 6, 8, 9):
            self.assertTrue(ids[index].startswith("call_"))
            self.assertEqual(uuid.UUID(hex=ids[index][5:]).version, 4)
        self.assertEqual(originals, before)
        next_call = normalize_tool_calls([call(call_id="same")], used)[0]
        self.assertNotIn(next_call.id, ids)
        self.assertIn(next_call.id, used)

    def test_id_boundaries_and_uuid_collision_retry(self):
        for value in ("x", "x" * 64, "provider:id/1"):
            self.assertEqual(normalize_tool_calls([call(call_id=value)])[0].id, value)
        first = uuid.UUID("00000000-0000-4000-8000-000000000001")
        second = uuid.UUID("00000000-0000-4000-8000-000000000002")
        used = {"call_" + first.hex}
        with mock.patch("bot.agent_protocol.uuid.uuid4", side_effect=[first, second]):
            result = normalize_tool_calls([call(call_id=None)], used)[0]
        self.assertEqual(result.id, "call_" + second.hex)

    def test_ids_reject_whitespace_and_control_characters(self):
        invalid = (" with-space", "with-space ", "in ternal", "a\tb", "a\nb", "a\rb",
                   "a\x00b", "a\x1fb", "a\x7fb", "a\x85b", "a\u00a0b", "a\u200bb",
                   "a\u202eb", "a\ud800b")
        for value in invalid:
            with self.subTest(value=ascii(value)):
                result = normalize_tool_calls([call(call_id=value)])[0]
                self.assertNotEqual(result.id, value)
                self.assertEqual(uuid.UUID(hex=result.id[5:]).version, 4)
        for value in ("dash-id_0", "provider:id/1", "provider.id+1", "a" * 64):
            self.assertEqual(normalize_tool_calls([call(call_id=value)])[0].id, value)

    def test_invalid_names_use_non_routable_placeholder(self):
        for name in (None, 123, [], "", " ", "a.b", "a/b", "a b", "中文", "a" * 65):
            with self.subTest(name=name):
                item = normalize_tool_calls([call(name=name)])[0]
                self.assertTrue(item.error)
                self.assertEqual(item.function.name, "__invalid_tool_call__")
        for name in ("a", "_", "-", "a" * 64, "  unknown_name-0  "):
            with self.subTest(name=name):
                item = normalize_tool_calls([call(name=name)])[0]
                self.assertFalse(item.error)
                self.assertEqual(item.function.name, name.strip())

    def test_missing_function_bad_type_and_flattened_fields_fail_closed(self):
        malformed = [
            None, "not a call", [], {}, {"id": "flat", "name": "safe_tool", "arguments": {}},
            {"id": "flat", "type": "function", "name": "safe_tool", "arguments": {}},
            {"type": "function", "function": None},
            {"type": "function", "function": "safe_tool"},
            {"type": "function", "function": {"tool_name": "safe_tool", "args": {}}},
            {"function": {"name": "safe_tool", "arguments": "{}"}},
            call(type="custom"), call(type=None), call(type=123),
        ]
        for raw in malformed:
            with self.subTest(raw=raw):
                item = normalize_tool_calls([raw])[0]
                self.assertTrue(item.error)
                self.assertEqual(item.type, "function")
                self.assertEqual(item.function.name, "__invalid_tool_call__")
                self.assertIsInstance(item.function.arguments, str)

    def test_non_batch_is_one_error_not_iterated(self):
        def not_consumed():
            raise AssertionError("generator should not be consumed")
            yield
        for raw in (None, "abc", dsml(), {"calls": []}, call(), 1, {1}, not_consumed()):
            result = normalize_tool_calls(raw)
            self.assertEqual(len(result), 1)
            self.assertTrue(result[0].error)
            self.assertEqual(result[0].function.name, "__invalid_tool_call__")

    def test_none_missing_and_json_null_stay_distinguishable(self):
        missing = call()
        del missing["function"]["arguments"]
        actual_none, omitted, json_null = normalize_tool_calls([call(None), missing, call("null")])
        for result in (actual_none, omitted):
            self.assertIsNone(result.raw_arguments)
            self.assertEqual(result.function.arguments, "{}")
            self.assertEqual(result.error, "")
        self.assertEqual(json_null.raw_arguments, "null")
        self.assertEqual(json_null.function.arguments, "null")
        self.assertTrue(json_null.error)
        self.assertEqual(json_null.function.name, "safe_tool")

    def test_bad_json_strings_are_deferred_without_rewriting(self):
        for raw in ("", " ", "\ufeff{}", "{broken JSON", '{"path":"C:\\Users\\test"}', '{"x":'):
            with self.subTest(raw=raw):
                result = normalize_tool_calls([call(raw)])[0]
                self.assertEqual(result.function.arguments, raw)
                self.assertEqual(result.raw_arguments, raw)
                self.assertEqual(result.error, "")

    def test_reject_non_object_roots_and_non_finite_numbers(self):
        invalid = [[], [1], (), 1, 1.2, True, object(),
                   "[]", "1", '"text"', "true", "null", "NaN", "Infinity",
                   '{"x": NaN}', '{"x": Infinity}',
                   {"x": float("nan")}, {"x": float("inf")}, {"x": object()}]
        cyclic = {}
        cyclic["self"] = cyclic
        invalid.append(cyclic)
        for raw in invalid:
            with self.subTest(kind=type(raw).__name__):
                result = normalize_tool_calls([call(raw)])[0]
                self.assertTrue(result.error)
                self.assertIsInstance(result.function.arguments, str)
                self.assertLess(len(result.error), 256)

    def test_no_argument_values_in_diagnostics_or_repr(self):
        class Unserializable:
            def __deepcopy__(self, memo):
                raise ValueError("PRIVATE_ARGUMENT_VALUE")
            def __str__(self):
                raise AssertionError("must not stringify an arbitrary object")
            def __repr__(self):
                raise AssertionError("must not repr an arbitrary object")
        secret = "PRIVATE_ARGUMENT_VALUE" * 10000
        for raw in ({"value": Unserializable()}, {"value": secret, "bad": object()}, Unserializable()):
            result = normalize_tool_calls([call(raw, name=secret, type="wrong")])[0]
            self.assertTrue(result.error)
            self.assertLess(len(result.error), 256)
            self.assertNotIn("PRIVATE_ARGUMENT_VALUE", result.error)
            self.assertNotIn("PRIVATE_ARGUMENT_VALUE", repr(result))

    def test_no_text_or_flattened_fallback(self):
        result = normalize_tool_calls([{
            "type": "function", "content": dsml(),
            "function": {"name": "unknown_legal_name", "args": {"command": "NEVER"}},
            "arguments": {"command": "NEVER"},
        }])[0]
        self.assertFalse(result.error)
        self.assertIsNone(result.raw_arguments)
        self.assertEqual(result.function.arguments, "{}")


class InspectToolMarkupTests(unittest.TestCase):
    def assert_removed(self, markup, expected="beforeafter"):
        cleaned, detected = inspect_tool_markup("before" + markup + "after")
        self.assertTrue(detected)
        self.assertEqual(cleaned, expected)
        self.assertNotIn("SECRET_COMMAND", cleaned)

    def test_double_pipe_dsml_and_all_wrapper_spellings(self):
        for pipes in ("｜｜", "||", "｜", "|", "|｜"):
            for word in ("calls", "tool_calls", "function_calls"):
                with self.subTest(pipes=pipes, word=word):
                    self.assert_removed(dsml(pipes=pipes, word=word))

    def test_dsml_spaces_case_and_multiline_payload(self):
        markup = ('< || DSML || calls >\n'
                  '< || DSML || invoke name="safe_tool">\n'
                  '< || DSML || parameter name="command">SECRET_COMMAND\n'
                  'more SECRET_COMMAND</ || DSML || parameter>\n'
                  '</ || DSML || invoke>\n</ || DSML || calls >')
        self.assert_removed(markup)
        self.assert_removed(markup.replace("DSML", "dsml"))

    def test_native_tokens_and_ascii_wrappers(self):
        for pipes in ("｜", "|", "||"):
            self.assert_removed(native(pipes=pipes))
        for opening, closing in (
            ("<|tool_calls|>", "</|tool_calls|>"),
            ("<|tool_calls_begin|>", "<|tool_calls_end|>"),
            ("<|tool_call|>", "</|tool_call|>"),
            ("<|tool_calls|>", "<|/tool_calls|>"),
        ):
            self.assert_removed(opening + "SECRET_COMMAND" + closing)

    def test_complete_outer_removes_everything_not_just_invoke(self):
        markup = dsml(body='{"command": "SECRET_COMMAND", "not_json": <div>SECRET_COMMAND</div>}')
        self.assert_removed(markup)
        self.assert_removed("<||DSML||calls>SECRET_COMMAND</||DSML||calls>")
        self.assert_removed("<|tool_calls|>not JSON SECRET_COMMAND</|tool_calls|>")

    def test_multiple_nested_and_mixed_live_blocks(self):
        text = "A" + dsml() + "B" + native() + "C<|tool_calls|>SECRET_COMMAND</|tool_calls|>D"
        self.assertEqual(inspect_tool_markup(text), ("ABCD", True))
        self.assert_removed("<||DSML||calls>" + dsml() + "SECRET_COMMAND</||DSML||calls>")

    def test_truncated_openers_cut_to_end_without_payload_leak(self):
        for markup in (
            '<||DSML||calls>SECRET_COMMAND',
            '<||DSML||invoke name="safe_tool">SECRET_COMMAND',
            '<||DSML||parameter name="command">SECRET_COMMAND',
            '<｜tool▁calls▁begin｜>SECRET_COMMAND',
            '<｜tool▁call▁begin｜>SECRET_COMMAND',
            '<|tool_calls|>SECRET_COMMAND',
            '<||DSML||invoke name="safe_tool"',
            '<｜tool▁calls▁beg', '<||DS', '<|tool_', '<||',
        ):
            with self.subTest(markup=markup):
                self.assertEqual(inspect_tool_markup("before" + markup), ("before", True))
        self.assertEqual(inspect_tool_markup("before" + dsml()[:-10]), ("before", True))

    def test_orphan_end_tokens_are_only_stripped(self):
        for marker in (
            '</||DSML||calls>', '</||DSML||invoke>', '</||DSML||parameter>',
            '<｜tool▁calls▁end｜>', '<｜tool▁call▁end｜>', '</|tool_calls|>', '<|/tool_calls|>',
        ):
            self.assert_removed(marker)

    def test_fenced_inline_indented_and_blockquotes_are_preserved(self):
        for markup in (dsml(), native(), '<|tool_calls|>SECRET_COMMAND</|tool_calls|>'):
            examples = (
                f"```xml\n{markup}\n```", f"~~~xml\n{markup}\n~~~",
                f"   ````xml\n{markup}\n   ````", f"`{markup}`", f"``{markup}``",
                f"> {markup}", f"  > {markup}", f"    {markup}", f"\t{markup}",
                f"```xml\n{markup}",  # 未闭合的代码围栏同样是示例
            )
            for example in examples:
                with self.subTest(example=example[:50]):
                    self.assertEqual(inspect_tool_markup(example), (example, False))
        multi = "\n".join("    " + line for line in dsml("one\ntwo").splitlines())
        self.assertEqual(inspect_tool_markup(multi), (multi, False))

    def test_explicit_quotes_preserve_tag_and_whole_block_examples(self):
        for left, right in (('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’"),
                            ("「", "」"), ("『", "』"), ("«", "»")):
            for markup in (dsml(), native(), '<|tool_calls|>', '<||DS',
                           '<||DSML||calls>', '<｜tool▁calls▁begin｜>'):
                example = f"示例 {left}{markup}{right}，不是调用。"
                with self.subTest(example=example[:60]):
                    self.assertEqual(inspect_tool_markup(example), (example, False))
            example = left + "example: " + dsml() + right
            self.assertEqual(inspect_tool_markup(example), (example, False))
            example = "示例" + left + dsml() + right
            self.assertEqual(inspect_tool_markup(example), (example, False))

    def test_quotes_inside_payload_do_not_protect_live_markup(self):
        for body in ('{"command": "SECRET_COMMAND"}', "'SECRET_COMMAND'", '“SECRET_COMMAND”'):
            self.assert_removed(dsml(body))
            self.assert_removed(native(body))
        self.assert_removed('<||DSML||invoke name="safe_tool">SECRET_COMMAND</||DSML||invoke>')

    def test_mixed_examples_and_live_blocks_only_remove_live(self):
        example = "```xml\n" + dsml() + "\n```\n"
        text = example + "before" + native() + "after"
        self.assertEqual(inspect_tool_markup(text), (example + "beforeafter", True))
        example = '"' + dsml() + '"'
        self.assertEqual(inspect_tool_markup(example + dsml()), (example, True))

    def test_quoted_closer_does_not_end_a_live_outer_block(self):
        for quoted in ('`</||DSML||calls>`', '"</||DSML||calls>"',
                       '\n```xml\n</||DSML||calls>\n```\n'):
            markup = '<||DSML||calls>' + quoted + 'SECRET_COMMAND</||DSML||calls>'
            self.assert_removed(markup)
        self.assert_removed('<|tool_calls|>`</|tool_calls|>`SECRET_COMMAND</|tool_calls|>')

    def test_explicit_code_boundary_does_not_protect_later_live_text(self):
        example = '```xml\n<||DSML||calls>\n```\n'
        text = example + '<||DSML||calls>SECRET_COMMAND</||DSML||calls></||DSML||calls>'
        self.assertEqual(inspect_tool_markup(text), (example, True))
        example = '`<|tool_calls|>`'
        self.assertEqual(inspect_tool_markup(example + native()), (example, True))

    def test_normal_html_and_mentions_are_unchanged(self):
        for text in ("", "ordinary text", "DSML is a protocol, not a tool call.",
                     "Mention tool_calls in prose", "<div>content</div>",
                     "<calls>ordinary XML</calls>", "<invoke>XML</invoke>",
                     "a < b and b > c", "trailing <", "<|unrelated|>",
                     "  keep whitespace\n", "The tool_calling convention"):
            self.assertEqual(inspect_tool_markup(text), (text, False))

    def test_inspection_never_parses_command_or_json(self):
        with mock.patch("bot.agent_protocol.json.loads", side_effect=AssertionError("no parsing")):
            self.assert_removed(dsml('{"command": "SECRET_COMMAND"}'))
            self.assert_removed(native('{"command": "SECRET_COMMAND"}'))


class MarkupPrefixTests(unittest.TestCase):
    def test_all_splits_of_canonical_headers_are_pending(self):
        for marker in ('<||DSML||calls>', '<｜｜ DSML ｜｜ invoke name="x">',
                       '<｜tool▁calls▁begin｜>', '<|tool_calls|>', '</|tool_calls|>'):
            for end in range(1, len(marker)):
                with self.subTest(marker=marker, end=end):
                    self.assertTrue(markup_prefix_pending("text " + marker[:end]))
            self.assertFalse(markup_prefix_pending(marker))

    def test_complete_markers_and_non_protocol_text_not_pending(self):
        for text in ("", "normal text", "DSML", "<div", "<other", "<|unknown", dsml(), native()):
            self.assertFalse(markup_prefix_pending(text))

    def test_markdown_and_quoted_prefix_examples_not_pending(self):
        for example in ('`<||DS`', '"<||DS"', '“<|tool_”', '```xml\n<||DS',
                        '~~~\n<|tool_', '> <||DS', '    <|tool_', '\t<|tool_'):
            self.assertEqual(inspect_tool_markup(example), (example, False))
            self.assertFalse(markup_prefix_pending(example))


if __name__ == "__main__":
    unittest.main()
