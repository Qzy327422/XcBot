"""角色生活 WebUI 后端：鉴权、隔离、并发版本、幂等与配置保存。

全部离线：临时 config.json + 假 runtime，不启动服务器、不连接 QQ、不调用模型。
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import webui

ACCOUNT = "123456"
OTHER_ACCOUNT = "654321"
ROLE = "role-a"
OTHER_ROLE = "role-b"


def role_cfg(**kwargs):
    base = dict(webui._role_life_module().DEFAULT_ROLE_CONFIG)
    base.update({"enabled": True})
    base.update(kwargs)
    return base


class FakeRuntime:
    """按 life-core 最终签名记录调用并模拟 CAS。"""

    def __init__(self, *, current=ACCOUNT, connected=True, corrupt=False,
                 enabled=True):
        self.current = current
        self.is_connected = connected
        self.corrupt = corrupt
        self.enabled = enabled
        self.calls = []
        self.status_data = {
            "enabled": enabled, "available": True, "corrupt": corrupt,
            "account": ACCOUNT, "role_id": ROLE,
            "revision": "rev-1", "epoch": 2, "state_version": 7,
            "last_advance": 100.0, "date": "2026-03-10", "sleeping": False,
            "plan": {"plan_id": "p1", "serial": 1, "items": []},
            "mood": {}, "recent": [], "usage": {"role_24h": 1, "role_limit": 24,
                                                "account_limit": 192, "account_id": ACCOUNT},
            "queue": 0, "jobs": [], "metrics": {}, "config": role_cfg(),
            "next_node": None,
        }
        if corrupt:
            self.status_data.update({"available": False, "error": "state identity mismatch",
                                     "jobs": [], "metrics": {},
                                     "budget_unknown": True,
                                     "budget_unknown_until": 2000000000.0,
                                     "usage": {"role_24h": None, "role_limit": 24,
                                               "account_limit": 192, "corrupt": True}})
            # 真实 engine.status 在损坏分支不返回 revision/epoch/state_version
            for key in ("revision", "epoch", "state_version"):
                self.status_data.pop(key, None)

    # -- runtime protocol --
    def connected(self):
        return self.is_connected

    def accounts(self):
        return {"current": self.current, "known": [ACCOUNT, OTHER_ACCOUNT],
                "roles": {ACCOUNT: [ROLE], OTHER_ACCOUNT: [OTHER_ROLE]}}

    def status(self, account, role_id):
        self.calls.append(("status", account, role_id))
        data = dict(self.status_data)
        data.update({"account": account, "role_id": role_id})
        return data

    def records(self, account, role_id, kind, page, page_size):
        self.calls.append(("records", account, role_id, kind, page, page_size))
        return {"items": [], "page": page, "page_size": page_size,
                "total": 0, "pages": 1}

    def _cas(self, version):
        revision, epoch, state_version = version
        return (revision == "rev-1" and epoch == 2 and state_version == 7)

    def enqueue(self, account, role_id, kind, request_id, revision, epoch,
                state_version, args=None):
        self.calls.append(("enqueue", account, role_id, kind, request_id,
                           revision, epoch, state_version, args))
        if not self._cas((revision, epoch, state_version)):
            return {"job_id": None, "error": "stale"}
        return {"job_id": f"job-{kind}", "error": ""}

    def retry(self, account, role_id, job_id, request_id, revision, epoch,
              state_version):
        self.calls.append(("retry", account, role_id, job_id, request_id))
        if not self._cas((revision, epoch, state_version)):
            return {"job_id": None, "error": "stale"}
        return {"job_id": "job-retry", "error": ""}

    def regenerate_plan(self, account, role_id, request_id, revision, epoch,
                        state_version):
        self.calls.append(("regenerate_plan", account, role_id, request_id))
        if not self._cas((revision, epoch, state_version)):
            return {"job_id": None, "error": "stale"}
        return {"job_id": "job-plan", "error": ""}

    def clear(self, account, role_id, request_id, revision, epoch,
              state_version, allow_corrupt):
        self.calls.append(("clear", account, role_id, request_id, revision,
                           epoch, state_version, allow_corrupt))
        if self.corrupt:
            return {"ok": bool(allow_corrupt), "error": "" if allow_corrupt else "corrupt"}
        if not self._cas((revision, epoch, state_version)):
            return {"ok": False, "error": "stale"}
        return {"ok": True, "error": ""}


class RoleLifeWebUITest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.config_path = base / "config.json"
        self.original = {
            "Others": {
                "personality_presets": [
                    {"id": ROLE, "name": "甲", "prompt": "甲人设"},
                    {"id": OTHER_ROLE, "name": "乙", "prompt": "乙人设"},
                ],
                "llm_providers": [{"id": "p1", "base_url": "https://offline.invalid",
                                   "keys": ["k"], "models": [
                                       {"name": "chat", "enabled": True}]}],
            },
            "ProactiveReply": {"max_account_background_calls_per_day": 192},
        }
        self.config_path.write_text(json.dumps(self.original), encoding="utf-8")
        for name, value in (("CONFIG_PATH", self.config_path),
                            ("LEGACY_CONFIG_PATHS", []),
                            ("_config_saved_callback", None)):
            p = patch.object(webui, name, value)
            p.start()
            self.addCleanup(p.stop)
        for name in ("force_apply_llm_endpoints_from_config", "_apply_webui_runtime_update"):
            p = patch.object(webui, name)
            p.start()
            self.addCleanup(p.stop)
        webui.set_role_life_runtime(None)
        self.addCleanup(webui.set_role_life_runtime, None)
        with webui._role_life_runtime_lock:
            webui._ROLE_LIFE_RECORD_CACHE.clear()

    def payload(self, action, **extra):
        data = {"action": action, "account": ACCOUNT, "role_id": ROLE,
                "expected_revision": "rev-1", "expected_epoch": 2,
                "expected_state_version": 7, "request_id": "req-1"}
        data.update(extra)
        return data

    def saved(self):
        return json.loads(self.config_path.read_text(encoding="utf-8"))

    # -- 无 QQ / 独立 WebUI 可读写 --------------------------------------
    def test_disconnected_state_is_editable_and_uses_global_single_life(self):
        with patch("bot.role_life_store.DATA_DIR", Path(self.temp.name) / "data"):
            data = webui.collect_role_life_state()
            self.assertTrue(data["connected"])
            self.assertFalse(data["runtime_running"])
            self.assertTrue(data["editable"])
            self.assertEqual(data["account"], "global")
            self.assertEqual(data["role_id"], "default")
            self.assertEqual(data["read_only_reason"], "")
            self.assertIsInstance(data["status"], dict)
            self.assertEqual([p["id"] for p in data["presets"]], [ROLE, OTHER_ROLE])
            # 独立 WebUI 无主程序运行时也能直接保存配置与手动加备注，但触发模型生成返回 503
            webui.save_role_life_config(role_cfg(enabled=True, activities_per_day=7))
            self.assertTrue(self.saved()["RoleLife"]["enabled"])
            st = webui.collect_role_life_state()["status"]
            note_res = webui.role_life_action({
                "action": "add_life_note", "text": "离线也能手写备注",
                "request_id": "offline-note-1",
                "expected_revision": st["revision"],
                "expected_epoch": st["epoch"],
                "expected_state_version": st["state_version"],
            })
            self.assertTrue(note_res["ok"])
            st2 = webui.collect_role_life_state()["status"]
            with self.assertRaises(webui.RoleLifeUnavailableError):
                webui.role_life_action({
                    "action": "regenerate_plan", "request_id": "offline-gen-1",
                    "expected_revision": st2["revision"],
                    "expected_epoch": st2["epoch"],
                    "expected_state_version": st2["state_version"],
                })

    def test_legacy_nested_role_life_config_migrates_to_flat_view(self):
        seeded = dict(self.original)
        seeded["RoleLife"] = {"roles": {OTHER_ACCOUNT: {OTHER_ROLE: role_cfg(timezone="Asia/Tokyo")}}}
        self.config_path.write_text(json.dumps(seeded), encoding="utf-8")
        with patch("bot.role_life_store.DATA_DIR", Path(self.temp.name) / "data"):
            data = webui.collect_role_life_state()
            self.assertEqual(data["account"], "global")
            self.assertEqual(data["role_id"], "default")
            self.assertEqual(data["config"]["timezone"], "Asia/Tokyo")
            self.assertTrue(data["editable"])

    def test_corrupt_state_hides_zero_usage_and_forbids_jobs(self):
        runtime = FakeRuntime(corrupt=True)
        webui.set_role_life_runtime(runtime)
        data = webui.collect_role_life_state(ACCOUNT, ROLE)
        self.assertTrue(data["status"]["corrupt"])
        self.assertEqual(data["status"]["error"], "state identity mismatch")
        # 真实损坏状态没有可用版本字段：页面不能靠它们认成“0 次调用”。
        for key in ("revision", "epoch", "state_version"):
            self.assertNotIn(key, data["status"])
        # 用量未知必须显式下发；role_24h 为 None，不是 0
        self.assertTrue(data["status"]["budget_unknown"])
        self.assertEqual(data["status"]["usage"]["role_24h"], None)
        with self.assertRaises(PermissionError):
            webui.role_life_action(self.payload("enqueue", kind="plan"))
        # 损坏时只有显式 allow_corrupt 才能清空；不带确认一律拒绝
        with self.assertRaises(PermissionError):
            webui.role_life_action(self.payload("clear"))
        # 页面上的空版本/0 不会让清空“永远失败”：损坏分支不需要可校验版本
        result = webui.role_life_action(self.payload(
            "clear", allow_corrupt=True, expected_revision="",
            expected_epoch=0, expected_state_version=0))
        self.assertTrue(result["ok"])
        clear_calls = [c for c in runtime.calls if c[0] == "clear"]
        self.assertTrue(clear_calls[-1][-1])
        self.assertEqual(clear_calls[-1][3], "req-1")
        # 损坏清空仍需要幂等 ID
        no_id = self.payload("clear", allow_corrupt=True)
        no_id.pop("request_id")
        with self.assertRaises(ValueError):
            webui.role_life_action(no_id)

    # -- 统一配置保存与校验 ---------------------------------------------
    def test_config_save_rejects_invalid_values(self):
        with self.assertRaises(ValueError):
            webui.save_role_life_config({"enabled": "yes"})
        with self.assertRaises(ValueError):
            webui.save_role_life_config({"sleep_start": "25:00"})

    def test_config_save_merges_partial_fields_into_flat_role_life(self):
        webui.set_role_life_runtime(FakeRuntime(current="global"))
        webui.save_role_life_config({"enabled": True, "activities_per_day": 6})
        saved = self.saved()
        self.assertEqual(saved["RoleLife"]["activities_per_day"], 6)
        self.assertTrue(saved["RoleLife"]["enabled"])
        # 局部保存不覆盖同级字段
        webui.save_role_life_config({"timezone": "Asia/Tokyo"})
        saved = self.saved()
        self.assertEqual(saved["RoleLife"]["activities_per_day"], 6)
        self.assertEqual(saved["RoleLife"]["timezone"], "Asia/Tokyo")
        self.assertEqual(saved["Others"]["personality_presets"],
                         self.original["Others"]["personality_presets"])
        self.assertFalse(saved["RoleLife"]["maintenance_enabled"])

    def test_unrelated_save_keeps_role_life_section(self):
        webui.save_role_life_config(role_cfg())
        webui.save_config_bundle({"config_json": {"Others": {"bot_name": "离线"}}})
        self.assertEqual(self.saved()["RoleLife"]["enabled"], True)

    def test_invalid_role_life_payload_never_writes(self):
        before = self.config_path.read_bytes()
        with self.assertRaises(ValueError):
            webui.save_config_bundle({"config_json": {"RoleLife": {
                "roles": {ACCOUNT: {ROLE: {"sleep_start": "25:00"}}}}}})
        with self.assertRaises(ValueError):
            webui.save_config_bundle({"config_json": {"RoleLife": {"bad_field": 1}}})
        self.assertEqual(self.config_path.read_bytes(), before)

    def test_role_life_config_change_allowed_without_qq_connection(self):
        webui.set_role_life_runtime(None)
        webui.save_config_bundle({"config_json": {"RoleLife": {"enabled": True, "sleep_hours": 7}}})
        saved = self.saved()["RoleLife"]
        self.assertTrue(saved["enabled"])
        self.assertEqual(saved["sleep_hours"], 7)

    # -- 操作 / 版本 / 幂等 --------------------------------------------
    def test_jobs_require_all_versions_and_request_id(self):
        runtime = FakeRuntime()
        webui.set_role_life_runtime(runtime)
        for key in ("request_id", "expected_revision", "expected_epoch",
                    "expected_state_version"):
            data = self.payload("enqueue", kind="plan")
            data.pop(key)
            with self.subTest(key=key), self.assertRaises(ValueError):
                webui.role_life_action(data)
        # 缺任何一项都没有真正调用引擎入队
        self.assertFalse([c for c in runtime.calls if c[0] == "enqueue"])
        with self.assertRaises(ValueError):
            webui.role_life_action(self.payload("enqueue", kind="bogus"))

    def test_successful_operations_pass_revision_epoch_state_version(self):
        runtime = FakeRuntime()
        webui.set_role_life_runtime(runtime)
        self.assertEqual(webui.role_life_action(self.payload("enqueue", kind="plan")),
                         {"ok": True, "job_id": "job-plan"})
        self.assertEqual(webui.role_life_action(self.payload("regenerate_plan")),
                         {"ok": True, "job_id": "job-plan"})
        self.assertEqual(webui.role_life_action(self.payload("retry", job_id="old")),
                         {"ok": True, "job_id": "job-retry"})
        enqueue = [c for c in runtime.calls if c[0] == "enqueue"][-1]
        self.assertEqual(enqueue[5:8], ("rev-1", 2, 7))
        retry = [c for c in runtime.calls if c[0] == "retry"][-1]
        self.assertEqual(retry[3], "old")
        with self.assertRaises(RuntimeError):
            # 过期版本：假 runtime 的 CAS 拒绝，页面必须收到错误而不是假装成功
            webui.role_life_action(self.payload("enqueue", kind="plan",
                                                expected_state_version=1))

    def test_clear_is_idempotent_by_request_id_at_engine_and_not_repeated_here(self):
        runtime = FakeRuntime()
        webui.set_role_life_runtime(runtime)
        first = webui.role_life_action(self.payload("clear", request_id="clear-1"))
        second = webui.role_life_action(self.payload("clear", request_id="clear-1"))
        self.assertEqual(first, {"ok": True, "job_id": None})
        self.assertEqual(second, {"ok": True, "job_id": None})
        # WebUI 每次都把同一个 request_id 原样交给引擎，由引擎保证不二次清空
        clear_calls = [c for c in runtime.calls if c[0] == "clear"]
        self.assertEqual([c[3] for c in clear_calls], ["clear-1", "clear-1"])
        # 正常清空：预算保留、不显示未知、不带 allow_corrupt
        self.assertEqual(clear_calls[-1][-1], False)

    def test_normal_status_exposes_budget_unknown_fields_and_backup(self):
        runtime = FakeRuntime()
        runtime.status_data.update({
            "budget_unknown": False, "budget_unknown_until": 0.0,
            "recovered_from_corrupt": True,
            "corrupt_backup": "state.corrupt-1760000000000.json.bak",
        })
        webui.set_role_life_runtime(runtime)
        status = webui.collect_role_life_state(ACCOUNT, ROLE)["status"]
        self.assertFalse(status["budget_unknown"])
        self.assertTrue(status["recovered_from_corrupt"])
        self.assertEqual(status["corrupt_backup"], "state.corrupt-1760000000000.json.bak")
        self.assertEqual(status["usage"]["role_24h"], 1)  # 账本保留，不是 0 也不是未知

    def test_disabled_role_rejects_jobs_but_status_still_readable(self):
        runtime = FakeRuntime(enabled=False)
        webui.set_role_life_runtime(runtime)
        with self.assertRaises(PermissionError):
            webui.role_life_action(self.payload("enqueue", kind="plan"))
        self.assertFalse(webui.collect_role_life_state(ACCOUNT, ROLE)["status"]["enabled"])

    # -- 记录分页 -------------------------------------------------------
    def test_experience_kind_pagination_filters_server_side(self):
        runtime = FakeRuntime()
        webui.set_role_life_runtime(runtime)

        def records(account, role_id, kind, page, page_size):
            pool = [
                {"id": "a1", "kind": "activity", "text": "活动", "created_at": 3, "dream": False},
                {"id": "d1", "kind": "diary", "text": "日记", "created_at": 2, "dream": False},
                {"id": "m1", "kind": "dream", "text": "梦", "created_at": 1, "dream": True},
            ]
            start = (page - 1) * page_size
            return {"items": pool[start:start + page_size], "page": page,
                    "page_size": page_size, "total": len(pool),
                    "pages": max(1, (len(pool) + page_size - 1) // page_size)}

        runtime.records = records
        diary = webui._role_life_experiences_page(ACCOUNT, ROLE, {"diary"}, 1, 20, 7)
        self.assertEqual([i["id"] for i in diary["items"]], ["d1"])
        self.assertEqual(diary["total"], 1)
        dream = webui._role_life_experiences_page(ACCOUNT, ROLE, {"dream"}, 1, 20, 8)
        self.assertEqual([i["id"] for i in dream["items"]], ["m1"])
        everything = webui._role_life_experiences_page(
            ACCOUNT, ROLE, {"activity", "diary", "dream"}, 1, 2, 9)
        self.assertEqual(everything["total"], 3)
        self.assertEqual(everything["pages"], 2)
        self.assertEqual(len(everything["items"]), 2)

    def test_corrupt_records_do_not_look_empty(self):
        runtime = FakeRuntime()
        webui.set_role_life_runtime(runtime)

        def records(*_args):
            return {"items": [], "page": 1, "page_size": 20, "total": 0,
                    "pages": 1, "corrupt": True, "error": "state unreadable"}

        runtime.records = records
        data = webui._role_life_experiences_page(ACCOUNT, ROLE, {"diary"}, 1, 20, 7)
        self.assertTrue(data["corrupt"])
        self.assertTrue(data["error"])

    def test_transient_read_error_is_not_corrupt_and_never_allows_clear(self):
        runtime = FakeRuntime()
        webui.set_role_life_runtime(runtime)

        def status(_account, _role_id):
            raise OSError("disk temporarily unavailable")

        runtime.status = status
        data = webui.collect_role_life_state(ACCOUNT, ROLE)
        status_data = data["status"]
        # 临时读取异常≠损坏：不能给出 corrupt，也不能诱导用户清空。
        self.assertFalse(status_data["corrupt"])
        self.assertFalse(status_data["available"])
        self.assertIn("读取失败", status_data["read_error"])
        self.assertNotIn("revision", status_data)
        with self.assertRaises(webui.RoleLifeUnavailableError):
            webui.role_life_action(self.payload("clear", allow_corrupt=True))
        # 引擎（清空/入队）一次都没被调用
        self.assertFalse([c for c in runtime.calls
                          if c[0] in ("clear", "enqueue", "retry", "regenerate_plan")])

    def test_non_dict_status_is_treated_as_read_error(self):
        runtime = FakeRuntime()
        webui.set_role_life_runtime(runtime)
        runtime.status = lambda _a, _r: "garbage"
        status_data = webui._role_life_selected_status(ACCOUNT, ROLE)
        self.assertFalse(status_data["corrupt"])
        self.assertTrue(status_data["read_error"])

    def test_records_read_error_is_503_style_not_empty_success(self):
        runtime = FakeRuntime()
        webui.set_role_life_runtime(runtime)

        def records(*_args):
            raise OSError("disk error")

        runtime.records = records
        # 读盘异常必须向上抛（路由按 503 返回），不能吞成“空列表成功”。
        with self.assertRaises(OSError):
            webui._role_life_experiences_page(ACCOUNT, ROLE, {"diary"}, 1, 20, 7)
        source = (ROOT / "webui.py").read_text(encoding="utf-8")
        self.assertIn("except RoleLifeUnavailableError", source)
        self.assertIn('"retryable": True', source)

    def test_corrupt_status_never_serves_stale_cached_records(self):
        """损坏状态没有 state_version，不能命中旧缓存读出损坏前的内容。"""
        runtime = FakeRuntime()
        webui.set_role_life_runtime(runtime)
        calls = []

        def records(account, role_id, kind, page, page_size):
            calls.append(kind)
            return {"items": [{"id": "old", "kind": "activity", "text": "损坏前"}],
                    "page": page, "page_size": page_size, "total": 1, "pages": 1}

        runtime.records = records
        stale = webui._role_life_experiences_page(
            ACCOUNT, ROLE, {"activity", "diary", "dream"}, 1, 20, 0)
        self.assertEqual(stale["items"][0]["id"], "old")  # 预置 version=0 缓存
        calls.clear()

        # 之后状态变成损坏：GET records 必须直接返回 corrupt，不得读缓存。
        runtime.corrupt = True
        runtime.status_data.update({"corrupt": True, "available": False,
                                    "error": "state identity mismatch",
                                    "budget_unknown": True})
        for key in ("revision", "epoch", "state_version"):
            runtime.status_data.pop(key, None)
        status = webui._role_life_selected_status(ACCOUNT, ROLE)
        self.assertTrue(status["corrupt"])
        # 真实路由分发函数：corrupt 分支直接构造损坏响应，不读 records/缓存。
        for kind in ("experiences", "activity", "diary", "dream"):
            data = webui._role_life_records_for_route(
                runtime, ACCOUNT, ROLE, kind, 1, 20)
            self.assertTrue(data["corrupt"])
            self.assertEqual(data["items"], [])
            self.assertEqual(data["error"], "state identity mismatch")
        self.assertEqual(calls, [])  # 没有再次读 records

    def test_real_corrupt_status_still_only_allows_confirmed_clear(self):
        runtime = FakeRuntime(corrupt=True)
        webui.set_role_life_runtime(runtime)
        status_data = webui.collect_role_life_state(ACCOUNT, ROLE)["status"]
        self.assertTrue(status_data["corrupt"])
        self.assertNotIn("read_error", status_data)
        with self.assertRaises(PermissionError):
            webui.role_life_action(self.payload("clear"))  # 未确认
        # 损坏分支：不需要可校验版本，但仍要求幂等 request_id
        result = webui.role_life_action(self.payload(
            "clear", allow_corrupt=True, expected_revision="",
            expected_epoch=0, expected_state_version=0))
        self.assertTrue(result["ok"])

    # -- 记录缓存 / 版本 ------------------------------------------------
    def test_records_cache_keyed_by_real_state_version(self):
        runtime = FakeRuntime()
        webui.set_role_life_runtime(runtime)
        calls = []

        def records(account, role_id, kind, page, page_size):
            calls.append((page, page_size))
            return {"items": [{"id": f"i{len(calls)}", "kind": "diary", "text": "x"}],
                    "page": page, "page_size": page_size, "total": 1, "pages": 1}

        runtime.records = records
        first = webui._role_life_experiences_page(ACCOUNT, ROLE, {"diary"}, 1, 20, 7)
        second = webui._role_life_experiences_page(ACCOUNT, ROLE, {"diary"}, 1, 20, 7)
        self.assertEqual(len(calls), 1)  # 同版本命中缓存
        self.assertEqual(second, first)
        # 引擎内容版本推进后缓存必须失效：清空/新建记录立即反映。
        third = webui._role_life_experiences_page(ACCOUNT, ROLE, {"diary"}, 1, 20, 8)
        self.assertEqual(len(calls), 2)
        self.assertEqual(third["items"][0]["id"], "i2")

    def test_experiences_route_uses_current_state_version(self):
        """GET kind=experiences 与 activity/diary/dream 一样，版本变化后缓存失效。"""
        runtime = FakeRuntime()
        webui.set_role_life_runtime(runtime)
        calls = []

        def records(account, role_id, kind, page, page_size):
            calls.append(kind)
            return {"items": [{"id": "e", "kind": "activity", "text": "t"}],
                    "page": page, "page_size": page_size, "total": 1, "pages": 1}

        runtime.records = records
        webui._role_life_experiences_page(ACCOUNT, ROLE,
                                          {"activity", "diary", "dream"}, 1, 20, 7)
        webui._role_life_experiences_page(ACCOUNT, ROLE,
                                          {"activity", "diary", "dream"}, 1, 20, 7)
        self.assertEqual(len(calls), 1)
        runtime.status_data["state_version"] = 9
        webui._role_life_experiences_page(
            ACCOUNT, ROLE, {"activity", "diary", "dream"}, 1, 20,
            int(webui._role_life_selected_status(ACCOUNT, ROLE)["state_version"]))
        self.assertEqual(len(calls), 2)

    # -- 预设 ID 稳定性 -------------------------------------------------
    def test_sync_personality_presets_keeps_valid_ids_and_never_renumbers(self):
        others = {"personality_prompt": "旧", "personality_presets": [
            {"id": "default", "name": "默认", "prompt": "旧"},
            {"id": "p2", "name": "乙", "prompt": "乙"},
            {"id": "p3", "name": "丙", "prompt": "丙"},
        ], "active_personality_preset": "p2"}
        webui.sync_personality_presets(others)
        self.assertEqual([p["id"] for p in others["personality_presets"]],
                         ["default", "p2", "p3"])
        # 删除中间预设后保存，后面的 id 不变，不会“顺位继承”
        others["personality_presets"] = [p for p in others["personality_presets"]
                                         if p["id"] != "p2"]
        webui.sync_personality_presets(others)
        self.assertEqual([p["id"] for p in others["personality_presets"]],
                         ["default", "p3"])
        # 无关保存反复执行也不改 id
        for _ in range(3):
            webui.sync_personality_presets(others)
        self.assertEqual([p["id"] for p in others["personality_presets"]],
                         ["default", "p3"])

    def test_sync_personality_presets_derives_stable_ids_for_illegal_and_duplicates(self):
        others = {"personality_prompt": "", "personality_presets": [
            {"id": "bad id!", "name": "非法", "prompt": "x"},
            {"id": "dup", "name": "一", "prompt": "1"},
            {"id": "dup", "name": "二", "prompt": "2"},
            {"id": "", "name": "无 ID", "prompt": "3"},
        ]}
        webui.sync_personality_presets(others)
        first = [p["id"] for p in others["personality_presets"]]
        self.assertEqual(len(first), len(set(first)))
        self.assertEqual(first[1], "dup")  # 第一条合法保留
        webui.sync_personality_presets(others)
        self.assertEqual([p["id"] for p in others["personality_presets"]], first)


class ConcurrencyTests(RoleLifeWebUITest):
    def test_concurrent_partial_saves_do_not_lose_updates(self):
        threads = [
            threading.Thread(target=webui.save_role_life_config, args=({"preference": "喜欢安静"},)),
            threading.Thread(target=webui.save_role_life_config, args=({"sleep_hours": 7},)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        rl_saved = self.saved()["RoleLife"]
        self.assertEqual(rl_saved["preference"], "喜欢安静")
        self.assertEqual(rl_saved["sleep_hours"], 7)


class RouteContractTests(unittest.TestCase):
    """路由与鉴权契约（不起服务器，检查真实源码分发点）。"""

    def test_routes_exist_under_global_api_guard(self):
        source = (ROOT / "webui.py").read_text(encoding="utf-8")
        for path in ('"/api/role-life"', '"/api/role-life/records"',
                     '"/api/role-life/action"', '"/api/role-life/config"'):
            self.assertIn(path, source)
        # 三条 POST 路由都在 do_POST 的 _guard 之后；GET 同理。
        self.assertGreater(source.index('elif parsed.path == "/api/role-life/action"'),
                           source.index("def do_POST(self):"))
        self.assertGreater(source.index('elif parsed.path == "/api/role-life/config"'),
                           source.index("def do_POST(self):"))
        self.assertGreater(source.index('elif parsed.path == "/api/role-life"'),
                           source.index("def do_GET(self):"))

    def test_live_server_requires_token_and_validates_actions(self):
        """真实起一个本地 HTTP 服务：token 鉴权、只读与 403/400/409 状态码。"""
        import threading
        import urllib.error
        import urllib.request

        class Runtime(FakeRuntime):
            error_type = "validation"

            def enqueue(self, account, role_id, kind, request_id, revision, epoch,
                        state_version, args=None):
                return {"job_id": "job-plan", "error": ""}

            def upsert_plan_item(self, account, role_id, params, request_id,
                                 revision, epoch, state_version):
                return {"ok": False, "error": "标题过长" if self.error_type == "validation" else "版本已变化",
                        "error_type": self.error_type}

        with tempfile.TemporaryDirectory() as directory:
            static_dir = Path(directory) / "static"
            sibling_dir = Path(directory) / "static-secret"
            static_dir.mkdir()
            sibling_dir.mkdir()
            (sibling_dir / "secret.js").write_text("SECRET=1;", encoding="utf-8")
            cfg_path = Path(directory) / "config.json"
            cfg_path.write_text(json.dumps({
                "WebUI": {"access_token": "tok", "host": "127.0.0.1", "port": 0},
                "Others": {"personality_presets": [{"id": ROLE, "prompt": "x"}]},
            }), encoding="utf-8")
            parked = [patch.object(webui, "BASE_DIR", Path(directory)),
                      patch.object(webui, "CONFIG_PATH", cfg_path),
                      patch.object(webui, "_config_saved_callback", None),
                      patch.object(webui, "force_apply_llm_endpoints_from_config"),
                      patch.object(webui, "_apply_webui_runtime_update")]
            for item in parked:
                item.start()
            try:
                rt = Runtime()
                webui.set_role_life_runtime(rt)
                server = webui._WebUIServer(("127.0.0.1", 0), webui.WebUIHandler)
                port = server.server_address[1]
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()

                def call(path, method="GET", body=None, token=None):
                    headers = {"Content-Type": "application/json"}
                    if token:
                        headers["X-WebUI-Token"] = token
                    req = urllib.request.Request(
                        f"http://127.0.0.1:{port}{path}", method=method,
                        data=json.dumps(body).encode() if body is not None else None,
                        headers=headers)
                    try:
                        with urllib.request.urlopen(req, timeout=5) as response:
                            return response.status, json.loads(response.read())
                    except urllib.error.HTTPError as error:
                        return error.code, json.loads(error.read())

                try:
                    self.assertEqual(call("/api/role-life?account=%s&role=%s"
                                          % (ACCOUNT, ROLE))[0], 401)
                    status, data = call("/api/role-life?account=%s&role=%s"
                                        % (ACCOUNT, ROLE), token="tok")
                    self.assertEqual(status, 200)
                    self.assertTrue(data["data"]["editable"])
                    self.assertEqual(data["data"]["status"]["state_version"], 7)
                    status, data = call("/api/role-life/records?account=%s&role=%s&kind=diary"
                                        % (ACCOUNT, ROLE), token="tok")
                    self.assertEqual(status, 200)
                    self.assertEqual(data["data"]["page"], 1)
                    body = {"action": "enqueue", "account": ACCOUNT,
                            "role_id": ROLE, "kind": "plan", "request_id": "req-live",
                            "expected_revision": "rev-1", "expected_epoch": 2,
                            "expected_state_version": 7}
                    status, data = call("/api/role-life/action", "POST", body, token="tok")
                    self.assertEqual(status, 200)
                    self.assertEqual(data["data"]["job_id"], "job-plan")
                    status, _ = call("/api/role-life/action", "POST",
                                     {k: v for k, v in body.items()
                                      if k != "expected_state_version"}, token="tok")
                    self.assertEqual(status, 400)
                    status, _ = call("/api/role-life/action", "POST",
                                     dict(body, action="bogus"), token="tok")
                    self.assertEqual(status, 400)
                    # 核心校验错误返回 400，冲突才返回 409
                    upsert_body = {
                        "action": "upsert_plan_item", "account": ACCOUNT,
                        "role_id": ROLE, "request_id": "req-u1",
                        "expected_revision": "rev-1", "expected_epoch": 2,
                        "expected_state_version": 7,
                        "params": {"title": "t"},
                    }
                    rt.error_type = "validation"
                    self.assertEqual(
                        call("/api/role-life/action", "POST", upsert_body, token="tok")[0],
                        400,
                    )
                    rt.error_type = "conflict"
                    self.assertEqual(
                        call("/api/role-life/action", "POST", upsert_body, token="tok")[0],
                        409,
                    )
                    rt.error_type = "storage"
                    storage_code, storage_data = call(
                        "/api/role-life/action", "POST", upsert_body, token="tok",
                    )
                    self.assertEqual(storage_code, 503)
                    self.assertTrue(storage_data.get("retryable"))
                    # 配置保存带 expected_config_revision 防多页面旧快照互相覆盖
                    cfg_state_status, cfg_state = call(
                        f"/api/role-life?account={ACCOUNT}&role={ROLE}", token="tok",
                    )
                    self.assertEqual(cfg_state_status, 200)
                    rev0 = cfg_state["data"]["config_revision"]
                    s1, d1 = call("/api/role-life/config", "POST", {
                        "account": ACCOUNT, "role_id": ROLE,
                        "expected_config_revision": rev0,
                        "config": {"sleep_hours": 7},
                    }, token="tok")
                    self.assertEqual(s1, 200)
                    rev1 = d1["data"]["config_revision"]
                    self.assertNotEqual(rev1, rev0)
                    s_stale, _ = call("/api/role-life/config", "POST", {
                        "account": ACCOUNT, "role_id": ROLE,
                        "expected_config_revision": rev0,
                        "config": {"preference": "晒太阳"},
                    }, token="tok")
                    self.assertEqual(s_stale, 409)
                    s2, d2 = call("/api/role-life/config", "POST", {
                        "account": ACCOUNT, "role_id": ROLE,
                        "expected_config_revision": rev1,
                        "config": {"preference": "晒太阳"},
                    }, token="tok")
                    self.assertEqual(s2, 200)
                    self.assertEqual(d2["data"]["config"]["sleep_hours"], 7)
                    self.assertEqual(d2["data"]["config"]["preference"], "晒太阳")
                    # 静态路径逃逸到兄弟目录 static-secret 必须返回 404
                    import http.client
                    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                    conn.request("GET", "/static/../static-secret/secret.js",
                                 headers={"X-WebUI-Token": "tok"})
                    resp = conn.getresponse()
                    resp.read()
                    conn.close()
                    self.assertEqual(resp.status, 404)
                finally:
                    server.shutdown()
                    server.server_close()
            finally:
                webui.set_role_life_runtime(None)
                for item in reversed(parked):
                    item.stop()

    def test_registration_is_optional_and_none_means_disconnected(self):
        webui.set_role_life_runtime(None)
        self.assertIsNone(webui.get_role_life_runtime())
        marker = object()
        webui.set_role_life_runtime(marker)
        self.assertIs(webui.get_role_life_runtime(), marker)
        webui.set_role_life_runtime(None)
        self.assertIsNone(webui.get_role_life_runtime())


class WiringTests(unittest.TestCase):
    """最终单一契约：`enqueue(..., args=None)`，不再有 7 参兼容分支。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config_path = Path(self.temp.name) / "config.json"
        self.config_path.write_text(json.dumps({
            "Others": {"personality_presets": [{"id": ROLE, "prompt": "x"}]}}),
            encoding="utf-8")
        p = patch.object(webui, "CONFIG_PATH", self.config_path)
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(webui, "_config_saved_callback", None)
        p.start()
        self.addCleanup(p.stop)
        for name in ("force_apply_llm_endpoints_from_config", "_apply_webui_runtime_update"):
            p = patch.object(webui, name)
            p.start()
            self.addCleanup(p.stop)
        webui.set_role_life_runtime(None)
        self.addCleanup(webui.set_role_life_runtime, None)
        with webui._role_life_runtime_lock:
            webui._ROLE_LIFE_RECORD_CACHE.clear()

    def payload(self, action, **extra):
        data = {"action": action, "account": ACCOUNT, "role_id": ROLE,
                "expected_revision": "rev-1", "expected_epoch": 2,
                "expected_state_version": 7, "request_id": "req-wire"}
        data.update(extra)
        return data

    def test_seven_arg_closure_is_no_longer_accepted(self):
        """旧 7 参接线已移除：只差 args 的闭包必须报错，不能静默降级。"""

        class Runtime:
            def connected(self): return True
            def accounts(self): return {"current": ACCOUNT, "known": [ACCOUNT], "roles": {ACCOUNT: [ROLE]}}
            def status(self, account, role_id):
                return {"enabled": True, "corrupt": False, "revision": "rev-1",
                        "epoch": 2, "state_version": 7}
            def enqueue(self, account, role_id, kind, request_id, revision, epoch,
                        state_version):
                raise AssertionError("7 参闭包不应再被调用")

        webui.set_role_life_runtime(Runtime())
        with self.assertRaises(TypeError):
            webui.role_life_action(self.payload("enqueue", kind="plan"))

    def test_eight_arg_runtime_receives_args_keyword_and_reports_stale(self):
        received = []

        class Runtime:
            def connected(self): return True
            def accounts(self): return {"current": ACCOUNT, "known": [ACCOUNT], "roles": {ACCOUNT: [ROLE]}}
            def status(self, account, role_id):
                return {"enabled": True, "corrupt": False, "revision": "rev-1",
                        "epoch": 2, "state_version": 7}
            def enqueue(self, account, role_id, kind, request_id, revision, epoch,
                        state_version, args=None):
                received.append((kind, request_id, revision, epoch, state_version, args))
                if revision != "rev-1" or state_version != 7:
                    return {"job_id": None, "error": "stale"}
                return {"job_id": "j2", "error": ""}

        webui.set_role_life_runtime(Runtime())
        args = {"item": {"id": "x", "title": "t"}}
        result = webui.role_life_action(self.payload("enqueue", kind="activity", args=args))
        self.assertEqual(result["job_id"], "j2")
        self.assertEqual(received[-1], ("activity", "req-wire", "rev-1", 2, 7, args))
        with self.assertRaises(RuntimeError):
            webui.role_life_action(self.payload("enqueue", kind="plan",
                                                expected_revision="rev-old"))


