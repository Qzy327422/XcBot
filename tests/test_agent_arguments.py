"""Agent 参数与 MCP 离线回归；只用隔离注册表和内存 handler。"""
import asyncio
import copy
import json
import sys
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot import agent, agent_mcp


FILE_SCHEMA = {
    "type": "object",
    "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
    "required": ["path", "content"],
}


async def memory_handler(args, ctx):
    return "ok"


def spec(schema=None, **kwargs):
    return agent.ToolSpec("memory", "offline", copy.deepcopy(FILE_SCHEMA if schema is None else schema),
                          memory_handler, **kwargs)


class OfflineMixin:
    def setUp(self):
        super().setUp()
        self.registry = agent.ToolRegistry()
        self.forbidden_calls = []

        def forbidden(*args, **kwargs):
            self.forbidden_calls.append(True)
            raise AssertionError("离线测试禁止网络和外部命令")

        self.addCleanup(lambda: self.assertFalse(self.forbidden_calls, "触发过网络或外部命令"))
        for target in ("socket.socket.connect", "socket.socket.connect_ex", "socket.getaddrinfo",
                       "urllib.request.urlopen", "http.client.HTTPConnection.connect", "subprocess.Popen"):
            guard = patch(target, side_effect=forbidden)
            guard.start()
            self.addCleanup(guard.stop)
        isolated = patch.object(agent, "REGISTRY", self.registry)
        isolated.start()
        self.addCleanup(isolated.stop)


