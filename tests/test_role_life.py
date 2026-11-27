# -*- coding: utf-8 -*-
"""角色身份与生活内核离线测试。

全部使用 TempDirectory、fake clock、fake 模型和 fake 账号额度；不联网、
不读真实聊天、不启动 QQ。运行：python -m pytest tests/test_role_life.py -q
"""
from __future__ import annotations

import asyncio
import copy
import inspect
import json
import tempfile
import time as monotime
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from bot import persona as persona_mod
from bot import role_life as rl
from bot import role_life_store as rls
from bot.persona import Persona, persona_role_key, resolve_persona
from bot.role_life_store import RoleLifeStore

TZ = ZoneInfo("Asia/Shanghai")
ACCOUNT = "123456"
ROLE = "p1"


def ts(day: str, hhmm: str) -> float:
    hour, minute = (int(x) for x in hhmm.split(":"))
    y, m, d = (int(x) for x in day.split("-"))
    return datetime(y, m, d, hour, minute, tzinfo=TZ).timestamp()


class Harness:
    def __init__(self, tmp, cfg: dict, *, generate=None, reserve=None,
                 persona_revision="rev-1", persona_text="你是测试角色"):
        self.now = ts("2026-03-10", "09:00")
        self.store = RoleLifeStore(Path(tmp))
        self.config = {"roles": {ACCOUNT: {ROLE: rl.normalize_role_config(cfg)}}}
        self.generate_calls = []
        self.reserve_calls = []
        self.log = []
        self.persona_text = persona_text
        # 用真实的 persona 模块解析，revision 由模板与 bot_name 算出。
        self.others = {
            "personality_prompt": persona_text,
            "personality_presets": [
                {"id": ROLE, "name": "甲", "prompt": persona_text},
                {"id": "p2", "name": "乙", "prompt": "另一个人设"},
            ],
            "active_personality_preset": ROLE,
            "private_personality_preset": "",
            "group_personality_preset": "",
        }
        self.revision = persona_mod.resolve_persona(
            self.others, bot_name="测试机器人").revision
        if persona_revision != "rev-1":
            self.revision = persona_revision
        self.generate = generate or self.default_generate
        self.reserve = reserve or self.default_reserve

        self.engine = rl.RoleLifeEngine(
            self.store,
            config_getter=lambda: self.config,
            persona_getter=lambda: self.others,
            bot_name="测试机器人",
            generate=self.generate,
            reserve_account=self.reserve,
            account_limit=lambda: 192,
            clock=lambda: self.now,
            log=self.log.append,
        )
        self.engine.bind_account(ACCOUNT, 1)

    def default_reserve(self, account):
        self.reserve_calls.append(account)
        return True

    async def default_generate(self, job, reserve_attempt):
        # 适配器契约：reserve 拒绝时返回 None，绝不发请求。
        if not await reserve_attempt():
            return None
        self.generate_calls.append(job)
        if job.kind == "plan":
            day = job.args["date"]
            return {"items": [
                {"title": "晨间阅读", "purpose": "安静看书", "start": "09:00", "end": "10:00"},
                {"title": "整理房间", "purpose": "收拾桌面", "start": "14:00", "end": "15:00"},
                # 睡段内的活动必须被拒绝
                {"title": "深夜散步", "purpose": "不该存在", "start": "01:00", "end": "02:00"},
                # 与第一条重叠，必须被拒绝
                {"title": "重叠活动", "purpose": "不该存在", "start": "09:30", "end": "09:45"},
            ]}
        if job.kind == "activity":
            return {"text": f"{job.args['item']['title']}结束了，感觉还不错。"}
        if job.kind == "diary":
            return {"text": "今天过得挺充实。"}
        if job.kind == "dream":
            return {"text": "梦见一条很长的走廊。"}
        if job.kind == "maintenance":
            return {"mood": "平静", "energy": "一般"}
        raise AssertionError(f"unexpected kind {job.kind}")

    def expect(self):
        current = self.store.load(ACCOUNT, ROLE)
        return {"expected_revision": current["revision"],
                "expected_epoch": int(current["epoch"]),
                "expected_state_version": int(current["state_version"])}

    def enqueue(self, kind, request_id, args=None):
        return self.engine.enqueue(ACCOUNT, ROLE, kind, request_id=request_id,
                                   args=args, **self.expect())

    def clear(self, request_id):
        return self.engine.clear_role(ACCOUNT, ROLE, request_id=request_id,
                                      **self.expect())

    async def advance(self, day, hhmm):
        self.now = ts(day, hhmm)
        await self.engine.advance(self.now)

    async def tick(self, seconds: float = 0.0):
        """模拟下一次 tick：只推进 now，不强制秒级连续。"""
        await self.engine.advance(self.now + seconds)
        self.now += seconds

    async def run_until(self, day, hhmm, step=600):
        """按真实 tick 节奏推进到目标时刻（步长小于离线阈值）。"""
        target = ts(day, hhmm)
        while self.now + step < target:
            await self.tick(step)
            await self.wait_idle()
        await self.advance(day, hhmm)
        return await self.wait_idle()

    async def wait_idle(self, timeout=10.0):
        deadline = monotime.monotonic() + timeout
        while monotime.monotonic() < deadline:
            state = self.store.load(ACCOUNT, ROLE)
            active = [j for j in state.get("jobs", {}).values()
                      if j.get("status") in ("pending", "running")]
            if not active and self.engine._queue_size == 0:
                return state
            await asyncio.sleep(0.01)
        raise AssertionError("engine did not become idle")

    def state(self):
        return self.store.load(ACCOUNT, ROLE)


def enabled_cfg(**overrides):
    cfg = {"enabled": True, "activities_per_day": 4}
    cfg.update(overrides)
    return cfg


# ---------------------------------------------------------------------------
# 身份解析
# ---------------------------------------------------------------------------

class PersonaTests(unittest.TestCase):
    OTHERS = {
        "personality_prompt": "旧人格 {bot_name}",
        "personality_presets": [
            {"id": "default", "name": "默认", "prompt": "默认人格 {user_name}"},
            {"id": "p1", "name": "甲", "prompt": "甲人格"},
            {"id": "p2", "name": "乙", "prompt": "甲人格"},
        ],
        "active_personality_preset": "p1",
        "private_personality_preset": "",
        "group_personality_preset": "p2",
    }

    def resolve(self, context="", override=""):
        return resolve_persona(self.OTHERS, bot_name="忻城", user_name="你",
                               context_type=context, preset_id_override=override)

    def test_default_and_context_following(self):
        self.assertEqual(self.resolve().role_id, "p1")
        self.assertEqual(self.resolve().raw_prompt, "甲人格")
        self.assertEqual(self.resolve("private").role_id, "p1")  # 空串跟随默认
        self.assertEqual(self.resolve("group").role_id, "p2")

    def test_override_and_invalid_override(self):
        self.assertEqual(self.resolve("private", "p2").role_id, "p2")
        self.assertEqual(self.resolve("private", "gone").role_id, "p1")

    def test_empty_preset_raises_not_fallback(self):
        others = dict(self.OTHERS, active_personality_preset="empty")
        others["personality_presets"] = others["personality_presets"] + [
            {"id": "empty", "name": "空", "prompt": "  "}]
        with self.assertRaises(ValueError) as ctx:
            resolve_persona(others, bot_name="X", user_name="你")
        self.assertIn("内容为空", str(ctx.exception))

    def test_missing_prompt_raises(self):
        with self.assertRaises(ValueError):
            resolve_persona({}, bot_name="X", user_name="你")

    def test_same_text_different_presets_not_merged(self):
        first, second = self.resolve("group"), self.resolve("private", "p1")
        self.assertEqual(first.raw_prompt, second.raw_prompt)
        self.assertNotEqual(first.role_id, second.role_id)
        self.assertNotEqual(first.revision, second.revision)

    def test_revision_tracks_role_template_only(self):
        base = self.resolve()
        # 同一人设分别面向 A/B：revision 必须相等，只有渲染结果不同。
        to_a = resolve_persona(self.OTHERS, bot_name="忻城", user_name="A")
        to_b = resolve_persona(self.OTHERS, bot_name="忻城", user_name="B")
        self.assertEqual(to_a.revision, to_b.revision)
        self.assertEqual(to_a.revision, base.revision)
        # {bot_name} 已在模板中保留，渲染时才替换
        default_persona = resolve_persona(
            dict(self.OTHERS, active_personality_preset=""), bot_name="忻城", user_name="A")
        self.assertIn("{bot_name}", default_persona.raw_prompt)
        self.assertEqual(default_persona.render(bot_name="忻城"), "旧人格 忻城")
        self.assertNotEqual(default_persona.render(bot_name="忻城"),
                            default_persona.render(bot_name="另一个"))
        # {user_name} 同理：只影响渲染
        user_persona = resolve_persona(
            dict(self.OTHERS, active_personality_preset="default"),
            bot_name="忻城", user_name="A")
        self.assertEqual(user_persona.render(user_name="A"), "默认人格 A")
        self.assertEqual(user_persona.render(user_name="B"), "默认人格 B")
        # 改模板才改 revision
        changed = dict(self.OTHERS)
        changed["personality_presets"] = [dict(p) for p in self.OTHERS["personality_presets"]]
        changed["personality_presets"][1]["prompt"] = "甲人格改"
        new = resolve_persona(changed, bot_name="忻城", user_name="你")
        self.assertEqual(base.role_id, new.role_id)
        self.assertNotEqual(base.revision, new.revision)
        # 同一角色改 bot_name（角色共同配置）也应改变 revision
        self.assertNotEqual(base.revision, resolve_persona(
            self.OTHERS, bot_name="别的名字").revision)

    def test_role_key_is_collision_free_and_rejects_empty(self):
        self.assertNotEqual(persona_role_key("p1"), "p1")  # 统一哈希编码
        self.assertNotEqual(persona_role_key("a/b"), persona_role_key("a_b"))
        self.assertEqual(persona_role_key("a/b"), persona_role_key("a/b"))
        self.assertLessEqual(len(persona_role_key("x" * 400)), 64)
        for key in (persona_role_key("../etc"), persona_role_key("角色"),
                    persona_role_key("x" * 400)):
            self.assertRegex(key, r"^[A-Za-z0-9_-]+$")
        with self.assertRaises(ValueError):
            persona_role_key("")
        with self.assertRaises(ValueError):
            persona_role_key("   " and "")


# ---------------------------------------------------------------------------
# 配置与存储
# ---------------------------------------------------------------------------

class ConfigStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def test_config_defaults_and_bounds(self):
        cfg = rl.normalize_role_config({})
        self.assertFalse(cfg["enabled"])
        self.assertTrue(cfg["plan_enabled"])
        self.assertFalse(cfg["maintenance_enabled"])
        self.assertEqual(cfg["maintenance_interval_seconds"], 1800)
        self.assertEqual(cfg["sleep_start"], "00:30")
        self.assertEqual(cfg["diary_time"], "23:50")
        self.assertNotIn("role_daily_limit", cfg)
        self.assertNotIn("weather_city", cfg)
        self.assertEqual(cfg["timezone"], "Asia/Shanghai")
        with self.assertRaises(ValueError):
            rl.normalize_role_config({"activities_per_day": 13})
        # 已移除旧字段输入时应安全忽略，不报错也不保留。
        legacy = rl.normalize_role_config({"role_daily_limit": 0, "weather_city": "上海"})
        self.assertNotIn("role_daily_limit", legacy)
        self.assertNotIn("weather_city", legacy)
        with self.assertRaises(ValueError):
            rl.normalize_role_config({"sleep_start": "24:00"})
        with self.assertRaises(ValueError):
            rl.normalize_role_config({"timezone": "Not/AZone"})
        with self.assertRaises(ValueError):
            rl.normalize_role_config({"enabled": "yes"})
        dropped = rl.normalize_role_config({"unknown_key": 1})
        self.assertNotIn("unknown_key", dropped)

    def test_section_isolation_and_invalid_account(self):
        section = rl.normalize_role_life_section({"roles": {ACCOUNT: {ROLE: {"enabled": True}}}})
        self.assertEqual(rl.role_config(section, ACCOUNT, ROLE)["enabled"], True)
        self.assertIsNone(rl.role_config(section, "999", ROLE))
        self.assertIsNone(rl.role_config(section, ACCOUNT, "p2"))
        with self.assertRaises(ValueError):
            rl.normalize_role_life_section({"roles": {"../evil": {ROLE: {}}}})

    def test_store_isolated_by_account_and_role(self):
        store = RoleLifeStore(Path(self.temp.name))
        store.update(ACCOUNT, "p1", lambda s: s["experiences"].append({
            "id": "e1", "kind": "activity", "date": "2026-03-10", "created_at": 1,
            "text": "只有 p1", "source_ids": [], "origin": "role_simulation", "dream": False}))
        self.assertEqual(len(store.load(ACCOUNT, "p2")["experiences"]), 0)
        self.assertEqual(len(store.load("654321", "p1")["experiences"]), 0)
        self.assertTrue(str(store.state_path(ACCOUNT, "p1")).endswith("state.json"))
        self.assertNotIn("..", str(store.state_path(ACCOUNT, "../x")))
        with self.assertRaises(ValueError):
            store.state_path("account-name", "p1")
        self.assertFalse(Path(store.state_path(ACCOUNT, "never-seen")).exists())

    def test_store_clear_keeps_budget_and_bumps_epoch(self):
        store = RoleLifeStore(Path(self.temp.name))
        def seed(state):
            state["budget"]["calls"] = [1000.0]
            state["experiences"].append({
                "id": "e1", "kind": "activity", "date": "2026-03-10", "created_at": 1,
                "text": "旧经历", "source_ids": [], "origin": "role_simulation", "dream": False})
            state["deliveries"]["group|1"] = {"item_ids": ["e1"], "attempts": {"e1": "sent"}}
        store.update(ACCOUNT, ROLE, seed)
        cleared = store.clear(ACCOUNT, ROLE)
        self.assertEqual(cleared["epoch"], 1)
        self.assertEqual(cleared["experiences"], [])
        self.assertEqual(cleared["budget"]["calls"], [1000.0])
        self.assertEqual(cleared["deliveries"]["group|1"]["attempts"], {"e1": "sent"})

    def test_corrupt_state_raises_and_is_not_overwritten(self):
        store = RoleLifeStore(Path(self.temp.name))
        path = store.state_path(ACCOUNT, ROLE)
        path.parent.mkdir(parents=True, exist_ok=True)
        original = b"{not json"
        path.write_bytes(original)
        with self.assertRaises(rls.RoleLifeCorruptError):
            store.load(ACCOUNT, ROLE)
        # 常规更新绝不覆盖损坏文件
        with self.assertRaises(rls.RoleLifeCorruptError):
            store.update(ACCOUNT, ROLE, lambda s: s.update(experiences=[]))
        self.assertEqual(path.read_bytes(), original)
        state, error = store.try_load(ACCOUNT, ROLE)
        self.assertIsNone(state)
        self.assertTrue(error)
        # 身份字段不匹配同样拒绝
        path.write_text(json.dumps({"account": "999", "role_id": ROLE}), encoding="utf-8")
        with self.assertRaises(rls.RoleLifeCorruptError):
            store.load(ACCOUNT, ROLE)

    def test_experiences_and_jobs_are_pruned(self):
        store = RoleLifeStore(Path(self.temp.name))
        def seed(state):
            state["experiences"] = [{
                "id": f"e{i}", "kind": "activity", "date": "2026-03-10",
                "created_at": i, "text": "x", "source_ids": [],
                "origin": "role_simulation", "dream": False}
                for i in range(rls.MAX_EXPERIENCES + 20)]
            state["jobs"] = {f"j{i}": {"id": f"j{i}", "kind": "plan", "status": "succeeded",
                                      "created_at": i} for i in range(rls.MAX_JOBS + 20)}
            state["job_order"] = [f"j{i}" for i in range(rls.MAX_JOBS + 20)]
        state = store.update(ACCOUNT, ROLE, seed)
        self.assertEqual(len(state["experiences"]), rls.MAX_EXPERIENCES)
        self.assertEqual(len(state["jobs"]), rls.MAX_JOBS)


