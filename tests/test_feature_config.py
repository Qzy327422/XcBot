"""Offline feature-configuration regression tests; no main/server/QQ startup."""
import ast
import copy
import threading
from collections import defaultdict
from datetime import date
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import webui
from bot.llm_config import (
    build_all_provider_endpoints,
    build_llm_endpoints_from_providers,
    build_planner_endpoints,
    force_apply_llm_endpoints_from_config,
    normalize_llm_provider_rotation,
    normalize_llm_providers_config,
    sync_provider_config,
)


def provider(**kwargs):
    return {
        "id": "service", "base_url": "https://offline.invalid/v1",
        "keys": ["offline-secret-key"], "http_proxy": "127.0.0.1:7890",
        "models": [], "planner_models": [], "embedding_models": [], **kwargs,
    }


def legacy():
    return [{"base_url": "https://legacy.invalid", "model": "old-chat", "keys": ["old-secret"]}]


class OrderedSlotTests(unittest.TestCase):
    def manager(self):
        from key_manager import SiliconFlowKeyManager
        manager = object.__new__(SiliconFlowKeyManager)
        manager._initialized = False
        manager.__init__([dict(provider_id=p, model=m, base_url=f"https://{p}.invalid",
                               keys=["offline-1", "offline-2"]) for p, m in
                          [("a", "same"), ("b", "same"), ("a", "second"), ("b", "second")]])
        return manager

    def test_keyword_exclusion_preserves_interleaved_order_and_is_request_local(self):
        manager = self.manager()
        tried, order = set(), []
        for _ in range(4):
            current = manager.get_next_for_request(tried_keys=tried)
            base, key, model, _, _, display, proxy, _ = current
            order.append(display)
            manager.exclude_model_slot(tried, base, key, model, proxy)
        self.assertEqual(order, ["a/same", "b/same", "a/second", "b/second"])
        self.assertIsNone(manager.get_next_for_request(tried_keys=tried))
        self.assertEqual(manager.get_next_for_request()[5], "a/same")

    def test_structure_change_clears_manual_but_transport_change_preserves(self):
        manager = self.manager()
        manager.start_index = 2
        endpoints = copy.deepcopy(manager.endpoints)
        endpoints[0].update(keys=["changed"], timeout_seconds=19, http_proxy="http://localhost:9")
        manager.set_endpoints(endpoints)
        self.assertEqual(manager.start_index, 2)
        self.assertEqual(manager._iter_slot_positions(), [2, 0, 1, 3])
        for changed in (endpoints + [dict(endpoints[0], model="new")], endpoints[:-1],
                        [endpoints[1], endpoints[0], *endpoints[2:]]):
            manager.start_index = 2
            manager.set_endpoints(changed)
            self.assertEqual(manager.start_index, 0)


class ProviderTests(unittest.TestCase):
    def test_legacy_only_migrates_when_providers_absent(self):
        for models in ({"planner_models": ["plan"]}, {"embedding_models": ["embed"]}):
            with self.subTest(models=models):
                cfg = {"llm_providers": [provider(**models)], "llm_endpoints": legacy()}
                providers, rotation = normalize_llm_providers_config(cfg)
                self.assertEqual(providers[0]["id"], "service")
                self.assertEqual(rotation, [])
                self.assertEqual(build_llm_endpoints_from_providers(cfg), [])
                self.assertEqual(normalize_llm_provider_rotation(cfg), [])
                setter = []
                self.assertEqual(force_apply_llm_endpoints_from_config({"Others": cfg}, setter.append), [])
                self.assertEqual(setter, [[]])
        self.assertEqual(normalize_llm_provider_rotation({"llm_providers": [], "llm_endpoints": legacy()}), [])
        migrated = {"llm_endpoints": legacy()}
        sync_provider_config(migrated)
        self.assertEqual(migrated["llm_providers"][0]["models"][0]["name"], "old-chat")
        self.assertEqual(migrated["llm_endpoints"][0]["model"], "old-chat")

    def test_planner_normalization_and_endpoint_shape(self):
        cfg = {"api_request_timeout_seconds": 77, "llm_providers": [provider(
            models=["chat"], planner_models=[
                {"model": " plan ", "enabled": "true", "timeout_seconds": "12.9"},
                {"name": "disabled", "enabled": "false"},
                {"name": "fallback", "timeout_seconds": "invalid"},
                {"name": "minimum", "timeout_seconds": -5},
                {"name": "nonfinite", "timeout_seconds": float("inf")},
                {"name": "plan"}, "", None,
            ], embedding_models=["embed"])]}
        sync_provider_config(cfg)
        normalized = cfg["llm_providers"][0]["planner_models"]
        self.assertEqual(normalized[0], {"name": "plan", "enabled": True, "timeout_seconds": 12})
        self.assertFalse(normalized[1]["enabled"])
        self.assertEqual([x["timeout_seconds"] for x in normalized[2:]], [77, 1, 77])
        self.assertEqual([x["model"] for x in cfg["llm_endpoints"]], ["chat"])
        self.assertEqual([x["model"] for x in build_all_provider_endpoints(cfg)], ["chat"])
        endpoints = build_planner_endpoints(cfg)
        self.assertEqual([x["model"] for x in endpoints], ["plan", "fallback", "minimum", "nonfinite"])
        self.assertEqual(set(endpoints[0]), set(cfg["llm_endpoints"][0]))
        self.assertEqual(endpoints[0]["provider_id"], "service")
        self.assertEqual(endpoints[0]["display_model"], "service/plan")
        self.assertEqual(endpoints[0]["http_proxy"], "http://127.0.0.1:7890")
        self.assertEqual(endpoints[0]["keys"], ["offline-secret-key"])
        # A separate subclass avoids touching the live chat key-manager singleton.
        from key_manager import SiliconFlowKeyManager
        class IsolatedPlannerManager(SiliconFlowKeyManager):
            _instance = None
        manager = IsolatedPlannerManager(endpoints=endpoints)
        self.assertEqual(manager.key_list[0]["display_model"], "service/plan")
        self.assertEqual(manager.key_list[0]["timeout_seconds"], 12)


class WebUIConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.config = self.base / "config.json"
        self.plugins = self.base / "plugins"
        self.plugins.mkdir()
        self.original = {"untouched": {"tokenless": [1, 2]}, "Others": {},
                         "PluginConfigs": {"unloaded": {"old": {"arbitrary": True}},
                                           "weather": {"legacy_option": "keep"}}}
        self.config.write_text(json.dumps(self.original), encoding="utf-8")
        for name, value in (("CONFIG_PATH", self.config), ("PLUGIN_DIR", self.plugins),
                            ("LEGACY_CONFIG_PATHS", []), ("_plugin_schemas_callback", None),
                            ("_config_saved_callback", None)):
            p = patch.object(webui, name, value)
            p.start()
            self.addCleanup(p.stop)
        for name in ("force_apply_llm_endpoints_from_config", "_apply_webui_runtime_update"):
            p = patch.object(webui, name)
            p.start()
            self.addCleanup(p.stop)
        self.schema = {"default_city": {"type": "string", "default": "北京"},
                       "cache_minutes": {"type": "number", "min": 0, "max": 1440, "default": 30},
                       "show_detail": {"type": "bool", "default": False},
                       "mode": {"type": "select", "options": ["brief", "full"]},
                       "cities": {"type": "list"},
                       "count": {"type": "int", "min": 1, "max": 3}}

    def metadata(self, name, text):
        directory = self.plugins / name
        directory.mkdir()
        (directory / "metadata.yaml").write_text(text, encoding="utf-8")

    def runtime_schema(self):
        webui.set_plugin_schemas_callback(lambda: {"weather": {"schema": self.schema, "values": {"default_city": "上海"}}})

    def test_callback_overrides_yaml_and_supports_module_plugin(self):
        self.metadata("weather", "config_schema:\n  default_city:\n    type: number\n    default: 999\n")
        self.runtime_schema()
        webui.set_plugin_schemas_callback(lambda: {
            "weather": {"schema": self.schema, "values": {"default_city": "上海"}},
            "module_plugin": {"schema": {"active": {"type": "bool", "default": True}}},
            "no_schema": {}, "d_disabled": {"schema": self.schema}})
        result = webui._collect_plugin_schemas()
        self.assertEqual(result["weather"]["schema"]["default_city"]["type"], "string")
        self.assertEqual(result["weather"]["values"]["default_city"], "上海")
        self.assertTrue(result["module_plugin"]["values"]["active"])
        self.assertEqual(result["no_schema"], {"schema": {}, "values": {}})
        self.assertNotIn("d_disabled", result)
        webui.set_plugin_schemas_callback(lambda: {})
        self.assertEqual(webui._collect_plugin_schemas(), {})

    def test_standalone_yaml_only_no_plugin_execution(self):
        self.metadata("weather", "config_schema:\n  default_city:\n    type: string\n    default: 北京\n")
        self.metadata("empty", "name: empty\n")
        self.metadata("d_disabled", "config_schema: {}\n")
        self.metadata("__pycache__", "config_schema: {}\n")
        (self.plugins / "weather" / "setup.py").write_text("raise RuntimeError('never import')", encoding="utf-8")
        (self.plugins / "single.py").write_text("raise RuntimeError('never import')", encoding="utf-8")
        result = webui._collect_plugin_schemas()
        self.assertEqual(set(result), {"weather", "empty", "single"})
        self.assertEqual(result["single"], {"schema": {}, "values": {}})
        self.assertEqual(result["empty"]["schema"], {})
        self.assertEqual(result["weather"]["values"]["default_city"], "北京")

    def test_save_roundtrip_preserves_top_level_unloaded_and_planner(self):
        self.runtime_schema()
        cfg = {"Others": {"llm_providers": [provider(planner_models=[
            {"model": " plan ", "enabled": "on", "timeout_seconds": "19"}])], "llm_endpoints": legacy()},
               "PluginConfigs": {"weather": {"default_city": "上海", "cache_minutes": 0, "show_detail": True}},
               "ProactiveReply": {"group": {"enabled": True, "whitelist": [123, "00123"]}}}
        webui.save_config_bundle({"config_json": cfg})
        saved = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(saved["untouched"], self.original["untouched"])
        self.assertEqual(saved["PluginConfigs"]["unloaded"], self.original["PluginConfigs"]["unloaded"])
        self.assertEqual(saved["PluginConfigs"]["weather"]["legacy_option"], "keep")
        self.assertEqual(saved["Others"]["llm_endpoints"], [])
        self.assertEqual(saved["Others"]["llm_rotation"], [])
        self.assertEqual(saved["Others"]["llm_providers"][0]["planner_models"],
                         [{"name": "plan", "enabled": True, "timeout_seconds": 19}])
        self.assertEqual(saved["ProactiveReply"]["group"]["whitelist"], ["123"])
        self.assertFalse(saved["ProactiveReply"]["private"]["enabled"])
        self.assertEqual(webui._collect_plugin_schemas()["weather"]["values"]["default_city"], "上海")
        webui.save_config_bundle({"config_json": saved})
        self.assertEqual(json.loads(self.config.read_text(encoding="utf-8")), saved)

    def test_legacy_reply_quote_false_survives_unrelated_save_and_modern_takes_priority(self):
        for legacy_value in (False, "false", 0):
            with self.subTest(legacy=legacy_value):
                initial = copy.deepcopy(self.original)
                initial["split_reply_quote"] = {"default_enabled": legacy_value}
                self.config.write_text(json.dumps(initial), encoding="utf-8")
                self.assertFalse(webui.collect_config_bundle()["feature_switches"]["split_reply_quote"])
                webui.save_config_bundle({"config_json": {"Others": {"bot_name": "离线测试"}}})
                saved = json.loads(self.config.read_text(encoding="utf-8"))
                self.assertFalse(saved["FeatureSwitches"]["split_reply_quote"])
                self.assertNotIn("split_reply_quote", {k: v for k, v in saved.items() if k != "FeatureSwitches"})
                initial["FeatureSwitches"] = {"split_reply_quote": True}
                self.config.write_text(json.dumps(initial), encoding="utf-8")
                self.assertTrue(webui.collect_config_bundle()["feature_switches"]["split_reply_quote"])
                webui.save_config_bundle({"config_json": {"Others": {"bot_name": "仍保持开启"}}})
                self.assertTrue(json.loads(self.config.read_text(encoding="utf-8"))["FeatureSwitches"]["split_reply_quote"])

    def test_legacy_proactive_account_id_removed_and_proactive_limits_exposed(self):
        fields = next(section["fields"] for section in webui.build_ui_schema({})
                      if section["key"] == "proactive")
        by_path = {field["path"]: field for field in fields}
        self.assertNotIn("ProactiveReply.legacy_account_id", by_path)
        self.assertNotIn("ProactiveReply.max_account_background_calls_per_day", by_path)
        self.assertEqual(by_path["ProactiveReply.group.max_contacts_per_day"]["default"], 0)
        self.assertEqual(by_path["ProactiveReply.group.max_contacts_per_day"]["min"], 0)
        self.assertEqual(by_path["ProactiveReply.private.max_contacts_per_day"]["default"], 0)
        self.assertEqual(by_path["ProactiveReply.private.max_contacts_per_day"]["min"], 0)
        webui.save_config_bundle({"config_json": {"ProactiveReply": {
            "legacy_account_id": "999", "private": {"enabled": False, "max_contacts_per_day": 5}}}})
        saved = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertNotIn("legacy_account_id", saved["ProactiveReply"])
        self.assertEqual(saved["ProactiveReply"]["private"]["max_contacts_per_day"], 5)
        self.assertFalse(saved["ProactiveReply"]["private"]["enabled"])

    def test_memory_schema_migration_and_lowering_roundtrip(self):
        from webui_core.features import compression_trigger_rounds, DEFAULT_FEATURE_SWITCHES
        self.assertEqual(compression_trigger_rounds({"compression_threshold": 60, "auto_compress_after_messages": 44}), 60)
        self.assertEqual(compression_trigger_rounds({"auto_compress_after_messages": 24}), 24)
        schema = {s["key"]: s for s in webui.build_ui_schema({})}
        ai_paths = {f["path"] for f in schema["ai"]["fields"]}
        self.assertTrue({"Others.auto_compress_after_messages", "Others.compression_keep_recent", "Others.context_max_messages"} <= ai_paths)
        self.assertFalse(any("compress" in f["path"] or "summary_per_day_limit" in f["path"] for f in schema["features"]["fields"]))
        for key in ("weather", "check_account", "image_generation", "quote"):
            self.assertIn(key, DEFAULT_FEATURE_SWITCHES)
        webui.save_config_bundle({"config_json": {"Others": {"compression_threshold": 60, "auto_compress_after_messages": 44}, "FeatureSwitches": {"weather": False}}})
        saved = json.loads(self.config.read_text(encoding="utf-8"))
        # 默认最大上下文轮数为 60，触发轮数 >= 最大轮数时会自动压低到最大轮数 - 1（59）以保证先压缩后截断
        self.assertEqual(saved["Others"]["auto_compress_after_messages"], 59)
        self.assertNotIn("compression_threshold", saved["Others"])
        self.assertFalse(saved["FeatureSwitches"]["weather"])
        # Even an unmigrated file must permit lowering through the UI.
        saved["Others"]["compression_threshold"] = 80
        self.config.write_text(json.dumps(saved), encoding="utf-8")
        self.assertEqual(webui.get_ui_value({"config_json": saved}, "Others.auto_compress_after_messages"), 80)
        webui.save_ui_state({"form_values": {"Others.auto_compress_after_messages": 25}})
        webui.save_ui_state({"form_values": {"Others.compression_keep_recent": 10}})
        saved = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(saved["Others"]["auto_compress_after_messages"], 25)
        self.assertNotIn("compression_threshold", saved["Others"])

    def test_legacy_save_still_migrates(self):
        webui.save_config_bundle({"config_json": {"Others": {"llm_endpoints": legacy()}}})
        saved = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(saved["Others"]["llm_providers"][0]["models"][0]["name"], "old-chat")

    def test_explicit_empty_providers_clear_stale_chat_cache(self):
        webui.save_config_bundle({"config_json": {"Others": {
            "llm_providers": [], "llm_endpoints": legacy()}}})
        saved = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(saved["Others"]["llm_providers"], [])
        self.assertEqual(saved["Others"]["llm_rotation"], [])
        self.assertEqual(saved["Others"]["llm_endpoints"], [])

    def test_invalid_plugin_input_never_writes(self):
        self.runtime_schema()
        cases = [("cache_minutes", -1), ("cache_minutes", 1441), ("cache_minutes", True),
                 ("cache_minutes", float("nan")), ("cache_minutes", "30"),
                 ("show_detail", "false"), ("default_city", []), ("mode", "unknown"),
                 ("cities", "北京"), ("cities", [1]), ("count", 1.5),
                 ("bad.path", "escape")]
        before = self.config.read_bytes()
        for key, value in cases:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                webui.save_config_bundle({"config_json": {"PluginConfigs": {"weather": {key: value}}}})
            self.assertEqual(self.config.read_bytes(), before)
        with self.assertRaises(ValueError):
            webui.save_config_bundle({"config_json": {"PluginConfigs": {"../outside": {"x": 1}}}})
        self.assertEqual(self.config.read_bytes(), before)
        webui.save_config_bundle({"config_json": {"PluginConfigs": {"weather": {
            "cities": ["北京"], "mode": "brief", "count": 2, "unknown": {"keep": True}}}}})
        self.assertTrue(json.loads(self.config.read_text(encoding="utf-8"))["PluginConfigs"]["weather"]["unknown"]["keep"])

    def test_invalid_proactive_never_writes(self):
        before = self.config.read_bytes()
        for raw in ([], {"group": []}, {"private": {"enabled": "true"}},
                    {"group": {"trigger_probability": 1.1}},
                    {"group": {"check_interval_seconds": 1}},
                    {"group": {"max_consecutive_bot_messages": 1.5}},
                    {"private": {"whitelist": "123"}},
                    {"private": {"blacklist": [False]}},
                    {"group": {"trigger_probability": float("inf")}}):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                webui.save_config_bundle({"config_json": {"ProactiveReply": raw}})
            self.assertEqual(self.config.read_bytes(), before)

    def test_form_paths_checked_before_deep_set(self):
        before = self.config.read_bytes()
        for path in ("PluginConfigs.weather.default_city.extra", "PluginConfigs...enabled",
                     "PluginConfigs", "ProactiveReply.group.enabled.extra"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                webui.save_ui_state({"form_values": {path: True}})
            self.assertEqual(self.config.read_bytes(), before)
        self.runtime_schema()
        webui.save_ui_state({"form_values": {"PluginConfigs.weather.default_city": "杭州"}})
        self.assertEqual(json.loads(self.config.read_text(encoding="utf-8"))["PluginConfigs"]["weather"]["default_city"], "杭州")

    def test_store_yaml_top_level_string_whitelist_and_disabled_filter(self):
        text = '''name: forged
path: ../../escape
installed: true
version: "1.2.3"
author: "Alice: Bob"
description: >-
  first line
  second line
entry: setup.py
config_schema:
  name: nested-name
  path: nested-path
  installed: nested-installed
'''
        self.metadata("local", text)
        self.metadata("d_disabled", "name: hidden")
        self.metadata("__pycache__", "name: hidden")
        archive = io.BytesIO()
        prefix = webui.PLUGIN_STORE_REPO.split("/")[-1] + "-main/plugins/"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr(prefix + "remote/metadata.yaml", text)
        response = SimpleNamespace()
        class Response:
            def __enter__(self):
                return response
            def __exit__(self, *args):
                pass
        with patch.object(webui, "_github_accelerated_urls", return_value=["https://offline.invalid"]), \
             patch.object(webui, "_make_opener") as opener, \
             patch.object(webui, "_read_capped_response", return_value=archive.getvalue()):
            opener.return_value.open.return_value = Response()
            result = {x["name"]: x for x in webui._store_registry()}
        self.assertEqual(set(result), {"local", "remote"})
        self.assertEqual(result["remote"]["path"], "plugins/remote")
        self.assertFalse(result["remote"]["installed"])
        self.assertTrue(result["local"]["installed"])
        self.assertEqual(result["local"]["path"], "")
        self.assertEqual(result["remote"]["description"], "first line second line")
        self.assertEqual(result["remote"]["author"], "Alice: Bob")
        self.assertNotIn("config_schema", result["remote"])
        self.assertEqual(webui._store_metadata("version: 123\nauthor: [bad]"), {})


class WeatherTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        spec = importlib.util.spec_from_file_location("offline_weather", ROOT / "plugins/weather/setup.py")
        self.weather = importlib.util.module_from_spec(spec)
        self.weather.XCBOT_VERSION = "v4.0"
        spec.loader.exec_module(self.weather)
        self.calls = []
        owner = self
        class Response:
            status = 200
            def __init__(self, value):
                self.value = value
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            async def json(self):
                return copy.deepcopy(self.value)
        class Session:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            def get(self, url, params, **kwargs):
                owner.calls.append((url, params))
                if url == owner.weather.GEO_API:
                    return Response({"results": [{"name": params["name"], "country_code": "CN", "latitude": 1, "longitude": 2}]})
                return Response({"current": {"temperature_2m": 21, "relative_humidity_2m": 70,
                                              "weather_code": 0, "wind_speed_10m": 8},
                                 "daily": {"time": ["2026-10-26"], "weather_code": [0],
                                           "temperature_2m_max": [25], "temperature_2m_min": [15]}})
        p = patch.object(self.weather.aiohttp, "ClientSession", side_effect=lambda **kwargs: Session())
        p.start()
        self.addCleanup(p.stop)

    async def test_cache_ttl_city_and_detail_changes_behavior(self):
        w = self.weather
        with patch.object(w.time, "monotonic", return_value=100):
            brief = await w._get_weather("北京", str, None, cache_minutes=30, show_detail=False)
            detailed = await w._get_weather("北京", str, None, cache_minutes=30, show_detail=True)
        self.assertEqual(len(self.calls), 2)
        self.assertNotIn("湿度", brief)
        self.assertNotIn("风速", brief)
        self.assertIn("湿度", detailed)
        self.assertIn("风速", detailed)
        self.assertIn("21°C", brief)
        self.assertIn("今天", brief)
        with patch.object(w.time, "monotonic", return_value=161):
            await w._get_weather("北京", str, None, cache_minutes=1)
        self.assertEqual(len(self.calls), 4)
        await w._get_weather("上海", str, None, cache_minutes=30)
        self.assertEqual(len(self.calls), 6)
        await w._get_weather("上海", str, None, cache_minutes=0)
        await w._get_weather("上海", str, None, cache_minutes=0)
        self.assertEqual(len(self.calls), 10)

    async def test_cache_is_bounded(self):
        w = self.weather
        with patch.object(w, "_CACHE_MAX_CITIES", 2):
            for city in ("北京", "上海", "杭州"):
                await w._get_weather(city, str, None)
        self.assertEqual(list(w._weather_cache), ["上海", "杭州"])
        self.assertEqual(w._cache_ttl(99999), 1440 * 60)
        self.assertEqual(w._cache_ttl(-1), 0)

    async def test_default_city_explicit_city_and_empty_prompt(self):
        w = self.weather
        actions = SimpleNamespace(send=AsyncMock())
        manager = SimpleNamespace(Message=lambda x: x)
        segments = SimpleNamespace(Text=lambda x: x)
        cfg = {"default_city": "上海", "cache_minutes": 0, "show_detail": False}
        async def send(order):
            return await w.on_message(order, actions, manager, segments, "bot", "/", user_id=123,
                                      get_plugin_config=lambda key, default: cfg.get(key, default))
        self.assertTrue(await send("天气"))
        self.assertEqual(self.calls[0][1]["name"], "上海")
        self.assertNotIn("湿度", actions.send.call_args.kwargs["message"])
        cfg["default_city"] = "北京"
        await send("天气")
        self.assertEqual(self.calls[2][1]["name"], "北京")
        cfg["show_detail"] = True
        await send("天气 杭州")
        self.assertEqual(self.calls[4][1]["name"], "杭州")
        self.assertIn("湿度", actions.send.call_args.kwargs["message"])
        cfg["default_city"] = ""
        calls = len(self.calls)
        await send("天气")
        self.assertEqual(len(self.calls), calls)
        self.assertIn("请指定城市名称", actions.send.call_args.kwargs["message"])
        self.assertFalse(await send("天气真好"))


class MemoryRuntimeTests(unittest.TestCase):
    def test_response_metadata_and_actual_hot_apply_cancel_only_closed_type(self):
        from bot import proactive_reply
        from webui_core.features import FEATURE_META
        meta = {item["key"]: item for item in FEATURE_META}
        for key in ("group_chat", "private_chat"):
            self.assertIn("所有响应总开关", meta[key]["desc"])
            self.assertIn("WebUI", meta[key]["desc"])
        for key in ("ai_chat", "compression_commands", "quote", "plugin_admin_commands"):
            self.assertIn(key, meta)  # 旧键保留，隐藏 UI 不删除后端语义。
        tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "apply_runtime_config")
        config = SimpleNamespace(log_level="info")
        env = {"RUNTIME_CONNECTION_SNAPSHOT": {}, "read_runtime_config": lambda: {},
            "Configurator": SimpleNamespace(Config=lambda **kwargs: SimpleNamespace(load_from_file=lambda: {}),
                ConfigManager=lambda raw: SimpleNamespace(get_cfg=lambda: config)),
            "CONFIG_FILE": "offline-unused", "load_user_cfg": lambda: {"bot_name": "fake", "bot_name_en": "fake", "version_name": "test", "reminder": "/"},
            "get_connection_signature": lambda raw: {}, "normalize_seconds_config": lambda value, **kwargs: value,
            "normalize_bool_config": lambda value, **kwargs: value,
            "apply_api_rotation_settings": lambda *args, **kwargs: None,
            "close_runtime_llm_clients": lambda: None, "logger": SimpleNamespace(set_level=lambda *args: None),
            "is_feature_enabled": lambda key, default=True: key not in ("group_chat", "plugins_external"),
            "loaded_plugins": [], "disabled_plugins": [], "failed_plugins": [],
            "traceback": SimpleNamespace(print_exc=lambda: None)}
        exec(compile(ast.Module(body=[node], type_ignores=[]), "hot-apply-offline", "exec"), env)
        with patch.object(proactive_reply, "cancel_observations") as cancel:
            self.assertTrue(env["apply_runtime_config"](False))
        cancel.assert_called_once_with("response_disabled", kind="group")

    def test_compression_uses_one_config_and_manual_switch_does_not_gate_auto(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ContextCompressor")
        gate = next(n for n in cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_compress_locked")
        gate_source = ast.get_source_segment(source, gate)
        self.assertIn("msg_count < self._effective_threshold(context)", gate_source)
        self.assertNotIn('user_cfg.get("compression_threshold"', source)
        # Find the actual scheduling condition rather than importing live main.
        start = source.index("                            self.auto_compress_enabled and")
        condition = source[start - 60:source.index("already_locked=True", start)]
        self.assertNotIn('is_feature_enabled("compression_commands"', condition)
        self.assertIn("clamp_compression_trigger_rounds", condition)
        self.assertNotIn("def set_auto_compress(self, enabled: bool, threshold", source)

    def test_group_context_defaults_enabled_without_overriding_explicit_false(self):
        from webui_core.features import DEFAULT_FEATURE_SWITCHES
        defaults = json.loads((ROOT / "config.default.json").read_text(encoding="utf-8-sig"))
        self.assertTrue(DEFAULT_FEATURE_SWITCHES["group_chat_context"])
        self.assertTrue(defaults["FeatureSwitches"]["group_chat_context"])
        merged = dict(DEFAULT_FEATURE_SWITCHES)
        merged.update({"group_chat_context": False})
        self.assertFalse(merged["group_chat_context"])
        tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
        method = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                      and node.name == "is_group_chat_context_enabled")
        calls = []
        env = {"is_feature_enabled": lambda key, default: calls.append((key, default)) or default}
        exec(compile(ast.Module(body=[method], type_ignores=[]), "context-default-offline", "exec"), env)
        self.assertTrue(env["is_group_chat_context_enabled"]())
        self.assertEqual(calls, [("group_chat_context", True)])

    def test_persona_keeps_custom_content_and_adds_conversation_continuity(self):
        tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                   and node.name == "EnhancedContextManager")
        method = next(node for node in cls.body if isinstance(node, ast.FunctionDef)
                      and node.name == "_get_system_prompt")
        cfg = {"personality_presets": [{"id": "test", "prompt": "{bot_name}用简短语气和{user_name}聊天。"}],
               "active_personality_preset": "test"}
        env = {"bot_name": "测试角色", "get_runtime_others": lambda: cfg, "user_cfg": {}}
        exec(compile(ast.Module(body=[method], type_ignores=[]), "persona-offline", "exec"), env)
        prompt = env["_get_system_prompt"](object(), "测试用户", "private")
        self.assertTrue(prompt.startswith("测试角色用简短语气和测试用户聊天。"))
        self.assertIn("【对话衔接】", prompt)
        self.assertIn("不必每次以问题", prompt)
        self.assertIn("不重复追问", prompt)
        self.assertEqual(cfg["personality_presets"][0]["prompt"], "{bot_name}用简短语气和{user_name}聊天。")

    def test_summary_unlimited_preserves_per_group_inflight_slot(self):
        tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
        names = {"can_summary_today", "try_begin_summary", "end_summary"}
        nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
        env = {"date": date, "daily_summary_records": defaultdict(lambda: defaultdict(int)),
               "SUMMARY_PER_DAY_LIMIT": 1, "_summary_inflight_lock": threading.Lock(),
               "_summary_inflight_groups": set()}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "main.py", "exec"), env)
        env["daily_summary_records"]["g"][date.today().isoformat()] = 999
        self.assertTrue(env["can_summary_today"]("g")[0])
        self.assertTrue(env["try_begin_summary"]("g")[0])
        self.assertFalse(env["try_begin_summary"]("g")[0])
        self.assertTrue(env["try_begin_summary"]("other")[0])
        env["end_summary"]("g")
        self.assertTrue(env["try_begin_summary"]("g")[0])

    def test_stats_restore_never_replaces_user_settings(self):
        tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "load_compression_stats")
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "data/compression"
            target.mkdir(parents=True)
            (target / "compression_stats.json").write_text(json.dumps({"threshold": 99, "keep_recent": 80, "sessions": {"g": 3}, "last_compression_times": {"g": 123}}))
            import os
            env = {"os": os, "json": json, "BASE_DIR": directory}
            exec(compile(ast.Module(body=[node], type_ignores=[]), "main.py", "exec"), env)
            compressor = SimpleNamespace(compression_threshold=25, keep_recent=10, compression_count={}, last_compression_time={})
            self.assertTrue(env["load_compression_stats"](compressor))
            self.assertEqual((compressor.compression_threshold, compressor.keep_recent), (25, 10))
            self.assertEqual(compressor.compression_count, {"g": 3})
            self.assertEqual(compressor.last_compression_time, {"g": 123})