class RealClosureTests(unittest.TestCase):
    """真实 RoleLifeEngine 闭包串联（fake clock/model，临时存储）。

    覆盖 enqueue 成功、过期版本失败、正常 clear 保留账本与幂等、
    corrupt 只允许 allow_corrupt 清空。
    """

    def setUp(self):
        from bot import role_life as rl
        from bot.role_life_store import RoleLifeStore
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.store = RoleLifeStore(base / "state")
        self.config_path = base / "config.json"
        self.config_path.write_text(json.dumps({
            "Others": {"personality_presets": [{"id": ROLE, "prompt": "你是甲"}]}}),
            encoding="utf-8")
        self.now = time.time()  # store 用真实时间写 budget_unknown_until，必须同一时基
        self.cfg = rl.normalize_role_config({"enabled": True, "sleep_enabled": False})
        self.others = {
            "personality_presets": [{"id": ROLE, "name": "甲", "prompt": "你是甲"}],
            "active_personality_preset": ROLE,
            "private_personality_preset": "", "group_personality_preset": "",
            "personality_prompt": "你是甲",
        }
        self.generated = []

        async def generate(job, reserve_attempt):
            if not await reserve_attempt():
                return None
            self.generated.append(job.kind)
            if job.kind == "plan":
                return {"items": [{"title": "晨间阅读", "purpose": "看书",
                                   "start": "09:00", "end": "10:00"}]}
            return {"text": "完成了。"}

        self.engine = rl.RoleLifeEngine(
            self.store,
            config_getter=lambda: self.cfg,
            persona_getter=lambda: self.others,
            bot_name="测试",
            generate=generate,
            reserve_account=lambda account: True,
            account_limit=lambda: 192,
            clock=lambda: self.now,
        )
        self.engine.bind_account("global", 1)

        def enqueue(account, role_id, kind, request_id, revision, epoch,
                    state_version, args=None):
            return {"job_id": self.engine.enqueue(
                account, role_id, kind, request_id=request_id, args=args,
                expected_revision=revision, expected_epoch=epoch,
                expected_state_version=state_version), "error": ""}

        def retry(account, role_id, job_id, request_id, revision, epoch, state_version):
            return {"job_id": self.engine.retry_job(
                account, role_id, job_id, request_id=request_id,
                expected_revision=revision, expected_epoch=epoch,
                expected_state_version=state_version), "error": ""}

        def regenerate_plan(account, role_id, request_id, revision, epoch, state_version):
            return {"job_id": self.engine.regenerate_plan(
                account, role_id, request_id=request_id, expected_revision=revision,
                expected_epoch=epoch, expected_state_version=state_version),
                "error": ""}

        def clear(account, role_id, request_id, revision, epoch, state_version,
                  allow_corrupt=False):
            return {"ok": self.engine.clear_role(
                account, role_id, request_id=request_id, expected_revision=revision,
                expected_epoch=epoch, expected_state_version=state_version,
                allow_corrupt=allow_corrupt), "error": ""}

        def upsert_plan_item(account, role_id, params, request_id, revision, epoch, state_version):
            try:
                p = params or {}
                item = self.engine.upsert_plan_item(
                    account, role_id, item_id=p.get("item_id", ""),
                    title=p.get("title", ""), purpose=p.get("purpose", ""),
                    start_hhmm=p.get("start", ""), end_hhmm=p.get("end", ""),
                    date=p.get("date", ""), request_id=request_id,
                    expected_revision=revision, expected_epoch=epoch,
                    expected_state_version=state_version)
                return {"ok": True, "item": item}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

        def delete_plan_item(account, role_id, item_id, request_id, revision, epoch, state_version):
            try:
                ok = self.engine.delete_plan_item(
                    account, role_id, item_id=item_id, request_id=request_id,
                    expected_revision=revision, expected_epoch=epoch,
                    expected_state_version=state_version)
                return {"ok": ok}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

        def trigger_activity(account, role_id, item_id, request_id, revision, epoch, state_version):
            try:
                jid = self.engine.trigger_activity_experience(
                    account, role_id, item_id=item_id, request_id=request_id,
                    expected_revision=revision, expected_epoch=epoch,
                    expected_state_version=state_version)
                return {"job_id": jid, "error": ""}
            except Exception as exc:
                return {"job_id": None, "error": str(exc)}

        def update_experience(account, role_id, params, request_id, revision, epoch, state_version):
            try:
                p = params or {}
                exp = self.engine.update_experience(
                    account, role_id, experience_id=p.get("experience_id", ""),
                    plan_item_id=p.get("plan_item_id", ""), text=p.get("text", ""),
                    request_id=request_id, expected_revision=revision,
                    expected_epoch=epoch, expected_state_version=state_version)
                return {"ok": True, "experience": exp}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

        def delete_experience(account, role_id, experience_id, request_id, revision, epoch, state_version):
            try:
                ok = self.engine.delete_experience(
                    account, role_id, experience_id=experience_id, request_id=request_id,
                    expected_revision=revision, expected_epoch=epoch,
                    expected_state_version=state_version)
                return {"ok": ok}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

        def add_life_note(account, role_id, text, request_id, revision, epoch, state_version):
            try:
                note = self.engine.add_life_note(
                    account, role_id, text, source="manual",
                    expected_revision=revision, expected_epoch=epoch,
                    expected_state_version=state_version)
                return {"ok": True, "note": note}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

        def delete_life_note(account, role_id, note_id, request_id, revision, epoch, state_version):
            try:
                ok = self.engine.delete_life_note(
                    account, role_id, note_id=note_id, request_id=request_id,
                    expected_revision=revision, expected_epoch=epoch,
                    expected_state_version=state_version)
                return {"ok": ok}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

        webui.set_role_life_runtime({
            "status": self.engine.status, "records": self.engine.list_records,
            "accounts": lambda: {"current": "global", "known": ["global"],
                                 "roles": {"global": ["default"]}},
            "enqueue": enqueue, "retry": retry, "regenerate_plan": regenerate_plan,
            "clear": clear, "connected": lambda: True,
            "upsert_plan_item": upsert_plan_item,
            "delete_plan_item": delete_plan_item,
            "trigger_activity": trigger_activity,
            "update_experience": update_experience,
            "delete_experience": delete_experience,
            "add_life_note": add_life_note,
            "delete_life_note": delete_life_note,
        })
        self.addCleanup(webui.set_role_life_runtime, None)
        for name, value in (("CONFIG_PATH", self.config_path),
                            ("_config_saved_callback", None)):
            p = patch.object(webui, name, value)
            p.start()
            self.addCleanup(p.stop)
        for name in ("force_apply_llm_endpoints_from_config", "_apply_webui_runtime_update"):
            p = patch.object(webui, name)
            p.start()
            self.addCleanup(p.stop)

    def payload(self, action, **extra):
        status = webui.collect_role_life_state(ACCOUNT, ROLE)["status"]
        data = {"action": action, "account": ACCOUNT, "role_id": ROLE,
                "expected_revision": status["revision"],
                "expected_epoch": status["epoch"],
                "expected_state_version": status["state_version"],
                "request_id": "real-1"}
        data.update(extra)
        return data

    def test_first_action_has_revision_and_enqueues(self):
        status = webui.collect_role_life_state(ACCOUNT, ROLE)["status"]
        self.assertTrue(status["revision"])  # core 已回退解析 persona revision
        result = webui.role_life_action(self.payload("regenerate_plan"))
        self.assertTrue(result["job_id"])

    def test_stale_version_is_rejected_by_real_engine(self):
        data = self.payload("enqueue", kind="plan")
        data["expected_state_version"] = 999
        with self.assertRaises(RuntimeError):
            webui.role_life_action(data)

    def test_clear_keeps_budget_and_repeat_is_idempotent(self):
        data = self.payload("clear", request_id="real-clear")
        self.assertTrue(webui.role_life_action(data)["ok"])
        status = webui.collect_role_life_state(ACCOUNT, ROLE)["status"]
        epoch_after_first = status["epoch"]
        self.assertEqual(epoch_after_first, 1)
        self.assertFalse(status["budget_unknown"])
        # 同 request_id 第二次：不再写盘、epoch 不变
        again = self.payload("clear", request_id="real-clear")
        self.assertTrue(webui.role_life_action(again)["ok"])
        self.assertEqual(
            webui.collect_role_life_state(ACCOUNT, ROLE)["status"]["epoch"],
            epoch_after_first)

    def test_corrupt_requires_allow_corrupt_and_backs_up(self):
        path = self.store.state_path("global", "default")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"{not json")
        status = webui.collect_role_life_state(ACCOUNT, ROLE)["status"]
        self.assertTrue(status["corrupt"])
        for key in ("revision", "epoch", "state_version"):
            self.assertNotIn(key, status)
        self.assertIsNone(status["usage"]["role_24h"])
        with self.assertRaises(PermissionError):
            webui.role_life_action({"action": "clear", "account": ACCOUNT,
                                    "role_id": ROLE, "request_id": "c1",
                                    "expected_revision": "", "expected_epoch": 0,
                                    "expected_state_version": 0})
        ok = webui.role_life_action({"action": "clear", "account": ACCOUNT,
                                     "role_id": ROLE, "request_id": "c1",
                                     "allow_corrupt": True})
        self.assertTrue(ok["ok"])
        status = webui.collect_role_life_state(ACCOUNT, ROLE)["status"]
        self.assertTrue(status["recovered_from_corrupt"])
        backup = status["corrupt_backup"]
        self.assertTrue(backup.startswith("state.corrupt-"))
        self.assertTrue((path.parent / backup).exists())
        self.assertTrue(status["budget_unknown"])
        self.assertGreater(status["budget_unknown_until"], 0)

    def test_schedule_experience_notes_crud_actions_and_schema_no_192_cap(self):
        schema_paths = [
            f["path"] for page in webui.build_ui_schema({}) for f in page.get("fields", [])
        ]
        self.assertNotIn("ProactiveReply.max_account_background_calls_per_day", schema_paths)
        # 新增日程
        res = webui.role_life_action(self.payload(
            "upsert_plan_item", request_id="u1",
            params={"title": "听音乐", "purpose": "放松", "start": "14:00", "end": "15:00", "date": "2026-03-10"},
        ))
        self.assertTrue(res["ok"])
        item_id = res["item"]["id"]
        # 为该活动手写经历并编辑/删除
        exp_res = webui.role_life_action(self.payload(
            "update_experience", request_id="e1",
            params={"plan_item_id": item_id, "text": "循环听了很久。"},
        ))
        self.assertTrue(exp_res["ok"])
        exp_id = exp_res["experience"]["id"]
        self.assertTrue(webui.role_life_action(self.payload(
            "update_experience", request_id="e2",
            params={"experience_id": exp_id, "text": "顺便泡了热茶。"},
        ))["ok"])
        self.assertTrue(webui.role_life_action(self.payload(
            "delete_experience", request_id="e3", experience_id=exp_id,
        ))["ok"])
        # 添加与删除生活备注
        note_res = webui.role_life_action(self.payload(
            "add_life_note", request_id="n1", text="周末想逛旧书店",
        ))
        self.assertTrue(note_res["ok"])
        note_id = note_res["note"]["id"]
        self.assertTrue(webui.role_life_action(self.payload(
            "delete_life_note", request_id="n2", note_id=note_id,
        ))["ok"])


if __name__ == "__main__":
    unittest.main()
