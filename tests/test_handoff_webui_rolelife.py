"""Handoff Phase 4 regressions: P0-5, P1-4, P1-5, P1-7, P2-2, P2-5."""
import ast
import asyncio
import datetime
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config_migrate
import webui
from bot import role_life as rl
from bot.persona import Persona
from bot.role_life_store import GLOBAL_ACCOUNT, GLOBAL_ROLE, RoleLifeStore


class RoleLifeConfigSaveMigrationTests(unittest.TestCase):
    def test_default_config_rolelife_flat_without_comments_and_roundtrip_migration(self):
        default_cfg = json.loads((ROOT / "config.default.json").read_text(encoding="utf-8"))
        rl_def = default_cfg["RoleLife"]
        self.assertIn("enabled", rl_def)
        self.assertNotIn("roles", rl_def)
        self.assertFalse(any(k.startswith("_comment") for k in rl_def))

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            cfg_path = tmp_dir / "config.json"
            backup_dir = tmp_dir / "backups"
            cfg_data = json.loads((ROOT / "config.default.json").read_text(encoding="utf-8"))
            cfg_path.write_text(json.dumps(cfg_data, ensure_ascii=False, indent=2), encoding="utf-8")

            with patch.object(webui, "CONFIG_PATH", cfg_path), \
                 patch.object(webui, "_config_saved_callback", None), \
                 patch.object(webui, "force_apply_llm_endpoints_from_config"), \
                 patch.object(webui, "_apply_webui_runtime_update"):
                bundle = {"config_json": json.loads(cfg_path.read_text(encoding="utf-8"))}
                bundle["config_json"]["RoleLife"] = {
                    "_comment": "stale comment",
                    "roles": {},
                    "role_daily_limit": 5,
                    "weather_city": "Beijing",
                    "enabled": True,
                    "timezone": "Asia/Shanghai",
                }
                webui._save_config_bundle_locked(bundle)

                changed = config_migrate.ensure_config_up_to_date(
                    cfg_path, template_path=ROOT / "config.default.json", backup_dir=backup_dir
                )
                self.assertFalse(changed)
                after_mig = json.loads(cfg_path.read_text(encoding="utf-8"))
                self.assertNotIn("roles", after_mig["RoleLife"])
                self.assertNotIn("role_daily_limit", after_mig["RoleLife"])
                self.assertNotIn("weather_city", after_mig["RoleLife"])
                self.assertFalse(any(k.startswith("_comment") for k in after_mig["RoleLife"]))

                # Saving any unrelated WebUI page succeeds cleanly
                webui._save_ui_state_locked({"form_values": {"Others.reminder": "!"}})
                after_ui = json.loads(cfg_path.read_text(encoding="utf-8"))
                self.assertEqual(after_ui["Others"]["reminder"], "!")
                self.assertTrue(after_ui["RoleLife"]["enabled"])