class ArgumentTests(OfflineMixin, unittest.TestCase):
    def test_bom_dict_and_empty(self):
        for raw in ('\ufeff {"path":"x","content":""}', {"path": "x", "content": ""}):
            args, error, unwrapped = agent.prepare_tool_args(spec(), raw)
            self.assertEqual(args, {"path": "x", "content": ""})
            self.assertEqual(error, "")
            self.assertFalse(unwrapped)
        empty = spec({"type": "object", "properties": {}})
        for raw in (None, "", " \n ", "\ufeff", "{}", {}):
            self.assertEqual(agent.prepare_tool_args(empty, raw), ({}, "", False))

    def test_missing_is_required_error_not_default(self):
        for raw in (None, "", {}, {"path": "x"}):
            args, error, _ = agent.prepare_tool_args(spec(), raw)
            self.assertEqual(args, {})
            self.assertIn("缺少必填字段", error)
        default_schema = {"type": "object", "properties": {"count": {"type": "integer", "default": 2}}}
        self.assertEqual(agent.prepare_tool_args(spec(default_schema), {}), ({}, "", False))

    def test_non_object_json_and_raw_scalars_are_rejected(self):
        for raw in ("null", "[]", "42", "true", '"text"', [], 0, False, b"{}"):
            with self.subTest(raw=raw):
                self.assertTrue(agent._parse_args(raw)[1])

    def test_bad_json_is_never_repaired(self):
        for raw in ('{"path":"C:\\Users\\name","content":"x"}', '{"path":"x",}', '{"x":',
                    '{"x":1}{"y":2}', '{"path":"SECRET_VALUE", "content":}'):
            with self.subTest(raw=raw):
                args, error, unwrapped = agent._parse_args(raw)
                self.assertEqual(args, {})
                self.assertTrue(error)
                self.assertNotIn("SECRET_VALUE", error)
                self.assertFalse(unwrapped)
        payload = {"path": "C:\\Users\\name", "content": "line\nnext\tend"}
        self.assertEqual(agent._parse_args(json.dumps(payload))[0], payload)

    def test_duplicate_keys_and_nonfinite_are_rejected(self):
        for raw in ('{"x":1,"x":2}', '{"obj":{"x":1,"x":2}}', '{"x":NaN}',
                    '{"x":Infinity}', '{"x":-Infinity}', '{"x":1e999}'):
            self.assertTrue(agent._parse_args(raw)[1])

    def test_only_native_dict_values_and_string_keys(self):
        for value in (float("nan"), float("inf"), (1, 2), {1, 2}, b"x", object(), {1: "x"}):
            with self.subTest(type=type(value)):
                self.assertTrue(agent._parse_args({"x": value})[1])
        cycle = {}
        cycle["self"] = cycle
        self.assertTrue(agent._parse_args(cycle)[1])
        raw = {"x": [{"a": None, "b": False, "c": 1.25}]}
        parsed = agent._parse_args(raw)[0]
        self.assertEqual(parsed, raw)
        parsed["x"][0]["a"] = "changed"
        self.assertIsNone(raw["x"][0]["a"])

    def test_single_wrapper_and_log_redaction(self):
        logs = []
        ctx = agent.AgentContext(log=lambda text, tag: logs.append(text))
        payload = {"path": "SECRET_PATH", "content": "SECRET_CONTENT"}
        for wrapper in ("args", "arguments", "params"):
            self.assertEqual(agent.prepare_tool_args(spec(), {wrapper: payload}, ctx), (payload, "", True))
        self.assertTrue(any("解包" in line for line in logs))
        self.assertFalse(any("SECRET" in line for line in logs))
        self.assertFalse(agent.prepare_tool_args(spec(), {"args": {"args": payload}})[2])
        self.assertFalse(agent.prepare_tool_args(spec(), {"args": {"content": "missing path"}})[2])
        self.assertFalse(agent.prepare_tool_args(spec(), {"args": payload, "other": 1})[2])
        # 没有 schema 证据时不解包；也不把带未知字段、无法通过 schema 的对象当 wrapper。
        self.assertFalse(agent._parse_args({"args": payload}, set(payload))[2])
        strict = spec({**FILE_SCHEMA, "additionalProperties": False})
        self.assertFalse(agent.prepare_tool_args(strict, {"args": {**payload, "unknown": 1}})[2])

    def test_builtin_optional_parameters_unwrap_all_three_wrappers(self):
        schema = {"type": "object", "properties": {"user_id": {"type": "string"}}}
        tool = spec(schema)
        payload = {"user_id": "222"}
        for wrapper in ("args", "arguments", "params"):
            for raw in ({wrapper: payload}, json.dumps({wrapper: payload})):
                with self.subTest(wrapper=wrapper, raw=raw):
                    logs = []
                    ctx = agent.AgentContext(log=lambda text, tag: logs.append(text))
                    self.assertEqual(agent.prepare_tool_args(tool, raw, ctx), (payload, "", True))
                    self.assertEqual(len(logs), 1)
                    self.assertIn("解包", logs[0])
                    self.assertNotIn("222", logs[0])
                    self.assertNotIn("丢弃", logs[0])
        self.assertEqual(agent.prepare_tool_args(tool, {}), ({}, "", False))
        self.assertEqual(agent.prepare_tool_args(tool, payload), (payload, "", False))

    def test_builtin_optional_ambiguous_wrappers_are_errors_not_defaults(self):
        schema = {"type": "object", "properties": {"user_id": {"type": "string"}}}
        tool = spec(schema)
        for wrapper in ("args", "arguments", "params"):
            for inner in ({}, {"user_di": "SECRET_TARGET"}, {"user_id": 222},
                          {"user_id": "222", "target": "SECRET_TARGET"},
                          {wrapper: {"user_id": "SECRET_TARGET"}}, None, [], "SECRET_TARGET"):
                for raw in ({wrapper: inner}, json.dumps({wrapper: inner})):
                    with self.subTest(wrapper=wrapper, inner=inner, raw=raw):
                        args, error, unwrapped = agent.prepare_tool_args(tool, raw)
                        self.assertEqual(args, {})
                        self.assertTrue(error)
                        self.assertNotIn("SECRET_TARGET", error)
                        self.assertFalse(unwrapped)
            for raw in ({wrapper: {"user_id": "222"}, "extra": "SECRET_TARGET"},
                        {wrapper: {"user_id": "222"}, "user_id": "333"},
                        {wrapper: {}, "args" if wrapper != "args" else "params": {"user_id": "222"}}):
                with self.subTest(wrapper=wrapper, raw=raw):
                    args, error, unwrapped = agent.prepare_tool_args(tool, raw)
                    self.assertEqual(args, {})
                    self.assertTrue(error)
                    self.assertNotIn("SECRET_TARGET", error)
                    self.assertFalse(unwrapped)

    def test_zero_parameter_schema_keeps_unknown_wrapper_filtering(self):
        tool = spec({"type": "object", "properties": {}})
        for wrapper in ("args", "arguments", "params"):
            for inner in ({}, {"user_id": "222"}, None):
                with self.subTest(wrapper=wrapper, inner=inner):
                    self.assertEqual(agent.prepare_tool_args(tool, {wrapper: inner}), ({}, "", False))

    def test_optional_wrapper_discovery_uses_local_refs(self):
        schema = {"$defs": {"body": {"type": "object", "properties": {"user_id": {"type": "string"}}}},
                  "$ref": "#/$defs/body"}
        for wrapper in ("args", "arguments", "params"):
            with self.subTest(wrapper=wrapper):
                self.assertEqual(agent.prepare_tool_args(spec(schema), {wrapper: {"user_id": "222"}}),
                                 ({"user_id": "222"}, "", True))

    def test_real_wrapper_named_fields_have_priority(self):
        for key in ("args", "arguments", "params"):
            schema = {"type": "object", "properties": {key: {"type": "object"}, "path": {"type": "string"}}}
            raw = {key: {"path": "value"}}
            self.assertEqual(agent.prepare_tool_args(spec(schema), raw), (raw, "", False))
            schema["properties"][key] = {"type": "integer"}
            args, error, unwrapped = agent.prepare_tool_args(spec(schema), raw)
            self.assertEqual(args, {})
            self.assertTrue(error)
            self.assertFalse(unwrapped)
        for wrapper in ("args", "arguments", "params"):
            for required in ([], ["path"]):
                schema = {"type": "object", "patternProperties": {"^" + wrapper + "$": {"type": "object"}},
                          "properties": {"path": {"type": "string"}}, "required": required}
                raw = {wrapper: {"path": "x"}}
                for filtering in (False, True):
                    with self.subTest(wrapper=wrapper, required=required, filtering=filtering):
                        self.assertEqual(agent._parse_args(raw, {"path"}, schema,
                                                          filter_unknown_args=filtering), (raw, "", False))
                result = agent.prepare_tool_args(spec(schema, filter_unknown_args=False), raw)
                self.assertFalse(result[2])
                if required:
                    self.assertTrue(result[1])
                else:
                    self.assertEqual(result, (raw, "", False))

    def test_mcp_valid_dynamic_wrapper_like_parameter_is_never_unwrapped(self):
        for policy in ({}, {"additionalProperties": True}, {"additionalProperties": {"type": "object"}}):
            schema = {"type": "object", "properties": {"command": {"type": "string"}}, **policy}
            for wrapper in ("args", "arguments", "params"):
                for inner in ({"command": "keep original"}, {}, {"target": "keep original"},
                              {"command": "keep original", "target": "keep original"}):
                    with self.subTest(policy=policy, wrapper=wrapper, inner=inner):
                        raw = {wrapper: inner}
                        tool = spec(schema, filter_unknown_args=False)
                        self.assertEqual(agent.prepare_tool_args(tool, raw), (raw, "", False))
                        self.assertEqual(agent._parse_args(raw, tool.allowed_keys(), schema), (raw, "", False))
                        self.assertEqual(agent._parse_args(raw, tool.allowed_keys(), schema,
                                                          filter_unknown_args=False), (raw, "", False))
                required = {**schema, "required": ["command"]}
                self.assertEqual(agent.prepare_tool_args(spec(required, filter_unknown_args=False),
                                                         {wrapper: {"command": "keep original"}}),
                                 ({"command": "keep original"}, "", True))

    def test_bounded_regex_valid_semantics_without_global_re_changes(self):
        import re
        from jsonschema import Draft202012Validator
        from jsonschema.validators import validator_for
        from jsonschema import _keywords
        original_search = re.search
        schema = {"type": "object", "patternProperties": {"^S_": {"type": "string"}},
                  "properties": {"known": {"type": "integer"}}, "additionalProperties": False}
        tool = spec(schema, filter_unknown_args=False)
        self.assertEqual(agent.prepare_tool_args(tool, {"S_name": "ok", "known": 2})[1], "")
        self.assertTrue(agent.prepare_tool_args(tool, {"S_name": 2})[1])
        self.assertTrue(agent.prepare_tool_args(tool, {"extra": "SECRET"})[1])
        typed = {**schema, "additionalProperties": {"type": "integer"}}
        self.assertFalse(agent.prepare_tool_args(spec(typed, filter_unknown_args=False), {"extra": 1})[1])
        self.assertTrue(agent.prepare_tool_args(spec(typed, filter_unknown_args=False), {"extra": "SECRET"})[1])
        # 无 patternProperties 的 unevaluatedProperties 仍支持标准组合与 pattern 子验证。
        unevaluated = {"type": "object", "allOf": [{"properties": {"a": {"type": "integer"}}}],
                       "unevaluatedProperties": {"type": "string", "pattern": "^ok$"}}
        tool = spec(unevaluated, filter_unknown_args=False)
        self.assertFalse(agent.prepare_tool_args(tool, {"a": 1, "extra": "ok"})[1])
        self.assertTrue(agent.prepare_tool_args(tool, {"a": 1, "extra": "SECRET"})[1])
        self.assertIs(re.search, original_search)
        self.assertIs(Draft202012Validator.VALIDATORS["pattern"], _keywords.pattern)
        self.assertIs(validator_for({"$schema": "https://json-schema.org/draft/2020-12/schema"}), Draft202012Validator)

    def test_unsupported_pattern_and_unevaluated_combinations_fail_closed(self):
        for schema in (
            {"patternProperties": {"^x": True}, "unevaluatedProperties": False},
            {"$defs": {"branch": {"patternProperties": {"^x": True}}},
             "$ref": "#/$defs/branch", "unevaluatedProperties": False},
            {"allOf": [{"patternProperties": {"^x": True}}], "unevaluatedProperties": False},
            {"custom": {"patternProperties": {"^x": True}},
             "$ref": "#/custom", "unevaluatedProperties": False},
            {"$schema": "https://json-schema.org/draft/2019-09/schema",
             "patternProperties": {"^x": True}, "unevaluatedProperties": False},
        ):
            with self.subTest(schema=schema):
                args, error, unwrapped = agent.prepare_tool_args(spec(schema, filter_unknown_args=False), {"SECRET": 1})
                self.assertEqual(args, {})
                self.assertIn("暂不支持", error)
                self.assertNotIn("SECRET", error)
                self.assertFalse(unwrapped)

    def test_builtin_unknown_keys_and_zero_properties_are_filtered(self):
        logs = []
        raw = {"path": "x", "content": "", "unknown": "SECRET"}
        args, error, _ = agent.prepare_tool_args(spec(), raw, agent.AgentContext(log=lambda text, tag: logs.append(text)))
        self.assertEqual(args, {"path": "x", "content": ""})
        self.assertFalse(error)
        self.assertIn("unknown", logs[0])
        self.assertNotIn("SECRET", logs[0])
        self.assertIn("unknown", raw)
        self.assertEqual(agent.prepare_tool_args(spec({"type": "object", "properties": {}}), {"extra": 1}),
                         ({}, "", False))

    def test_mcp_additional_properties_preserved_or_rejected_by_schema(self):
        for policy in (True, {"type": "integer"}):
            schema = {"type": "object", "properties": {}, "additionalProperties": policy}
            self.assertEqual(agent.prepare_tool_args(spec(schema, filter_unknown_args=False), {"extra": 1}),
                             ({"extra": 1}, "", False))
        for policy in (False, {"type": "string"}):
            self.assertTrue(agent.prepare_tool_args(spec({"type": "object", "additionalProperties": policy},
                                                         filter_unknown_args=False), {"extra": 1})[1])

    def test_validation_does_not_coerce_types_and_messages_omit_values(self):
        schema = {"type": "object", "properties": {"count": {"type": "integer", "minimum": 1, "maximum": 3},
                  "mode": {"enum": ["a", "b"]}, "text": {"type": "string", "pattern": "^ok$"}}}
        for raw in ({"count": "2"}, {"count": True}, {"count": 0}, {"count": 4},
                    {"mode": "SECRET"}, {"text": "SECRET"}):
            args, error, _ = agent.prepare_tool_args(spec(schema), raw)
            self.assertFalse(args)
            self.assertIn(next(iter(raw)), error)
            self.assertNotIn("SECRET", error)

    def test_local_defs_refs_composition_and_dialect_without_network(self):
        schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "https://offline.invalid/root.json",
            "$defs": {"body": FILE_SCHEMA},
            "allOf": [{"$ref": "#/$defs/body"}],
        }
        tool = spec(schema, filter_unknown_args=False)
        self.assertEqual(tool.allowed_keys(), {"path", "content"})
        self.assertEqual(agent.prepare_tool_args(tool, {"args": {"path": "x", "content": ""}}),
                         ({"path": "x", "content": ""}, "", True))
        self.assertTrue(agent.prepare_tool_args(tool, {"path": "x"})[1])
        # draft-07 的 tuple items 不能被展示层移除 $schema 后误当 2020-12。
        draft7 = {"$schema": "http://json-schema.org/draft-07/schema#", "type": "object",
                  "properties": {"items": {"type": "array", "items": [{"type": "integer"}]}}}
        shown = agent_mcp._normalize_schema(draft7)
        tool = spec(shown, filter_unknown_args=False, validation_parameters=draft7)
        self.assertEqual(agent.prepare_tool_args(tool, {"items": [1]})[1], "")
        self.assertTrue(agent.prepare_tool_args(tool, {"items": ["1"]})[1])
        draft4 = {"$schema": "http://json-schema.org/draft-04/schema#", "type": "object",
                  "properties": {"count": {"type": "integer", "minimum": 0, "exclusiveMinimum": True}}}
        tool = spec(draft4, filter_unknown_args=False)
        self.assertFalse(agent.prepare_tool_args(tool, {"count": 1})[1])
        self.assertTrue(agent.prepare_tool_args(tool, {"count": 0})[1])

    def test_combination_constraints_and_local_definitions(self):
        schema = {"type": "object", "definitions": {"id": {"type": "integer"}},
                  "properties": {"id": {"$ref": "#/definitions/id"}},
                  "oneOf": [{"required": ["id"]}, {"required": ["name"]}]}
        tool = spec(schema, filter_unknown_args=False)
        self.assertFalse(agent.prepare_tool_args(tool, {"id": 2})[1])
        self.assertTrue(agent.prepare_tool_args(tool, {"id": "2"})[1])
        self.assertTrue(agent.prepare_tool_args(tool, {"id": 2, "name": "x"})[1])
        anyof = spec({"type": "object", "anyOf": [{"properties": {"a": {"const": 1}}, "required": ["a"]},
                                                   {"properties": {"b": {"type": "string"}}, "required": ["b"]}]})
        self.assertFalse(agent.prepare_tool_args(anyof, {"b": "x"})[1])
        self.assertTrue(agent.prepare_tool_args(anyof, {"a": 2})[1])

    def test_remote_refs_unknown_dialects_invalid_schema_fail_closed(self):
        schemas = [
            {"$ref": "https://offline.invalid/schema"}, {"$ref": "file:///secret"},
            {"$ref": "other.json#/x"}, {"$dynamicRef": "https://offline.invalid/x"},
            {"$recursiveRef": "https://offline.invalid/x"},
            {"$defs": {"unused": {"$ref": "https://offline.invalid/x"}}},
            {"$schema": "https://offline.invalid/custom-meta"},
            {"$defs": {"unused": {"$schema": "https://offline.invalid/custom-meta"}}},
            {"type": "unknown"}, {"properties": []}, {"required": "path"},
            {"properties": {"a": {"$ref": "#/$defs/missing"}}},
            {"custom": {"$ref": "https://offline.invalid/x"}, "$ref": "#/custom"},
        ]
        for schema in schemas:
            with self.subTest(schema=schema):
                for _ in range(2):
                    args, error, _ = agent.prepare_tool_args(spec(schema, filter_unknown_args=False), {"a": 1})
                    self.assertEqual(args, {})
                    self.assertTrue(error)
                    self.assertNotIn("https://", error)
        malformed = spec()
        malformed.parameters = []
        self.assertTrue(agent.prepare_tool_args(malformed, {})[1])

    def test_unvisited_local_reference_and_subschema_errors_also_fail_closed(self):
        for schema in (
            {"type": "object", "properties": {"optional": {"$ref": "#/$defs/missing"}}},
            {"anyOf": [True, {"$ref": "#/$defs/missing"}]},
            {"$defs": {"unused": {"$ref": "#/$defs/missing"}}},
            {"$defs": {"unused": {"$ref": "#/$defs/not_schema"}, "not_schema": 3}},
        ):
            with self.subTest(schema=schema):
                self.assertTrue(agent.prepare_tool_args(spec(schema, filter_unknown_args=False), {})[1])

    def test_schema_changes_are_not_stale(self):
        tool = spec()
        self.assertFalse(agent.prepare_tool_args(tool, {"path": "x", "content": ""})[1])
        tool.parameters["properties"]["content"]["minLength"] = 1
        self.assertTrue(agent.prepare_tool_args(tool, {"path": "x", "content": ""})[1])

    def test_schema_validator_cache_reuses_content_but_is_bounded(self):
        agent._cached_argument_validator.cache_clear()
        self.addCleanup(agent._cached_argument_validator.cache_clear)
        tool = spec()
        for _ in range(3):
            self.assertFalse(agent.prepare_tool_args(tool, {"path": "x", "content": ""})[1])
        self.assertEqual(agent._cached_argument_validator.cache_info().misses, 1)
        self.assertEqual(agent._cached_argument_validator.cache_info().hits, 2)
        for number in range(140):
            agent._argument_validator({"type": "object", "title": str(number)})
        self.assertLessEqual(agent._cached_argument_validator.cache_info().currsize, 128)
        before = agent._cached_argument_validator.cache_info().currsize
        agent._argument_validator({"type": "object", "description": "x" * 70000})
        self.assertEqual(agent._cached_argument_validator.cache_info().currsize, before)

    def test_literal_ref_keys_are_data_and_local_dynamic_refs_are_supported(self):
        literal = {"$ref": "https://offline.invalid/literal", "$schema": "ordinary data"}
        schema = {"type": "object", "properties": {
            "$ref": {"type": "string"}, "$schema": {"type": "string"},
            "payload": {"const": literal}}, "required": ["payload"]}
        raw = {**literal, "payload": literal}
        self.assertEqual(agent.prepare_tool_args(spec(schema, filter_unknown_args=False), raw), (raw, "", False))
        dynamic = {"$defs": {"text": {"$anchor": "text", "type": "string"}},
                   "type": "object", "properties": {"value": {"$dynamicRef": "#text"}}}
        tool = spec(dynamic, filter_unknown_args=False)
        self.assertFalse(agent.prepare_tool_args(tool, {"value": "x"})[1])
        self.assertTrue(agent.prepare_tool_args(tool, {"value": 1})[1])

    def test_mcp_normalize_is_deepcopy_and_does_not_discard_semantics(self):
        original = {"$schema": "https://json-schema.org/draft/2020-12/schema", "$defs": {"x": {"type": "string"}},
                    "definitions": {"legacy": {"type": "number"}}, "additionalProperties": False,
                    "properties": {"x": {"$ref": "#/$defs/x"}}, "allOf": [{"required": ["x"]}]}
        snapshot = copy.deepcopy(original)
        normalized = agent_mcp._normalize_schema(original)
        self.assertNotIn("$schema", normalized)
        self.assertEqual(normalized, {key: value for key, value in original.items() if key != "$schema"})
        normalized["$defs"]["x"]["type"] = "integer"
        self.assertEqual(original, snapshot)
        self.assertTrue(agent.prepare_tool_args(spec(agent_mcp._normalize_schema(None), filter_unknown_args=False), {})[1])


