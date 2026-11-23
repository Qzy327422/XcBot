# -*- coding: utf-8 -*-
"""角色生活接入真实入口的离线集成测试。

做法与现有主动/AST 夹具一致：从 main.py 只提取要执行的函数/常量，用真实
`bot.role_life.RoleLifeEngine` + TempDirectory + fake clock / fake 模型 / fake
actions 驱动，不导入入口脚本、不启动 QQ、不联网、不读真实配置或聊天记录。

覆盖：
- 完整一天：计划 → 活动经历 → 日记 → 睡眠 → 梦，且预算每次真实请求恰好一次；
- 私聊秘密绝不进入共享生活提示词；
- 慢生活模型等待期间 advance 只落盘入队，不阻塞观察/冷启动路径；
- 睡眠不阻塞普通回复请求，只暂停主动；
- 每次 call 扣一次共享账号预算，拒绝时一个请求都不发；
- 人设切换会让旧 job 失效，并与普通聊天 system 同一次解析；
- 重启恢复不重放 unknown；
- partial/unknown 回执不自动重发，sent 不降级。
"""
from __future__ import annotations

import ast
import asyncio
import copy
import json
import re
import tempfile
import threading
import types
import unittest
from collections import OrderedDict, deque
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from bot import persona as persona_mod
from bot import role_life as rl
from bot.role_life_store import RoleLifeStore

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "main.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)
TZ = ZoneInfo("Asia/Shanghai")
ACCOUNT = "123456"
ROLE = "p1"
SECRET = "私聊秘密-不要进入共享生活-7f3b"


def ts(day: str, hhmm: str) -> float:
    hour, minute = (int(x) for x in hhmm.split(":"))
    y, m, d = (int(x) for x in day.split("-"))
    return datetime(y, m, d, hour, minute, tzinfo=TZ).timestamp()


def persona_others(prompt: str = "你是安静的角色 {bot_name}", active: str = ROLE) -> dict:
    return {
        "personality_prompt": "旧人格 {bot_name}",
        "personality_presets": [
            {"id": ROLE, "name": "甲", "prompt": prompt},
            {"id": "p2", "name": "乙", "prompt": "另一个人设 {user_name}"},
        ],
        "active_personality_preset": active,
        "private_personality_preset": "",
        "group_personality_preset": "",
    }


def extract_helpers(env: dict) -> dict:
    names = {
        "_role_life_prompt_for_job", "_role_life_parse_output", "_role_life_user_suffix",
        "_role_life_mark_presented", "_role_life_recently_presented", "_role_life_presented_key",
        "_role_life_begin_delivery", "_role_life_finish_delivery", "_role_life_send_still_allowed",
        "_role_life_planner_material", "_role_life_filter_life_ids", "_role_life_generate",
        "_role_life_model_manager", "_role_life_tried_for", "_role_life_status_code",
        "_role_life_is_explicit_rejection", "_role_life_public_snapshot",
        "_role_life_apply_to_context", "_proactive_chat_persona", "_RoleLifeFixedManager",
        "_merge_extra_user_suffix", "_role_life_build_user_suffix",
        "_role_life_commit_presented", "_role_life_draft_snapshot",
        "_role_life_draft_still_valid", "_resolve_context_persona",
        "_role_life_resolve_for", "_role_life_target",
    }
    nodes = []
    for node in TREE.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name in names:
            nodes.append(copy.deepcopy(node))
        elif isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id in {
                    "_ROLE_LIFE_KIND_PROMPTS", "_ROLE_LIFE_TRIED", "_ROLE_LIFE_ADAPTER_LOCK",
                    "_ROLE_LIFE_REJECTED_STATUS", "_ROLE_LIFE_TRIED_LIMIT",
                    "_ROLE_LIFE_PRESENTED", "_ROLE_LIFE_PRESENTED_LIMIT",
                    "_ROLE_LIFE_PRESENTED_PER_TARGET"} for target in node.targets):
            nodes.append(copy.deepcopy(node))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) \
                and node.target.id in {
                    "_ROLE_LIFE_KIND_PROMPTS", "_ROLE_LIFE_TRIED", "_ROLE_LIFE_ADAPTER_LOCK",
                    "_ROLE_LIFE_REJECTED_STATUS", "_ROLE_LIFE_TRIED_LIMIT",
                    "_ROLE_LIFE_PRESENTED", "_ROLE_LIFE_PRESENTED_LIMIT",
                    "_ROLE_LIFE_PRESENTED_PER_TARGET"}:
            # 带类型标注的赋值（如 `_ROLE_LIFE_PRESENTED: OrderedDict = ...`）是
            # AnnAssign，不处理会让 helper 里 NameError。
            nodes.append(copy.deepcopy(node))
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(ROOT / "main.py"), "exec"), env)
    return env


class FakeManager:
    """key_manager 形状的最小 fake；记录实际被选的槽。"""

    def __init__(self, slots):
        self.slots = list(slots)
        self.attempts = []

    def get_attempt_count(self):
        return len(self.slots) or 1

    def get_next_for_request(self, tried_keys=None, include_cooldown=True,
                             require_multimodal=False, **_kwargs):
        tried = tried_keys or set()
        for slot in self.slots:
            identity = self.make_attempt_identity(slot["base"], slot["key"],
                                                  slot["model"], slot.get("proxy", ""))
            if identity in tried:
                continue
            self.attempts.append(slot)
            return (slot["base"], slot["key"], slot["model"], False, 5,
                    f"{slot['provider']}/{slot['model']}", slot.get("proxy", ""),
                    {"reasoning_effort": ""})
        return None

    def make_attempt_identity(self, base_url, key, model, http_proxy=""):
        return (str(base_url), str(model), str(key), str(http_proxy))

    def mark_success(self, *args, **kwargs):
        return None

    def mark_failure(self, *args, **kwargs):
        return None


def ok_response(payload: dict):
    message = types.SimpleNamespace(content=json.dumps(payload, ensure_ascii=False))
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])


class LifeHarness:
    """真实引擎 + main 适配器，全部离线。"""

    def __init__(self, tmp: Path, *, model, config=None, others=None,
                 persona_prompt="你是安静的角色 {bot_name}"):
        self.now = ts("2026-03-10", "08:30")
        self.tmp = tmp
        self.store = RoleLifeStore(tmp)
        self.others = others or persona_others(persona_prompt)
        self.config = {"roles": {ACCOUNT: {
            ROLE: rl.normalize_role_config({"enabled": True, "activities_per_day": 4,
                                           **(config or {})})}}}
        self.reserve_calls = []
        self.account_calls = []
        self.completion_calls = []
        self.model = model
        # main 适配器依赖的间接入口，按真实签名以 fake 提供。
        self.model_manager = FakeManager([
            {"base": "https://offline.invalid/v1", "key": "k1", "model": "m1",
             "provider": "prov"}])
        self.env = {
            "asyncio": asyncio, "json": json, "re": re, "time": __import__("time"),
            "OrderedDict": OrderedDict, "deque": deque, "threading": threading,
            "bot_name": "测试角色",
            "get_runtime_others": lambda: self.others,
            "_get_active_account_id": lambda: ACCOUNT,
            "_get_role_life_runtime_config": lambda: self.config,
            "normalize_proxy_url": lambda value: value,
            "split_reasoning_output": lambda message: (message.content, ""),
            "role_life": rl, "user_cfg": {},
        }
        class Manager:
            def _resolve_chat_persona(self, user_name, context_type, preset_id_override=""):
                return persona_mod.resolve_persona(
                    self_.others, bot_name="测试角色", user_name=user_name,
                    context_type=context_type, preset_id_override=preset_id_override)

            def _format_system_prompt(self, persona, user_name):
                return persona.render(user_name=user_name, bot_name="测试角色")

        self_ = self
        self.env["cmc"] = Manager()
        extract_helpers(self.env)
        self.env["_role_life_model_manager"] = lambda job: self.model_manager
        self.env["_role_life_completion"] = self._completion
        self.engine = rl.RoleLifeEngine(
            self.store,
            config_getter=lambda: self.config,
            persona_getter=lambda: self.others,
            bot_name="测试角色",
            generate=self.env["_role_life_generate"],
            reserve_account=self._reserve_account,
            account_limit=lambda: 192,
            clock=lambda: self.now,
            log=lambda msg: None,
        )
        self.engine.bind_account(ACCOUNT, 1)
        rl.set_engine(self.engine)

    def _reserve_account(self, account):
        self.reserve_calls.append(account)
        return True

    async def _completion(self, base_url, api_key, proxy, timeout_seconds, kwargs):
        self.completion_calls.append(kwargs)
        await asyncio.sleep(0)  # 让出 loop，不引入真实网络等待
        return self.model(self, kwargs)

    async def advance(self, hhmm: str, day: str = "2026-03-10"):
        self.now = ts(day, hhmm)
        await self.engine.advance(self.now)
        return await self.wait_idle()

    async def run_until(self, day: str, hhmm: str, step: int = 120):
        """按真实 tick 节奏推进（步长小于连续运行阈值），允许活动正常结束。"""
        target = ts(day, hhmm)
        while self.now + step < target:
            self.now += step
            await self.engine.advance(self.now)
            await self.wait_idle()
        return await self.advance(hhmm, day)

    async def wait_idle(self, timeout=5.0):
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            state = self.store.load(ACCOUNT, ROLE)
            active = [job for job in state.get("jobs", {}).values()
                      if job.get("status") in ("pending", "running")]
            if not active and self.engine._queue_size == 0:
                return state
            await asyncio.sleep(0.01)
        raise AssertionError("engine did not become idle")

    async def cleanup(self):
        await self.engine.stop()
        rl.set_engine(None)


def plan_model(harness, kwargs):
    """按作业类型返回各自期望的结构，模拟真实模型的一次输出。"""
    kind = kind_of_prompt(kwargs)
    if kind == "plan":
        return ok_response({"items": [
            {"title": "晨间阅读", "purpose": "安静看书", "start": "09:00", "end": "10:00"},
            {"title": "整理房间", "purpose": "收拾桌面", "start": "14:00", "end": "15:00"},
        ]})
    if kind == "activity":
        return ok_response({"text": "晨间阅读结束了，心里很安静。"})
    if kind == "diary":
        return ok_response({"text": "今天读了一会儿书，也整理了房间。"})
    if kind == "dream":
        return ok_response({"text": "梦见一条很长的走廊，尽头有光。"})
    return ok_response({"mood": "平静", "energy": "一般"})


def kind_of_prompt(kwargs) -> str:
    # 结构要求写在 user 消息里，system 只有角色与总规则。
    text = "\n".join(str(message.get("content") or "")
                     for message in kwargs.get("messages", []))
    if "活动名" in text:
        return "plan"
    if "简短经历" in text:
        return "activity"
    if "短日记" in text:
        return "diary"
    if "醒来后记得的梦" in text:
        return "dream"
    if '"mood"' in text:
        return "maintenance"
    return "unknown"


class FullDayIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    async def test_real_main_adapter_plans_acts_diaries_dreams_with_one_budget_per_call(self):
        harness = LifeHarness(Path(self.temp.name), model=plan_model)
        self.addAsyncCleanup(harness.cleanup)
        await harness.advance("08:30")
        state = harness.state() if hasattr(harness, "state") else harness.store.load(ACCOUNT, ROLE)
        today = "2026-03-10"
        self.assertIn(today, state["plans"])
        self.assertEqual([i["title"] for i in state["plans"][today]["items"]],
                         ["晨间阅读", "整理房间"])
        # 活动结束节点生成并提交经历：必须按真实 tick 连续推进，才允许“结束经历”。
        await harness.run_until("2026-03-10", "10:05")
        kinds = [e["kind"] for e in harness.store.load(ACCOUNT, ROLE)["experiences"]]
        self.assertIn("activity", kinds)
        # 日记时间
        await harness.run_until("2026-03-10", "23:55")
        kinds = [e["kind"] for e in harness.store.load(ACCOUNT, ROLE)["experiences"]]
        self.assertIn("diary", kinds)
        # 睡觉 → 次日醒来生成梦：真实 tick 必须先跨过睡段，醒来才会产生一次
        # 醒后任务；直接跳到早上不会被当成“睡过一觉”。
        await harness.run_until("2026-03-11", "01:00")
        snapshot = harness.engine.snapshot(ACCOUNT, ROLE, harness.now)
        self.assertTrue(snapshot["sleeping"])
        harness.now = ts("2026-03-11", "08:45")
        await harness.engine.advance(harness.now)
        await harness.wait_idle()
        kinds = [e["kind"] for e in harness.store.load(ACCOUNT, ROLE)["experiences"]]
        self.assertIn("dream", kinds)
        # 每次真实请求：一次账号预算、一次真实 completion
        self.assertEqual(len(harness.reserve_calls), len(harness.completion_calls))
        self.assertTrue(harness.completion_calls)
        # 所有提示词都标明这是模拟，且用中性称呼
        for call in harness.completion_calls:
            text = call["messages"][0]["content"] + call["messages"][1]["content"]
            self.assertIn("模拟", text)
            self.assertIn("测试角色", text)

    async def test_private_secret_never_reaches_shared_life_prompts(self):
        harness = LifeHarness(Path(self.temp.name), model=plan_model)
        self.addAsyncCleanup(harness.cleanup)
        # 把“私聊秘密”放进运行时人设之外的所有可能入口：历史、旁听、会话名。
        harness.others["recent_private_secret"] = SECRET
        await harness.advance("08:30")
        self.assertTrue(harness.completion_calls)
        for call in harness.completion_calls:
            self.assertNotIn(SECRET, json.dumps(call, ensure_ascii=False))

    async def test_slow_life_model_does_not_block_advance_or_observation_start(self):
        release = asyncio.Event()

        def slow_model(harness, kwargs):
            return ok_response({"items": [
                {"title": "慢慢做的事", "purpose": "不急", "start": "09:00", "end": "10:00"}]})

        async def blocking_completion(self, base_url, api_key, proxy, timeout_seconds, kwargs):
            self.completion_calls.append(kwargs)
            await release.wait()
            return slow_model(self, kwargs)

        harness = LifeHarness(Path(self.temp.name), model=slow_model)
        harness.env["_role_life_completion"] = blocking_completion.__get__(harness)
        self.addAsyncCleanup(harness.cleanup)
        # advance 本身不能等模型：它只落盘并入队就返回。
        harness.now = ts("2026-03-10", "08:30")
        await asyncio.wait_for(harness.engine.advance(harness.now), timeout=1.0)
        await asyncio.sleep(0.05)
        self.assertEqual(len(harness.completion_calls), 1)  # 后台 worker 正在等
        # 慢请求期间仍能继续推进状态与读取快照（观察/冷场路径不受阻）。
        snapshot = harness.engine.snapshot(ACCOUNT, ROLE, harness.now)
        self.assertTrue(snapshot["enabled"])
        await asyncio.wait_for(harness.engine.advance(harness.now + 60), timeout=1.0)
        release.set()
        await harness.wait_idle()
        self.assertEqual(len(harness.reserve_calls), len(harness.completion_calls))

    async def test_sleep_does_not_block_normal_reply_and_pauses_proactive(self):
        harness = LifeHarness(Path(self.temp.name), model=plan_model)
        self.addAsyncCleanup(harness.cleanup)
        harness.now = ts("2026-03-10", "02:00")
        await harness.engine.advance(harness.now)
        snapshot = harness.engine.snapshot(ACCOUNT, ROLE, harness.now)
        self.assertTrue(snapshot["sleeping"])
        suffix = harness.env["_role_life_user_suffix"](ACCOUNT, ROLE)
        self.assertIn("睡眠", suffix)
        # 睡眠只暂停主动发送资格
        self.assertFalse(harness.env["_role_life_send_still_allowed"](ACCOUNT, ROLE))
        # 普通回复路径：同一次解析写回 system，并返回请求级 suffix，不改历史
        class Context:
            context_type = "private"
            chat_id = 777
            system_prompt = "旧 system"
            history = [{"role": "user", "content": "晚上好"}]
        class Manager:
            def _resolve_chat_persona(self, user_name, context_type, preset_id_override=""):
                return persona_mod.resolve_persona(harness.others, bot_name="测试角色",
                                                   user_name=user_name, context_type=context_type,
                                                   preset_id_override=preset_id_override)
            def _format_system_prompt(self, persona, user_name):
                return persona.render(user_name=user_name, bot_name="测试角色")

        harness.env["cmc"] = Manager()
        ctx = Context()
        before = copy.deepcopy(ctx.history)
        suffix2 = harness.env["_role_life_apply_to_context"](ctx)
        self.assertIn("睡眠", suffix2)
        self.assertEqual(ctx.history, before)  # 不把快照追加进 history
        self.assertNotEqual(ctx.system_prompt, "旧 system")

    async def test_budget_denial_sends_no_request(self):
        harness = LifeHarness(Path(self.temp.name), model=plan_model)
        self.addAsyncCleanup(harness.cleanup)
        harness.env["_role_life_generate"]  # real adapter
        harness._reserve_account = lambda account: False
        # 用新的 reserve_account 替换引擎里的引用
        harness.engine._reserve_account = lambda account: False
        harness.now = ts("2026-03-10", "08:30")
        await harness.engine.advance(harness.now)
        await harness.wait_idle()
        self.assertEqual(harness.completion_calls, [])
        jobs = list(harness.store.load(ACCOUNT, ROLE)["jobs"].values())
        self.assertEqual(jobs[-1]["status"], "failed")
        self.assertEqual(jobs[-1]["error"], "budget_denied")

    async def test_persona_switch_invalidates_old_job_and_uses_new_role(self):
        harness = LifeHarness(Path(self.temp.name), model=plan_model)
        self.addAsyncCleanup(harness.cleanup)
        await harness.advance("08:30")
        self.assertTrue(harness.store.load(ACCOUNT, ROLE)["plans"])
        old_revision = harness.store.load(ACCOUNT, ROLE)["revision"]
        # 同一角色改文本：revision 变化，旧计划不丢
        harness.others["personality_presets"][0]["prompt"] = "你是新的安静角色 {bot_name}"
        await harness.advance("08:31")
        state = harness.store.load(ACCOUNT, ROLE)
        self.assertNotEqual(state["revision"], old_revision)
        self.assertTrue(state["plans"])
        # 切换到另一个角色：生成请求只能看到新角色模板
        await harness.engine.stop()
        harness.others["active_personality_preset"] = "p2"
        config = {"roles": {ACCOUNT: {"p2": rl.normalize_role_config(
            {"enabled": True, "activities_per_day": 2})}}}
        engine = rl.RoleLifeEngine(
            harness.store, config_getter=lambda: config,
            persona_getter=lambda: harness.others, bot_name="测试角色",
            generate=harness.env["_role_life_generate"],
            reserve_account=lambda account: True, account_limit=lambda: 192,
            clock=lambda: harness.now + 60, log=lambda msg: None)
        engine.bind_account(ACCOUNT, 1)
        rl.set_engine(engine)
        harness.config = config
        harness.completion_calls.clear()
        await engine.advance(harness.now + 60)
        deadline = asyncio.get_running_loop().time() + 5
        while asyncio.get_running_loop().time() < deadline:
            state = harness.store.load(ACCOUNT, "p2")
            if (not [j for j in state.get("jobs", {}).values()
                     if j.get("status") in ("pending", "running")]
                    and engine._queue_size == 0):
                break
            await asyncio.sleep(0.01)
        await engine.stop()
        self.assertTrue(harness.completion_calls)
        for call in harness.completion_calls:
            self.assertIn("另一个人设", json.dumps(call, ensure_ascii=False))

    async def test_restart_unknown_job_is_not_replayed(self):
        harness = LifeHarness(Path(self.temp.name), model=plan_model)
        self.addAsyncCleanup(harness.cleanup)
        # 直接写入一个“请求可能已发出”的 running 作业，模拟进程重启。
        def mutate(state):
            state["jobs"]["plan-deadbeef"] = {
                "id": "plan-deadbeef", "kind": "plan", "status": "running",
                "created_at": harness.now, "started_at": harness.now, "finished_at": 0,
                "args": {"date": "2026-03-10"}, "error": "", "result_id": "",
                "revision": state.get("revision", ""), "config_revision": "",
                "epoch": int(state.get("epoch") or 0), "generation": 1, "attempts": 1,
                "dedupe_key": "plan:2026-03-10", "request_id": "",
            }
        harness.store.update(ACCOUNT, ROLE, mutate)
        harness.completion_calls.clear()
        await harness.advance("08:31")
        job = harness.store.load(ACCOUNT, ROLE)["jobs"]["plan-deadbeef"]
        self.assertEqual(job["status"], "unknown")
        self.assertEqual(harness.completion_calls, [])  # 绝不自动重放

    async def test_partial_or_unknown_delivery_is_not_resent_and_sent_never_downgrades(self):
        harness = LifeHarness(Path(self.temp.name), model=plan_model)
        self.addAsyncCleanup(harness.cleanup)
        await harness.advance("08:30")
        await harness.run_until("2026-03-10", "10:05")  # 产生一条活动经历
        status = harness.engine.status(ACCOUNT, ROLE)
        items = [e["id"] for e in harness.store.load(ACCOUNT, ROLE)["experiences"]]
        self.assertTrue(items)
        args = dict(is_group=False, chat_id="777", item_ids=[items[0]],
                    request_id="req-1", expected_revision=status["revision"],
                    expected_epoch=status["epoch"],
                    expected_state_version=status["state_version"])
        ticket = harness.engine.begin_delivery(ACCOUNT, ROLE, **args)
        self.assertTrue(ticket["ticket"])
        self.assertTrue(harness.engine.finish_delivery(ACCOUNT, ROLE, ticket["ticket"], "partial"))
        # 每次外部写操作都要重新取最新 CAS 版本。
        def fresh():
            now_status = harness.engine.status(ACCOUNT, ROLE)
            return {"expected_revision": now_status["revision"],
                    "expected_epoch": now_status["epoch"],
                    "expected_state_version": now_status["state_version"]}
        # partial 不回退、不重发：同一目标/item 再登记拿不到该项
        again = harness.engine.begin_delivery(
            ACCOUNT, ROLE, is_group=False, chat_id="777", item_ids=[items[0]],
            request_id="req-2", **fresh())
        self.assertEqual(again["items"], [])
        # 另一个目标仍可分享
        other = harness.engine.begin_delivery(
            ACCOUNT, ROLE, is_group=True, chat_id="888", item_ids=[items[0]],
            request_id="req-3", **fresh())
        self.assertEqual(other["items"], [items[0]])
        self.assertTrue(harness.engine.finish_delivery(ACCOUNT, ROLE, other["ticket"], "sent"))
        # sent 之后再用另一个 request 也不会重复
        third = harness.engine.begin_delivery(
            ACCOUNT, ROLE, is_group=True, chat_id="888", item_ids=[items[0]],
            request_id="req-4", **fresh())
        self.assertEqual(third["items"], [])
        # 已 sent 的 ticket 收到降级回执：幂等接受但账本不能降级。
        harness.engine.finish_delivery(ACCOUNT, ROLE, other["ticket"], "failed")
        state = harness.store.load(ACCOUNT, ROLE)
        target = state["deliveries"]["group|888"]
        self.assertEqual(target["attempts"][items[0]], "sent")
        # 别的账号不能完成本账号 ticket
        self.assertFalse(harness.engine.finish_delivery("654321", ROLE,
                                                        third["ticket"], "sent"))


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    """适配器契约：每 call 一次预算、明确拒绝才换 key、unknown 不重试。"""

    def setUp(self):
        self.others = persona_others()
        self.env = {
            "asyncio": asyncio, "json": json, "re": re, "time": __import__("time"),
            "OrderedDict": OrderedDict, "deque": deque, "threading": threading,
            "bot_name": "测试角色",
            "get_runtime_others": lambda: self.others,
            "_get_active_account_id": lambda: ACCOUNT,
            "normalize_proxy_url": lambda value: value,
            "split_reasoning_output": lambda message: (message.content, ""),
            "role_life": rl, "user_cfg": {},
        }
        extract_helpers(self.env)
        self.job = types.SimpleNamespace(
            job_id="job-1", account=ACCOUNT, role_id=ROLE, role_key="k",
            kind="plan", revision="r", config_revision="c", epoch=0, generation=1,
            persona_template="你是角色 {bot_name}",
            config={"activities_per_day": 2}, args={"date": "2026-03-10"})

    async def test_one_reserve_and_one_request_per_generate(self):
        managers, reserves, calls = [], [], []

        def manager_for(job):
            manager = FakeManager([{"base": "b", "key": "k1", "model": "m1", "provider": "p"}])
            managers.append(manager)
            return manager

        async def completion(base, key, proxy, timeout, kwargs):
            calls.append(kwargs)
            return ok_response({"items": [
                {"title": "看书", "purpose": "安静", "start": "09:00", "end": "10:00"}]})

        self.env["_role_life_model_manager"] = manager_for
        self.env["_role_life_completion"] = completion

        async def reserve():
            reserves.append(1)
            return True

        result = await self.env["_role_life_generate"](self.job, reserve)
        self.assertEqual(result["items"][0]["title"], "看书")
        self.assertEqual(len(reserves), 1)
        self.assertEqual(len(calls), 1)

    async def test_reserve_denied_sends_no_request(self):
        calls = []
        self.env["_role_life_model_manager"] = lambda job: FakeManager(
            [{"base": "b", "key": "k1", "model": "m1", "provider": "p"}])

        async def completion(*args, **kwargs):
            calls.append(kwargs)
            return ok_response({"items": []})

        self.env["_role_life_completion"] = completion

        async def reserve():
            return False

        self.assertIsNone(await self.env["_role_life_generate"](self.job, reserve))
        self.assertEqual(calls, [])

    async def test_explicit_rejection_is_retryable_and_unknown_is_not(self):
        class Rejected(Exception):
            status_code = 401

        class ServerError(Exception):
            status_code = 500

        reserves, calls = [], []

        def manager_for(job):
            return FakeManager([{"base": "b", "key": "k1", "model": "m1", "provider": "p"},
                                {"base": "b", "key": "k2", "model": "m1", "provider": "p"}])

        self.env["_role_life_model_manager"] = manager_for

        async def reserve():
            reserves.append(1)
            return True

        async def rejected(*args, **kwargs):
            calls.append(kwargs)
            raise Rejected("bad key")

        self.env["_role_life_completion"] = rejected
        with self.assertRaises(rl.LifeRequestRejected):
            await self.env["_role_life_generate"](self.job, reserve)
        self.assertEqual(len(reserves), 1)
        self.assertEqual(len(calls), 1)
        # 同 job 下一次会换另一个 key，并且仍先扣预算
        async def success(*args, **kwargs):
            calls.append(kwargs)
            return ok_response({"items": [
                {"title": "第二把钥匙", "purpose": "可用", "start": "09:00", "end": "10:00"}]})

        self.env["_role_life_completion"] = success
        result = await self.env["_role_life_generate"](self.job, reserve)
        self.assertEqual(result["items"][0]["title"], "第二把钥匙")
        self.assertEqual(len(reserves), 2)
        self.assertEqual(len(calls), 2)

        # 5xx/断连按 unknown 抛出：绝不在适配器里再打第二个 key
        unknown_job = copy.copy(self.job)
        unknown_job.job_id = "job-2"

        async def server_error(*args, **kwargs):
            calls.append(kwargs)
            raise ServerError("upstream")

        self.env["_role_life_completion"] = server_error
        before = len(calls)
        with self.assertRaises(rl.LifeGenerationUnknown):
            await self.env["_role_life_generate"](unknown_job, reserve)
        self.assertEqual(len(calls), before + 1)

    async def test_missing_explicit_model_is_not_sent_and_not_charged(self):
        self.others = persona_others()
        job = copy.copy(self.job)
        job.config = {"model_override": "prov/not-installed", "activities_per_day": 2}
        reserves = []

        async def reserve():
            reserves.append(1)
            return True

        with mock.patch.object(self.env["_role_life_model_manager"], "__call__",
                               side_effect=rl.LifeNotSent("configured_model_unavailable")):
            with self.assertRaises(rl.LifeNotSent):
                await self.env["_role_life_generate"](job, reserve)
        self.assertEqual(reserves, [])

    async def test_fixed_manager_never_falls_back_to_other_models(self):
        manager = self.env["_RoleLifeFixedManager"]([
            {"base_url": "b", "keys": ["k1"], "model": "m1",
             "display_model": "p/m1", "timeout_seconds": 5, "http_proxy": "",
             "reasoning_effort": ""},
            {"base_url": "b", "keys": ["k2"], "model": "m2",
             "display_model": "p/m2", "timeout_seconds": 5, "http_proxy": "",
             "reasoning_effort": ""},
        ])
        first = manager.get_next_for_request()
        identity = manager.make_attempt_identity("b", "k1", "m1", "")
        second = manager.get_next_for_request(tried_keys={identity})
        models = {(first[2]), (second[2])}
        self.assertEqual(models, {"m1", "m2"})