class ImportSafetyTests(unittest.TestCase):
    def test_safe_import_does_not_start_threads_server_or_main(self):
        code = '''import sys, socket, threading
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
with patch.object(threading.Thread, "start", side_effect=AssertionError("thread started")), patch.object(socket.socket, "bind", side_effect=AssertionError("server bound")):
    import bot.llm_config
    import bot.proactive_reply
    import webui
    assert webui._server is None and webui._server_thread is None
    assert "main" not in sys.modules
    assert "hyper" not in sys.modules
print("safe import ok")
'''
        result = subprocess.run([sys.executable, "-c", code, str(ROOT)], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("safe import ok", result.stdout)

    def test_config_migrate_upgrades_untouched_old_proactive_defaults_and_cleans_dead_keys(self):
        import config_migrate
        template = json.loads((ROOT / "config.default.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            cfg_path = base / "config.json"
            backup_dir = base / "config_backup"
            old_cfg = {
                "config_version": 7,
                "Others": {"version_name": "4.0.0", "bot_name": "老机器人"},
                "RoleLife": {
                    "roles": {
                        "999": {
                            "default": {"enabled": True, "role_daily_limit": 24, "weather_city": "上海", "sleep_hours": 7}
                        }
                    }
                },
                "ProactiveReply": {
                    "max_account_background_calls_per_day": 192,
                    "legacy_account_id": "999",
                    "group": {
                        "enabled": True,
                        "trigger_probability": 0.3,          # 旧默认 -> 应升级为 0.25
                        "check_interval_seconds": 60,        # 旧默认 -> 应升级为 180
                        "idle_threshold_seconds": 1200,      # 用户自定义 -> 必须保留 1200！
                        "min_interval_between_proactive_seconds": 1800,  # 旧默认 -> 3600
                        "max_contacts_per_day": 6,           # 旧默认 -> 0（不限制）
                        "max_background_calls_per_day": 48,  # 已废除 -> 清理
                        "observation": {
                            "enabled": True,
                            "trigger_probability": 0.5,      # 旧默认 -> 0.35
                            "window_seconds": 300,
                            "max_continuation_rounds": 3,
                        },
                    },
                    "private": {
                        "enabled": False,
                        "trigger_probability": 0.1,          # 旧默认 -> 0.2
                        "check_interval_seconds": 120,       # 旧默认 -> 180
                        "idle_threshold_seconds": 43200,     # 旧默认 -> 7200
                        "min_interval_between_proactive_seconds": 86400,  # 旧默认 -> 10800
                        "max_contacts_per_day": 6,           # 旧默认 -> 0（不限制）
                        "max_background_calls_per_day": 48,  # 已废除 -> 清理
                        "observation": {"enabled": True, "trigger_probability": 0.5},
                    },
                },
            }
            cfg_path.write_text(json.dumps(old_cfg, ensure_ascii=False, indent=2), encoding="utf-8")
            tpl_path = base / "config.default.json"
            tpl_path.write_text(json.dumps(template, ensure_ascii=False, indent=2), encoding="utf-8")
            self.assertTrue(config_migrate.ensure_config_up_to_date(str(cfg_path), str(tpl_path), str(backup_dir)))
            self.assertTrue(any(backup_dir.iterdir()))
            migrated = json.loads(cfg_path.read_text(encoding="utf-8"))
            self.assertGreaterEqual(migrated["config_version"], 9)
            self.assertEqual(migrated["Others"]["bot_name"], "老机器人")
            # 清理废字段
            self.assertNotIn("max_account_background_calls_per_day", migrated["ProactiveReply"])
            self.assertNotIn("legacy_account_id", migrated["ProactiveReply"])
            self.assertNotIn("max_background_calls_per_day", migrated["ProactiveReply"]["group"])
            self.assertNotIn("max_background_calls_per_day", migrated["ProactiveReply"]["private"])
            self.assertNotIn("window_seconds", migrated["ProactiveReply"]["group"]["observation"])
            self.assertNotIn("max_continuation_rounds", migrated["ProactiveReply"]["group"]["observation"])
            role_cfg = migrated["RoleLife"]["roles"]["999"]["default"]
            self.assertNotIn("role_daily_limit", role_cfg)
            self.assertNotIn("weather_city", role_cfg)
            self.assertEqual(role_cfg["sleep_hours"], 7)
            # 未自定义的旧默认被升级，用户自定义 1200 原封不动保留
            g = migrated["ProactiveReply"]["group"]
            self.assertEqual(g["trigger_probability"], 0.25)
            self.assertEqual(g["check_interval_seconds"], 180)
            self.assertEqual(g["idle_threshold_seconds"], 1200)
            self.assertEqual(g["min_interval_between_proactive_seconds"], 3600)
            self.assertEqual(g["max_contacts_per_day"], 0)
            self.assertEqual(g["observation"]["trigger_probability"], 0.35)
            self.assertTrue(g["observation"]["enabled"])
            p = migrated["ProactiveReply"]["private"]
            self.assertEqual(p["trigger_probability"], 0.2)
            self.assertEqual(p["check_interval_seconds"], 180)
            self.assertEqual(p["idle_threshold_seconds"], 7200)
            self.assertEqual(p["min_interval_between_proactive_seconds"], 10800)
            self.assertEqual(p["max_contacts_per_day"], 0)
            self.assertEqual(p["observation"]["trigger_probability"], 0.35)
            self.assertFalse(p["observation"]["enabled"])
            # 升到 v9 后如果用户在 WebUI 手动开启私聊观察与设置上限 10，再次启动迁移不会重置回去
            migrated["ProactiveReply"]["private"]["observation"]["enabled"] = True
            migrated["ProactiveReply"]["private"]["max_contacts_per_day"] = 10
            cfg_path.write_text(json.dumps(migrated, ensure_ascii=False, indent=2), encoding="utf-8")
            self.assertFalse(config_migrate.ensure_config_up_to_date(str(cfg_path), str(tpl_path), str(backup_dir)))
            reloaded = json.loads(cfg_path.read_text(encoding="utf-8"))
            self.assertTrue(reloaded["ProactiveReply"]["private"]["observation"]["enabled"])
            self.assertEqual(reloaded["ProactiveReply"]["private"]["max_contacts_per_day"], 10)
            # v8 旧默认 private=4 且用户把 group 改为 10 时：升级到 v9 会保留 10 并把 4 改为 0
            v8_cfg = copy.deepcopy(migrated)
            v8_cfg["config_version"] = 8
            v8_cfg["ProactiveReply"]["group"]["max_contacts_per_day"] = 10
            v8_cfg["ProactiveReply"]["private"]["max_contacts_per_day"] = 4
            v8_cfg["ProactiveReply"]["private"]["max_background_calls_per_day"] = 200
            cfg_path.write_text(json.dumps(v8_cfg, ensure_ascii=False, indent=2), encoding="utf-8")
            self.assertTrue(config_migrate.ensure_config_up_to_date(str(cfg_path), str(tpl_path), str(backup_dir)))
            upgraded_v9 = json.loads(cfg_path.read_text(encoding="utf-8"))
            self.assertEqual(upgraded_v9["ProactiveReply"]["group"]["max_contacts_per_day"], 10)
            self.assertEqual(upgraded_v9["ProactiveReply"]["private"]["max_contacts_per_day"], 0)
            self.assertNotIn("max_background_calls_per_day", upgraded_v9["ProactiveReply"]["private"])
            # 高于模板版本（如未来降级回旧代码启动）的 config_version 不能被降级回 9
            future_cfg = copy.deepcopy(migrated)
            future_cfg["config_version"] = 99
            future_cfg["ProactiveReply"]["private"].pop("max_contacts_per_day", None)
            cfg_path.write_text(json.dumps(future_cfg, ensure_ascii=False, indent=2), encoding="utf-8")
            self.assertTrue(config_migrate.ensure_config_up_to_date(str(cfg_path), str(tpl_path), str(backup_dir)))
            reloaded_future = json.loads(cfg_path.read_text(encoding="utf-8"))
            self.assertEqual(reloaded_future["config_version"], 99)
            old_future = base / "config-old-future.json"
            new_future = base / "config-new-future.json"
            old_future.write_text(json.dumps(future_cfg, ensure_ascii=False, indent=2), encoding="utf-8")
            new_future.write_text(json.dumps(template, ensure_ascii=False, indent=2), encoding="utf-8")
            config_migrate.migrate(str(old_future), str(new_future), str(backup_dir))
            direct_cfg = json.loads(new_future.read_text(encoding="utf-8"))
            self.assertEqual(direct_cfg["config_version"], 99)


if __name__ == "__main__":
    unittest.main()