class WebUISecurityAndConfigTests(unittest.TestCase):
    def setUp(self):
        with webui._login_fails_lock:
            webui._login_fails.clear()

    def tearDown(self):
        with webui._login_fails_lock:
            webui._login_fails.clear()

    def test_agent_tools_representative_card_wins_over_untouched_siblings(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "config.json"
            cfg_data = json.loads((ROOT / "config.default.json").read_text(encoding="utf-8"))
            cfg_path.write_text(json.dumps(cfg_data, ensure_ascii=False), encoding="utf-8")
            with patch.object(webui, "CONFIG_PATH", cfg_path), \
                 patch.object(webui, "_config_saved_callback", None), \
                 patch.object(webui, "force_apply_llm_endpoints_from_config"), \
                 patch.object(webui, "_apply_webui_runtime_update"):
                ui = webui.collect_ui_state()
                fs_meta = next(m for m in ui["agent_tool_meta"] if m["key"] == "file_write")
                self.assertIn("execute_shell", fs_meta["members"])
                # Even if execute_shell is submitted last with old True/admin, rep file_write False/user must win
                webui._save_config_bundle_locked({
                    "config_json": cfg_data,
                    "agent_tools": {
                        "file_write": {"enabled": False, "level": "user"},
                        "execute_shell": {"enabled": True, "level": "admin"},
                        "execute_python": {"enabled": True, "level": "admin"},
                    },
                })
                saved = json.loads(cfg_path.read_text(encoding="utf-8"))
                self.assertFalse(saved["Agent"]["tools"]["file_write"]["enabled"])
                self.assertFalse(saved["Agent"]["tools"]["execute_shell"]["enabled"])
                self.assertFalse(saved["Agent"]["tools"]["execute_python"]["enabled"])
                self.assertEqual(saved["Agent"]["tools"]["execute_shell"]["level"], "user")

    def test_safe_plugin_dest_blocks_traversal_and_special_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdir = Path(tmp) / "plugins"
            pdir.mkdir()
            with patch.object(webui, "PLUGIN_DIR", pdir):
                for bad in ("", ".", "..", "..zip".removesuffix(".zip"), "C:\\Windows", "C:foo", "../a", "\\a", "bad.", "bad\x00name"):
                    with self.assertRaises(ValueError, msg=f"should reject {bad!r}"):
                        webui._safe_plugin_dest(bad)
                good = webui._safe_plugin_dest("my_plugin")
                self.assertEqual(good, (pdir / "my_plugin").resolve())

    def test_guard_rate_limits_all_api_routes(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "config.json"
            cfg_path.write_text(json.dumps({"WebUI": {"access_token": "secret-tok", "host": "127.0.0.1", "port": 0}}), encoding="utf-8")
            with patch.object(webui, "BASE_DIR", Path(tmp)), \
                 patch.object(webui, "CONFIG_PATH", cfg_path):
                server = webui._WebUIServer(("127.0.0.1", 0), webui.WebUIHandler)
                port = server.server_address[1]
                t = threading.Thread(target=server.serve_forever, daemon=True)
                t.start()
                try:
                    def call(path, token=None):
                        headers = {}
                        if token:
                            headers["X-WebUI-Token"] = token
                        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers)
                        try:
                            with urllib.request.urlopen(req, timeout=5) as resp:
                                return resp.status, json.loads(resp.read())
                        except urllib.error.HTTPError as err:
                            return err.code, json.loads(err.read())

                    got_429 = False
                    for _ in range(webui.LOGIN_MAX_FAILS + 2):
                        code, _ = call("/api/config", token="wrong")
                        if code == 429:
                            got_429 = True
                            break
                        self.assertEqual(code, 401)
                    self.assertTrue(got_429)
                    # Subsequent request to another endpoint even with valid token must be locked with 429
                    code2, _ = call("/api/auth/state", token="secret-tok")
                    self.assertEqual(code2, 429)
                finally:
                    server.shutdown()
                    server.server_close()

    def test_partial_save_ui_state_preserves_users_and_missing_plugin_defaults_not_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            cfg_path = tmp_dir / "config.json"
            pdir = tmp_dir / "plugins"
            (pdir / "demo_p").mkdir(parents=True)
            (pdir / "demo_p" / "metadata.yaml").write_text(
                "name: demo_p\nconfig_schema:\n  api_key:\n    type: string\n    label: Key\n  mode:\n    type: select\n    default: fast\n",
                encoding="utf-8",
            )
            cfg_data = json.loads((ROOT / "config.default.json").read_text(encoding="utf-8"))
            cfg_data["owner"] = ["10001"]
            cfg_data["Others"]["ROOT_User"] = ["10002"]
            cfg_data["black_list"] = ["99999"]
            cfg_path.write_text(json.dumps(cfg_data, ensure_ascii=False), encoding="utf-8")

            with patch.object(webui, "CONFIG_PATH", cfg_path), \
                 patch.object(webui, "PLUGIN_DIR", pdir), \
                 patch.object(webui, "_plugin_schemas_callback", None), \
                 patch.object(webui, "_config_saved_callback", None), \
                 patch.object(webui, "force_apply_llm_endpoints_from_config"), \
                 patch.object(webui, "_apply_webui_runtime_update"):
                webui._save_ui_state_locked({"form_values": {"Others.reminder": "#"}})
                saved = json.loads(cfg_path.read_text(encoding="utf-8"))
                self.assertEqual(saved["owner"], ["10001"])
                self.assertEqual(saved["Others"]["ROOT_User"], ["10002"])
                self.assertEqual(saved["black_list"], ["99999"])
                self.assertEqual(saved["Others"]["reminder"], "#")

                schemas = webui._collect_plugin_schemas()
                self.assertNotIn("api_key", schemas["demo_p"]["values"])
                self.assertEqual(schemas["demo_p"]["values"]["mode"], "fast")

    def test_update_check_uses_http_proxy_and_stat_uses_last_rebuild(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "config.json"
            cfg_path.write_text(json.dumps({"Others": {"http_proxy": "http://127.0.0.1:7890"}}), encoding="utf-8")
            with patch.object(webui, "CONFIG_PATH", cfg_path), \
                 patch("urllib.request.build_opener") as mock_build:
                class _FakeResp:
                    def __enter__(self): return self
                    def __exit__(self, *a): pass
                    def geturl(self): return "https://github.com/org/repo/releases/tag/v9.9.9"
                mock_opener = unittest.mock.MagicMock()
                mock_opener.open.return_value = _FakeResp()
                mock_build.return_value = mock_opener
                tag, _url = webui._scrape_latest_tag_via_redirect("org/repo")
                self.assertEqual(tag, "v9.9.9")
                handlers = mock_build.call_args.args
                self.assertTrue(any(isinstance(h, urllib.request.ProxyHandler) for h in handlers))

        self.assertIn("last_rebuild", webui._stat_inc_state)


class RoleLifeEdgeFixTests(unittest.TestCase):
    def _make_engine(self, tmp, others=None, cfg_extra=None):
        store = RoleLifeStore(Path(tmp))
        cfg = dict(rl.DEFAULT_ROLE_CONFIG, enabled=True, **(cfg_extra or {}))

        async def _fake_gen(job, reserve_attempt):
            await reserve_attempt()
            return {"items": []}

        engine = rl.RoleLifeEngine(
            store,
            config_getter=lambda: dict(cfg),
            persona_getter=lambda: dict(others if others is not None else {}),
            bot_name="小测",
            generate=_fake_gen,
            reserve_account=lambda _acct: True,
            account_limit=lambda: 192,
        )
        engine.bind_account(GLOBAL_ACCOUNT, 1)
        self.addCleanup(lambda: asyncio.run(engine.stop()))
        return engine, store, cfg

    def test_persona_fallback_retry_job_and_main_reset_global_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine, store, _cfg = self._make_engine(tmp, others={})
            p = engine._persona(GLOBAL_ACCOUNT, GLOBAL_ROLE)
            self.assertIsInstance(p, Persona)
            self.assertEqual(p.role_id, GLOBAL_ROLE)
            self.assertEqual(p.preset_id, GLOBAL_ROLE)

            # Reset command source passes _role_life_target(engine, acct, ...) to clear_session_shares
            main_src = (ROOT / "main.py").read_text(encoding="utf-8")
            self.assertIn("_role_life_target(engine, acct", main_src)

            # Seed failed job then mutate preference -> revision changes -> retry_job still keeps state synced
            engine.store.update(GLOBAL_ACCOUNT, GLOBAL_ROLE, lambda s: s["jobs"].__setitem__("j1", {
                "id": "j1", "kind": "plan", "status": "failed", "created_at": 100,
                "args": {"date": "2026-03-10"}, "revision": p.revision,
                "config_revision": "old-cfg", "epoch": 0, "generation": 0, "attempts": 1,
                "dedupe_key": "plan:2026-03-10", "request_id": "r1",
            }))
            snap = engine.status(GLOBAL_ACCOUNT, GLOBAL_ROLE, 1000)
            new_id = engine.retry_job(
                GLOBAL_ACCOUNT, GLOBAL_ROLE, "j1",
                request_id="ret-1",
                expected_revision=snap["revision"],
                expected_epoch=snap["epoch"],
                expected_state_version=snap["state_version"],
            )
            self.assertTrue(new_id)
            st, _ = store.try_load(GLOBAL_ACCOUNT, GLOBAL_ROLE)
            self.assertEqual(st["revision"], p.revision)

    def test_duplicate_diary_dream_plan_cap_failed_activity_mood_snapshot_pagination_dst(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine, store, cfg = self._make_engine(
                tmp, others={"personality_prompt": "你是测试角色"},
                cfg_extra={"sleep_enabled": True, "timezone": "America/New_York"},
            )
            now = datetime.datetime(2026, 3, 8, 10, 0, tzinfo=rl.ZoneInfo("America/New_York")).timestamp()
            store.update(GLOBAL_ACCOUNT, GLOBAL_ROLE, lambda s: s.update({
                "mood": {"mood": "开心", "energy": "充沛", "source_ids": [], "updated_at": now},
                "sleep": {"date": "2026-03-08", "start": now - 7200, "end": now - 600, "wake_dream_done": False},
                "experiences": [
                    {"id": "diary-2026-03-08", "kind": "diary", "date": "2026-03-08", "created_at": now, "text": "已有日记", "dream": False, "source_ids": [], "origin": "role_simulation"},
                    {"id": f"dream-{int(now - 7200)}", "kind": "dream", "date": "2026-03-08", "created_at": now, "text": "已有梦", "dream": True, "source_ids": [], "origin": "role_simulation"},
                ],
                "plans": {
                    "2026-03-08": {
                        "plan_id": "2026-03-08-p1", "serial": 1, "generated_at": now - 3600,
                        "items": [
                            {"id": f"h{k}", "title": f"历史{k}", "purpose": "p", "start": now - 3600 + k * 60, "end": now - 3550 + k * 60, "status": "completed", "origin": "role_simulation"}
                            for k in range(10)
                        ] + [{"id": "act-fail", "title": "卡住的活动", "purpose": "p", "start": now - 100, "end": now - 10, "status": "active", "origin": "role_simulation"}],
                    }
                },
                "jobs": {
                    "j-act": {"id": "j-act", "kind": "activity", "status": "running", "created_at": now - 5, "args": {"item": {"id": "act-fail"}}, "dedupe_key": "activity:act-fail"},
                },
            }))

            snap = engine.status(GLOBAL_ACCOUNT, GLOBAL_ROLE, now)
            with self.assertRaises(ValueError):
                engine.enqueue(
                    GLOBAL_ACCOUNT, GLOBAL_ROLE, "diary", request_id="dup-d",
                    args={"date": "2026-03-08"}, now=now,
                    expected_revision=snap["revision"], expected_epoch=snap["epoch"], expected_state_version=snap["state_version"],
                )
            with self.assertRaises(ValueError):
                engine.enqueue(
                    GLOBAL_ACCOUNT, GLOBAL_ROLE, "dream", request_id="dup-m", now=now,
                    expected_revision=snap["revision"], expected_epoch=snap["epoch"], expected_state_version=snap["state_version"],
                )

            # _finish_job failed moves overdue active activity out of active
            engine._finish_job(GLOBAL_ACCOUNT, GLOBAL_ROLE, "j-act", "failed", "err", "")
            st, _ = store.try_load(GLOBAL_ACCOUNT, GLOBAL_ROLE)
            act_item = next(i for i in st["plans"]["2026-03-08"]["items"] if i["id"] == "act-fail")
            self.assertEqual(act_item["status"], "interrupted")

            # _apply_maintenance preserves previous mood when result lacks mood
            err = engine._apply_maintenance(st, rl.LifeJob("jm", GLOBAL_ACCOUNT, GLOBAL_ROLE, "default", "maintenance", "r", "c", 0, 0, "", cfg, {}), {"energy": "一般"}, now)
            self.assertEqual(err, "")
            self.assertEqual(st["mood"]["mood"], "开心")
            self.assertEqual(st["mood"]["energy"], "一般")

            # _apply_plan caps items to MAX_PLAN_ITEMS - len(history) AND uses DST-aware per-day clock
            job = rl.LifeJob("jp", GLOBAL_ACCOUNT, GLOBAL_ROLE, "default", "plan", "r", "c", 0, 0, "", {**cfg, "activities_per_day": 12}, {"date": "2026-03-08", "replace": True})
            res_items = [{"title": f"新{k}", "purpose": "p", "start": f"{12 + k:02d}:00", "end": f"{12 + k:02d}:30"} for k in range(5)]
            err2 = engine._apply_plan(st, job, {"items": res_items}, rl._Clock("America/New_York"), now)
            self.assertEqual(err2, "")
            self.assertLessEqual(len(st["plans"]["2026-03-08"]["items"]), rl.MAX_PLAN_ITEMS)

            # list_records clamps oversized page to pages
            recs = engine.list_records(GLOBAL_ACCOUNT, GLOBAL_ROLE, "experiences", page=99, page_size=20)
            self.assertEqual(recs["page"], recs["pages"])

            # snapshot reflects live sleeping vs sleep_enabled=False
            engine_off, _, _ = self._make_engine(tmp, others={"personality_prompt": "你是测试角色"}, cfg_extra={"sleep_enabled": False})
            snap_off = engine_off.snapshot(GLOBAL_ACCOUNT, GLOBAL_ROLE, now)
            self.assertFalse(snap_off["sleeping"])
            self.assertFalse(snap_off["recent_woke_up"])
            self.assertIsNone(snap_off["sleep"])

            # P2-2 #3: retrying a failed activity job sets manual=True so _apply_activity restores interrupted item instead of failing with stale:item_missing
            p_act = engine._persona(GLOBAL_ACCOUNT, GLOBAL_ROLE)
            store.update(GLOBAL_ACCOUNT, GLOBAL_ROLE, lambda s: (
                s["plans"]["2026-03-08"]["items"].__setitem__(-1, {
                    "id": "act-retry", "title": "中断重跑活动", "purpose": "p",
                    "start": now - 100, "end": now + 600, "status": "interrupted", "origin": "role_simulation",
                }),
                s["jobs"].__setitem__("j-retry-act", {
                    "id": "j-retry-act", "kind": "activity", "status": "failed", "created_at": now - 5,
                    "args": {"date": "2026-03-08", "item": {"id": "act-retry", "title": "中断重跑活动"}},
                    "revision": p_act.revision, "config_revision": "c1", "epoch": 0, "generation": 0, "attempts": 1,
                    "dedupe_key": "activity:act-retry", "request_id": "ra1",
                }),
            ))
            snap_act = engine.status(GLOBAL_ACCOUNT, GLOBAL_ROLE, now)
            retried_id = engine.retry_job(
                GLOBAL_ACCOUNT, GLOBAL_ROLE, "j-retry-act",
                request_id="ra2",
                expected_revision=snap_act["revision"],
                expected_epoch=snap_act["epoch"],
                expected_state_version=snap_act["state_version"],
            )
            st_after_retry, _ = store.try_load(GLOBAL_ACCOUNT, GLOBAL_ROLE)
            retried_raw = st_after_retry["jobs"][retried_id]
            self.assertTrue(retried_raw["args"].get("manual"))
            act_job = rl.LifeJob(
                retried_id, GLOBAL_ACCOUNT, GLOBAL_ROLE, "default", "activity",
                "ra2", retried_raw["config_revision"], retried_raw["epoch"],
                retried_raw["generation"], retried_raw["dedupe_key"], cfg, retried_raw["args"],
            )
            apply_err = engine._apply_activity(st_after_retry, act_job, {
                "text": "活动顺利补充体验完成", "mood": "开心", "energy": "充沛",
            }, now)
            self.assertEqual(apply_err, "")
            restored = next(i for i in st_after_retry["plans"]["2026-03-08"]["items"] if i["id"] == "act-retry")
            self.assertEqual(restored["status"], "completed")


if __name__ == "__main__":
    unittest.main()