class PlannerLifeBridgeTests(unittest.IsolatedAsyncioTestCase):
    """真实 `_proactive_run_planner`：睡眠闸门、life_ids 过滤、提示词材料。"""

    def environment(self, output):
        prompts = []
        others = persona_others()
        engine = types.SimpleNamespace(
            is_sleeping=lambda account, role_id: True,
            candidates=lambda *args, **kwargs: [
                {"item_id": "exp-1", "kind": "activity", "date": "2026-03-10",
                 "text": "读了一小时书", "created_at": 1, "origin": "role_simulation"},
                {"item_id": "dream-1", "kind": "dream", "date": "2026-03-10",
                 "text": "梦到走廊", "created_at": 2, "origin": "role_simulation"},
            ],
            status=lambda *args, **kwargs: {
                "revision": "r", "epoch": 0, "state_version": 0, "enabled": True},
        )
        rl.set_engine(engine)
        self.addCleanup(lambda: rl.set_engine(None))

        class Model:
            def __init__(self, prompt):
                self.prompt = prompt

            async def agen_content(self, prompt, **kwargs):
                prompts.append(prompt)
                return output, 1, 1, 1

        class Context:
            history = [{"role": "user", "content": SECRET}]
            _history_lock = threading.Lock()
            _busy = 0
            total_tokens = 0

            def _enforce_message_limit(self):
                pass

            def _save_memory(self):
                pass

        class Manager:
            def get_context(self, *args):
                return Context()

            def _get_system_prompt(self, user_name, context_type):
                return "人格"

            def _resolve_chat_persona(self, user_name, context_type):
                return persona_mod.resolve_persona(others, bot_name="测试角色",
                                                   user_name=user_name,
                                                   context_type=context_type)

            def _format_system_prompt(self, persona, user_name):
                return "人格"

        class Actions:
            async def send(self, **kwargs):
                return {"status": "ok", "retcode": 0}

        env = {
            "asyncio": asyncio, "json": json, "time": __import__("time"), "re": re,
            "random": __import__("random"), "copy": copy, "threading": threading,
            "OrderedDict": OrderedDict, "deque": deque,
            "cmc": Manager(),
            "proactive_group_chat_context": types.SimpleNamespace(
                tail=lambda *args, **kwargs: ["群友: 今天聊考试"]),
            "get_all_blacklist": lambda: set(),
            "get_group_chat_context_max_messages": lambda: 30,
            "get_runtime_setting": lambda *args: {},
            "proactive_reply": __import__("bot.proactive_reply", fromlist=["x"]),
            "_qq_actions_lock": threading.Lock(),
            "_current_qq_actions": Actions(),
            "_current_bot_self_id": ACCOUNT,
            "_ProactiveContext": Model,
            "_get_planner_manager": lambda: "m",
            "_PROACTIVE_INITIAL_WAIT_SYSTEM": "init",
            "_PROACTIVE_GROUP_SYSTEM": "group",
            "_PROACTIVE_PRIVATE_SYSTEM": "private",
            "_role_life_planner_material": None,
            "_role_life_recently_presented": None,
            "_role_life_filter_life_ids": None,
            "_role_life_begin_delivery": None,
            "_role_life_send_still_allowed": None,
            "_role_life_finish_delivery": None,
            "_proactive_target_allowed": lambda *args: True,
            "_ai_reply_allowed": lambda *args, **kwargs: True,
            "_make_social_context": lambda request: types.SimpleNamespace(
                finished=False, wait_seconds=None, consume_reply=lambda: None),
            "_generate_proactive_content": None,
            "_hard_blacklisted_author": lambda *args: False,
            "_memory_author_visible": lambda *args: True,
            "_receipt_succeeded": lambda receipt: True,
            "_extract_send_message_id": lambda receipt: 1,
            "_proactive_parse_reply": lambda text: (False, ""),
            "build_final_send_parts": lambda text: [text],
            "Manager": types.SimpleNamespace(Message=lambda *x: x),
            "Segments": types.SimpleNamespace(Text=lambda x: x),
            "attach_trace_send": lambda *args: True,
        }
        extract_helpers(env)
        # 只替换全局引擎入口，其余 helper 用 main 的真实实现。
        node = next(node for node in TREE.body if isinstance(node, ast.AsyncFunctionDef)
                    and node.name == "_proactive_run_planner")
        exec(compile(ast.Module(body=[copy.deepcopy(node)], type_ignores=[]),
                     str(ROOT / "main.py"), "exec"), env)
        patches = [
            mock.patch.object(env["proactive_reply"], "get_session_state",
                              lambda *args: {"activity_version": 1}),
            mock.patch.object(env["proactive_reply"], "can_send",
                              lambda *args, **kwargs: True),
            mock.patch.object(env["proactive_reply"], "history_allowed", lambda *args: True),
            mock.patch.object(env["proactive_reply"], "set_observation_wait",
                              lambda *args, **kwargs: False),
            mock.patch.object(env["proactive_reply"], "record_plan", lambda *args, **kwargs: None),
            mock.patch.object(env["proactive_reply"], "mark_attempt", lambda *args, **kwargs: None),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        return env, prompts

    async def test_sleep_paused_proactive(self):
        env, prompts = self.environment("[SEND]\n聊聊")
        self.assertFalse(await env["_proactive_run_planner"](False, "123", {}))
        self.assertEqual(prompts, [])

    async def test_sleep_initial_wait_finishes_observation_without_fallback(self):
        env, prompts = self.environment("[PASS]")
        finish_calls, wait_calls = [], []
        with mock.patch.object(env["proactive_reply"], "finish_observation",
                               side_effect=lambda *a, **k: finish_calls.append((a, k)) or True), \
             mock.patch.object(env["proactive_reply"], "set_observation_wait",
                               side_effect=lambda *a, **k: wait_calls.append((a, k)) or True):
            res = await env["_proactive_run_planner"](
                False, "123", {"_observation": {"generation": 7, "initial_wait": True}})
        self.assertFalse(res)
        self.assertEqual(prompts, [])
        self.assertEqual(len(finish_calls), 1)
        self.assertEqual(finish_calls[0][1], {"expected_activity_version": 1, "expected_generation": 7})
        self.assertEqual(wait_calls, [])

    async def test_life_material_present_and_life_ids_filtered(self):
        env, prompts = self.environment(
            "text\n[SEND]\n聊聊")
        engine = rl.get_engine()
        engine.is_sleeping = lambda *args, **kwargs: False
        # 真实 planner 的 sleep 检查之后，才轮到材料注入。
        env["_role_life_begin_delivery"] = None
        env["_role_life_finish_delivery"] = None
        env["_role_life_send_still_allowed"] = None
        # 检查过滤：只有真实候选项可用
        filtered = env["_role_life_filter_life_ids"](
            {"items": [{"item_id": "exp-1"}, {"item_id": "dream-1"}]},
            ["exp-1", "fake-999", "dream-1"])
        self.assertEqual(filtered, ["exp-1", "dream-1"])
        # 真 planner 的提示词包含生活材料；私聊原文只出现在它本来该在的
        # “最近对话”里，绝不进入“角色生活材料”段（那是共享生活，不能泄露会话）。
        await env["_proactive_run_planner"](False, "123", {})
        judge_prompt = prompts[0]
        self.assertIn("exp-1", judge_prompt)
        self.assertIn("角色生活材料", judge_prompt)
        life_section = judge_prompt.split("角色生活材料", 1)[1].split("可选angle", 1)[0]
        self.assertNotIn(SECRET, life_section)
        # 梦必须被标成梦，不能当现实经历
        self.assertIn("dream", life_section)
        self.assertIn('"dream": true', life_section)


class OrdinaryReplyLedgerTests(unittest.IsolatedAsyncioTestCase):
    """真实 process_and_send：只有确认回执才记账，失败/并发不串账。"""

    def setUp(self):
        from bot import proactive_reply
        from bot.message_runtime import (CURRENT_REQUEST, MessageIndex, ReplyDecision,
                                         RequestState, bind_request)
        self.CURRENT_REQUEST = CURRENT_REQUEST
        self.MessageIndex = MessageIndex
        self.ReplyDecision = ReplyDecision
        self.RequestState = RequestState
        self.bind_request = bind_request
        self.proactive_reply = proactive_reply
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.harness = LifeHarness(Path(self.temp.name), model=plan_model)
        self.addCleanup(lambda: rl.set_engine(None))

        class Manager:
            def _resolve_chat_persona(self, user_name, context_type, preset_id_override=""):
                return persona_mod.resolve_persona(
                    self_.harness.others, bot_name="测试角色", user_name=user_name,
                    context_type=context_type, preset_id_override=preset_id_override)

            def _format_system_prompt(self, persona, user_name):
                return persona.render(user_name=user_name, bot_name="测试角色")

        self_ = self
        manager = Manager()
        self.harness.env["cmc"] = manager
        self.env = {
            "cmc": manager,
            "asyncio": asyncio, "re": re, "random": __import__("random"),
            "threading": threading, "OrderedDict": OrderedDict, "deque": deque,
            "json": json, "time": __import__("time"),
            "get_runtime_others": lambda: self.harness.others,
            "_get_active_account_id": lambda: ACCOUNT,
            "_get_role_life_runtime_config": lambda: self.harness.config,
            "normalize_proxy_url": lambda value: value,
            "split_reasoning_output": lambda message: (message.content, ""),
            "role_life": rl, "user_cfg": {},
            "CURRENT_REQUEST": CURRENT_REQUEST,
        }
        extract_helpers(self.env)
        # 真实入口在发送边界会再用一次请求状态；测试里只取当前绑定请求，
        # 不重复走黑名单策略（那是已有测试覆盖的旧逻辑）。
        self.env["_normal_ai_state"] = lambda: CURRENT_REQUEST.get()
        self.env["_consume_social_reply"] = lambda state: None
        self.env["_receipt_succeeded"] = (
            lambda ret: isinstance(ret, dict) and ret.get("status") == "ok"
            and ret.get("retcode") == 0)
        self.env["_extract_send_message_id"] = (
            lambda ret: (ret.get("data") or {}).get("message_id"))
        self.env["build_final_send_parts"] = lambda text: str(text).split("<split>")
        self.env["find_llm_reply_failover_keyword"] = lambda text: ""
        self.env["log_console"] = lambda *args, **kwargs: None
        self.env["is_split_reply_quote_enabled"] = lambda gid: False
        self.env["attach_trace_send"] = lambda *args: True
        self.env["Manager"] = types.SimpleNamespace(Message=lambda *x: x)
        self.env["Segments"] = types.SimpleNamespace(Text=lambda x: ("text", x),
                                                     Reply=lambda x: ("reply", x))
        node = next(node for node in TREE.body if isinstance(node, ast.AsyncFunctionDef)
                    and node.name == "process_and_send")
        exec(compile(ast.Module(body=[copy.deepcopy(node)], type_ignores=[]),
                     str(ROOT / "main.py"), "exec"), self.env)
        self.patch = mock.patch.object(self.proactive_reply, "notify_bot_message_sent",
                                       lambda *args, **kwargs: True)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def request(self, chat_id="777"):
        decision = self.ReplyDecision(ACCOUNT, False, str(chat_id), actor_id=str(chat_id),
                                      source="normal", sample=0.0)
        return self.RequestState(ACCOUNT, False, str(chat_id), decision)

    def presented(self, chat_id="777"):
        # 与 main 真实 helper 共用同一份进程内账本（harness.env 是引擎侧，
        # self.env 是 main 侧；这里断言的是 main 的记账结果）。
        return self.env["_role_life_recently_presented"](
            ACCOUNT, ROLE, False, str(chat_id))

    def context(self, chat_id, name):
        return types.SimpleNamespace(
            context_type="private", chat_id=int(chat_id), user_display_name=name,
            system_prompt="旧", history=[], _role_life_request=None)

    async def prepare_life_item(self):
        harness = self.harness
        await harness.advance("08:30")
        await harness.run_until("2026-03-10", "10:05")
        items = [e["id"] for e in harness.store.load(ACCOUNT, ROLE)["experiences"]]
        self.assertTrue(items)
        return items

    async def test_only_confirmed_send_marks_presented(self):
        await self.prepare_life_item()
        event = types.SimpleNamespace(group_id=None, user_id=777, self_id=ACCOUNT,
                                      message_id=None)
        sent = []

        class Actions:
            def __init__(self, receipt):
                self.receipt = receipt

            async def send(self, **kwargs):
                sent.append(kwargs)
                return self.receipt

        failing = self.request()
        # 真实链路里 bind_request 贯穿 handler → agen → process_and_send。
        with self.bind_request(failing):
            self.env["_role_life_apply_to_context"](self.context("777", "真实昵称"))
            self.assertTrue(getattr(failing, "life_meta", None))
            await self.env["process_and_send"](
                Actions({"status": "failed", "retcode": 100}), event, "没发出去", False)
            self.assertEqual(self.presented(), set())  # 失败回执不记账
            # 同一请求后续再发成功，才记账
            await self.env["process_and_send"](
                Actions({"status": "ok", "retcode": 0, "data": {"message_id": 9}}),
                event, "发出去了", False)
        self.assertTrue(self.presented())

    async def test_concurrent_requests_do_not_cross_commit(self):
        await self.prepare_life_item()
        event_a = types.SimpleNamespace(group_id=None, user_id=111, self_id=ACCOUNT,
                                        message_id=None)
        event_b = types.SimpleNamespace(group_id=None, user_id=222, self_id=ACCOUNT,
                                        message_id=None)
        a, b = self.request("111"), self.request("222")
        # 两个请求先后完成“准备材料”。A 的模型失败意味着 A 的请求结束，
        # 不会进入发送；B 随后正常发送成功。旧 A 迟到发送也不能拿 B 的 ids。
        with self.bind_request(a):
            self.env["_role_life_apply_to_context"](self.context("111", "甲"))
        with self.bind_request(b):
            self.env["_role_life_apply_to_context"](self.context("222", "乙"))
        self.assertFalse(self.presented("111"))

        class Actions:
            async def send(self, **kwargs):
                return {"status": "ok", "retcode": 0, "data": {"message_id": 1}}

        # A 的模型失败（请求结束，不提交）；B 正常发送成功，只提交 B。
        with self.bind_request(b):
            await self.env["process_and_send"](Actions(), event_b, "乙的回复", False)
        self.assertTrue(self.presented("222"))
        self.assertEqual(self.presented("111"), set())
        # A 的旧请求晚到也不会把 B 的 ids 记到 A：它只用 A 自己的 life_meta。
        # 注意两个目标引用同一份共享生活材料，所以“都已呈现”是正确结果；
        # 关键断言是 B 的发送没有提前替 A 记账（上一行已验证）。
        with self.bind_request(a):
            await self.env["process_and_send"](Actions(), event_a, "甲的迟到回复", False)
        self.assertTrue(self.presented("111"))
        self.assertTrue(self.presented("222"))

    async def test_plain_reply_does_not_mark_without_life_meta(self):
        await self.prepare_life_item()
        event = types.SimpleNamespace(group_id=None, user_id=777, self_id=ACCOUNT,
                                      message_id=None)
        state = self.request()
        self.assertIsNone(getattr(state, "life_meta", None))

        class Actions:
            async def send(self, **kwargs):
                return {"status": "ok", "retcode": 0, "data": {"message_id": 1}}

        with self.bind_request(state):
            await self.env["process_and_send"](Actions(), event, "普通回复", False)
        self.assertEqual(self.presented(), set())


class WebUIRuntimeClosureTests(unittest.IsolatedAsyncioTestCase):
    """真实 runtime 闭包（main 注册到 webui 的 dict）串联 engine。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.harness = LifeHarness(Path(self.temp.name), model=plan_model)

    def build_runtime(self):
        captured = {}
        env = dict(self.harness.env)
        env.update({
            "threading": threading, "time": __import__("time"),
            "role_life": rl, "json": json,
            "_role_life_persona_for": lambda account: (ROLE, "rev"),
            "_role_life_stop_sync": lambda: None,
        })
        extract_helpers(env)
        assign = next(node for node in TREE.body if isinstance(node, ast.Assign)
                      and any(isinstance(target, ast.Name)
                              and target.id == "_ROLE_LIFE_ENGINE_LOCK"
                              for target in node.targets))
        exec(compile(ast.Module(body=[copy.deepcopy(assign)], type_ignores=[]),
                     str(ROOT / "main.py"), "exec"), env)
        env["_set_role_life_runtime"] = lambda runtime: captured.update(runtime)
        env["_get_proactive_runtime_config"] = lambda: {}
        env["get_runtime_setting"] = lambda *args, **kwargs: {}
        register = next(node for node in TREE.body if isinstance(node, ast.FunctionDef)
                        and node.name == "_register_role_life_runtime")
        exec(compile(ast.Module(body=[copy.deepcopy(register)], type_ignores=[]),
                     str(ROOT / "main.py"), "exec"), env)
        env["_register_role_life_runtime"](self.harness.engine)
        return captured

    async def test_runtime_success_and_stale_version(self):
        # 先真实推进一次，让状态里写入 persona revision；否则外部写操作
        # 因缺少 revision 按设计拒绝。
        await self.harness.advance("09:00")
        runtime = self.build_runtime()
        status = runtime["status"](ACCOUNT, ROLE)
        self.assertTrue(status["available"])
        self.assertIn("state_version", status)
        job_id = runtime["enqueue"](ACCOUNT, ROLE, "plan", "req-web-1",
                                    status["revision"], status["epoch"],
                                    status["state_version"])["job_id"]
        self.assertTrue(job_id)
        await self.harness.wait_idle()
        again = runtime["enqueue"](ACCOUNT, ROLE, "plan", "req-web-1",
                                   status["revision"], status["epoch"],
                                   status["state_version"])["job_id"]
        self.assertEqual(again, job_id)
        stale = runtime["enqueue"](ACCOUNT, ROLE, "plan", "req-web-2",
                                   status["revision"], status["epoch"],
                                   status["state_version"] + 999)
        self.assertFalse(stale["job_id"])
        self.assertTrue(stale["error"])
        missing = runtime["retry"](ACCOUNT, ROLE, "job-missing", "req-web-3",
                                   status["revision"], status["epoch"],
                                   status["state_version"])
        self.assertFalse(missing["job_id"])
        now = runtime["status"](ACCOUNT, ROLE)
        regen = runtime["regenerate_plan"](ACCOUNT, ROLE, "req-web-4",
                                           now["revision"], now["epoch"],
                                           now["state_version"])
        self.assertTrue(regen["job_id"])
        await self.harness.wait_idle()
        now = runtime["status"](ACCOUNT, ROLE)
        cleared = runtime["clear"](ACCOUNT, ROLE, "req-web-5", now["revision"],
                                   now["epoch"], now["state_version"], False)
        self.assertTrue(cleared["ok"])
        after = runtime["status"](ACCOUNT, ROLE)
        old = runtime["clear"](ACCOUNT, ROLE, "req-web-6", now["revision"],
                               now["epoch"], now["state_version"], False)
        self.assertFalse(old["ok"])
        self.assertEqual(int(after["epoch"]), int(now["epoch"]) + 1)
        self.assertEqual(runtime["accounts"]()["current"], ACCOUNT)
        self.assertTrue(runtime["connected"]())

    async def test_corrupt_clear_requires_allow_corrupt(self):
        await self.harness.advance("09:00")
        runtime = self.build_runtime()
        path = self.harness.store.state_path(ACCOUNT, ROLE)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        status = runtime["status"](ACCOUNT, ROLE)
        self.assertTrue(status.get("corrupt"))
        denied = runtime["clear"](ACCOUNT, ROLE, "req-corrupt-1", "", None, None, False)
        self.assertFalse(denied["ok"])
        allowed = runtime["clear"](ACCOUNT, ROLE, "req-corrupt-2", "", None, None, True)
        self.assertTrue(allowed["ok"])
        self.assertFalse(runtime["status"](ACCOUNT, ROLE).get("corrupt"))


class FinalReviewRegressionTests(unittest.IsolatedAsyncioTestCase):
    """针对最终 review 的四条真实函数断言。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    async def asyncSetUp(self):
        self.harness = LifeHarness(Path(self.temp.name), model=plan_model)
        self.addAsyncCleanup(self.harness.cleanup)

    async def prepare_item(self):
        await self.harness.advance("08:30")
        await self.harness.run_until("2026-03-10", "10:05")
        return [e["id"] for e in self.harness.store.load(ACCOUNT, ROLE)["experiences"]]

    async def test_a_draft_revalidates_same_context_persona_and_revision(self):
        # 群聊与私聊跟随不同预设：群聊草稿必须按群聊解析核验，不能被默认
        # active 的私聊角色误拒；改文本（revision 变化）仍要作废。
        harness = self.harness
        others = {
            "personality_prompt": "默认",
            "personality_presets": [
                {"id": "private-role", "name": "私", "prompt": "私聊人格"},
                {"id": "group-role", "name": "群", "prompt": "群聊人格"},
            ],
            "active_personality_preset": "private-role",
            "private_personality_preset": "",
            "group_personality_preset": "group-role",
        }
        harness.others.clear()
        harness.others.update(others)
        config = {"roles": {ACCOUNT: {"group-role": rl.normalize_role_config(
            {"enabled": True, "activities_per_day": 2})}}}
        harness.config = config
        harness.env["_get_role_life_runtime_config"] = lambda: config
        engine = rl.RoleLifeEngine(
            harness.store, config_getter=lambda: config,
            persona_getter=lambda: harness.others, bot_name="测试角色",
            generate=harness.env["_role_life_generate"],
            reserve_account=lambda account: True, account_limit=lambda: 192,
            clock=lambda: harness.now, log=lambda msg: None)
        engine.bind_account(ACCOUNT, 1)
        rl.set_engine(engine)
        await engine.advance(harness.now)
        deadline = asyncio.get_running_loop().time() + 5
        while asyncio.get_running_loop().time() < deadline:
            state = harness.store.load(ACCOUNT, "group-role")
            if not [j for j in state.get("jobs", {}).values()
                    if j.get("status") in ("pending", "running")]:
                break
            await asyncio.sleep(0.01)
        draft = harness.env["_role_life_draft_snapshot"](
            ACCOUNT, "group-role", context_type="group", user_name="用户123")
        self.assertIsNotNone(draft)
        self.assertTrue(harness.env["_role_life_draft_still_valid"](
            ACCOUNT, "group-role", draft))
        # 改群聊人格文本：revision 变化，旧草稿失效
        harness.others["personality_presets"][1]["prompt"] = "群聊人格改"
        self.assertFalse(harness.env["_role_life_draft_still_valid"](
            ACCOUNT, "group-role", draft))
        await engine.stop()

    async def test_b_begin_failure_or_subset_abandons_whole_draft(self):
        await self.prepare_item()
        sent_parts = []

        def make_env(begin_result):
            env, _prompts = self._planner_env(
                "主动正文<split>第二段", begin_result=begin_result)
            return env

        class Actions:
            async def send(self, **kwargs):
                sent_parts.append(kwargs)
                return {"status": "ok", "retcode": 0, "data": {"message_id": 1}}

        for result in (None, {"ticket": "", "items": [], "target": "private|123"},
                       {"ticket": "t", "items": ["only-one"], "target": "private|123"}):
            sent_parts.clear()
            env = make_env(result)
            env["_role_life_planner_material"] = lambda *a, **k: {
                "items": [{"item_id": "exp-1", "kind": "activity", "date": "2026-03-10",
                           "text": "读书", "dream": False, "origin": "role_simulation"},
                          {"item_id": "exp-2", "kind": "activity", "date": "2026-03-10",
                           "text": "整理", "dream": False, "origin": "role_simulation"}],
                "sleeping": False, "enabled": True}
            env["_role_life_draft_snapshot"] = lambda *a, **k: {"role_id": ROLE}
            env["_role_life_draft_still_valid"] = lambda *a, **k: True
            env["_current_qq_actions"] = Actions()
            env["_role_life_begin_delivery"] = lambda *a, **k: result
            env["_role_life_finish_delivery"] = lambda *a, **k: None
            env["_role_life_send_still_allowed"] = lambda *a, **k: True
            env["_proactive_parse_reply"] = lambda text: (True, "angle")
            env["_planner_tool_decision"] = lambda *a, **k: {
                "action": "reply", "reason": "", "seconds": None, "angle": "",
                "fact_ids": [], "life_ids": ["exp-1", "exp-2"], "avoid": [],
                "purpose": "chat"}
            await env["_proactive_run_planner"](False, "123", {})
            self.assertEqual(sent_parts, [], f"begin={result!r} 不应发送任何分段")

    def _writer_model(self, content):
        class Model:
            def __init__(self, prompt):
                self.prompt = prompt

            async def agen_content(self, prompt, **kwargs):
                return content, 1, 1, 1
        return Model

    def _planner_env(self, writer_output, begin_result=None):
        harness = self.harness
        prompts = []

        class PlannerModel:
            # 原生工具决策优先；真实 planner 走 native 而不是解析文本。
            planner_decision = {"action": "reply", "reason": "life", "seconds": None,
                                "angle": "", "fact_ids": [],
                                "life_ids": ["exp-1", "exp-2"], "avoid": [],
                                "purpose": "chat"}
            planner_raw_output = "[SEND]"

            def __init__(self, prompt):
                self.prompt = prompt

            async def agen_content(self, prompt, **kwargs):
                prompts.append(prompt)
                # 同一个类既当 judge 又当 writer：按提示词区分角色。
                if "主动发言" in str(prompt) or "最终只输出实际" in str(prompt):
                    return writer_output, 1, 1, 1
                return "[SEND]\\n理由", 1, 1, 1

        class WriterModel:
            planner_decision = None

            def __init__(self, prompt):
                self.prompt = prompt

            async def agen_content(self, prompt, **kwargs):
                prompts.append(prompt)
                return writer_output, 1, 1, 1

        class Context:
            history = [{"role": "user", "content": "你好"}]
            _history_lock = threading.Lock()
            _busy = 0
            total_tokens = 0

            def _enforce_message_limit(self):
                pass

            def _save_memory(self):
                pass

        class Manager:
            def get_context(self, *args):
                return Context()

            def _get_system_prompt(self, user_name, context_type):
                return "人格"

            def _resolve_chat_persona(self, user_name, context_type, preset_id_override=""):
                return persona_mod.resolve_persona(
                    harness.others, bot_name="测试角色", user_name=user_name,
                    context_type=context_type, preset_id_override=preset_id_override)

            def _format_system_prompt(self, persona, user_name):
                return "人格"

        env = {
            "asyncio": asyncio, "json": json, "time": __import__("time"), "re": re,
            "random": __import__("random"), "copy": copy, "threading": threading,
            "OrderedDict": OrderedDict, "deque": deque,
            "cmc": Manager(),
            "proactive_group_chat_context": types.SimpleNamespace(
                tail=lambda *args, **kwargs: ["群友: 你好"]),
            "get_all_blacklist": lambda: set(),
            "get_group_chat_context_max_messages": lambda: 30,
            "get_runtime_setting": lambda *args: {},
            "proactive_reply": __import__("bot.proactive_reply", fromlist=["x"]),
            "_qq_actions_lock": threading.Lock(),
            "_current_qq_actions": None,
            "_current_bot_self_id": ACCOUNT,
            "_ProactiveContext": PlannerModel,
            "_get_planner_manager": lambda: "m",
            "_PROACTIVE_INITIAL_WAIT_SYSTEM": "init",
            "_PROACTIVE_GROUP_SYSTEM": "group",
            "_PROACTIVE_PRIVATE_SYSTEM": "private",
            "_proactive_target_allowed": lambda *args: True,
            "_ai_reply_allowed": lambda *args, **kwargs: True,
            "_make_social_context": lambda request: types.SimpleNamespace(
                finished=False, wait_seconds=None, consume_reply=lambda: None),
            "_generate_proactive_content": None,
            "_hard_blacklisted_author": lambda *args: False,
            "_memory_author_visible": lambda *args: True,
            "_receipt_succeeded": lambda receipt: True,
            "_extract_send_message_id": lambda receipt: 1,
            "build_final_send_parts": lambda text: text.split("<split>"),
            "find_llm_reply_failover_keyword": lambda text: "",
            "Manager": types.SimpleNamespace(Message=lambda *x: x),
            "Segments": types.SimpleNamespace(Text=lambda x: x),
            "attach_trace_send": lambda *args: True,
        }
        extract_helpers(env)

        async def fake_generate(writer, prompt, request):
            text, total, _p, _c = await writer.agen_content(prompt)
            return text, total

        env["_generate_proactive_content"] = fake_generate
        node = next(node for node in TREE.body if isinstance(node, ast.AsyncFunctionDef)
                    and node.name == "_proactive_run_planner")
        exec(compile(ast.Module(body=[copy.deepcopy(node)], type_ignores=[]),
                     str(ROOT / "main.py"), "exec"), env)
        patches = [
            mock.patch.object(env["proactive_reply"], "get_session_state",
                              lambda *args: {"activity_version": 1}),
            mock.patch.object(env["proactive_reply"], "can_send",
                              lambda *args, **kwargs: True),
            mock.patch.object(env["proactive_reply"], "history_allowed", lambda *args: True),
            mock.patch.object(env["proactive_reply"], "set_observation_wait",
                              lambda *args, **kwargs: False),
            mock.patch.object(env["proactive_reply"], "record_plan",
                              lambda *args, **kwargs: None),
            mock.patch.object(env["proactive_reply"], "mark_attempt",
                              lambda *args, **kwargs: None),
            mock.patch.object(env["proactive_reply"], "notify_bot_message_sent",
                              lambda *args, **kwargs: True),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        return env, prompts

    async def test_c_all_parts_confirmed_before_sent_and_partial_recorded(self):
        await self.prepare_item()
        finishes = []
        sent = []

        class Actions:
            def __init__(self, fail_at=None):
                self.fail_at = fail_at

            async def send(self, **kwargs):
                if self.fail_at is not None and len(sent) == self.fail_at:
                    raise RuntimeError("第二段失败")
                sent.append(kwargs)
                return {"status": "ok", "retcode": 0, "data": {"message_id": 1}}

        # 两段都成功 → 最后记 sent（不是首段就记）
        env, _ = self._planner_env("第一段<split>第二段")
        env["_role_life_planner_material"] = lambda *a, **k: {
            "items": [{"item_id": "exp-1", "kind": "activity", "date": "2026-03-10",
                       "text": "读书", "dream": False, "origin": "role_simulation"}],
            "sleeping": False, "enabled": True}
        env["_role_life_draft_snapshot"] = lambda *a, **k: {"role_id": ROLE}
        env["_role_life_draft_still_valid"] = lambda *a, **k: True
        env["_role_life_begin_delivery"] = lambda *a, **k: {
            "ticket": "ticket-1", "items": ["exp-1"], "target": "private|123"}
        env["_role_life_finish_delivery"] = lambda *a, **k: finishes.append((a, k))
        env["_role_life_send_still_allowed"] = lambda *a, **k: True
        env["_current_qq_actions"] = Actions()
        env["_proactive_parse_reply"] = lambda text: (True, "angle")
        env["_planner_tool_decision"] = lambda *a, **k: {
            "action": "reply", "reason": "", "seconds": None, "angle": "",
            "fact_ids": [], "life_ids": ["exp-1"], "avoid": [], "purpose": "chat"}
        await env["_proactive_run_planner"](False, "123", {})
        self.assertEqual(len(sent), 2)
        self.assertEqual(finishes[0][0][3], "sent")

        # 第二段发送前被睡眠/资格闸门拦住 → partial，且不自动重发。
        finishes.clear()
        sent.clear()
        env, _ = self._planner_env("第一段<split>第二段")
        env["_role_life_planner_material"] = lambda *a, **k: {
            "items": [{"item_id": "exp-1", "kind": "activity", "date": "2026-03-10",
                       "text": "读书", "dream": False, "origin": "role_simulation"}],
            "sleeping": False, "enabled": True}
        env["_role_life_draft_snapshot"] = lambda *a, **k: {"role_id": ROLE}
        env["_role_life_draft_still_valid"] = lambda *a, **k: True
        env["_role_life_begin_delivery"] = lambda *a, **k: {
            "ticket": "ticket-2", "items": ["exp-1"], "target": "private|123"}
        env["_role_life_finish_delivery"] = lambda *a, **k: finishes.append((a, k))
        checks = {"n": 0}

        def guard(*args, **kwargs):
            checks["n"] += 1
            return checks["n"] == 1  # 首段放行，第二段前模拟进入睡眠

        env["_role_life_send_still_allowed"] = guard
        env["_current_qq_actions"] = Actions()
        env["_proactive_parse_reply"] = lambda text: (True, "angle")
        env["_planner_tool_decision"] = lambda *a, **k: {
            "action": "reply", "reason": "", "seconds": None, "angle": "",
            "fact_ids": [], "life_ids": ["exp-1"], "avoid": [], "purpose": "chat"}
        await env["_proactive_run_planner"](False, "123", {})
        self.assertEqual(len(sent), 1)
        self.assertEqual(finishes[-1][0][3], "partial")

    async def test_d_ordinary_persona_injection_happens_after_history_lock(self):
        # 真实 EnhancedLimitedDeepSeekContext.agen_content：先拿历史锁，再改
        # system_prompt；没有 CURRENT_REQUEST 时（WebUI 沙盒）不注入 QQ 生活。
        from bot.message_runtime import CURRENT_REQUEST
        harness = self.harness
        events = []
        real_apply = harness.env["_role_life_apply_to_context"]

        def traced(context):
            events.append("life")
            return real_apply(context)

        class Lock:
            def __init__(self):
                self.locked = False

            def acquire(self, blocking=True):
                events.append("lock")
                self.locked = True
                return True

            def release(self):
                self.locked = False

            def __enter__(self):
                return self.acquire()

            def __exit__(self, *args):
                self.release()

        env = {
            "asyncio": asyncio, "threading": threading, "json": json, "re": re,
            "OrderedDict": OrderedDict, "deque": deque, "time": __import__("time"),
            "get_runtime_others": lambda: harness.others,
            "_get_active_account_id": lambda: ACCOUNT,
            "_get_role_life_runtime_config": lambda: harness.config,
            "normalize_proxy_url": lambda value: value,
            "split_reasoning_output": lambda message: (message.content, ""),
            "role_life": rl, "user_cfg": {},
            "CURRENT_REQUEST": CURRENT_REQUEST,
        }
        extract_helpers(env)
        env["_role_life_apply_to_context"] = traced
        env["_normal_ai_state"] = lambda: None
        env["acquire_history_lock"] = lambda lock: asyncio.sleep(0) or True
        env["_merge_extra_user_suffix"] = lambda text, extra: text + (extra or "")
        # 只取 agen_content 开头：验证“锁后注入”顺序即可，不跑模型。
        owner = next(node for node in TREE.body if isinstance(node, ast.ClassDef)
                     and node.name == "EnhancedLimitedDeepSeekContext")
        method = next(node for node in owner.body if isinstance(node, ast.AsyncFunctionDef)
                      and node.name == "agen_content")
        source = ast.unparse(method)
        self.assertLess(source.index("acquire_history_lock"), source.index("_role_life_apply_to_context"))
        # 无 CURRENT_REQUEST 时 _role_life_apply_to_context 不写 life_meta。
        class Sandbox:
            context_type = "webui"
            chat_id = 1
            system_prompt = "沙盒"
            history = []
        self.assertEqual(real_apply(Sandbox()), "")
        self.assertIsNone(getattr(Sandbox(), "life_meta", None))

    async def test_e_parse_output_extracts_fenced_json_and_retries_invalid(self):
        outputs = [
            "这是说明文字，没有有效字段",
            '好的，安排如下：\n```json\n{"items":[{"title":"泡茶","purpose":"休息","start":"09:00","end":"10:00"}]}\n```',
        ]

        def model(harness, kwargs):
            return types.SimpleNamespace(choices=[types.SimpleNamespace(
                message=types.SimpleNamespace(content=outputs.pop(0)))])

        with tempfile.TemporaryDirectory() as d1:
            h = LifeHarness(Path(d1), model=model)
            self.addAsyncCleanup(h.cleanup)
            state = await h.advance("08:30")
            jobs = list(state["jobs"].values())
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]["status"], "succeeded")
            self.assertEqual(jobs[0]["attempts"], 2)
            self.assertEqual(state["plans"]["2026-03-10"]["items"][0]["title"], "泡茶")

        # 连续两次无法解析时应记 invalid，而不是误报 not_sent
        def bad_model(harness, kwargs):
            return types.SimpleNamespace(choices=[types.SimpleNamespace(
                message=types.SimpleNamespace(content="totally not json"))])

        with tempfile.TemporaryDirectory() as d2:
            h2 = LifeHarness(Path(d2), model=bad_model)
            self.addAsyncCleanup(h2.cleanup)
            state2 = await h2.advance("08:30")
            failed = [j for j in state2["jobs"].values() if j["status"] == "failed"]
            self.assertEqual(len(failed), 1)
            self.assertTrue(failed[0]["error"].startswith("invalid:"), failed[0]["error"])
            self.assertEqual(failed[0]["attempts"], 2)

    def test_f_plan_prompt_includes_current_time_sleep_and_existing_items(self):
        job = rl.LifeJob(
            job_id="j1", account=ACCOUNT, role_id=ROLE, role_key="k",
            kind="plan", revision="r1", config_revision="c1", epoch=0,
            generation=1, persona_template="你是测试角色",
            config=rl.normalize_role_config({"enabled": True, "sleep_enabled": True,
                                             "sleep_start": "00:30", "sleep_hours": 8}),
            args={"date": "2026-03-10", "current_time": "14:20",
                  "plan_context": [{"title": "午休", "start": "12:00", "end": "13:00", "status": "completed"}]},
        )
        msgs = self.harness.env["_role_life_prompt_for_job"](job)
        user_msg = msgs[1]["content"]
        self.assertIn("当前时间：14:20", user_msg)
        self.assertIn("睡眠时段：00:30 起 8 小时", user_msg)
        self.assertIn("午休", user_msg)

    def test_g_mood_tone_calendar_and_chat_feedback(self):
        with tempfile.TemporaryDirectory() as d1:
            h = LifeHarness(Path(d1), model=plan_model)
            self.addAsyncCleanup(h.cleanup)
            # 验证日期/节令/季节注入到 plan/diary prompt（无城市天气）
            job = rl.LifeJob(
                job_id="j2", account=ACCOUNT, role_id=ROLE, role_key="k",
                kind="diary", revision="r1", config_revision="c1", epoch=0,
                generation=1, persona_template="你是测试角色",
                config=rl.normalize_role_config({"enabled": True, "env_awareness_enabled": True}),
                args={"date": "2026-10-01", "material": [{"kind": "activity", "date": "2026-10-01", "text": "散步"}]},
            )
            msgs = h.env["_role_life_prompt_for_job"](job)
            self.assertIn("国庆节", msgs[1]["content"])
            self.assertNotIn("当地天气", msgs[1]["content"])

            # 验证 mood_tone 注入（含刚醒）以及关闭时不注入心情句式，且已移除聊天约定自动提取
            snap = {"enabled": True, "available": True, "sleeping": False, "recent_woke_up": True,
                    "mood_tone_enabled": True, "mood": {"mood": "惬意", "energy": "充沛"},
                    "plan": {"items": [{"id": "p1", "title": "稍后整理书架", "status": "planned"}]},
                    "recent": [
                        {"id": "e1", "text": "喝了茶", "dream": False},
                        {"id": "e2", "text": "第二条不该带进普通聊天", "dream": False},
                    ]}
            with mock.patch.dict(h.env, {"_role_life_public_snapshot": lambda a, r, now=None: snap}):
                suffix, ids = h.env["_role_life_build_user_suffix"](ACCOUNT, ROLE)
                self.assertIn("刚睡醒不久", suffix)
                self.assertIn("心情「惬意」", suffix)
                self.assertIn("喝了茶", suffix)
                self.assertNotIn("第二条不该带进普通聊天", suffix)
                self.assertNotIn("稍后整理书架", suffix)
                self.assertEqual(ids, ["e1"])
                self.assertLessEqual(len(suffix), 480)
                snap["mood_tone_enabled"] = False
                suffix_off, _ = h.env["_role_life_build_user_suffix"](ACCOUNT, ROLE)
                self.assertNotIn("刚睡醒不久", suffix_off)
                self.assertNotIn("心情「惬意」", suffix_off)
            self.assertNotIn("_role_life_extract_chat_note", h.env)

    async def test_h_planner_receives_live_state_summary_when_free(self):
        snap = {
            "enabled": True, "available": True, "sleeping": False, "recent_woke_up": True,
            "is_free": True, "mood_tone_enabled": True, "env_awareness_enabled": True,
            "date": "2026-10-01", "timezone": "Asia/Shanghai",
            "mood": {"mood": "悠闲", "energy": "充沛"},
            "current_activity": None,
            "plan": {"items": [{"id": "p2", "title": "夜晚看电影", "status": "planned"}]},
            "life_notes": [{"id": "n1", "text": "手写备注不塞进聊天状态"}],
            "recent": [{"id": "exp-1", "kind": "activity", "date": "2026-10-01", "text": "泡了壶乌龙茶"}],
        }
        fake_engine = types.SimpleNamespace(
            is_sleeping=lambda *a, **k: False,
            candidates=lambda *a, **k: [{
                "item_id": "exp-1", "kind": "activity", "date": "2026-10-01",
                "text": "泡了壶乌龙茶", "dream": False,
            }],
        )
        async def ok_send(**kwargs):
            return {"status": "ok", "retcode": 0, "data": {"message_id": 1}}

        env, prompts = self._planner_env("国庆放假在家好闲，刚泡了壶茶")
        env["_role_life_public_snapshot"] = lambda a, r, now=None: snap
        env["_current_qq_actions"] = types.SimpleNamespace(send=ok_send)
        env["_proactive_parse_reply"] = lambda text: (True, "idle")
        env["_planner_tool_decision"] = lambda *a, **k: {
            "action": "reply", "reason": "idle", "seconds": None, "angle": "",
            "fact_ids": [], "life_ids": ["exp-1"], "avoid": [], "purpose": "chat",
        }
        env["_role_life_draft_snapshot"] = lambda *a, **k: {"role_id": ROLE}
        env["_role_life_draft_still_valid"] = lambda *a, **k: True
        env["_role_life_begin_delivery"] = lambda *a, **k: {"ticket": "t1", "items": ["exp-1"]}
        env["_role_life_finish_delivery"] = lambda *a, **k: None
        env["_role_life_send_still_allowed"] = lambda *a, **k: True
        with mock.patch("bot.role_life.get_engine", return_value=fake_engine):
            mat = env["_role_life_planner_material"](ACCOUNT, ROLE, False, "123")
            self.assertTrue(mat["is_free"])
            self.assertFalse(mat["busy"])
            self.assertIn("暂时空闲", mat["state_summary"])
            self.assertIn("国庆节", mat["state_summary"])
            self.assertIn("心情「悠闲」", mat["state_summary"])
            self.assertNotIn("夜晚看电影", mat["state_summary"])
            self.assertNotIn("手写备注不塞进聊天状态", mat["state_summary"])
            await env["_proactive_run_planner"](False, "123", {})
        self.assertGreaterEqual(len(prompts), 2)
        self.assertIn("角色当前生活状态", prompts[0])
        self.assertIn("暂时空闲", prompts[0])
        self.assertIn("角色当前生活状态", prompts[1])

    async def test_i_manual_notes_only_and_not_injected_into_chat_state(self):
        self.assertIsNone(self.harness.engine.add_life_note(
            ACCOUNT, ROLE, "和群友约好周末去公园看展",
            scope="group:111", now=self.harness.now,
        ))
        self.harness.engine.add_life_note(
            ACCOUNT, ROLE, "手动备注：周末整理书架",
            source="manual", now=self.harness.now,
        )
        snap = self.harness.env["_role_life_public_snapshot"](ACCOUNT, ROLE, scope="group:222")
        self.assertEqual([n["text"] for n in snap["life_notes"]], ["手动备注：周末整理书架"])
        suffix, _ = self.harness.env["_role_life_build_user_suffix"](
            ACCOUNT, ROLE, is_group=True, chat_id="222",
        )
        self.assertNotIn("整理书架", suffix)
        mat = self.harness.env["_role_life_planner_material"](ACCOUNT, ROLE, False, "333")
        self.assertNotIn("整理书架", mat["state_summary"])

    async def test_j_apply_to_context_works_without_qq_and_never_auto_adds_chat_notes(self):
        from bot.message_runtime import CURRENT_REQUEST, ReplyDecision, RequestState
        from bot.role_life_store import GLOBAL_ACCOUNT, GLOBAL_ROLE
        exp_ids = await self.prepare_item()
        exp_text = next(
            e["text"] for e in self.harness.store.load(ACCOUNT, ROLE)["experiences"]
            if e["id"] == exp_ids[0]
        )
        env = dict(self.harness.env)
        env.update({
            "CURRENT_REQUEST": CURRENT_REQUEST,
            "_current_bot_self_id": None,
            "_get_active_account_id": lambda: "",
        })
        extract_helpers(env)
        ctx = types.SimpleNamespace(
            context_type="private", chat_id="555",
            system_prompt="基础设定", history=[], user_name="测试用户",
        )
        req = RequestState(ACCOUNT, False, "555", ReplyDecision(ACCOUNT, False, "555", source="normal"))
        token = CURRENT_REQUEST.set(req)
        try:
            suffix = env["_role_life_apply_to_context"](ctx, user_message="约好周末一起吃火锅")
        finally:
            CURRENT_REQUEST.reset(token)
        self.assertEqual(req.life_meta["account"], ACCOUNT)
        self.assertIn(exp_text[:60], suffix)
        env["_role_life_commit_presented"](req, False, "555")
        # 已取消聊天约定自动写入，life_notes 保持为空
        self.assertEqual(self.harness.store.load(ACCOUNT, ROLE)["life_notes"], [])
        # 切换为全局生活绑定且未连 QQ 时，普通聊天照样可读统一生活
        self.harness.engine.bind_account(GLOBAL_ACCOUNT, 1)
        self.harness.store.save(GLOBAL_ACCOUNT, GLOBAL_ROLE, self.harness.store.load(ACCOUNT, ROLE))
        self.harness.config = rl.normalize_role_config({"enabled": True})
        suffix_global = env["_role_life_apply_to_context"](ctx, user_message="你好呀")
        self.assertIn(exp_text[:60], suffix_global)

    async def test_k_clear_current_qq_actions_keeps_global_role_life_running(self):
        from bot import proactive_reply
        from bot.message_runtime import CURRENT_REQUEST
        from bot.role_life_store import GLOBAL_ACCOUNT
        old_gen = object()
        self.harness.engine.bind_account(GLOBAL_ACCOUNT, 1)
        rl.set_engine(self.harness.engine)
        env = {
            "role_life": rl,
            "proactive_reply": proactive_reply,
            "CURRENT_REQUEST": CURRENT_REQUEST,
            "_qq_actions_lock": threading.RLock(),
            "_current_qq_actions": object(),
            "_current_bot_self_id": ACCOUNT,
            "_proactive_connection_generation": old_gen,
            "_proactive_bootstrap_pending": True,
            "_proactive_bootstrap_ready": True,
            "GROUP_REPLIED_MESSAGES": {"1": set()},
            "PRIVATE_REPLIED_MESSAGES": {"2": set()},
            "_proactive_reset_lock": threading.RLock(),
            "_proactive_reset_ids": {"1"},
            "_register_role_life_runtime": lambda *a, **k: None,
            "INDEX": types.SimpleNamespace(clear_all=lambda: None),
        }
        nodes = [copy.deepcopy(node) for node in TREE.body
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and node.name in {"clear_current_qq_actions"}]
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(ROOT / "main.py"), "exec"), env)
        with mock.patch.object(proactive_reply, "cancel_observations") as cancel_obs:
            env["clear_current_qq_actions"]()
        cancel_obs.assert_called_once()
        # QQ 断连清理不影响独立运行的全局角色生活
        self.assertEqual(self.harness.engine._account, GLOBAL_ACCOUNT)
        self.assertIsNot(env["_proactive_connection_generation"], old_gen)
        self.assertFalse(env["_proactive_bootstrap_ready"])

    async def test_l_pre_judge_life_draft_aborts_send_if_deleted_during_judge(self):
        exp_ids = await self.prepare_item()
        exp_id = exp_ids[0]
        env, _prompts = self._planner_env("主动聊聊")
        env.update({
            "role_life": rl,
            "bot_name": "测试角色",
            "get_runtime_others": lambda: self.harness.others,
            "_get_role_life_runtime_config": lambda: self.harness.config,
            "_get_active_account_id": lambda: ACCOUNT,
        })
        extract_helpers(env)
        # 重新绑定真实 _proactive_run_planner 使其看到更新后的 helper 与 env
        node = next(node for node in TREE.body if isinstance(node, ast.AsyncFunctionDef)
                    and node.name == "_proactive_run_planner")
        exec(compile(ast.Module(body=[copy.deepcopy(node)], type_ignores=[]),
                     str(ROOT / "main.py"), "exec"), env)
        sent_parts = []
        validity_checks = []
        orig_still_valid = env["_role_life_draft_still_valid"]

        def checked_valid(*args, **kwargs):
            ok = orig_still_valid(*args, **kwargs)
            validity_checks.append(ok)
            return ok

        env["_role_life_draft_still_valid"] = checked_valid

        class Actions:
            async def send(self, **kwargs):
                sent_parts.append(kwargs)
                return {"status": "ok", "retcode": 0, "data": {"message_id": 1}}

        env["_current_qq_actions"] = Actions()
        env["_ProactiveContext"].planner_decision = {
            "action": "reply", "reason": "life", "seconds": None,
            "angle": "", "fact_ids": [], "life_ids": [exp_id],
            "avoid": [], "purpose": "chat",
        }
        orig_agen = env["_ProactiveContext"].agen_content
        called = {"n": 0}
        deleted = {"ok": False}

        async def racing_agen(model_self, prompt, **kwargs):
            called["n"] += 1
            res = await orig_agen(model_self, prompt, **kwargs)
            if called["n"] == 1:
                deleted["ok"] = self.harness.engine.delete_experience(
                    ACCOUNT, ROLE, experience_id=exp_id,
                )
            return res

        env["_ProactiveContext"].agen_content = racing_agen
        env["_proactive_parse_reply"] = lambda text: (True, "life")
        result = await env["_proactive_run_planner"](False, "123", {})
        self.assertTrue(deleted["ok"])
        self.assertIn(False, validity_checks)
        self.assertFalse(result)
        self.assertEqual(sent_parts, [])

    async def test_m_life_summary_without_life_ids_still_checks_draft_version(self):
        await self.prepare_item()
        self.harness.engine.add_life_note(
            ACCOUNT, ROLE, "周末去看画展",
            source="manual", now=self.harness.now,
        )
        env, _prompts = self._planner_env("主动聊聊")
        env.update({
            "role_life": rl,
            "bot_name": "测试角色",
            "get_runtime_others": lambda: self.harness.others,
            "_get_role_life_runtime_config": lambda: self.harness.config,
            "_get_active_account_id": lambda: ACCOUNT,
        })
        extract_helpers(env)
        node = next(node for node in TREE.body if isinstance(node, ast.AsyncFunctionDef)
                    and node.name == "_proactive_run_planner")
        exec(compile(ast.Module(body=[copy.deepcopy(node)], type_ignores=[]),
                     str(ROOT / "main.py"), "exec"), env)
        sent_parts = []
        validity_checks = []
        orig_still_valid = env["_role_life_draft_still_valid"]

        def checked_valid(*args, **kwargs):
            ok = orig_still_valid(*args, **kwargs)
            validity_checks.append(ok)
            return ok

        env["_role_life_draft_still_valid"] = checked_valid

        class Actions:
            async def send(self, **kwargs):
                sent_parts.append(kwargs)
                return {"status": "ok", "retcode": 0, "data": {"message_id": 1}}

        env["_current_qq_actions"] = Actions()
        env["_ProactiveContext"].planner_decision = {
            "action": "reply", "reason": "life", "seconds": None,
            "angle": "", "fact_ids": [], "life_ids": [],
            "avoid": [], "purpose": "chat",
        }
        orig_agen = env["_ProactiveContext"].agen_content
        called = {"n": 0}

        async def racing_agen(model_self, prompt, **kwargs):
            called["n"] += 1
            res = await orig_agen(model_self, prompt, **kwargs)
            if called["n"] == 1:
                note_id = self.harness.store.load(ACCOUNT, ROLE)["life_notes"][0]["id"]
                self.harness.engine.delete_life_note(ACCOUNT, ROLE, note_id)
            return res

        env["_ProactiveContext"].agen_content = racing_agen
        env["_proactive_parse_reply"] = lambda text: (True, "life")
        result = await env["_proactive_run_planner"](False, "123", {})
        self.assertIn(False, validity_checks)
        self.assertFalse(result)
        self.assertEqual(sent_parts, [])

    async def test_n_post_begin_delivery_clear_aborts_second_split_part(self):
        exp_ids = await self.prepare_item()
        exp_id = exp_ids[0]
        env, _prompts = self._planner_env("第一段<split>第二段")
        env.update({
            "role_life": rl,
            "bot_name": "测试角色",
            "get_runtime_others": lambda: self.harness.others,
            "_get_role_life_runtime_config": lambda: self.harness.config,
            "_get_active_account_id": lambda: ACCOUNT,
        })
        extract_helpers(env)
        node = next(node for node in TREE.body if isinstance(node, ast.AsyncFunctionDef)
                    and node.name == "_proactive_run_planner")
        exec(compile(ast.Module(body=[copy.deepcopy(node)], type_ignores=[]),
                     str(ROOT / "main.py"), "exec"), env)
        sent_parts = []

        class Actions:
            async def send(self, **kwargs):
                sent_parts.append(kwargs)
                # 首段发送后管理员清空角色生活，第二段前必须在发送循环中拦截
                st = self.harness.store.load(ACCOUNT, ROLE) if hasattr(self, "harness") else None
                return {"status": "ok", "retcode": 0, "data": {"message_id": len(sent_parts)}}

        harness_ref = self.harness

        async def racing_send(**kwargs):
            sent_parts.append(kwargs)
            if len(sent_parts) == 1:
                st = harness_ref.store.load(ACCOUNT, ROLE)
                harness_ref.engine.clear_role(
                    ACCOUNT, ROLE, request_id="clr-between-parts",
                    expected_revision=st["revision"],
                    expected_epoch=int(st["epoch"]),
                    expected_state_version=int(st["state_version"]),
                )
            return {"status": "ok", "retcode": 0, "data": {"message_id": len(sent_parts)}}

        actions = Actions()
        actions.send = racing_send
        env["_current_qq_actions"] = actions
        env["_ProactiveContext"].planner_decision = {
            "action": "reply", "reason": "life", "seconds": None,
            "angle": "", "fact_ids": [], "life_ids": [exp_id],
            "avoid": [], "purpose": "chat",
        }
        env["_proactive_parse_reply"] = lambda text: (True, "life")
        await env["_proactive_run_planner"](False, "123", {})
        self.assertEqual(len(sent_parts), 1)

    async def test_o_stale_clear_current_qq_actions_does_not_unbind_new_connection(self):
        from bot import proactive_reply
        from bot.message_runtime import CURRENT_REQUEST
        rl.set_engine(self.harness.engine)
        actions_a = object()
        actions_b = object()
        gen_b = object()
        env = {
            "role_life": rl,
            "proactive_reply": proactive_reply,
            "CURRENT_REQUEST": CURRENT_REQUEST,
            "_qq_actions_lock": threading.RLock(),
            "_current_qq_actions": actions_b,
            "_current_bot_self_id": "222222",
            "_proactive_connection_generation": gen_b,
            "_proactive_bootstrap_pending": False,
            "_proactive_bootstrap_ready": gen_b,
            "GROUP_REPLIED_MESSAGES": {},
            "PRIVATE_REPLIED_MESSAGES": {},
            "_proactive_reset_lock": threading.RLock(),
            "_proactive_reset_ids": set(),
            "_register_role_life_runtime": lambda *a, **k: None,
            "INDEX": types.SimpleNamespace(clear_all=lambda: None),
        }
        nodes = [copy.deepcopy(node) for node in TREE.body
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and node.name in {"clear_current_qq_actions", "_role_life_unbind_account"}]
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(ROOT / "main.py"), "exec"), env)

        # 场景 1：HTTP 旧连接 A 出错调用 clear(expected_actions=actions_a) 时不得清掉已上线的新连接 B
        self.harness.engine.bind_account("222222", id(gen_b))
        env["clear_current_qq_actions"](expected_actions=actions_a)
        self.assertIs(env["_current_qq_actions"], actions_b)
        self.assertEqual(self.harness.engine._account, "222222")

        # 场景 2：旧断连清理刚释放在 _qq_actions_lock 后，新连接 B 抢先绑定；旧 unbind 必须被 guard 挡住
        gen_a = object()
        env["_current_qq_actions"] = actions_a
        env["_current_bot_self_id"] = "111111"
        env["_proactive_connection_generation"] = gen_a
        self.harness.engine.bind_account("111111", id(gen_a))

        def takeover(*_a, **_kw):
            new_gen = object()
            env["_current_qq_actions"] = actions_b
            env["_current_bot_self_id"] = "222222"
            env["_proactive_connection_generation"] = new_gen
            self.harness.engine.bind_account("222222", id(new_gen))

        with mock.patch.object(proactive_reply, "cancel_observations", side_effect=takeover):
            env["clear_current_qq_actions"]()
        self.assertIs(env["_current_qq_actions"], actions_b)
        self.assertEqual(self.harness.engine._account, "222222")

    def test_p_proactive_life_material_stays_concise(self):
        snap = {
            "enabled": True, "available": True, "sleeping": False, "recent_woke_up": False,
            "mood_tone_enabled": True, "env_awareness_enabled": False,
            "mood": {"mood": "平和", "energy": "还行"},
            "current_activity": {"title": "散步", "purpose": "透透气"},
        }
        fake_engine = types.SimpleNamespace(
            is_sleeping=lambda *a, **k: False,
            candidates=lambda *a, **k: [
                {"item_id": f"e{i}", "kind": "activity", "date": "2026-03-10",
                 "text": "很长的一段经历" * 80, "dream": False}
                for i in range(3)
            ],
        )
        with mock.patch("bot.role_life.get_engine", return_value=fake_engine):
            with mock.patch.dict(self.harness.env, {"_role_life_public_snapshot": lambda *a, **k: snap}):
                mat = self.harness.env["_role_life_planner_material"](ACCOUNT, ROLE, False, "123")
        self.assertLessEqual(len(mat["state_summary"]), 360)
        self.assertEqual(len(mat["items"]), 3)
        for item in mat["items"]:
            self.assertLessEqual(len(item["text"]), 240)

    def test_q_crud_error_separates_storage_oserror_from_conflict_and_validation(self):
        from bot.role_life_store import RoleLifeConflict
        registered = {}
        env = {
            "_get_active_account_id": lambda: ACCOUNT,
            "_get_role_life_runtime_config": lambda: self.harness.config,
            "_set_role_life_runtime": lambda rt: registered.update(rt),
        }
        node = next(copy.deepcopy(n) for n in TREE.body
                    if isinstance(n, ast.FunctionDef) and n.name == "_register_role_life_runtime")
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(ROOT / "main.py"), "exec"), env)

        class StubEngine:
            def delete_life_note(self, *a, **kw):
                raise self.exc

        stub = StubEngine()
        env["_register_role_life_runtime"](stub)
        for exc, expected_type in (
            (PermissionError("disk readonly"), "storage"),
            (OSError("io failure"), "storage"),
            (RoleLifeConflict("stale"), "conflict"),
            (ValueError("bad input"), "validation"),
        ):
            stub.exc = exc
            res = registered["delete_life_note"](ACCOUNT, ROLE, "n1", "req1", "rev-1", 1, 1)
            self.assertFalse(res["ok"])
            self.assertEqual(res["error_type"], expected_type)


if __name__ == "__main__":
    unittest.main()