class ExecutionTests(OfflineMixin, unittest.IsolatedAsyncioTestCase):
    async def test_optional_target_wrappers_never_fall_back_after_filtering(self):
        # 只在内存中模拟目标默认逻辑，不导入或调用真实戳一戳工具。
        targets = []
        effects = []

        async def record(args, ctx):
            targets.append(args.get("user_id") or ctx.user_id)
            return "ok"

        tool = spec({"type": "object", "properties": {"user_id": {"type": "string"}}}, side_effect=True)
        tool.handler = record
        ctx = agent.AgentContext(user_id="111")
        settings = agent.AgentSettings(enabled_tools={tool.name: True})
        for wrapper in ("args", "arguments", "params"):
            with self.subTest(wrapper=wrapper):
                for inner in ({}, {"user_di": "222"}, {"user_id": 222},
                              {"user_id": "222", "target": "333"},
                              {wrapper: {"user_id": "222"}}, None, [], "222"):
                    before = (list(targets), list(effects))
                    result = await agent._exec_one(tool, {wrapper: inner}, ctx, settings, effects.append)
                    self.assertTrue(result[0].startswith("error:"))
                    self.assertEqual(result[0], result[1])
                    self.assertEqual((targets, effects), before)
                raw = {wrapper: {"user_id": "222"}, "extra": "333"}
                before = (list(targets), list(effects))
                result = await agent._exec_one(tool, raw, ctx, settings, effects.append)
                self.assertTrue(result[0].startswith("error:"))
                self.assertEqual((targets, effects), before)
                self.assertEqual(await agent._exec_one(tool, {wrapper: {"user_id": "222"}},
                                                      ctx, settings, effects.append), ("ok", "ok"))
        self.assertEqual(targets, ["222"] * 3)
        self.assertEqual(effects, [tool.name] * 3)
        self.assertEqual(await agent._exec_one(tool, {}, ctx, settings, effects.append), ("ok", "ok"))
        self.assertEqual(targets, ["222"] * 3 + ["111"])
        self.assertEqual(effects, [tool.name] * 4)

    async def test_optional_wrapper_inner_regex_timeout_never_reaches_handler(self):
        import time
        calls = []
        effects = []

        async def record(args, ctx):
            calls.append(args)
            return "ok"

        tool = spec({"type": "object", "properties": {
            "user_id": {"type": "string", "pattern": "^(a+)+$"}}}, side_effect=True)
        tool.handler = record
        self.assertFalse(agent._argument_validator(tool.parameters)[1])
        for wrapper in ("args", "arguments", "params"):
            with self.subTest(wrapper=wrapper):
                started = time.monotonic()
                result = await agent._exec_one(tool, {wrapper: {"user_id": "a" * 20000 + "!SECRET_TARGET"}},
                                              agent.AgentContext(user_id="111"),
                                              agent.AgentSettings(enabled_tools={tool.name: True}), effects.append)
                self.assertLess(time.monotonic() - started, 0.5)
                self.assertIn("正则匹配超时", result[0])
                self.assertNotIn("SECRET_TARGET", result[0])
                self.assertEqual(result[0], result[1])
                self.assertFalse(calls)
                self.assertFalse(effects)

    async def test_authorization_api_matches_execution_errors_without_schema_access(self):
        tool = spec(level="admin")
        tool.name = "mcp__offline"
        tool.parameters = object()  # 授权不得读取它，更不能编译恶意 schema。
        ctx = agent.AgentContext(user_id="offline-user")
        for settings, expected in (
            (agent.AgentSettings(), "当前已被管理员关闭"),
            (agent.AgentSettings(enabled_tools={"mcp_tools": True}, allowed_names=set()), "不在本次对话"),
            (agent.AgentSettings(enabled_tools={"mcp_tools": True}), "权限不足"),
        ):
            with patch.object(agent, "prepare_tool_args", side_effect=AssertionError("不得预检")) as check:
                error = agent.tool_authorization_error(tool, ctx, settings)
                self.assertTrue(error.startswith("error:"))
                self.assertIn(expected, error)
                self.assertEqual(await agent._exec_one(tool, {}, ctx, settings), (error, error))
                check.assert_not_called()
        settings = agent.AgentSettings(enabled_tools={"mcp_tools": True}, allowed_names={tool.name},
                                       level_overrides={"mcp_tools": "user"})
        self.assertEqual(agent.tool_authorization_error(tool, ctx, settings), "")
        with patch.object(agent, "tool_authorization_error", return_value="error: shared gate") as gate, \
                patch.object(agent, "prepare_tool_args") as prepare:
            self.assertEqual(await agent._exec_one(tool, {}, ctx, settings), ("error: shared gate", "error: shared gate"))
            gate.assert_called_once_with(tool, ctx, settings)
            prepare.assert_not_called()

    async def test_long_regex_backtracking_is_bounded_across_all_supported_paths(self):
        import time
        # 用 regex 的 timeout 真实中断，不用 mocked matcher；stdlib re 会指数回溯。
        attack = "a" * 20000 + "!SECRET_VALUE"
        pattern = "^(a+)+$"
        cases = [
            ({"type": "object", "properties": {"text": {"type": "string", "pattern": pattern}}}, {"text": attack}),
            ({"type": "object", "patternProperties": {pattern: True}}, {attack: 1}),
            # additionalProperties 按顺序先跑，直接覆盖其内部的键匹配，不靠 patternProperties 先截断。
            ({"type": "object", "additionalProperties": False, "patternProperties": {pattern: True}}, {attack: 1}),
            ({"type": "object", "additionalProperties": {"type": "integer"}, "patternProperties": {pattern: True}}, {attack: 1}),
            ({"type": "object", "unevaluatedProperties": {"pattern": pattern}}, {"extra": attack}),
            ({"type": "object", "propertyNames": {"pattern": pattern}}, {attack: 1}),
            ({"type": "object", "allOf": [{"properties": {"text": {"pattern": pattern}}}]}, {"text": attack}),
            ({"type": "object", "$defs": {"text": {"pattern": pattern}},
              "properties": {"text": {"$ref": "#/$defs/text"}}}, {"text": attack}),
            # evolve 遇到显式子方言不能退回未限时的标准 validator。
            ({"type": "object", "properties": {"text": {"$schema": "http://json-schema.org/draft-07/schema#",
                                                          "pattern": pattern}}}, {"text": attack}),
            ({"type": "object", "properties": {"text": {"$schema": "https://json-schema.org/draft/2020-12/schema",
                                                          "pattern": pattern}}}, {"text": attack}),
        ]
        for number, (schema, raw) in enumerate(cases):
            with self.subTest(path=number):
                tool = spec(schema, filter_unknown_args=False, side_effect=True)
                events = []
                handler_calls = []

                async def record(args, ctx):
                    handler_calls.append(args)
                    return "ok"

                tool.handler = record
                # 排除 schema 构建耗时，直接测验证阶段与事件循环恢复。
                self.assertFalse(agent._argument_validator(schema)[1])
                tick = []
                asyncio.get_running_loop().call_soon(tick.append, "resumed")
                started = time.monotonic()
                raw_result, packaged = await agent._exec_one(tool, raw, agent.AgentContext(),
                    agent.AgentSettings(enabled_tools={tool.name: True}, tool_timeout=0.001), events.append)
                await asyncio.sleep(0)  # 让已排队的 loop callback 跑，不能被失控回溯永久阻塞。
                self.assertLess(time.monotonic() - started, 0.5)
                self.assertEqual(tick, ["resumed"])
                self.assertIn("正则匹配超时", raw_result)
                self.assertNotIn("SECRET_VALUE", raw_result)
                self.assertEqual(raw_result, packaged)
                self.assertFalse(events)
                self.assertFalse(handler_calls)

    async def test_all_permission_gates_precede_parameter_validation(self):
        tool = spec(level="admin", side_effect=True)
        events = []
        self.registry.register(tool)
        contexts = [
            (agent.AgentContext(user_level="admin"), agent.AgentSettings(enabled_tools={})),
            (agent.AgentContext(user_level="admin"), agent.AgentSettings(enabled_tools={tool.name: True}, allowed_names=set())),
            (agent.AgentContext(user_level="user"), agent.AgentSettings(enabled_tools={tool.name: True})),
        ]
        with patch.object(agent, "prepare_tool_args", side_effect=AssertionError("门禁前不应校验")) as check:
            for ctx, settings in contexts:
                raw, packaged = await agent._exec_one(tool, {"content": ""}, ctx, settings, events.append)
                self.assertTrue(raw.startswith("error:"))
                self.assertEqual(raw, packaged)
            check.assert_not_called()
        self.assertFalse(events)

    async def test_file_write_missing_content_never_reaches_handler(self):
        # 提取真实注册用 schema，但不导入文件工具，更不调用真实磁盘 handler。
        import ast
        tree = ast.parse((ROOT / "bot" / "agent_fs.py").read_text(encoding="utf-8"))
        actual_schema = None
        for node in tree.body:
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_tool_file_write":
                for decorator in node.decorator_list:
                    if isinstance(decorator, ast.Call):
                        for keyword in decorator.keywords:
                            if keyword.arg == "parameters":
                                actual_schema = ast.literal_eval(keyword.value)
        self.assertIsNotNone(actual_schema)
        self.assertEqual(actual_schema["required"], ["path", "content"])
        self.assertEqual({key: value["type"] for key, value in actual_schema["properties"].items()},
                         {"path": "string", "content": "string"})
        events = []

        async def record(args, ctx):
            events.append(("handler", args))
            return "ok"

        tool = spec(actual_schema, side_effect=True, level="admin")
        tool.name = "file_write"
        tool.handler = record
        self.registry.register(tool)
        ctx = agent.AgentContext(user_level="admin")
        settings = agent.AgentSettings(enabled_tools={tool.name: True})
        for args in ({"path": "memory-only"}, {"path": "memory-only", "content": None}):
            result = await agent._exec_one(tool, args, ctx, settings, lambda name: events.append(("effect", name)))
            self.assertTrue(result[0].startswith("error:"))
            self.assertFalse(events)
        self.assertEqual(await agent._exec_one(tool, {"path": "memory-only", "content": ""}, ctx, settings,
                                              lambda name: events.append(("effect", name))), ("ok", "ok"))
        self.assertEqual(events, [("effect", "file_write"), ("handler", {"path": "memory-only", "content": ""})])

    async def test_schema_error_and_parse_error_do_not_mark_sideeffect(self):
        events = []
        for tool, args in ((spec({"type": "bad"}, side_effect=True), {}),
                           (spec(side_effect=True), {"content": "x"}),
                           (spec(side_effect=True), {"path": "x", "content": object()})):
            raw, _ = await agent._exec_one(tool, args, agent.AgentContext(),
                                           agent.AgentSettings(enabled_tools={tool.name: True}), events.append)
            self.assertTrue(raw.startswith("error:"))
        self.assertFalse(events)

    async def test_sideeffect_is_marked_even_when_handler_reports_failure(self):
        events = []

        async def fail(args, ctx):
            events.append("handler")
            return "error: memory failure"

        tool = spec(side_effect=True)
        tool.handler = fail
        result = await agent._exec_one(tool, {"path": "x", "content": ""}, agent.AgentContext(),
                                       agent.AgentSettings(enabled_tools={tool.name: True}), events.append)
        self.assertEqual(events, [tool.name, "handler"])
        self.assertEqual(result, ("error: memory failure", "error: memory failure"))

    async def test_mcp_connect_keeps_original_schema_for_validation(self):
        schema = {"$schema": "http://json-schema.org/draft-07/schema#", "type": "object",
                  "properties": {"x": {"type": "integer"}}, "additionalProperties": False}

        @asynccontextmanager
        async def transport(_params):
            yield ("memory-in", "memory-out")

        class Session:
            def __init__(self, *args):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def initialize(self):
                pass

            async def list_tools(self):
                return SimpleNamespace(tools=[SimpleNamespace(name="read", description="offline", inputSchema=schema)])

        client = agent_mcp.MCPClient("memory", {"command": "never-executed"})
        with patch.object(agent_mcp, "stdio_client", transport), patch.object(agent_mcp, "ClientSession", Session), \
                patch.object(agent_mcp, "StdioServerParameters", lambda **kwargs: kwargs), \
                patch.object(agent_mcp.shutil, "which", return_value="never-executed"):
            await client.connect()
            await client.close()
        self.assertEqual(client.tools[0]["validation_parameters"], schema)
        self.assertNotIn("$schema", client.tools[0]["parameters"])
        client.tools[0]["validation_parameters"]["properties"]["x"]["type"] = "string"
        self.assertEqual(schema["properties"]["x"]["type"], "integer")

    async def test_mcp_refresh_registers_no_filter_and_full_schema(self):
        schema = {"$schema": "http://json-schema.org/draft-07/schema#", "type": "object",
                  "properties": {}, "additionalProperties": {"type": "integer"}}

        class MemoryClient:
            def __init__(self, name, config):
                self.session = object()
                self.tools = [{"name": "memory", "description": "offline", "parameters": agent_mcp._normalize_schema(schema),
                               "validation_parameters": copy.deepcopy(schema)}]

            async def connect(self):
                pass

            async def close(self):
                pass

        with patch.object(agent_mcp, "REGISTRY", self.registry), patch.object(agent_mcp, "_clients", {}), \
                patch.object(agent_mcp, "_registered", set()), patch.object(agent_mcp, "_lock", asyncio.Lock()), \
                patch.object(agent_mcp, "MCPClient", MemoryClient), \
                patch.object(agent_mcp, "is_available", return_value=(True, "")), \
                patch.object(agent_mcp, "load_server_configs", return_value={"offline": {"enabled": True}}), \
                patch.object(agent_mcp, "_make_handler", return_value=memory_handler):
            result = await agent_mcp.refresh()
            tool = self.registry.get(result["servers"][0]["tools"][0])
            self.assertFalse(tool.filter_unknown_args)
            self.assertEqual(tool.validation_parameters, schema)
            self.assertTrue(tool.side_effect)
            self.assertEqual(agent.prepare_tool_args(tool, {"extra": 7}), ({"extra": 7}, "", False))
            self.assertTrue(agent.prepare_tool_args(tool, {"extra": "7"})[1])
            # MCP gate 仍统一为 mcp_tools，成功结果继续保留不可信输出隔离。
            raw, packaged = await agent._exec_one(tool, {"extra": 7}, agent.AgentContext(user_level="admin"),
                                                  agent.AgentSettings(enabled_tools={"mcp_tools": True}))
            self.assertEqual(raw, "ok")
            self.assertIn('<tool_result source="external" untrusted="true">', packaged)


if __name__ == "__main__":
    unittest.main()
