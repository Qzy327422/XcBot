"""Offline observation config/schema/save tests; no main, callback or real API."""
import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import webui
from bot import proactive_reply as proactive


OBSERVATION_DEFAULTS = {
    "enabled": True,
    "trigger_probability": 0.5,
    "default_wait_seconds": 60,
    "min_wait_seconds": 10,
    "max_wait_seconds": 600,
    "debounce_seconds": 8,
    "max_debounce_seconds": 20,
    "min_continuation_interval_seconds": 30,
}
OBSERVATION_BOUNDS = {
    "trigger_probability": (0, 1),
    "default_wait_seconds": (1, 3600),
    "min_wait_seconds": (1, 3600),
    "max_wait_seconds": (1, 3600),
    "debounce_seconds": (0, 300),
    "max_debounce_seconds": (0, 300),
    "min_continuation_interval_seconds": (1, 3600),
}


def schema_fields():
    return {
        spec["path"]: spec
        for section in webui.build_ui_schema({})
        for spec in section.get("fields", [])
    }


def invalid_observations():
    yield from (None, [], "enabled", True, 0)
    for value in (None, 0, 1, "true", [], {}):
        yield {"enabled": value}
    for name, (minimum, maximum) in OBSERVATION_BOUNDS.items():
        for value in (None, True, False, "8", [], {}, float("nan"),
                      float("inf"), float("-inf"), 10 ** 400,
                      minimum - 1, maximum + 1):
            yield {name: value}
    yield {"min_wait_seconds": 61}
    yield {"max_wait_seconds": 59}
    yield {"default_wait_seconds": 9}
    yield {"default_wait_seconds": 601}
    yield {"debounce_seconds": 21, "max_debounce_seconds": 20}


class ObservationSchemaTests(unittest.TestCase):
    def test_defaults_agree_across_template_schema_and_real_normalizer(self):
        template = json.loads((ROOT / "config.default.json").read_text(encoding="utf-8"))
        normalized = proactive.normalize_config({})
        fields = schema_fields()
        self.assertTrue(template["FeatureSwitches"]["group_chat_context"])
        self.assertTrue(webui.DEFAULT_FEATURE_SWITCHES["group_chat_context"])
        for kind, category in (("group", "群聊观察"), ("private", "私聊观察")):
            with self.subTest(kind=kind):
                self.assertFalse(template["ProactiveReply"][kind]["enabled"])
                self.assertFalse(normalized[kind]["enabled"])
                self.assertIs(fields[f"ProactiveReply.{kind}.enabled"]["default"], False)
                self.assertEqual(template["ProactiveReply"][kind]["observation"], OBSERVATION_DEFAULTS)
                self.assertEqual(normalized[kind]["observation"], OBSERVATION_DEFAULTS)
                prefix = f"ProactiveReply.{kind}.observation."
                self.assertEqual({p.removeprefix(prefix) for p in fields if p.startswith(prefix)},
                                 set(OBSERVATION_DEFAULTS))
                for name, default in OBSERVATION_DEFAULTS.items():
                    spec = fields[prefix + name]
                    self.assertEqual(spec["category"], category)
                    self.assertEqual(spec["default"], default)
                    self.assertEqual(spec["type"], "bool" if name == "enabled" else "number")
                    if name in OBSERVATION_BOUNDS:
                        self.assertEqual((spec["min"], spec["max"]), OBSERVATION_BOUNDS[name])
                        self.assertEqual(spec["step"], 0.05 if name == "trigger_probability" else 1)
                self.assertIn("无人说话不催发", fields[prefix + "enabled"]["desc"])
                self.assertIn("不是必须发送", fields[prefix + "enabled"]["desc"])
                self.assertIn("普通一问一答不等", fields[prefix + "enabled"]["desc"])
                self.assertEqual(fields[prefix + "debounce_seconds"]["label"],
                                 "有人接话后合并几秒再判断")

    def test_weak_blacklist_metadata_explains_global_ai_scope_and_legacy_probability(self):
        fields = schema_fields()
        probability = fields["Others.weak_blacklist_trigger_probability"]
        self.assertIn("0~1", probability["desc"])
        self.assertIn("0~100", probability["desc"])
        self.assertIn("0=不回复", probability["desc"])
        self.assertIn("1=全部放行", probability["desc"])
        descriptions = [probability["desc"], fields["Others.weak_blacklist_users"]["desc"],
                        next(x["desc"] for x in webui.FEATURE_META if x["key"] == "weak_blacklist")]
        for desc in descriptions:
            self.assertIn("全局", desc)
            self.assertIn("不影响命令和插件", desc)
            self.assertIn("仅在引用他人消息时检查该作者", desc)
            self.assertIn("未引用或引用自己", desc)

    def test_real_normalizer_merges_independent_defaults_without_mutating_input(self):
        raw = {"group": {"observation": {"enabled": False, "debounce_seconds": 0}},
               "private": {"observation": {"default_wait_seconds": 90}}}
        before = copy.deepcopy(raw)
        cfg = proactive.normalize_config(raw)
        self.assertEqual(raw, before)
        self.assertEqual(cfg["group"]["observation"],
                         {**OBSERVATION_DEFAULTS, "enabled": False, "debounce_seconds": 0})
        self.assertEqual(cfg["private"]["observation"],
                         {**OBSERVATION_DEFAULTS, "default_wait_seconds": 90})
        cfg["group"]["observation"]["max_wait_seconds"] = 999
        self.assertEqual(cfg["private"]["observation"]["max_wait_seconds"], 600)
        self.assertEqual(proactive.normalize_config({})["group"]["observation"], OBSERVATION_DEFAULTS)

    def test_real_normalizer_rejects_invalid_nested_types_ranges_and_relations(self):
        for kind in ("group", "private"):
            for observation in invalid_observations():
                with self.subTest(kind=kind, observation=observation), self.assertRaises(ValueError):
                    proactive.normalize_config({kind: {"observation": observation}})

    def test_removed_fixed_window_and_round_limit_are_not_exposed_or_retained(self):
        cfg = proactive.normalize_config({kind: {"observation": {
            "window_seconds": 300, "max_continuation_rounds": 2}}
            for kind in ("group", "private")})
        fields = schema_fields()
        for kind in ("group", "private"):
            self.assertEqual(cfg[kind]["observation"], OBSERVATION_DEFAULTS)
            for name in ("window_seconds", "max_continuation_rounds"):
                self.assertNotIn(f"ProactiveReply.{kind}.observation.{name}", fields)


class ObservationSaveTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        self.config = base / "config.json"
        plugins = base / "plugins"
        plugins.mkdir()
        self.original = {
            "owner": ["12345"], "black_list": ["67890"],
            "Connection": {"access_token": "offline-preserve-token"},
            "Others": {"ROOT_User": ["12345"]},
            "FeatureSwitches": {"group_chat_context": True},
            "untouched": {"custom": [1, 2]},
            "ProactiveReply": {
                "group": {"enabled": False, "whitelist": ["123"], "blacklist": ["456"]},
                "private": {"enabled": False, "whitelist": ["789"]},
            },
        }
        self.config.write_text(json.dumps(self.original), encoding="utf-8")
        for name, value in (("CONFIG_PATH", self.config), ("PLUGIN_DIR", plugins),
                            ("LEGACY_CONFIG_PATHS", []), ("_plugin_schemas_callback", None),
                            ("_config_saved_callback", None)):
            p = patch.object(webui, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.side_effects = []
        for name in ("force_apply_llm_endpoints_from_config", "_apply_webui_runtime_update",
                     "apply_trace_max_records", "prune_old_logs"):
            p = patch.object(webui, name)
            self.side_effects.append(p.start())
            self.addCleanup(p.stop)
        for target in ("socket.socket.connect", "socket.create_connection", "threading.Thread.start"):
            p = patch(target, side_effect=AssertionError("offline test must not start runtime or network"))
            p.start()
            self.addCleanup(p.stop)

    def saved(self):
        return json.loads(self.config.read_text(encoding="utf-8"))

    def save_form(self, values):
        # The full form normally includes these unrelated list controls too.
        webui.save_ui_state({"form_values": {
            "manage_users": self.original["owner"],
            "black_list": self.original["black_list"], **values,
        }})

    def assert_preserved(self, saved):
        for name in ("Connection", "untouched", "owner", "black_list"):
            self.assertEqual(saved[name], self.original[name])
        self.assertEqual(saved["Others"]["ROOT_User"], self.original["Others"]["ROOT_User"])
        self.assertTrue(saved["FeatureSwitches"]["group_chat_context"])
        for kind in ("group", "private"):
            self.assertFalse(saved["ProactiveReply"][kind]["enabled"])
            self.assertEqual(saved["ProactiveReply"][kind]["whitelist"],
                             self.original["ProactiveReply"][kind]["whitelist"])

    def test_old_config_without_observation_loads_defaults_without_writing_then_saves(self):
        before = self.config.read_bytes()
        bundle = webui.collect_config_bundle()
        for spec in schema_fields().values():
            if ".observation." in spec["path"]:
                self.assertEqual(webui.get_ui_value(bundle, spec["path"], spec["default"]),
                                 spec["default"])
        self.assertEqual(self.config.read_bytes(), before)
        webui.save_config_bundle({"config_json": {}})
        saved = self.saved()
        self.assert_preserved(saved)
        for kind in ("group", "private"):
            self.assertEqual(saved["ProactiveReply"][kind]["observation"], OBSERVATION_DEFAULTS)
        webui.save_config_bundle({"config_json": saved})
        self.assertEqual(self.saved(), saved)

    def test_new_observation_defaults_roundtrip_through_four_level_form(self):
        values = {spec["path"]: spec["default"] for spec in schema_fields().values()
                  if ".observation." in spec["path"]}
        self.save_form(values)
        saved = self.saved()
        self.assert_preserved(saved)
        for kind in ("group", "private"):
            self.assertEqual(saved["ProactiveReply"][kind]["observation"], OBSERVATION_DEFAULTS)
        bundle = webui.collect_config_bundle()
        for path, value in values.items():
            self.assertEqual(webui.get_ui_value(bundle, path), value)
        self.save_form(values)
        self.assertEqual(self.saved(), saved)
        self.assertIsNone(webui._config_saved_callback)

    def test_partial_nested_patch_and_custom_form_values_preserve_sibling_fields(self):
        webui.save_config_bundle({"config_json": {"ProactiveReply": {
            "group": {"observation": {"default_wait_seconds": 90}},
            "private": {"observation": {"enabled": False}},
        }}})
        self.save_form({"ProactiveReply.group.observation.debounce_seconds": 0,
                        "ProactiveReply.private.observation.default_wait_seconds": 120})
        saved = self.saved()
        self.assert_preserved(saved)
        self.assertEqual(saved["ProactiveReply"]["group"]["observation"],
                         {**OBSERVATION_DEFAULTS, "default_wait_seconds": 90, "debounce_seconds": 0})
        self.assertEqual(saved["ProactiveReply"]["private"]["observation"],
                         {**OBSERVATION_DEFAULTS, "enabled": False, "default_wait_seconds": 120})

    def test_observation_probabilities_save_independently_from_cold_probabilities(self):
        self.save_form({"ProactiveReply.group.observation.trigger_probability": 0,
                        "ProactiveReply.private.observation.trigger_probability": 1})
        saved = self.saved()
        self.assertEqual(saved["ProactiveReply"]["group"]["observation"]["trigger_probability"], 0)
        self.assertEqual(saved["ProactiveReply"]["private"]["observation"]["trigger_probability"], 1)
        self.assertEqual(saved["ProactiveReply"]["group"]["trigger_probability"], 0.3)
        self.assertEqual(saved["ProactiveReply"]["private"]["trigger_probability"], 0.1)

    def test_schema_accepts_missing_fields_but_validates_four_level_values(self):
        webui._validate_proactive_config({})
        webui._validate_proactive_config({"group": {"observation": {"debounce_seconds": 0}}})
        for kind in ("group", "private"):
            for value in (None, True, "60", -1, 3601):
                with self.subTest(kind=kind, value=value), self.assertRaisesRegex(
                        ValueError, f"ProactiveReply.{kind}.observation.default_wait_seconds"):
                    webui._validate_proactive_config({kind: {"observation": {"default_wait_seconds": value}}})

    def test_invalid_nested_bundle_values_never_write_or_apply_runtime(self):
        before = self.config.read_bytes()
        for kind in ("group", "private"):
            for observation in invalid_observations():
                with self.subTest(kind=kind, observation=observation), self.assertRaises(ValueError):
                    webui.save_config_bundle({"config_json": {"ProactiveReply": {
                        kind: {"observation": observation}}}})
                self.assertEqual(self.config.read_bytes(), before)
        for effect in self.side_effects:
            effect.assert_not_called()
        self.assertFalse((self.config.parent / "config_backup").exists())

    def test_invalid_form_values_and_cross_field_relations_never_write(self):
        before = self.config.read_bytes()
        for kind in ("group", "private"):
            for observation in invalid_observations():
                if not isinstance(observation, dict):
                    continue
                values = {f"ProactiveReply.{kind}.observation.{key}": value
                          for key, value in observation.items()}
                with self.subTest(kind=kind, observation=observation), self.assertRaises(ValueError):
                    self.save_form(values)
                self.assertEqual(self.config.read_bytes(), before)
        for effect in self.side_effects:
            effect.assert_not_called()

    def test_form_path_allowlist_rejects_unknown_container_and_deeper_paths_before_mutation(self):
        paths = ["ProactiveReply", "ProactiveReply.group", "ProactiveReply.group.observation",
                 "ProactiveReply.group.enabled.extra", "ProactiveReply.group.observation.enabled.extra",
                 "ProactiveReply.group.observation.unknown", "ProactiveReply..observation.enabled",
                 "ProactiveReply.other.observation.enabled", "ProactiveReply.private.observation.window_seconds",
                 "ProactiveReply.private.observation.max_continuation_rounds"]
        before = self.config.read_bytes()
        for path in paths:
            with self.subTest(path=path):
                payload = {"config_json": copy.deepcopy(self.original)}
                unchanged = copy.deepcopy(payload)
                with self.assertRaises(ValueError):
                    webui.set_ui_value(payload, path, True)
                self.assertEqual(payload, unchanged)
                with self.assertRaises(ValueError):
                    self.save_form({path: True})
                self.assertEqual(self.config.read_bytes(), before)

    def test_numeric_boundary_values_are_accepted_by_schema_and_real_normalizer(self):
        for wait in (1, 3600):
            for debounce in (0, 300):
                observation = {
                    "enabled": False, "trigger_probability": 0.5, "min_wait_seconds": wait,
                    "default_wait_seconds": wait, "max_wait_seconds": wait,
                    "debounce_seconds": debounce, "max_debounce_seconds": debounce,
                    "min_continuation_interval_seconds": wait,
                }
                with self.subTest(wait=wait, debounce=debounce):
                    self.save_form({f"ProactiveReply.{kind}.observation.{key}": value
                                    for kind in ("group", "private")
                                    for key, value in observation.items()})
                    for kind in ("group", "private"):
                        self.assertEqual(self.saved()["ProactiveReply"][kind]["observation"], observation)


class ObservationImportSafetyTests(unittest.TestCase):
    def test_safe_import_does_not_start_runtime_network_or_modify_config(self):
        code = '''import builtins, pathlib, socket, sys, threading, urllib.request
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
real_open = builtins.open
def readonly_open(file, mode="r", *args, **kwargs):
    assert not any(flag in mode for flag in "wax+"), "import attempted a write"
    return real_open(file, mode, *args, **kwargs)
with patch.object(threading.Thread, "start", side_effect=AssertionError("thread started")), \\
     patch.object(socket.socket, "bind", side_effect=AssertionError("server bound")), \\
     patch.object(socket.socket, "connect", side_effect=AssertionError("network used")), \\
     patch.object(socket, "create_connection", side_effect=AssertionError("network used")), \\
     patch.object(urllib.request, "urlopen", side_effect=AssertionError("API used")), \\
     patch.object(builtins, "open", readonly_open), \\
     patch.object(pathlib.Path, "write_text", side_effect=AssertionError("import wrote a file")):
    import bot.proactive_reply as proactive
    import webui
    assert proactive.normalize_config({})["group"]["observation"]["enabled"] is True
    assert webui._server is None and webui._server_thread is None
    assert webui._config_saved_callback is None
    assert "main" not in sys.modules and "hyper" not in sys.modules
print("safe observation config import ok")
'''
        result = subprocess.run([sys.executable, "-B", "-c", code, str(ROOT)], cwd=tempfile.gettempdir(),
                                capture_output=True, text=True, timeout=30,
                                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("safe observation config import ok", result.stdout)


if __name__ == "__main__":
    unittest.main()