# ---------------------------------------------------------------------------
# 完整一天 / 状态机 / 恢复
# ---------------------------------------------------------------------------

class FullDayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.h = Harness(self.temp.name, enabled_cfg())
        self.addAsyncCleanup(self.h.engine.stop)

    def plan_items(self, state):
        return state["plans"]["2026-03-10"]["items"]

    async def test_full_day_plan_activity_diary_sleep_and_dream(self):
        h = self.h
        await h.advance("2026-03-10", "09:00")
        state = await h.wait_idle()
        items = self.plan_items(state)
        titles = [i["title"] for i in items]
        self.assertEqual(titles, ["晨间阅读", "整理房间"])
        self.assertTrue(all(i["status"] == "planned" for i in items))
        self.assertTrue(all(i["origin"] == "role_simulation" for i in items))
        # 计划生成后立刻按当前时间推进：第一条已到点
        await h.advance("2026-03-10", "09:00")
        state = await h.wait_idle()
        self.assertEqual(self.plan_items(state)[0]["status"], "active")
        # 日程 ≠ 完成：连续 tick 到结束时间，经历尚未生成时仍是 active
        state = await h.run_until("2026-03-10", "10:05")
        entered = [j for j in state["jobs"].values() if j["kind"] == "activity"]
        self.assertTrue(entered and all(j["status"] == "succeeded" for j in entered))
        items = self.plan_items(state)
        self.assertEqual(items[0]["status"], "completed")
        self.assertTrue(items[0]["experience_id"])
        activity_exps = [e for e in state["experiences"] if e["kind"] == "activity"]
        self.assertEqual(len(activity_exps), 1)
        self.assertEqual(activity_exps[0]["source_ids"], [items[0]["id"]])
        state = await h.run_until("2026-03-10", "23:55")
        # 第二条活动结束 + 日记
        diaries = [e for e in state["experiences"] if e["kind"] == "diary"]
        self.assertEqual(len(diaries), 1)
        self.assertEqual(diaries[0]["date"], "2026-03-10")
        self.assertEqual(self.plan_items(state)[1]["status"], "completed")
        # 跨午夜进入睡眠
        await h.advance("2026-03-11", "00:40")
        state = await h.wait_idle()
        self.assertTrue(h.engine.is_sleeping(ACCOUNT, ROLE, h.now))
        self.assertTrue(state["sleep"])
        self.assertFalse(state["sleep"]["wake_dream_done"])
        before = len(state["experiences"])
        await h.advance("2026-03-11", "02:00")
        state = await h.wait_idle()
        self.assertEqual(len(state["experiences"]), before)  # 睡觉不补内容
        # 醒来产生一次梦，且明确标记
        await h.advance("2026-03-11", "08:40")
        state = await h.wait_idle()
        dreams = [e for e in state["experiences"] if e["kind"] == "dream"]
        self.assertEqual(len(dreams), 1)
        self.assertTrue(dreams[0]["dream"])
        self.assertFalse(h.engine.is_sleeping(ACCOUNT, ROLE, h.now))
        # 聊天快照在睡眠期间也照常可读（不阻塞普通回复）
        snapshot = h.engine.snapshot(ACCOUNT, ROLE, ts("2026-03-10", "03:00"))
        self.assertTrue(snapshot["sleeping"])
        self.assertIn("recent", snapshot)

    async def test_advance_is_non_blocking_and_single_worker(self):
        h = self.h
        gate = asyncio.Event()
        started = asyncio.Event()
        active = {"now": 0, "max": 0}

        async def slow_generate(job, reserve_attempt):
            h.generate_calls.append(job)
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
            started.set()
            try:
                assert await reserve_attempt()
                await gate.wait()
                if job.kind == "plan":
                    return {"items": [{"title": "慢活动", "purpose": "等",
                                       "start": "11:00", "end": "12:00"}]}
                return {"text": "慢结果"}
            finally:
                active["now"] -= 1

        h.engine._generate = slow_generate
        begin = monotime.monotonic()
        await h.advance("2026-03-10", "09:00")
        self.assertLess(monotime.monotonic() - begin, 1.0)  # advance 不等待模型
        await asyncio.wait_for(started.wait(), 2)
        # 慢生成期间 tick 仍可推进
        await h.advance("2026-03-10", "09:30")
        gate.set()
        await h.wait_idle()
        self.assertEqual(active["max"], 1)

    async def test_queue_is_bounded(self):
        h = self.h
        async def quick(job, reserve_attempt):
            if not await reserve_attempt():
                return None
            return {"text": "x"}
        h.engine._generate = quick
        await h.advance("2026-03-10", "09:00")
        await h.wait_idle()
        for i in range(40):
            h.enqueue("maintenance", f"r{i}")
        # 队列与历史都有上界（入队即断言，不依赖 worker 消费速度）
        self.assertLessEqual(h.engine._queue_size, rl.MAX_TOTAL_QUEUE)
        state = h.state()
        self.assertLessEqual(len(state["jobs"]), rls.MAX_JOBS)
        await h.engine.stop()

    async def test_multi_day_offline_skips_without_backfill(self):
        h = self.h
        await h.advance("2026-03-10", "09:00")
        await h.wait_idle()
        h.generate_calls.clear()
        # 三天后才回来：旧计划只标 skipped，不补活动/日记
        await h.advance("2026-03-13", "12:00")
        state = await h.wait_idle()
        old = self.plan_items(state)
        self.assertTrue(all(i["status"] in ("skipped", "interrupted", "completed") for i in old))
        kinds = {j["kind"] for j in state["jobs"].values() if j["status"] == "succeeded"}
        self.assertNotIn("dream", kinds)
        new_days = [d for d in state["plans"] if d > "2026-03-10"]
        self.assertTrue(new_days, "offline return should still plan today's remaining day")
        for item in state["plans"][new_days[0]]["items"]:
            self.assertGreaterEqual(item["end"], h.now)

    async def test_restart_marks_running_unknown_and_never_replays(self):
        h = self.h
        def seed(state):
            state["jobs"]["legacy"] = {
                "id": "legacy", "kind": "plan", "status": "running",
                "created_at": h.now - 60, "started_at": h.now - 30,
                "args": {"date": "2026-03-10"}, "revision": h.revision, "epoch": 0,
                "generation": 1, "attempts": 1, "dedupe_key": "plan:2026-03-10",
            }
            state["job_order"].append("legacy")
        h.store.update(ACCOUNT, ROLE, seed)
        await h.advance("2026-03-10", "09:00")
        state = await h.wait_idle()
        self.assertEqual(state["jobs"]["legacy"]["status"], "unknown")
        self.assertEqual(state["jobs"]["legacy"]["error"], "interrupted_on_restart")
        # unknown 不自动重放，也不会生成新计划（dedupe 保留）
        plans = [j for j in state["jobs"].values() if j["kind"] == "plan" and j["id"] != "legacy"]
        self.assertEqual(plans, [])

    async def test_disabled_role_and_no_account_do_nothing(self):
        h = self.h
        h.config["roles"][ACCOUNT][ROLE]["enabled"] = False
        await h.advance("2026-03-10", "09:00")
        self.assertEqual(h.generate_calls, [])
        h.config["roles"][ACCOUNT][ROLE]["enabled"] = True
        h.engine.unbind_account(1)
        await h.advance("2026-03-10", "09:00")
        self.assertEqual(h.generate_calls, [])
        h.engine.bind_account(ACCOUNT, 2)
        await h.advance("2026-03-10", "09:00")
        await h.wait_idle()
        self.assertTrue(h.generate_calls)


class StateMachineEdgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    async def test_dream_only_once_per_wake_and_not_after_long_gap(self):
        h = Harness(self.temp.name, enabled_cfg())
        self.addAsyncCleanup(h.engine.stop)
        await h.advance("2026-03-10", "23:55")
        await h.wait_idle()
        # 跨到睡段
        await h.advance("2026-03-11", "00:40")
        await h.wait_idle()
        await h.advance("2026-03-11", "08:40")
        state = await h.wait_idle()
        self.assertEqual(len([e for e in state["experiences"] if e["kind"] == "dream"]), 1)
        self.assertTrue(state["sleep"]["wake_dream_done"])
        # 再次 tick 不重复做梦
        await h.advance("2026-03-11", "08:45")
        state = await h.wait_idle()
        self.assertEqual(len([e for e in state["experiences"] if e["kind"] == "dream"]), 1)
        # 长时间离开后回来（超过 3 小时），不补梦；之前的梦保留
        await h.advance("2026-03-12", "00:40")
        await h.wait_idle()
        dreams_before = len([e for e in h.state()["experiences"] if e["kind"] == "dream"])
        h.generate_calls.clear()
        await h.advance("2026-03-12", "12:00")
        state = await h.wait_idle()
        self.assertEqual(
            len([e for e in state["experiences"] if e["kind"] == "dream"]), dreams_before)
        self.assertFalse([c for c in h.generate_calls if c.kind == "dream"])

    async def test_maintenance_default_off_and_validated_when_on(self):
        h = Harness(self.temp.name, enabled_cfg())
        self.addAsyncCleanup(h.engine.stop)
        await h.advance("2026-03-10", "09:00")
        state = await h.wait_idle()
        self.assertFalse([j for j in state["jobs"].values() if j["kind"] == "maintenance"])
        h.config["roles"][ACCOUNT][ROLE]["maintenance_enabled"] = True
        await h.advance("2026-03-10", "09:30")
        state = await h.wait_idle()
        self.assertTrue([j for j in state["jobs"].values()
                         if j["kind"] == "maintenance" and j["status"] == "succeeded"])
        self.assertEqual(state["mood"]["mood"], "平静")
        # 非法维护输出被拒绝，保留旧状态
        h.config["roles"][ACCOUNT][ROLE]["maintenance_interval_seconds"] = 300

        async def bad(job, reserve_attempt):
            if not await reserve_attempt():
                return None
            return {"mood": "", "energy": ""}

        h.engine._generate = bad
        before = dict(state["mood"])
        await h.advance("2026-03-10", "09:35")
        state = await h.wait_idle()
        self.assertEqual(state["mood"], before)

    async def test_corrupt_state_blocks_generation_until_cleared(self):
        h = Harness(self.temp.name, enabled_cfg())
        self.addAsyncCleanup(h.engine.stop)
        path = h.store.state_path(ACCOUNT, ROLE)
        path.parent.mkdir(parents=True, exist_ok=True)
        original = b"{broken"
        path.write_bytes(original)
        await h.advance("2026-03-10", "09:00")
        self.assertEqual(h.generate_calls, [])
        self.assertEqual(path.read_bytes(), original)  # 原字节不变
        self.assertTrue(h.engine.snapshot(ACCOUNT, ROLE, h.now)["corrupt"])
        self.assertTrue(h.engine.status(ACCOUNT, ROLE, h.now)["corrupt"])
        self.assertTrue(h.engine.list_records(
            ACCOUNT, ROLE, "experiences")["corrupt"])
        self.assertEqual(h.engine.candidates(
            ACCOUNT, ROLE, is_group=True, chat_id="1", now=h.now), [])
        # 未二次确认不能清；显式 allow_corrupt=True 才重建
        self.assertFalse(h.engine.clear_role(
            ACCOUNT, ROLE, request_id="c1", expected_revision="x", expected_epoch=0))
        self.assertTrue(h.engine.clear_role(
            ACCOUNT, ROLE, request_id="c1", allow_corrupt=True))
        state = await h.wait_idle()
        self.assertFalse(state.get("corrupt"))
        # 完全不可读的损坏：原 budget 无法校验 -> 保护性暂停本角色 24h
        self.assertTrue(state["recovered_from_corrupt"])
        self.assertTrue(state["budget_unknown_until"] > h.now)
        self.assertTrue(state["corrupt_backup"].startswith("state.corrupt-"))
        self.assertTrue((path.parent / state["corrupt_backup"]).exists())
        status = h.engine.status(ACCOUNT, ROLE, h.now)
        self.assertTrue(status["budget_unknown"])
        self.assertTrue(status["recovered_from_corrupt"])
        self.assertEqual(h.engine.usage(ACCOUNT, ROLE, h.now)["budget_unknown"], True)
        self.assertFalse(h.engine._reserve_attempt(
            ACCOUNT, ROLE, rl.DEFAULT_ROLE_CONFIG))
        await h.advance("2026-03-10", "09:01")
        await h.wait_idle()
        self.assertEqual(h.generate_calls, [])  # 保护期内不再获得完整额度

    async def test_timezone_change_moves_day_boundary(self):
        async def ny_plan(job, reserve_attempt):
            if not await reserve_attempt():
                return None
            return {"items": [{"title": "傍晚散步", "purpose": "吹风",
                               "start": "21:00", "end": "22:00"}]}

        h = Harness(self.temp.name, enabled_cfg(timezone="America/New_York"),
                    generate=ny_plan)
        self.addAsyncCleanup(h.engine.stop)
        # 上海 2026-03-10 09:00 = 纽约 2026-03-09 20:00
        await h.advance("2026-03-10", "09:00")
        state = await h.wait_idle()
        # 日期按角色时区而不是上海时区
        self.assertIn("2026-03-09", state["plans"])
        self.assertNotIn("2026-03-10", state["plans"])
        item = state["plans"]["2026-03-09"]["items"][0]
        local_start = datetime.fromtimestamp(item["start"], ZoneInfo("America/New_York"))
        self.assertEqual((local_start.hour, local_start.minute), (21, 0))
        # 上海 09:00 = 纽约 20:00（未睡）；上海 13:00 = 纽约 01:00（睡段内）
        self.assertFalse(h.engine.is_sleeping(ACCOUNT, ROLE, h.now))
        self.assertTrue(h.engine.is_sleeping(
            ACCOUNT, ROLE, ts("2026-03-10", "13:00")))
        self.assertFalse(h.engine.is_sleeping(
            ACCOUNT, ROLE, ts("2026-03-11", "02:00")))  # 纽约 03-10 14:00 已醒

    async def test_default_legacy_role_is_independent_of_active_preset(self):
        # 生活角色 default 用旧 personality_prompt；当前聊天已切到 p1 时仍必须
        # 解析出旧文本，不能把用户正在用的 p1 人格偷给 default 生活。
        h = Harness(self.temp.name, enabled_cfg())
        h.others["personality_prompt"] = "旧默认人设"
        h.others["personality_presets"].append(
            {"id": "default", "name": "默认", "prompt": "旧默认人设"})
        h.config["roles"][ACCOUNT]["default"] = rl.normalize_role_config(enabled_cfg())
        legacy = h.engine._persona(ACCOUNT, "default")
        self.assertIsNotNone(legacy)
        self.assertEqual(legacy.role_id, "default")
        self.assertEqual(legacy.raw_prompt, "旧默认人设")
        active = h.engine._persona(ACCOUNT, ROLE)
        self.assertEqual(active.raw_prompt, h.persona_text)
        self.assertNotEqual(legacy.revision, active.revision)

    async def test_persona_error_does_not_create_jobs(self):
        h = Harness(self.temp.name, enabled_cfg())

        def broken():
            raise ValueError("主对话系统提示词为空")

        h.engine._persona_getter = broken
        self.addAsyncCleanup(h.engine.stop)
        await h.advance("2026-03-10", "09:00")
        state = h.state()
        self.assertEqual([j for j in state["jobs"].values() if j["status"] != "failed"], [])
        self.assertEqual(h.generate_calls, [])
        self.assertIn("人设解析失败", state["metrics"]["last_error"])

    async def test_sleep_window_filters_plan_items(self):
        async def night_plan(job, reserve_attempt):
            if not await reserve_attempt():
                return None
            return {"items": [{"title": "凌晨散步", "purpose": "睡不着",
                               "start": "01:00", "end": "01:30"}]}

        for sleep_enabled, expect in ((True, []), (False, ["凌晨散步"])):
            with self.subTest(sleep_enabled=sleep_enabled):
                temp = tempfile.TemporaryDirectory()
                self.addCleanup(temp.cleanup)
                h = Harness(temp.name, enabled_cfg(sleep_enabled=sleep_enabled),
                            generate=night_plan)
                try:
                    await h.advance("2026-03-10", "09:00")
                    await h.wait_idle()
                    # 手动排明天的计划：01:00 落在 00:30~08:30 睡段内
                    current = h.store.load(ACCOUNT, ROLE)
                    job_id = h.engine.enqueue(
                        ACCOUNT, ROLE, "plan", request_id="tomorrow",
                        args={"date": "2026-03-11"},
                        expected_revision=current["revision"],
                        expected_epoch=int(current["epoch"]),
                        expected_state_version=int(current["state_version"]))
                    self.assertTrue(job_id)
                    state = await h.wait_idle()
                    plan = state["plans"].get("2026-03-11")
                    titles = [i["title"] for i in plan["items"]] if plan else []
                    self.assertEqual(titles, expect)
                finally:
                    await h.engine.stop()


class BudgetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    async def test_exactly_one_role_and_account_reservation_per_request(self):
        h = Harness(self.temp.name, enabled_cfg())
        self.addAsyncCleanup(h.engine.stop)
        await h.advance("2026-03-10", "09:00")
        await h.wait_idle()
        state = await h.run_until("2026-03-10", "10:05")
        # 计划 + 第一段活动经历
        self.assertEqual(len(h.generate_calls), 2)
        self.assertEqual(len(h.reserve_calls), 2)
        state = h.state()
        self.assertEqual(len(state["budget"]["calls"]), 2)
        self.assertEqual(h.engine.usage(ACCOUNT, ROLE, h.now)["role_24h"], 2)
        self.assertIsNone(h.engine.usage(ACCOUNT, ROLE, h.now)["role_limit"])
        # 已移除单角色 24 小时调用上限：第 3 次（14:00 活动经历）正常执行
        state = await h.run_until("2026-03-10", "15:05")
        self.assertEqual(len(h.generate_calls), 3)
        self.assertEqual(len(h.reserve_calls), 3)
        self.assertEqual(len(state["budget"]["calls"]), 3)
        self.assertEqual(state["plans"]["2026-03-10"]["items"][1]["status"], "completed")

    async def test_failed_request_still_consumes_budget(self):
        calls = []

        async def failing(job, reserve_attempt):
            calls.append(job)
            assert await reserve_attempt()
            raise RuntimeError("provider boom")

        h = Harness(self.temp.name, enabled_cfg(role_daily_limit=1), generate=failing)
        self.addAsyncCleanup(h.engine.stop)
        await h.advance("2026-03-10", "09:00")
        state = await h.wait_idle()
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(h.reserve_calls), 1)
        self.assertEqual(len(state["budget"]["calls"]), 1)
        self.assertEqual(state["jobs"][calls[0].job_id]["status"], "unknown")
        self.assertIn("boom", state["metrics"]["last_error"])

    async def test_exception_categories_map_to_status_and_retry_policy(self):
        cases = [
            (rl.LifeNotSent("not sent"), "failed", 1),
            (rl.LifeRequestRejected("rejected"), "failed", 2),  # 确定拒绝最多重试一次
            (TimeoutError("slow"), "unknown", 1),  # 可能已计费，不重试
            (RuntimeError("opaque"), "unknown", 1),  # 未分类默认不确定
        ]
        for index, (error, expected_status, expected_calls) in enumerate(cases):
            with self.subTest(error=type(error).__name__):
                calls = []

                async def adapter(job, reserve_attempt, error=error, calls=calls):
                    calls.append(job)
                    if not await reserve_attempt():
                        return None
                    raise error

                temp = tempfile.TemporaryDirectory()
                self.addCleanup(temp.cleanup)
                h = Harness(temp.name, enabled_cfg(), generate=adapter)
                try:
                    await h.advance("2026-03-10", "09:00")
                    state = await h.wait_idle()
                    self.assertEqual(len(calls), expected_calls)
                    job_ids = {j["id"] for j in state["jobs"].values()}
                    self.assertEqual(len(job_ids), 1)
                    job = next(iter(state["jobs"].values()))
                    self.assertEqual(job["status"], expected_status)
                finally:
                    await h.engine.stop()

    async def test_account_denied_means_no_request_and_no_role_charge(self):
        adapter_calls = []

        async def adapter(job, reserve_attempt):
            adapter_calls.append(job)
            if not await reserve_attempt():
                return None
            return {"text": "不应到达"}

        h = Harness(self.temp.name, enabled_cfg(), reserve=lambda account: False,
                    generate=adapter)
        self.addAsyncCleanup(h.engine.stop)
        await h.advance("2026-03-10", "09:00")
        state = await h.wait_idle()
        self.assertEqual(h.generate_calls, [])
        self.assertEqual(len(adapter_calls), 1)  # 适配器被调用，但没有发出请求
        self.assertEqual(state["budget"]["calls"], [])
        self.assertTrue([j for j in state["jobs"].values() if j["error"] == "budget_denied"])

    async def test_activity_end_with_failed_generation_stays_uncompleted(self):
        async def generate(job, reserve_attempt):
            if not await reserve_attempt():
                return None
            if job.kind == "plan":
                return {"items": [{"title": "读书", "purpose": "学习",
                                   "start": "09:00", "end": "10:00"}]}
            if job.kind == "activity":
                return {"text": "   "}  # 空经历：校验失败
            return {"text": "内容"}

        h = Harness(self.temp.name, enabled_cfg(), generate=generate)
        self.addAsyncCleanup(h.engine.stop)
        await h.advance("2026-03-10", "09:00")
        await h.wait_idle()
        state = await h.run_until("2026-03-10", "10:05")
        item = state["plans"]["2026-03-10"]["items"][0]
        self.assertEqual(item["status"], "interrupted")  # 到点≠完成且不再长期卡在 active
        self.assertEqual(item["experience_id"], "")
        self.assertEqual([e for e in state["experiences"] if e["kind"] == "activity"], [])
        failed = [j for j in state["jobs"].values() if j["kind"] == "activity"]
        self.assertTrue(failed and all(j["status"] == "failed" for j in failed))
        # 再 tick 仍不会把计划装成经历/完成
        state = await h.run_until("2026-03-10", "10:10")
        self.assertEqual(state["plans"]["2026-03-10"]["items"][0]["status"], "interrupted")
        self.assertEqual([e for e in state["experiences"] if e["kind"] == "activity"], [])

    async def test_storage_failure_never_sends_request(self):
        h = Harness(self.temp.name, enabled_cfg())
        self.addAsyncCleanup(h.engine.stop)
        original = h.store.update

        def failing_update(account, role_id, mutator):
            raise OSError("disk full")

        h.store.update = failing_update
        try:
            # 预算落盘失败：不发请求、不返回 True（宁可少生成也不绕限）。
            self.assertFalse(h.engine._reserve_attempt(
                ACCOUNT, ROLE, rl.DEFAULT_ROLE_CONFIG))
            self.assertEqual(h.generate_calls, [])
        finally:
            h.store.update = original


class InvalidationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    async def test_clear_invalidates_old_ticket_and_late_receipt(self):
        h = Harness(self.temp.name, enabled_cfg())
        self.addAsyncCleanup(h.engine.stop)
        await h.advance("2026-03-10", "09:00")
        await h.wait_idle()
        state = await h.run_until("2026-03-10", "10:05")
        exp_id = next(e["id"] for e in state["experiences"] if e["kind"] == "activity")
        claimed = h.engine.begin_delivery(
            ACCOUNT, ROLE, is_group=True, chat_id="77", item_ids=[exp_id],
            request_id="before-clear",
            expected_revision=state["revision"],
            expected_epoch=int(state["epoch"]),
            expected_state_version=int(state["state_version"]))
        self.assertTrue(claimed["ticket"])
        self.assertTrue(h.clear("clear-after-ticket"))
        # 清空后旧 ticket 不能把状态写回，也不能伪装成已送达
        self.assertFalse(h.engine.finish_delivery(
            ACCOUNT, ROLE, claimed["ticket"], "sent"))
        after = h.state()
        self.assertEqual(after["experiences"], [])
        # 防重账保留为“未完整送达”，绝不因清空而被伪造成 sent
        attempts = after["deliveries"]["group|77"]["attempts"]
        self.assertEqual(attempts[exp_id], "unknown")
        self.assertEqual(after["receipts"][claimed["ticket"]]["status"], "pending")
        # 旧 epoch 的 begin_delivery 也拒绝
        self.assertIsNone(h.engine.begin_delivery(
            ACCOUNT, ROLE, is_group=True, chat_id="77", item_ids=["exp-old"],
            request_id="stale-page",
            expected_revision=state["revision"], expected_epoch=int(state["epoch"]),
            expected_state_version=int(state["state_version"])))

    async def test_clear_during_generation_rejects_late_result(self):
        gate = asyncio.Event()
        started = asyncio.Event()

        async def slow(job, reserve_attempt):
            assert await reserve_attempt()
            started.set()
            await gate.wait()
            if job.kind == "plan":
                return {"items": [{"title": "迟到计划", "purpose": "x",
                                   "start": "11:00", "end": "12:00"}]}
            return {"text": "迟到的经历"}

        h = Harness(self.temp.name, enabled_cfg(), generate=slow)
        self.addAsyncCleanup(h.engine.stop)
        await h.advance("2026-03-10", "09:00")
        await asyncio.wait_for(started.wait(), 2)
        self.assertTrue(h.clear("clear-1"))
        gate.set()
        state = await h.wait_idle()
        await h.engine.stop()
        self.assertEqual(state["plans"], {})
        self.assertEqual(state["experiences"], [])
        self.assertEqual(state["epoch"], 1)

    async def test_revision_change_rejects_late_result(self):
        gate = asyncio.Event()
        started = asyncio.Event()

        async def slow(job, reserve_attempt):
            assert await reserve_attempt()
            started.set()
            await gate.wait()
            return {"items": [{"title": "旧人设计划", "purpose": "x",
                               "start": "11:00", "end": "12:00"}]}

        h = Harness(self.temp.name, enabled_cfg(), generate=slow)
        self.addAsyncCleanup(h.engine.stop)
        await h.advance("2026-03-10", "09:00")
        await asyncio.wait_for(started.wait(), 2)
        h.others["personality_presets"][0]["prompt"] = "人设已改"
        h.revision = persona_mod.resolve_persona(h.others, bot_name="测试机器人").revision
        gate.set()
        state = await h.wait_idle()
        await h.engine.stop()
        self.assertEqual(state["plans"], {})
        self.assertTrue([j for j in state["jobs"].values()
                         if "stale" in str(j.get("error", ""))])

    async def test_config_change_rejects_late_result(self):
        gate = asyncio.Event()
        started = asyncio.Event()

        async def slow(job, reserve_attempt):
            assert await reserve_attempt()
            started.set()
            await gate.wait()
            return {"items": [{"title": "旧配置计划", "purpose": "x",
                               "start": "11:00", "end": "12:00"}]}

        h = Harness(self.temp.name, enabled_cfg(), generate=slow)
        self.addAsyncCleanup(h.engine.stop)
        await h.advance("2026-03-10", "09:00")
        await asyncio.wait_for(started.wait(), 2)
        # 热改配置（不是人设）：旧回包必须作废
        h.config["roles"][ACCOUNT][ROLE]["activities_per_day"] = 6
        gate.set()
        state = await h.wait_idle()
        await h.engine.stop()
        self.assertEqual(state["plans"], {})
        self.assertTrue([j for j in state["jobs"].values()
                         if "stale" in str(j.get("error", ""))])

    async def test_account_switch_rejects_late_result(self):
        gate = asyncio.Event()
        started = asyncio.Event()

        async def slow(job, reserve_attempt):
            assert await reserve_attempt()
            started.set()
            await gate.wait()
            return {"items": [{"title": "换号前计划", "purpose": "x",
                               "start": "11:00", "end": "12:00"}]}

        h = Harness(self.temp.name, enabled_cfg(), generate=slow)
        self.addAsyncCleanup(h.engine.stop)
        await h.advance("2026-03-10", "09:00")
        await asyncio.wait_for(started.wait(), 2)
        h.engine.unbind_account(1)
        h.engine.bind_account("777777", 2)
        gate.set()
        await h.wait_idle()
        await h.engine.stop()
        self.assertEqual(h.state()["plans"], {})

    async def test_stop_cancels_worker(self):
        started = asyncio.Event()
        seen = []

        async def hang(job, reserve_attempt):
            assert await reserve_attempt()
            seen.append(job.job_id)
            started.set()
            await asyncio.Event().wait()

        h = Harness(self.temp.name, enabled_cfg(), generate=hang)
        await h.advance("2026-03-10", "09:00")
        await asyncio.wait_for(started.wait(), 2)
        await asyncio.wait_for(h.engine.stop(), 3)
        state = h.state()
        self.assertEqual(state["jobs"][seen[0]]["status"], "unknown")
        await h.advance("2026-03-10", "09:30")
        self.assertEqual(len(seen), 1)


# ---------------------------------------------------------------------------
# 隐私边界与分享账本
# ---------------------------------------------------------------------------

class PrivacyAndDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def test_engine_never_imports_chat_sources(self):
        source = inspect.getsource(rl)
        for forbidden in ("chat_memory", "follow_up", "followups", "image_history",
                          "group_chat_context", "ai_memory"):
            self.assertNotIn(forbidden, source)

    async def test_generated_requests_only_carry_life_material(self):
        captured = []

        async def capture(job, reserve_attempt):
            captured.append(json.loads(json.dumps(job.args, default=str)))
            assert await reserve_attempt()
            if job.kind == "plan":
                return {"items": [{"title": "活动", "purpose": "目的",
                                   "start": "09:00", "end": "10:00"}]}
            return {"text": "内容"}

        h = Harness(self.temp.name, enabled_cfg(), generate=capture)
        self.addAsyncCleanup(h.engine.stop)
        await h.advance("2026-03-10", "09:00")
        await h.wait_idle()
        await h.advance("2026-03-10", "10:05")
        await h.wait_idle()
        allowed = {"date", "current_time", "item", "plan_context", "material", "sleep_start",
                   "sleep_end", "replace", "life_notes", "manual"}
        for args in captured:
            self.assertTrue(set(args) <= allowed, args)
            self.assertNotIn("用户私聊秘密", json.dumps(args, ensure_ascii=False))
        self.assertTrue(captured)
        # 不同账号/角色状态隔离
        other = h.store.load("999999", ROLE)
        self.assertEqual(other["experiences"], [])
        self.assertEqual(other["plans"], {})

    async def test_delivery_ledger_per_target_and_no_blind_retry(self):
        h = Harness(self.temp.name, enabled_cfg())
        self.addAsyncCleanup(h.engine.stop)
        await h.advance("2026-03-10", "09:00")
        await h.wait_idle()
        state = await h.run_until("2026-03-10", "10:05")
        exp_id = next(e["id"] for e in state["experiences"] if e["kind"] == "activity")
        group_items = h.engine.candidates(ACCOUNT, ROLE, is_group=True, chat_id="111", now=h.now)
        self.assertTrue(group_items)
        private_items = h.engine.candidates(ACCOUNT, ROLE, is_group=False, chat_id="111", now=h.now)
        self.assertTrue(private_items)  # 群与私聊互不消耗
        current = h.state()
        claimed = h.engine.begin_delivery(
            ACCOUNT, ROLE, is_group=True, chat_id="111", item_ids=[exp_id],
            request_id="deliver-1",
            expected_revision=current["revision"],
            expected_epoch=int(current["epoch"]),
            expected_state_version=int(current["state_version"]))
        self.assertEqual(claimed["items"], [exp_id])
        self.assertTrue(claimed["ticket"])
        # 同 request_id 幂等返回同 ticket，不重复占位
        again = h.engine.begin_delivery(
            ACCOUNT, ROLE, is_group=True, chat_id="111", item_ids=[exp_id],
            request_id="deliver-1",
            expected_revision=current["revision"],
            expected_epoch=int(current["epoch"]),
            expected_state_version=int(current["state_version"]))
        self.assertEqual(again["ticket"], claimed["ticket"])
        # 在途/未知：同一目标不再作为候选，但其他群仍可见
        self.assertEqual(h.engine.candidates(ACCOUNT, ROLE, is_group=True, chat_id="111",
                                             now=h.now), [])
        self.assertTrue(h.engine.candidates(ACCOUNT, ROLE, is_group=True, chat_id="222",
                                            now=h.now))
        # 发送确认后不降级；重复回执幂等
        self.assertTrue(h.engine.finish_delivery(
            ACCOUNT, ROLE, claimed["ticket"], "sent"))
        self.assertTrue(h.engine.finish_delivery(
            ACCOUNT, ROLE, claimed["ticket"], "sent"))
        self.assertTrue(h.engine.finish_delivery(
            ACCOUNT, ROLE, claimed["ticket"], "failed"))
        attempts = h.state()["deliveries"]["group|111"]["attempts"]
        self.assertEqual(attempts[exp_id], "sent")
        # /reset 只清该会话分享引用，不抹掉生活
        h.engine.clear_session_shares(ACCOUNT, ROLE, is_group=True, chat_id="111")
        self.assertEqual(h.engine.candidates(ACCOUNT, ROLE, is_group=True, chat_id="111",
                                             now=h.now)[0]["item_id"], exp_id)
        self.assertTrue(h.state()["experiences"])

    async def test_partial_and_unknown_remain_undelivered(self):
        h = Harness(self.temp.name, enabled_cfg())
        self.addAsyncCleanup(h.engine.stop)
        await h.advance("2026-03-10", "09:00")
        await h.wait_idle()
        state = await h.run_until("2026-03-10", "10:05")
        exp_id = state["experiences"][0]["id"]
        claimed = h.engine.begin_delivery(
            ACCOUNT, ROLE, is_group=False, chat_id="9", item_ids=[exp_id],
            request_id="p-1", expected_revision=state["revision"],
            expected_epoch=int(state["epoch"]),
            expected_state_version=int(state["state_version"]))
        self.assertTrue(h.engine.finish_delivery(
            ACCOUNT, ROLE, claimed["ticket"], "partial"))
        # 别的账号不能完成本账号 ticket
        self.assertFalse(h.engine.finish_delivery("654321", ROLE, claimed["ticket"], "sent"))
        attempts = h.state()["deliveries"]["private|9"]["attempts"]
        self.assertEqual(attempts[exp_id], "partial")
        # partial/unknown 不自动重试
        self.assertEqual(h.engine.candidates(ACCOUNT, ROLE, is_group=False, chat_id="9",
                                             now=h.now), [])


class ManualOpsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    async def test_enqueue_is_idempotent_and_retry_replays_failures(self):
        h = Harness(self.temp.name, enabled_cfg())
        self.addAsyncCleanup(h.engine.stop)
        await h.advance("2026-03-10", "09:00")
        await h.wait_idle()
        first = h.enqueue("plan", "req-1")
        second = h.enqueue("plan", "req-1")
        self.assertEqual(first, second)
        state = await h.wait_idle()
        # 自动计划已存在；同 request_id 只创建一个手动重排作业
        self.assertEqual(len([j for j in state["jobs"].values()
                              if j.get("request_id") == "req-1"]), 1)
        self.assertEqual(state["plans"]["2026-03-10"]["items"][0]["title"], "晨间阅读")
        # 失败任务手动重试（unknown 不会自动重放）
        def seed(state):
            state["jobs"]["bad"] = {"id": "bad", "kind": "maintenance", "status": "failed",
                                    "created_at": h.now, "args": {},
                                    "revision": h.revision, "epoch": 0, "generation": 1,
                                    "attempts": 2, "dedupe_key": "manual:bad"}
            state["job_order"].append("bad")
        h.store.update(ACCOUNT, ROLE, seed)
        current = h.state()
        retried = h.engine.retry_job(
            ACCOUNT, ROLE, "bad", request_id="retry-1",
            expected_revision=current["revision"],
            expected_epoch=int(current["epoch"]),
            expected_state_version=int(current["state_version"]))
        self.assertTrue(retried)
        self.assertNotEqual(retried, "bad")
        state = await h.wait_idle()
        self.assertEqual(state["jobs"][retried]["status"], "succeeded")
        self.assertEqual(state["mood"]["mood"], "平静")

    async def test_manual_ops_reject_stale_revision_or_epoch(self):
        h = Harness(self.temp.name, enabled_cfg())
        self.addAsyncCleanup(h.engine.stop)
        await h.advance("2026-03-10", "09:00")
        state = await h.wait_idle()
        self.assertEqual(state["revision"], h.revision)
        # 乐观并发：旧 revision/epoch 被拒绝，且不产生新作业
        version = int(state["state_version"])
        self.assertIsNone(h.engine.enqueue(
            ACCOUNT, ROLE, "plan", request_id="r-old", expected_revision="rev-0",
            expected_epoch=0, expected_state_version=version))
        self.assertIsNone(h.engine.enqueue(
            ACCOUNT, ROLE, "plan", request_id="r-epoch", expected_revision=h.revision,
            expected_epoch=99, expected_state_version=version))
        self.assertIsNone(h.engine.enqueue(
            ACCOUNT, ROLE, "plan", request_id="r-version", expected_revision=h.revision,
            expected_epoch=0, expected_state_version=version + 50))
        # 缺少显式版本/幂等 ID 的外部写操作一律拒绝
        self.assertIsNone(h.engine.enqueue(
            ACCOUNT, ROLE, "plan", request_id="r-no-version",
            expected_revision=h.revision, expected_epoch=0))
        self.assertIsNone(h.engine.enqueue(
            ACCOUNT, ROLE, "plan", request_id="r-no-version2",
            expected_revision=h.revision, expected_epoch=0, expected_state_version=None))
        self.assertIsNone(h.engine.retry_job(ACCOUNT, ROLE, "missing", request_id="x",
                                             expected_revision="rev-0"))
        new_id = h.engine.regenerate_plan(
            ACCOUNT, ROLE, request_id="r-ok", expected_revision=h.revision,
            expected_epoch=0, expected_state_version=version)
        self.assertTrue(new_id)
        state = await h.wait_idle()
        self.assertEqual(state["jobs"][new_id]["status"], "succeeded")
        self.assertEqual(len([j for j in state["jobs"].values() if j["kind"] == "plan"]), 2)

    async def test_snapshot_marks_dream_and_sleep(self):
        h = Harness(self.temp.name, enabled_cfg())
        self.addAsyncCleanup(h.engine.stop)
        await h.advance("2026-03-10", "09:00")
        await h.wait_idle()
        state = h.state()

        def seed(s):
            s["experiences"].append({
                "id": "dream-1", "kind": "dream", "date": "2026-03-10",
                "created_at": h.now, "text": "一个梦", "source_ids": [],
                "origin": "role_simulation", "dream": True})
        h.store.update(ACCOUNT, ROLE, seed)
        snap = h.engine.snapshot(ACCOUNT, ROLE, h.now)
        dream = next(r for r in snap["recent"] if r["id"] == "dream-1")
        self.assertTrue(dream["dream"])
        self.assertEqual(dream["kind"], "dream")
        self.assertFalse(snap["sleeping"])

    async def test_unknown_job_restart_makes_no_call_and_manual_retry_keeps_budget(self):
        h = Harness(self.temp.name, enabled_cfg())
        self.addAsyncCleanup(h.engine.stop)
        await h.advance("2026-03-10", "09:00")
        await h.wait_idle()
        # 模拟一次“请求可能已发出但回执丢失”：running + 已扣预算
        def seed(state):
            state["budget"]["calls"] = [h.now]
            state["jobs"]["lost"] = {
                "id": "lost", "kind": "maintenance", "status": "running",
                "created_at": h.now - 60, "started_at": h.now - 30, "args": {},
                "revision": h.revision, "epoch": 0, "generation": 1,
                "attempts": 1, "dedupe_key": "manual:lost",
            }
            state["job_order"].append("lost")
        h.store.update(ACCOUNT, ROLE, seed)
        h.generate_calls.clear()
        await h.advance("2026-03-10", "09:01")
        state = await h.wait_idle()
        self.assertEqual(state["jobs"]["lost"]["status"], "unknown")
        self.assertEqual(h.generate_calls, [])  # 重启不自动重放
        # 人工重试生成新 job ID，原预算账目保留（不回退）
        retried = h.engine.retry_job(
            ACCOUNT, ROLE, "lost", request_id="manual-retry",
            expected_revision=state["revision"],
            expected_epoch=int(state["epoch"]),
            expected_state_version=int(state["state_version"]))
        self.assertTrue(retried)
        self.assertNotEqual(retried, "lost")  # 新 job ID
        self.assertEqual(state["jobs"]["lost"]["status"], "unknown")  # 原作业不复活
        state = await h.wait_idle()
        self.assertEqual(state["jobs"][retried]["retry_of"], "lost")
        self.assertEqual(state["jobs"][retried]["status"], "succeeded")
        self.assertEqual(len(state["budget"]["calls"]), 2)  # 手动重试是新的一次真实请求
        # 重复点击同 request_id 不再追加新作业/新预算
        again = h.engine.retry_job(
            ACCOUNT, ROLE, "lost", request_id="manual-retry",
            expected_revision=state["revision"], expected_epoch=int(state["epoch"]),
            expected_state_version=int(state["state_version"]))
        self.assertEqual(again, retried)
        await h.wait_idle()
        self.assertEqual(len(h.state()["budget"]["calls"]), 2)

    async def test_status_and_records_pagination(self):
        h = Harness(self.temp.name, enabled_cfg())
        self.addAsyncCleanup(h.engine.stop)
        await h.advance("2026-03-10", "09:00")
        await h.wait_idle()
        status = h.engine.status(ACCOUNT, ROLE, h.now)
        self.assertTrue(status["enabled"])
        self.assertEqual(status["date"], "2026-03-10")
        self.assertIn("usage", status)
        self.assertIn("jobs", status)
        self.assertEqual(status["queue"], 0)
        plans = h.engine.list_records(ACCOUNT, ROLE, "plans", page=1, page_size=1)
        self.assertEqual(plans["total"], 2)
        self.assertEqual(len(plans["items"]), 1)
        self.assertEqual(plans["pages"], 2)
        self.assertIn(plans["items"][0]["status"], ("planned", "active", "completed"))
        with self.assertRaises(ValueError):
            h.engine.list_records(ACCOUNT, ROLE, "bogus")


# ---------------------------------------------------------------------------
# 审查补项：持久化 shape、公平轮转、历史淘汰、CAS、离线、DST、候选
# ---------------------------------------------------------------------------

class ReviewRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    async def test_disabled_role_never_sleep_gates(self):
        h = Harness(self.temp.name, {"enabled": False})
        self.addAsyncCleanup(h.engine.stop)
        at_1am = ts("2026-03-10", "01:00")
        self.assertFalse(h.engine.is_sleeping(ACCOUNT, ROLE, at_1am))
        self.assertEqual(h.engine.candidates(
            ACCOUNT, ROLE, is_group=True, chat_id="1", now=at_1am), [])
        await h.advance("2026-03-10", "01:00")
        self.assertFalse(h.store.state_path(ACCOUNT, ROLE).exists())

    async def test_existing_empty_json_is_corrupt_and_untouched(self):
        store = RoleLifeStore(Path(self.temp.name))
        path = store.state_path(ACCOUNT, ROLE)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"{}")
        with self.assertRaises(rls.RoleLifeCorruptError):
            store.load(ACCOUNT, ROLE)
        with self.assertRaises(rls.RoleLifeCorruptError):
            store.update(ACCOUNT, ROLE, lambda s: None)
        self.assertEqual(path.read_bytes(), b"{}")
        path.write_text(json.dumps({
            "schema": 1, "account": ACCOUNT, "role_id": ROLE, "epoch": 0,
            "state_version": 0, "plan_serial": 0, "last_advance": 0,
            "revision": "", "config_revision": "",
            "budget": {"calls": "nope", "role_daily_limit": 24},
            "clear_requests": [], "dedupe": [], "op_requests": [],
            "plans": {}, "experiences": [], "sleep": {}, "mood": {},
            "jobs": {}, "job_order": [], "deliveries": {}, "receipts": {},
            "metrics": {},
        }), encoding="utf-8")
        with self.assertRaises(rls.RoleLifeCorruptError):
            store.load(ACCOUNT, ROLE)

    async def test_fair_rotation_a_b_a_b(self):
        h = Harness(self.temp.name, enabled_cfg())
        self.addAsyncCleanup(h.engine.stop)
        h.engine._account = ACCOUNT
        h.engine._push_jobs([(ACCOUNT, "a", "a1"), (ACCOUNT, "a", "a2"),
                             (ACCOUNT, "b", "b1"), (ACCOUNT, "b", "b2")])
        order = []
        while True:
            entry = h.engine._pop_next()
            if entry is None:
                break
            order.append(entry[1] + entry[2])
        self.assertEqual(order, ["aa1", "bb1", "aa2", "bb2"])
        self.assertLessEqual(len(h.engine._role_rotation), 2)

    async def test_job_history_evicts_finished_and_keeps_producing(self):
        h = Harness(self.temp.name, enabled_cfg())
        self.addAsyncCleanup(h.engine.stop)
        await h.advance("2026-03-10", "09:00")
        await h.wait_idle()

        def seed(state):
            for i in range(rl.MAX_JOB_HISTORY):
                job_id = f"old{i}"
                state["jobs"][job_id] = {
                    "id": job_id, "kind": "maintenance", "status": "succeeded",
                    "created_at": i, "args": {}, "dedupe_key": f"seed:{i}",
                    "revision": h.revision, "epoch": 0, "generation": 1,
                }
                state["job_order"].append(job_id)
            state["jobs"]["pinned"] = {
                "id": "pinned", "kind": "maintenance", "status": "unknown",
                "created_at": 0, "args": {}, "dedupe_key": "pinned",
                "revision": h.revision, "epoch": 0, "generation": 1,
            }
            state["job_order"].append("pinned")
        h.store.update(ACCOUNT, ROLE, seed)
        await h.advance("2026-03-11", "09:00")
        state = await h.wait_idle()
        self.assertIn("pinned", state["jobs"])
        self.assertEqual(state["jobs"]["pinned"]["status"], "unknown")
        self.assertLessEqual(len(state["job_order"]), rl.MAX_JOB_HISTORY + 4)
        self.assertIn("2026-03-11", state["plans"])
        self.assertIn("seed:0", state["dedupe"])

    async def test_external_write_is_locked_cas(self):
        h = Harness(self.temp.name, enabled_cfg())
        self.addAsyncCleanup(h.engine.stop)
        state = await h.run_until("2026-03-10", "09:00")
        stale_version = int(state["state_version"])
        h.store.update(ACCOUNT, ROLE, lambda s: s["mood"].update(
            {"mood": "平静", "energy": "一般", "source_ids": [], "updated_at": 1}))
        self.assertIsNone(h.engine.enqueue(
            ACCOUNT, ROLE, "plan", request_id="cas-1",
            expected_revision=state["revision"],
            expected_epoch=int(state["epoch"]),
            expected_state_version=stale_version))
        self.assertFalse(h.engine.clear_role(
            ACCOUNT, ROLE, request_id="cas-clear",
            expected_revision=state["revision"],
            expected_epoch=int(state["epoch"]),
            expected_state_version=stale_version))
        fresh = h.state()
        self.assertTrue(h.engine.enqueue(
            ACCOUNT, ROLE, "plan", request_id="cas-2",
            expected_revision=fresh["revision"],
            expected_epoch=int(fresh["epoch"]),
            expected_state_version=int(fresh["state_version"])))
        await h.wait_idle()

    async def test_running_persist_failure_prevents_request(self):
        h = Harness(self.temp.name, enabled_cfg())
        self.addAsyncCleanup(h.engine.stop)
        await h.advance("2026-03-10", "09:00")
        await h.wait_idle()
        h.generate_calls.clear()
        original = h.store.update

        def failing(account, role_id, mutator, **kwargs):
            probe = h.store.load(account, role_id)
            if any(j.get("status") == "pending" for j in probe.get("jobs", {}).values()):
                raise OSError("disk full")
            return original(account, role_id, mutator, **kwargs)

        h.store.update = failing
        try:
            await h.tick(600)
        finally:
            h.store.update = original
        self.assertEqual(h.generate_calls, [])
        state = h.state()
        self.assertFalse([j for j in state["jobs"].values()
                          if j["kind"] == "activity" and j["status"] == "running"])

    async def test_reservation_rechecks_job_after_clear(self):
        h = Harness(self.temp.name, enabled_cfg())
        self.addAsyncCleanup(h.engine.stop)
        state = await h.advance("2026-03-10", "09:00") or await h.wait_idle()
        job_id = next(j for j in state["jobs"] if state["jobs"][j]["kind"] == "plan")
        cfg = h.config["roles"][ACCOUNT][ROLE]
        persona = h.engine._persona(ACCOUNT, ROLE)
        job = rl.LifeJob(
            job_id=job_id, account=ACCOUNT, role_id=ROLE, role_key="k",
            kind="plan", revision=persona.revision,
            config_revision=state["config_revision"], epoch=0, generation=1,
            persona_template=persona.raw_prompt, config=cfg, args={})
        calls_before = list(h.state()["budget"]["calls"])
        reserved_before = len(h.reserve_calls)
        self.assertTrue(h.clear("clear-before-reserve"))
        # 清空发生在排队之后：扣额度前复核失败，直接拒绝，不发请求、不扣新预算
        self.assertFalse(h.engine._reserve_attempt(ACCOUNT, ROLE, cfg, job))
        self.assertEqual(h.state()["budget"]["calls"], calls_before)
        self.assertEqual(len(h.reserve_calls), reserved_before)

    async def test_stable_ticks_do_not_write(self):
        writes = []
        h = Harness(self.temp.name, enabled_cfg())
        self.addAsyncCleanup(h.engine.stop)
        await h.advance("2026-03-10", "09:00")
        await h.wait_idle()
        await h.tick(1)  # 让“到点进入 active”这次真实变化先发生
        await h.wait_idle()
        with mock.patch.object(rls, "atomic_write_json",
                               side_effect=lambda *a, **k: writes.append(a[0])):
            for _ in range(10):
                await h.tick(1)
                await h.wait_idle()
        self.assertEqual(writes, [], f"stable ticks wrote {writes}")

    async def test_same_day_offline_planned_and_active_are_conservative(self):
        h = Harness(self.temp.name, enabled_cfg())
        self.addAsyncCleanup(h.engine.stop)
        await h.advance("2026-03-10", "09:00")
        await h.wait_idle()

        def at(hhmm):
            return ts("2026-03-10", hhmm)

        def seed(state):
            state["plans"]["2026-03-10"]["items"] = [
                {"id": "2026-03-10-p1-1", "title": "未开始", "purpose": "x",
                 "start": at("09:00"), "end": at("10:00"), "status": "planned",
                 "origin": "role_simulation", "started_at": 0, "ended_at": 0,
                 "experience_id": ""},
                {"id": "2026-03-10-p1-2", "title": "进行中", "purpose": "y",
                 "start": at("10:00"), "end": at("11:00"), "status": "active",
                 "origin": "role_simulation", "started_at": at("10:00"),
                 "ended_at": 0, "experience_id": ""},
            ]
        h.store.update(ACCOUNT, ROLE, seed)
        h.generate_calls.clear()
        await h.advance("2026-03-10", "12:00")
        state = await h.wait_idle()
        statuses = [i["status"] for i in state["plans"]["2026-03-10"]["items"]]
        self.assertEqual(statuses, ["skipped", "interrupted"])
        self.assertEqual([e for e in state["experiences"] if e["kind"] == "activity"], [])
        self.assertEqual(h.generate_calls, [])

    async def test_dst_day_uses_local_next_midnight(self):
        clock = rl._Clock("America/New_York")
        day = "2026-03-08"  # 纽约夏令时开始日（23 小时）
        length = clock.next_day_start(day) - clock.day_start(day)
        self.assertEqual(length, 23 * 3600)
        # 睡段用当地日历算起止，再用真实经过时长；DST 日整段仍是真实 8 小时
        start = clock.hhmm(day, "01:00")
        end = clock.plus_hours(start, 8)
        self.assertEqual(end - start, 8 * 3600)
        self.assertEqual(clock.local(end).hour, 10)

    async def test_dream_is_candidate_with_kind(self):
        h = Harness(self.temp.name, enabled_cfg())
        self.addAsyncCleanup(h.engine.stop)
        await h.advance("2026-03-10", "09:00")
        await h.wait_idle()

        def seed(state):
            state["experiences"].append({
                "id": "dream-1", "kind": "dream", "date": "2026-03-10",
                "created_at": h.now, "text": "梦见走廊", "source_ids": [],
                "origin": "role_simulation", "dream": True})
        h.store.update(ACCOUNT, ROLE, seed)
        items = h.engine.candidates(ACCOUNT, ROLE, is_group=True, chat_id="1", now=h.now)
        dream = next(i for i in items if i["item_id"] == "dream-1")
        self.assertEqual(dream["kind"], "dream")
        self.assertTrue(dream["dream"])
        h.config["roles"][ACCOUNT][ROLE]["enabled"] = False
        self.assertEqual(h.engine.candidates(
            ACCOUNT, ROLE, is_group=True, chat_id="2", now=h.now), [])

    async def test_replan_preserves_history_ids_and_stale_job_cannot_claim(self):
        h = Harness(self.temp.name, enabled_cfg())
        self.addAsyncCleanup(h.engine.stop)
        await h.advance("2026-03-10", "09:00")
        await h.wait_idle()
        first = h.state()
        old_ids = [i["id"] for i in first["plans"]["2026-03-10"]["items"]]
        state = await h.run_until("2026-03-10", "09:30")
        self.assertEqual(state["plans"]["2026-03-10"]["items"][0]["status"], "active")
        self.assertTrue(h.enqueue("plan", "replan-1"))
        state = await h.wait_idle()
        items = state["plans"]["2026-03-10"]["items"]
        ids = [i["id"] for i in items]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertIn(old_ids[0], ids)
        stale_response = h.engine._apply_activity(
            state,
            rl.LifeJob(job_id="x", account=ACCOUNT, role_id=ROLE, role_key="k",
                       kind="activity", revision=h.revision, config_revision="",
                       epoch=0, generation=1, persona_template="",
                       config=h.config["roles"][ACCOUNT][ROLE],
                       args={"date": "2026-03-10", "plan_serial": 0,
                             "item": {"id": ids[-1]}}),
            {"text": "冒领"}, ts("2026-03-10", "09:40"))
        self.assertTrue(stale_response.startswith("stale"))

    async def test_diary_material_marks_plan_status(self):
        h = Harness(self.temp.name, enabled_cfg())
        self.addAsyncCleanup(h.engine.stop)
        await h.advance("2026-03-10", "09:00")
        await h.wait_idle()
        state = h.state()
        material = h.engine._diary_material(state, "2026-03-10")
        plans = [m for m in material if m["kind"] == "plan"]
        self.assertTrue(plans)
        for entry in plans:
            self.assertIn("id", entry)
            self.assertIn(entry["status"], ("planned", "active", "completed"))


class ClearAtomicityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    async def _seed_one_experience(self, h):
        await h.advance("2026-03-10", "09:00")
        await h.wait_idle()

        def seed(state):
            state["budget"]["calls"] = [h.now - 60]
            state["experiences"].append({
                "id": "e1", "kind": "activity", "date": "2026-03-10",
                "created_at": h.now, "text": "旧经历", "source_ids": [],
                "origin": "role_simulation", "dream": False})
        h.store.update(ACCOUNT, ROLE, seed)
        return h.state()

    async def test_clear_and_idempotency_record_are_one_transaction(self):
        h = Harness(self.temp.name, enabled_cfg())
        self.addAsyncCleanup(h.engine.stop)
        await self._seed_one_experience(h)
        writes = []
        original = rls.atomic_write_json

        def counted(path, data, **kwargs):
            writes.append(path)
            return original(path, data, **kwargs)

        with mock.patch.object(rls, "atomic_write_json", side_effect=counted):
            self.assertTrue(h.clear("clear-once"))
        self.assertEqual(len(writes), 1, "clear must not need a second write")
        state = h.state()
        self.assertEqual(state["epoch"], 1)
        self.assertEqual(state["budget"]["calls"], [h.now - 60])  # 预算保留
        self.assertEqual(
            [e for e in state["op_requests"] if e.get("op") == "clear"][0]["result"], "ok")
        self.assertEqual(h.engine.usage(ACCOUNT, ROLE, h.now)["role_24h"], 1)

    async def test_second_write_failure_never_loses_idempotency(self):
        h = Harness(self.temp.name, enabled_cfg())
        self.addAsyncCleanup(h.engine.stop)
        await self._seed_one_experience(h)
        calls = []
        original = rls.atomic_write_json

        def fail_second(path, data, **kwargs):
            calls.append(path)
            if len(calls) > 1:
                raise OSError("disk full")
            return original(path, data, **kwargs)

        with mock.patch.object(rls, "atomic_write_json", side_effect=fail_second):
            self.assertTrue(h.clear("clear-atomic"))
            # 若请求重复，必须直接命中幂等记录，不再产生任何写盘尝试
            self.assertTrue(h.clear("clear-atomic"))
        self.assertEqual(len(calls), 1)

    async def test_repeat_clear_request_does_not_clear_new_content(self):
        h = Harness(self.temp.name, enabled_cfg())
        self.addAsyncCleanup(h.engine.stop)
        await self._seed_one_experience(h)
        self.assertTrue(h.clear("clear-repeat"))
        epoch_after_first = h.state()["epoch"]
        # 清空后重新产生了新内容（新活动经历）
        def seed_new(state):
            state["experiences"].append({
                "id": "e2", "kind": "activity", "date": "2026-03-10",
                "created_at": h.now + 1, "text": "新内容", "source_ids": [],
                "origin": "role_simulation", "dream": False})
        h.store.update(ACCOUNT, ROLE, seed_new)
        # 同 request_id（即使页面持有旧 version）：幂等返回成功，但不碰新内容
        self.assertTrue(h.clear("clear-repeat"))
        state = h.state()
        self.assertEqual(state["epoch"], epoch_after_first)
        self.assertEqual([e["id"] for e in state["experiences"]], ["e2"])

    async def test_same_request_id_different_payload_is_rejected(self):
        h = Harness(self.temp.name, enabled_cfg())
        self.addAsyncCleanup(h.engine.stop)
        await self._seed_one_experience(h)
        state = h.state()
        # 伪造同 request_id 的冲突 op（模拟另一类操作复用 ID）
        h.store.update(ACCOUNT, ROLE, lambda s: s["op_requests"].append({
            "op": "clear", "request_id": "shared-id", "payload_hash": "deadbeef",
            "result": "ok"}))
        self.assertFalse(h.engine.clear_role(
            ACCOUNT, ROLE, request_id="shared-id",
            expected_revision=state["revision"], expected_epoch=int(state["epoch"]),
            expected_state_version=int(state["state_version"])))

    async def test_corrupt_recovery_salvages_valid_budget(self):
        h = Harness(self.temp.name, enabled_cfg())
        self.addAsyncCleanup(h.engine.stop)
        path = h.store.state_path(ACCOUNT, ROLE)
        path.parent.mkdir(parents=True, exist_ok=True)
        # 身份字段坏了，但 budget 本身可独立校验
        path.write_text(json.dumps({
            "schema": 1, "account": "999", "role_id": ROLE,
            "budget": {"calls": [1234.5], "role_daily_limit": 24},
        }), encoding="utf-8")
        self.assertTrue(h.engine.clear_role(
            ACCOUNT, ROLE, request_id="salvage", allow_corrupt=True))
        state = h.state()
        self.assertEqual(state["budget"]["calls"], [1234.5])
        self.assertEqual(state["budget_unknown_until"], 0)
        self.assertFalse(h.engine.status(ACCOUNT, ROLE, h.now)["budget_unknown"])
        self.assertTrue(state["recovered_from_corrupt"])
        backup = path.parent / state["corrupt_backup"]
        self.assertTrue(backup.exists())
        self.assertIn('"account": "999"', backup.read_text(encoding="utf-8"))

    async def test_corrupt_unreadable_blocks_role_quota_for_24h(self):
        h = Harness(self.temp.name, enabled_cfg())
        self.addAsyncCleanup(h.engine.stop)
        path = h.store.state_path(ACCOUNT, ROLE)
        path.parent.mkdir(parents=True, exist_ok=True)
        original = b"not json at all"
        path.write_bytes(original)
        # Harness.expect() 会读损坏文件，这里直接按接口调用
        self.assertFalse(h.engine.clear_role(
            ACCOUNT, ROLE, request_id="corrupt-without-confirm"))
        self.assertEqual(path.read_bytes(), original)  # 未确认不销毁原件
        self.assertTrue(h.engine.clear_role(
            ACCOUNT, ROLE, request_id="corrupt-ok", allow_corrupt=True))
        state = h.state()
        self.assertGreater(state["budget_unknown_until"], h.now)
        self.assertTrue(state["corrupt_backup"].startswith("state.corrupt-"))
        backup = path.parent / state["corrupt_backup"]
        self.assertEqual(backup.read_bytes(), original)  # 原字节完整保留
        status = h.engine.status(ACCOUNT, ROLE, h.now)
        self.assertTrue(status["budget_unknown"])
        self.assertEqual(h.engine.usage(ACCOUNT, ROLE, h.now)["budget_unknown"], True)
        # 保护期内不能重新获得完整调用额度，也不消耗账号账本
        before = list(h.reserve_calls)
        self.assertFalse(h.engine._reserve_attempt(ACCOUNT, ROLE, rl.DEFAULT_ROLE_CONFIG))
        self.assertEqual(h.reserve_calls, before)
        # 过了保护期自动恢复（不改账号账本）
        later = state["budget_unknown_until"] + 1
        self.assertFalse(h.engine.status(ACCOUNT, ROLE, later)["budget_unknown"])


class FirstReadRevisionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    async def test_new_role_status_returns_persona_revision_before_first_advance(self):
        h = Harness(self.temp.name, enabled_cfg())
        self.addAsyncCleanup(h.engine.stop)
        # 尚未 advance：持久化 revision 为空，但页面必须有可用版本
        status = h.engine.status(ACCOUNT, ROLE, h.now)
        self.assertEqual(status["revision"], h.revision)
        self.assertEqual(status["state_version"], 0)
        snap = h.engine.snapshot(ACCOUNT, ROLE, h.now)
        self.assertEqual(snap["revision"], h.revision)

    async def test_first_enqueue_works_with_fallback_revision(self):
        h = Harness(self.temp.name, enabled_cfg())
        self.addAsyncCleanup(h.engine.stop)
        status = h.engine.status(ACCOUNT, ROLE, h.now)
        job_id = h.engine.enqueue(
            ACCOUNT, ROLE, "plan", request_id="first-open",
            expected_revision=status["revision"], expected_epoch=status["epoch"],
            expected_state_version=status["state_version"])
        self.assertTrue(job_id)
        state = await h.wait_idle()
        self.assertEqual(state["revision"], h.revision)
        self.assertEqual(state["jobs"][job_id]["status"], "succeeded")
        # 之后版本一致，正常写操作继续可用
        status = h.engine.status(ACCOUNT, ROLE, h.now)
        self.assertTrue(h.engine.regenerate_plan(
            ACCOUNT, ROLE, request_id="second",
            expected_revision=status["revision"], expected_epoch=status["epoch"],
            expected_state_version=status["state_version"]))
        await h.wait_idle()  # 让 worker 结束，临时目录才能在 Windows 清理

    async def test_persona_change_invalidates_before_first_advance(self):
        h = Harness(self.temp.name, enabled_cfg())
        self.addAsyncCleanup(h.engine.stop)
        status = h.engine.status(ACCOUNT, ROLE, h.now)
        h.others["personality_presets"][0]["prompt"] = "人设已改"
        self.assertIsNone(h.engine.enqueue(
            ACCOUNT, ROLE, "plan", request_id="stale-first",
            expected_revision=status["revision"], expected_epoch=status["epoch"],
            expected_state_version=status["state_version"]))
        fresh = h.engine.status(ACCOUNT, ROLE, h.now)
        self.assertNotEqual(fresh["revision"], status["revision"])
        self.assertTrue(h.engine.enqueue(
            ACCOUNT, ROLE, "plan", request_id="fresh-first",
            expected_revision=fresh["revision"], expected_epoch=fresh["epoch"],
            expected_state_version=fresh["state_version"]))
        await h.wait_idle()


class RegressionFixesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    async def test_plan_args_include_current_time_and_filter_before_limit(self):
        captured = []

        async def gen(job, reserve_attempt):
            assert await reserve_attempt()
            captured.append(job.args)
            if job.kind == "plan":
                return {"items": [
                    {"title": "过去1", "purpose": "已过", "start": "08:00", "end": "08:30"},
                    {"title": "睡段", "purpose": "熬夜", "start": "01:00", "end": "02:00"},
                    {"title": "有效1", "purpose": "A", "start": "10:00", "end": "11:00"},
                    {"title": "有效2", "purpose": "B", "start": "12:00", "end": "13:00"},
                ]}
            return {"text": "ok"}

        h = Harness(self.temp.name, enabled_cfg(activities_per_day=2), generate=gen)
        self.addAsyncCleanup(h.engine.stop)
        await h.advance("2026-03-10", "09:00")
        await h.wait_idle()
        self.assertEqual(captured[0]["current_time"], "09:00")
        items = h.state()["plans"]["2026-03-10"]["items"]
        self.assertEqual([i["title"] for i in items], ["有效1", "有效2"])
        # 再做一次活动推进 + 重排，校验 plan_context 带入已开始的条目
        await h.advance("2026-03-10", "10:15")
        await h.wait_idle()
        self.assertTrue(h.enqueue("plan", "replan-ctx"))
        await h.wait_idle()
        self.assertEqual(captured[-1]["current_time"], "10:15")
        self.assertEqual(captured[-1]["plan_context"][0]["title"], "有效1")
        self.assertEqual(captured[-1]["plan_context"][0]["status"], "active")

    async def test_snapshot_ignores_expired_active_and_apply_activity_handles_replan(self):
        h = Harness(self.temp.name, enabled_cfg())
        self.addAsyncCleanup(h.engine.stop)
        await h.advance("2026-03-10", "09:00")
        await h.wait_idle()
        await h.advance("2026-03-10", "09:30")
        state = h.state()
        first_item = state["plans"]["2026-03-10"]["items"][0]
        self.assertEqual(first_item["status"], "active")
        # 超过结束时间但未完成（如活动任务失败），snapshot 不得继续把它当 current_activity
        snap = h.engine.snapshot(ACCOUNT, ROLE, ts("2026-03-10", "12:00"))
        self.assertIsNone(snap["current_activity"])
        # 重排后 serial 升到 2，已在途的 serial=1 活动结果仍能完成原条目
        self.assertTrue(h.enqueue("plan", "replan-inflight"))
        state = await h.wait_idle()
        self.assertEqual(state["plans"]["2026-03-10"]["serial"], 2)
        err = h.engine._apply_activity(
            state,
            rl.LifeJob(job_id="act-1", account=ACCOUNT, role_id=ROLE, role_key="k",
                       kind="activity", revision=h.revision, config_revision="",
                       epoch=0, generation=1, persona_template="",
                       config=h.config["roles"][ACCOUNT][ROLE],
                       args={"date": "2026-03-10", "plan_serial": 1,
                             "item": {"id": first_item["id"]}}),
            {"text": "刚收尾看书。"}, ts("2026-03-10", "10:01"))
        self.assertEqual(err, "")
        updated = next(i for i in state["plans"]["2026-03-10"]["items"]
                       if i["id"] == first_item["id"])
        self.assertEqual(updated["status"], "completed")

    async def test_missing_persona_does_not_spam_writes_and_sleep_updates_end(self):
        h = Harness(self.temp.name, enabled_cfg(sleep_start="00:30", sleep_hours=8, diary_enabled=False))
        self.addAsyncCleanup(h.engine.stop)
        # 在睡段内修改 sleep_hours 时，sleep.end 应同步更新且保留 wake_dream_done
        await h.advance("2026-03-10", "01:00")
        self.assertEqual(h.state()["sleep"]["end"], ts("2026-03-10", "08:30"))
        h.config["roles"][ACCOUNT][ROLE]["sleep_hours"] = 6
        await h.advance("2026-03-10", "01:05")
        self.assertEqual(h.state()["sleep"]["end"], ts("2026-03-10", "06:30"))
        # diary_enabled=False 时 _next_node 不显示写日记，且能预告明天 00:30 睡觉
        snap = h.engine.snapshot(ACCOUNT, ROLE, ts("2026-03-10", "22:00"))
        self.assertEqual(snap["next_node"]["label"], "准备睡觉")
        self.assertEqual(snap["next_node"]["at"], ts("2026-03-11", "00:30"))
        # 人设缺失时仅写一次错误状态，不随后续 tick 不断写盘递增 state_version
        h.others["personality_presets"] = []
        await h.advance("2026-03-10", "22:05")
        ver1 = h.state()["state_version"]
        await h.advance("2026-03-10", "22:10")
        ver2 = h.state()["state_version"]
        self.assertEqual(ver1, ver2)


class NewFeaturesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def test_env_day_context_season_weekend_and_holidays(self):
        from bot.role_life_env import build_day_context
        self.assertEqual(build_day_context("2026-03-10"), "2026-03-10 星期二（工作日），春季")
        self.assertIn("周末", build_day_context("2026-08-15"))
        self.assertIn("夏季", build_day_context("2026-08-15"))
        self.assertIn("国庆节", build_day_context("2026-10-01"))
        self.assertIn("秋季", build_day_context("2026-10-01"))
        self.assertIn("冬季", build_day_context("2026-12-25"))
        self.assertIn("圣诞节", build_day_context("2026-12-25"))
        self.assertIn("春节", build_day_context("2026-02-17"))

    async def test_mood_piggyback_and_recent_woke_up_snapshot(self):
        async def gen(job, reserve_attempt):
            assert await reserve_attempt()
            if job.kind == "plan":
                return {"items": [{"title": "晒太阳", "purpose": "放松", "start": "09:00", "end": "10:00"}]}
            return {"text": "风很暖，喝了杯茶。", "mood": "惬意", "energy": "充足"}

        h = Harness(self.temp.name, enabled_cfg(sleep_start="00:30", sleep_hours=8), generate=gen)
        self.addAsyncCleanup(h.engine.stop)
        await h.advance("2026-03-10", "08:00")  # 睡醒前进入睡段
        await h.advance("2026-03-10", "08:40")  # 醒来 10 分钟内
        snap_wake = h.engine.snapshot(ACCOUNT, ROLE, h.now)
        self.assertTrue(snap_wake["recent_woke_up"])
        self.assertTrue(snap_wake["mood_tone_enabled"])
        await h.advance("2026-03-10", "09:10")
        await h.advance("2026-03-10", "10:05")
        await h.wait_idle()
        snap = h.engine.snapshot(ACCOUNT, ROLE, h.now)
        self.assertFalse(snap["recent_woke_up"])
        self.assertEqual(snap["mood"]["mood"], "惬意")
        self.assertEqual(snap["mood"]["energy"], "充足")

    async def test_sanitize_and_chat_feedback_notes_flow(self):
        h = Harness(self.temp.name, enabled_cfg())
        self.addAsyncCleanup(h.engine.stop)
        clean = rl.sanitize_chat_life_note(
            "张三 QQ 123456789 约好周末一起去https://example.com 看展 @李四 [CQ:face,id=1]",
            strip_names=("张三", "王五"),
        )
        self.assertNotIn("张三", clean)
        self.assertNotIn("123456789", clean)
        self.assertNotIn("https://", clean)
        self.assertNotIn("@李四", clean)
        self.assertNotIn("[CQ:", clean)
        self.assertIn("朋友", clean)
        self.assertIn("看展", clean)

        # 已取消聊天约定自动提取：chat_feedback 源不再写入备注
        self.assertIsNone(h.engine.add_life_note(
            ACCOUNT, ROLE, "和小明约好周末去公园看展",
            strip_names=("小明",), scope="private:111", now=ts("2026-03-10", "12:00"),
        ))
        # 手动备注（source="manual"）正常保存、去重、进日记且可删除
        manual = h.engine.add_life_note(
            ACCOUNT, ROLE, "这周末打算整理书架",
            source="manual", now=ts("2026-03-10", "12:08"),
        )
        self.assertIsNotNone(manual)
        self.assertIsNone(h.engine.add_life_note(
            ACCOUNT, ROLE, "这周末打算整理书架",
            source="manual", now=ts("2026-03-10", "12:09"),
        ))
        snap = h.engine.snapshot(ACCOUNT, ROLE, ts("2026-03-10", "12:10"))
        self.assertEqual([n["id"] for n in snap["life_notes"]], [manual["id"]])
        diary_mat = h.engine._diary_material(h.state(), "2026-03-10")
        self.assertTrue(any(m.get("kind") == "note" and "整理书架" in m.get("text", "") for m in diary_mat))
        self.assertTrue(h.engine.delete_life_note(ACCOUNT, ROLE, manual["id"]))
        self.assertEqual(h.state()["life_notes"], [])

    async def test_schedule_and_experience_crud_and_trigger(self):
        h = Harness(self.temp.name, enabled_cfg())
        self.addAsyncCleanup(h.engine.stop)
        # 无预先计划时手动增加两项日程
        a = h.engine.upsert_plan_item(
            ACCOUNT, ROLE, title="手冲咖啡", purpose="醒神",
            start_hhmm="09:00", end_hhmm="09:30", date="2026-03-10",
        )
        b = h.engine.upsert_plan_item(
            ACCOUNT, ROLE, title="整理书单", purpose="挑几本小说",
            start_hhmm="10:00", end_hhmm="11:00", date="2026-03-10",
        )
        # 时间重叠拒绝
        with self.assertRaises(ValueError):
            h.engine.upsert_plan_item(
                ACCOUNT, ROLE, title="冲突", start_hhmm="09:15", end_hhmm="10:15", date="2026-03-10",
            )
        # 修改未开始日程
        a2 = h.engine.upsert_plan_item(
            ACCOUNT, ROLE, item_id=a["id"], title="手冲黑咖啡", purpose="加冰",
            start_hhmm="09:00", end_hhmm="09:40",
        )
        self.assertEqual(a2["title"], "手冲黑咖啡")
        # 手动一键触发生成经历
        job_id = h.engine.trigger_activity_experience(
            ACCOUNT, ROLE, item_id=b["id"], request_id="trig-1",
        )
        self.assertTrue(job_id)
        await h.wait_idle()
        state = h.state()
        b_now = next(i for i in state["plans"]["2026-03-10"]["items"] if i["id"] == b["id"])
        self.assertEqual(b_now["status"], "completed")
        self.assertTrue(b_now["experience_id"])
        # 已完成的条目不允许删除
        with self.assertRaises(ValueError):
            h.engine.delete_plan_item(ACCOUNT, ROLE, item_id=b["id"])
        # 编辑并删除该经历（同时清空关联 item.experience_id）
        edited = h.engine.update_experience(ACCOUNT, ROLE, experience_id=b_now["experience_id"], text="挑了两本推理小说。")
        self.assertEqual(edited["text"], "挑了两本推理小说。")
        self.assertTrue(h.engine.delete_experience(ACCOUNT, ROLE, experience_id=b_now["experience_id"]))
        b_after = next(i for i in h.state()["plans"]["2026-03-10"]["items"] if i["id"] == b["id"])
        self.assertEqual(b_after["experience_id"], "")
        # 手写经历关联剩余 planned 条目并删除另一个
        manual_exp = h.engine.update_experience(ACCOUNT, ROLE, plan_item_id=a["id"], text="咖啡很香。")
        self.assertEqual(manual_exp["kind"], "activity")

    def test_independent_scheduler_and_snapshot_free_state(self):
        import time
        h = Harness(self.temp.name, enabled_cfg())
        calls = []
        orig_advance = h.engine.advance

        async def tracked_advance(now=None):
            calls.append(now)
            return await orig_advance(now)

        h.engine.advance = tracked_advance
        sched = rl.start_role_life_scheduler(h.engine, tick_seconds=0.02)
        try:
            self.assertIsNotNone(sched)
            deadline = time.monotonic() + 2.0
            while len(calls) < 2 and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertGreaterEqual(len(calls), 2)
            self.assertTrue(sched._thread and sched._thread.is_alive())
            snap = h.engine.snapshot(ACCOUNT, ROLE, h.now)
            self.assertTrue(snap["is_free"])
            self.assertTrue(snap["env_awareness_enabled"])
            self.assertEqual(snap["timezone"], "Asia/Shanghai")
        finally:
            rl.stop_role_life_scheduler(timeout=2.0)
            self.assertIsNone(rl._scheduler)

    async def test_store_failed_retries_save_without_reinvoking_model(self):
        for fail_writes in (1, 2):
            with self.subTest(fail_writes=fail_writes):
                with tempfile.TemporaryDirectory() as td:
                    gen_count = {"n": 0}
                    save_after_gen = {"n": 0}

                    async def gen(job, reserve_attempt):
                        if not await reserve_attempt():
                            return None
                        gen_count["n"] += 1
                        return {"items": [
                            {"title": "散步", "purpose": "透气", "start": "09:00", "end": "09:30"},
                        ]}

                    h = Harness(td, enabled_cfg(), generate=gen)
                    self.addAsyncCleanup(h.engine.stop)
                    orig_update = h.store.update

                    def flaky_update(*args, **kwargs):
                        if gen_count["n"] > 0:
                            save_after_gen["n"] += 1
                            if save_after_gen["n"] <= fail_writes:
                                raise OSError("disk full")
                        return orig_update(*args, **kwargs)

                    h.store.update = flaky_update
                    await h.advance("2026-03-10", "08:30")
                    await h.wait_idle()
                    self.assertEqual(gen_count["n"], 1)
                    jobs = list(h.state()["jobs"].values())
                    self.assertEqual(len(jobs), 1)
                    job = jobs[0]
                    if fail_writes == 1:
                        self.assertEqual(job["status"], "succeeded")
                        self.assertGreaterEqual(save_after_gen["n"], 2)
                    else:
                        self.assertEqual(job["status"], "failed")
                        self.assertIn("store_failed", job.get("error", ""))

    async def test_begin_delivery_skips_deleted_experience_and_bind_guard(self):
        h = Harness(self.temp.name, enabled_cfg())
        self.addAsyncCleanup(h.engine.stop)
        item = h.engine.upsert_plan_item(
            ACCOUNT, ROLE, title="手冲咖啡", purpose="醒神",
            start_hhmm="09:00", end_hhmm="09:30", date="2026-03-10",
        )
        exp = h.engine.update_experience(ACCOUNT, ROLE, plan_item_id=item["id"], text="刚泡了咖啡。")
        self.assertTrue(h.engine.delete_experience(ACCOUNT, ROLE, experience_id=exp["id"]))
        res = h.engine.begin_delivery(
            ACCOUNT, ROLE, is_group=False, chat_id="111",
            item_ids=[exp["id"]], request_id="deliv-1",
        )
        self.assertTrue(not res or res.get("items") == [])
        self.assertFalse(h.engine.bind_account("888888", 99, is_current=lambda: False))
        self.assertEqual(h.engine._account, ACCOUNT)

    async def test_legacy_cached_job_args_scrubbed_on_load_retry_and_run(self):
        with tempfile.TemporaryDirectory() as td:
            seen_args = []

            async def gen(job, reserve_attempt):
                if not await reserve_attempt():
                    return None
                seen_args.append((job.kind, copy.deepcopy(job.args)))
                if job.kind == "plan":
                    return {"items": [{"title": "泡茶", "purpose": "休息", "start": "11:00", "end": "11:30"}]}
                return {"text": "今天安静喝茶。"}

            h = Harness(td, enabled_cfg(plan_enabled=False, diary_enabled=False), generate=gen)
            self.addAsyncCleanup(h.engine.stop)
            await h.advance("2026-03-10", "08:30")
            await h.wait_idle()
            path = h.store.state_path(ACCOUNT, ROLE)
            raw = json.loads(path.read_text(encoding="utf-8"))
            raw["life_notes"] = [
                {"id": "n-old", "date": "2026-03-10", "text": "旧无scope聊天秘密",
                 "created_at": h.now, "source": "chat_feedback"},
                {"id": "n-man", "date": "2026-03-10", "text": "手写安全备注",
                 "created_at": h.now + 1, "source": "manual"},
            ]
            raw["jobs"]["j-old-plan"] = {
                "id": "j-old-plan", "kind": "plan", "status": "failed",
                "created_at": h.now, "started_at": 0, "finished_at": h.now,
                "args": {"date": "2026-03-10", "life_notes": ["旧无scope聊天秘密", "手写安全备注"]},
                "error": "old", "result_id": "", "revision": h.revision,
                "config_revision": raw.get("config_revision", ""),
                "epoch": int(raw.get("epoch") or 0), "generation": 1, "attempts": 1,
                "dedupe_key": "old-plan", "request_id": "r-old-p", "retry_of": "",
            }
            raw["jobs"]["j-old-diary"] = {
                "id": "j-old-diary", "kind": "diary", "status": "failed",
                "created_at": h.now + 1, "started_at": 0, "finished_at": h.now + 1,
                "args": {"date": "2026-03-10", "material": [
                    {"kind": "note", "text": "旧无scope聊天秘密"},
                    {"kind": "note", "text": "手写安全备注"},
                ]},
                "error": "old", "result_id": "", "revision": h.revision,
                "config_revision": raw.get("config_revision", ""),
                "epoch": int(raw.get("epoch") or 0), "generation": 1, "attempts": 1,
                "dedupe_key": "old-diary", "request_id": "r-old-d", "retry_of": "",
            }
            raw["job_order"] = ["j-old-plan", "j-old-diary"]
            path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

            loaded = h.store.load(ACCOUNT, ROLE)
            self.assertEqual(loaded["jobs"]["j-old-plan"]["args"].get("life_notes"), ["手写安全备注"])
            self.assertEqual(
                [m["text"] for m in loaded["jobs"]["j-old-diary"]["args"]["material"]],
                ["手写安全备注"],
            )
            # 先删掉手写备注，再重试旧失败作业：不应把任何已删除或聊天备注带给模型
            self.assertTrue(h.engine.delete_life_note(
                ACCOUNT, ROLE, "n-man", request_id="del-man", **h.expect(),
            ))
            retry_p = h.engine.retry_job(ACCOUNT, ROLE, "j-old-plan", request_id="ret-p", **h.expect())
            retry_d = h.engine.retry_job(ACCOUNT, ROLE, "j-old-diary", request_id="ret-d", **h.expect())
            self.assertTrue(retry_p and retry_d)
            await h.wait_idle()
            self.assertEqual(len(seen_args), 2)
            for kind, args in seen_args:
                if kind == "plan":
                    self.assertNotIn("life_notes", args)
                else:
                    self.assertEqual(args.get("material"), [])

    async def test_manual_crud_request_id_replay_and_payload_conflict(self):
        from bot.role_life_store import RoleLifeConflict
        h = Harness(self.temp.name, enabled_cfg())
        self.addAsyncCleanup(h.engine.stop)
        await h.advance("2026-03-10", "08:30")
        await h.wait_idle()

        exp1 = h.expect()
        created_item = h.engine.upsert_plan_item(
            ACCOUNT, ROLE, title="临摹钢笔字", purpose="静心",
            start_hhmm="15:00", end_hhmm="15:45", date="2026-03-10",
            request_id="req-up-1", **exp1,
        )
        ver_after_create = h.expect()["expected_state_version"]
        self.assertGreater(ver_after_create, exp1["expected_state_version"])
        # 拿着旧 state_version 重放相同请求，必须幂等返回原 item 且不二次写盘
        replayed_item = h.engine.upsert_plan_item(
            ACCOUNT, ROLE, title="临摹钢笔字", purpose="静心",
            start_hhmm="15:00", end_hhmm="15:45", date="2026-03-10",
            request_id="req-up-1", **exp1,
        )
        self.assertEqual(replayed_item["id"], created_item["id"])
        self.assertEqual(h.expect()["expected_state_version"], ver_after_create)
        with self.assertRaises(RoleLifeConflict):
            h.engine.upsert_plan_item(
                ACCOUNT, ROLE, title="篡改标题", purpose="静心",
                start_hhmm="15:00", end_hhmm="15:45", date="2026-03-10",
                request_id="req-up-1", **exp1,
            )

        exp2 = h.expect()
        created_exp = h.engine.update_experience(
            ACCOUNT, ROLE, plan_item_id=created_item["id"], text="写了一页字帖。",
            request_id="req-exp-1", **exp2,
        )
        replayed_exp = h.engine.update_experience(
            ACCOUNT, ROLE, plan_item_id=created_item["id"], text="写了一页字帖。",
            request_id="req-exp-1", **exp2,
        )
        self.assertEqual(replayed_exp["id"], created_exp["id"])

        exp3 = h.expect()
        self.assertTrue(h.engine.delete_experience(
            ACCOUNT, ROLE, experience_id=created_exp["id"],
            request_id="req-del-exp-1", **exp3,
        ))
        self.assertTrue(h.engine.delete_experience(
            ACCOUNT, ROLE, experience_id=created_exp["id"],
            request_id="req-del-exp-1", **exp3,
        ))

        exp4 = h.expect()
        note = h.engine.add_life_note(
            ACCOUNT, ROLE, "周末去书店买水彩纸", source="manual",
            request_id="req-note-1", **exp4,
        )
        replayed_note = h.engine.add_life_note(
            ACCOUNT, ROLE, "周末去书店买水彩纸", source="manual",
            request_id="req-note-1", **exp4,
        )
        self.assertEqual(replayed_note["id"], note["id"])

        exp5 = h.expect()
        self.assertTrue(h.engine.delete_life_note(
            ACCOUNT, ROLE, note["id"], request_id="req-del-note-1", **exp5,
        ))
        self.assertTrue(h.engine.delete_life_note(
            ACCOUNT, ROLE, note["id"], request_id="req-del-note-1", **exp5,
        ))

    async def test_global_single_life_preserves_history_across_persona_switch_and_migrates_legacy(self):
        with tempfile.TemporaryDirectory() as td:
            store = RoleLifeStore(Path(td))
            # 先写入一份旧版按 QQ/人设存下的状态（含手动备注和旧 chat_feedback 备注）
            legacy_state = rls.empty_state("998877", "p1")
            legacy_state["experiences"] = [{
                "id": "exp-legacy-1", "kind": "activity", "date": "2026-03-09",
                "text": "昨天下午在阳台种了薄荷。", "created_at": ts("2026-03-09", "16:00"),
                "source_ids": [], "dream": False,
            }]
            legacy_state["life_notes"] = [
                {"id": "n-chat", "date": "2026-03-09", "text": "旧聊天约定", "created_at": ts("2026-03-09", "12:00"), "source": "chat_feedback", "scope": "private:1"},
                {"id": "n-manual", "date": "2026-03-09", "text": "手写备注保留", "created_at": ts("2026-03-09", "12:01"), "source": "manual", "scope": ""},
            ]
            store.save("998877", "p1", legacy_state)
            self.assertTrue(rls.migrate_legacy_global_state(store))
            self.assertFalse(rls.migrate_legacy_global_state(store))
            migrated = store.load(rls.GLOBAL_ACCOUNT, rls.GLOBAL_ROLE)
            self.assertEqual([e["id"] for e in migrated["experiences"]], ["exp-legacy-1"])
            self.assertEqual([n["id"] for n in migrated["life_notes"]], ["n-manual"])

            seen_prompts = []
            others = {
                "personality_prompt": "",
                "personality_presets": [
                    {"id": "p1", "name": "猫娘", "prompt": "你是爱晒太阳的猫娘"},
                    {"id": "p2", "name": "管家", "prompt": "你是冷静严谨的英伦管家"},
                ],
                "active_personality_preset": "p1",
                "private_personality_preset": "",
                "group_personality_preset": "",
            }
            flat_cfg = rl.normalize_role_config({"enabled": True, "diary_enabled": False})
            now = [ts("2026-03-10", "09:00")]

            async def gen(job, reserve_attempt):
                if not await reserve_attempt():
                    return None
                seen_prompts.append((job.kind, job.persona_template))
                return {"items": [{"title": "泡茶", "purpose": "静心", "start": "10:00", "end": "10:30"}]}

            engine = rl.RoleLifeEngine(
                store,
                config_getter=lambda: flat_cfg,
                persona_getter=lambda: others,
                bot_name="小助手",
                generate=gen,
                clock=lambda: now[0],
            )
            self.addAsyncCleanup(engine.stop)
            engine.bind_account(rls.GLOBAL_ACCOUNT, 1)
            await engine.advance(now[0])
            for _ in range(100):
                st = store.load(rls.GLOBAL_ACCOUNT, rls.GLOBAL_ROLE)
                if st.get("plans", {}).get("2026-03-10"):
                    break
                await asyncio.sleep(0.01)
            self.assertIn("猫娘", seen_prompts[-1][1])
            # 切到新人设：原有经历与日程保留，手动操作不因切人设锁死，且后续生成改用新人设
            others["active_personality_preset"] = "p2"
            st_before = engine.status(rls.GLOBAL_ACCOUNT, rls.GLOBAL_ROLE)
            self.assertEqual(len(store.load(rls.GLOBAL_ACCOUNT, rls.GLOBAL_ROLE)["experiences"]), 1)
            self.assertTrue(engine.add_life_note(
                rls.GLOBAL_ACCOUNT, rls.GLOBAL_ROLE, "明天擦银器", source="manual",
                request_id="note-after-switch",
                expected_revision=st_before["revision"],
                expected_epoch=st_before["epoch"],
                expected_state_version=st_before["state_version"],
            ))
            now[0] = ts("2026-03-11", "09:00")
            await engine.advance(now[0])
            for _ in range(100):
                if len(seen_prompts) >= 2:
                    break
                await asyncio.sleep(0.01)
            self.assertIn("管家", seen_prompts[-1][1])
            # 旧经历仍然完好保留在同一份全局生活里
            final_state = store.load(rls.GLOBAL_ACCOUNT, rls.GLOBAL_ROLE)
            self.assertEqual([e["id"] for e in final_state["experiences"]], ["exp-legacy-1"])


if __name__ == "__main__":
    unittest.main()
