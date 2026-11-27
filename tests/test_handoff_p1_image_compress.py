"""Handoff P1-8 / P1-9 / P2-4 regressions: follow-up image sources, compression robustness, image history & group context."""
import ast
import asyncio
import base64
import json
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot import agent
from bot.group_chat_context import GroupChatContextManager
from bot.image_history import ImageHistoryStore, describe_retained_images, scope_for
from bot.message_runtime import ImageSource

MAIN_PATH = ROOT / "main.py"
MAIN_SOURCE = MAIN_PATH.read_text(encoding="utf-8-sig")
MAIN_TREE = ast.parse(MAIN_SOURCE, filename=str(MAIN_PATH))


def _load_rounds_module():
    rounds_source = (ROOT / "bot" / "rounds.py").read_text(encoding="utf-8-sig")
    rounds_tree = ast.parse(rounds_source)
    mod = types.ModuleType("bot.rounds")
    wanted = {"split_into_rounds", "fix_messages", "is_summary_message", "describe_message", "_text"}
    nodes = [
        node for node in rounds_tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
        or (isinstance(node, ast.FunctionDef) and node.name in wanted)
        or (isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "SUMMARY_PREFIX" for t in node.targets
        ))
    ]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "<offline-rounds>", "exec"), mod.__dict__)
    return mod


ROUNDS = _load_rounds_module()
COMPRESSOR_NODE = next(
    node for node in MAIN_TREE.body
    if isinstance(node, ast.ClassDef) and node.name == "ContextCompressor"
)
COMPRESSOR_CODE = compile(ast.Module(body=[COMPRESSOR_NODE], type_ignores=[]), "<offline-compressor>", "exec")
HISTORY_USER_MSG_NODE = next(
    node for node in MAIN_TREE.body
    if isinstance(node, ast.FunctionDef) and node.name == "_history_user_message"
)


class _DummyKeyManager:
    def __init__(self):
        self.failures = []
        self.successes = []
        self.calls = 0

    def get_attempt_count(self):
        return 1

    def get_next_for_request(self, **kwargs):
        return ("https://example.invalid/v1", "FAKE-KEY-TEST", "summary-model", False, 1, "p", "", {})

    def make_attempt_identity(self, *args):
        return args

    def mark_failure(self, *args, **kwargs):
        self.failures.append((args, kwargs))

    def mark_success(self, *args, **kwargs):
        self.successes.append((args, kwargs))

    def disable_key(self, *args, **kwargs):
        self.failures.append(("disable", args, kwargs))

    def is_default_key(self, *args, **kwargs):
        return True

    def exclude_model_slot(self, *args, **kwargs):
        return None


def _build_compressor(replies, key_manager=None, **kwargs):
    km = key_manager or _DummyKeyManager()
    replies_iter = list(replies)

    class _Completions:
        def create(self, **req):
            km.calls += 1
            item = replies_iter.pop(0)
            if isinstance(item, BaseException):
                raise item
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=item))]
            )

    client = SimpleNamespace(chat=types.SimpleNamespace(completions=_Completions()))
    bot_mod = types.ModuleType("bot")
    bot_mod.__path__ = []
    env = {
        "asyncio": asyncio,
        "time": time,
        "threading": threading,
        "user_cfg": {"compression_keep_recent": 2},
        "split_into_rounds": ROUNDS.split_into_rounds,
        "fix_messages": ROUNDS.fix_messages,
        "describe_message": ROUNDS.describe_message,
        "split_reasoning_output": lambda message: (message.content, ""),
        "SUMMARY_PREFIX": ROUNDS.SUMMARY_PREFIX,
        "SUMMARY_ROLE": "assistant",
        "key_manager": km,
        "acquire_history_lock": AsyncMock(return_value=True),
        "_HISTORY_LOCK_TIMEOUT": 5.0,
        "_memory_author_visible": lambda msg: not (isinstance(msg, dict) and msg.get("_memory_author_visible") is False),
        "clamp_compression_trigger_rounds": lambda trig, mr: trig,
        "_build_proxy_http_client": lambda proxy, timeout=30: None,
    }
    exec(COMPRESSOR_CODE, env)
    comp = env["ContextCompressor"](compression_threshold=kwargs.pop("compression_threshold", 1), **kwargs)
    comp._get_client = lambda *args: client
    return comp, km


def _valid_summary(extra=""):
    return (
        "【人物/偏好和边界】\n用户偏好简短回答。\n"
        "【关键事实/决定】\n确认测试方案。" + extra + "\n"
        "【约定/承诺/未完事项】\n本周完成回归。\n"
        "【话题进展/必要情绪】\n正常推进。\n"
        "【已有可靠图片文字描述】\n无。"
    )


class FollowUpImageSourcesTests(unittest.IsolatedAsyncioTestCase):
    def test_consume_follow_up_detail_preserves_sources_in_order(self):
        state = {}
        agent._ACTIVE_SESSIONS["s-fu"] = state
        try:
            src1 = ImageSource(url="https://img/1.png", self_id="100", chat_id="1", author_id="10", message_id="1", is_group=True)
            src2 = ImageSource(url="https://img/2.png", self_id="100", chat_id="1", author_id="10", message_id="2", is_group=True)
            self.assertTrue(agent.follow_up_session("s-fu", "第一条", image_urls=["https://img/1.png"], image_sources=(src1,)))
            self.assertTrue(agent.follow_up_session("s-fu", "第二条", image_urls=["https://img/1.png", "https://img/2.png"], image_sources=(src1, src2)))
            notice, texts, images = agent._consume_follow_up_detail(state)
            self.assertEqual(len(texts), 2)
            self.assertEqual(list(images), ["https://img/1.png", "https://img/2.png"])
            self.assertEqual(tuple(images.sources), (src1, src2))
            self.assertIn("第一条", notice)
        finally:
            agent._ACTIVE_SESSIONS.pop("s-fu", None)

    async def test_resolve_follow_up_image_blocks_forwards_sources_and_flattens_text(self):
        src = ImageSource(url="https://img/a.png", self_id="100", chat_id="1", author_id="42", message_id="99", is_group=False)
        imgs = agent.FollowUpImages()
        imgs.append("https://img/a.png")
        imgs.sources = (src,)
        seen = []

        async def builder(urls, sources=()):
            seen.append((list(urls), tuple(sources)))
            return [{"type": "text", "text": "[图片转述：一只橘猫]"}]

        ctx = SimpleNamespace(extra={"follow_up_image_builder": builder}, say=lambda *a: None)
        blocks = await agent._resolve_follow_up_image_blocks(ctx, imgs)
        self.assertEqual(seen, [(["https://img/a.png"], (src,))])
        built = agent.build_follow_up_image_blocks(blocks, "新消息")
        self.assertIsInstance(built, str)
        self.assertIn("一只橘猫", built)

    async def test_adapt_agent_retry_trail_flattens_text_only_list_and_skips_empty(self):
        names = {"_adapt_agent_retry_trail"}
        nodes = [n for n in MAIN_TREE.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]

        async def noop_relay(*args, **kwargs):
            return "[图片转述：测试]", 0, 0, 0

        env = {"relay_images_with_multimodal_model": noop_relay}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "<adapt>", "exec"), env)
        adapt = env["_adapt_agent_retry_trail"]

        ctx = SimpleNamespace(extra={})
        trail = [
            {"role": "user", "content": [{"type": "text", "text": "插话文字"}]},
            {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}]},
            {"role": "user", "content": []},
            {"role": "user", "content": [{"type": "text", "text": "   \n\t "}]},
        ]
        out, rt, rp, rc = await adapt(ctx, trail, False)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["content"], "插话文字")
        self.assertIsInstance(out[1]["content"], str)
        self.assertIn("测试", out[1]["content"])

    async def test_build_follow_up_image_blocks_returns_empty_when_all_filtered(self):
        owner = next(n for n in MAIN_TREE.body if isinstance(n, ast.ClassDef) and n.name == "EnhancedLimitedDeepSeekContext")
        agen_node = next(n for n in owner.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "agen_content")
        helper = next(n for n in ast.walk(agen_node) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_build_follow_up_image_blocks")
        env = {
            "self": SimpleNamespace(context_type="private", chat_id="1", history=[]),
            "asyncio": asyncio,
            "_prepare_history_turn_images": AsyncMock(return_value=("", [], [], (0, 0, 0))),
            "_agent_supports_multimodal": {"flag": True},
            "_agent_direct_image_mode": {"flag": False},
            "_fu_relay_usage": [0, 0, 0],
            "agent_ctx": SimpleNamespace(extra={}),
        }
        exec(compile(ast.Module(body=[helper], type_ignores=[]), "<fu-img>", "exec"), env)
        self.assertEqual(await env["_build_follow_up_image_blocks"](["https://img/a.png"], sources=()), [])


class CompressionHardeningTests(unittest.IsolatedAsyncioTestCase):
    def test_multi_round_summary_budget_does_not_halve_previous_summary(self):
        comp, _ = _build_compressor([])
        prev_summary = ROUNDS.SUMMARY_PREFIX + "10轮] 【人物/偏好和边界】\n" + ("A" * 1800)
        messages = [
            {"role": "assistant", "content": prev_summary},
            {"role": "user", "content": "新的问题" * 20},
            {"role": "assistant", "content": "新的回复" * 20},
        ]
        limit, source_chars, msg_count = comp._summary_budget(messages)
        self.assertGreaterEqual(limit, len(prev_summary))
        self.assertEqual(msg_count, 2)
        self.assertGreater(source_chars, len(prev_summary))

    async def test_validation_error_does_not_penalize_key_and_records_cooldown(self):
        comp, km = _build_compressor(["格式不对的纯文本"])
        ctx = SimpleNamespace(
            history=[
                {"role": "user", "content": f"第{i}轮用户输入：" + ("讨论详细上下文内容与安排。" * 8)}
                if i % 2 == 0 else
                {"role": "assistant", "content": f"第{i}轮助手回复：" + ("记录相关要点与待办事项。" * 8)}
                for i in range(12)
            ],
            max_rounds=20,
            get_message_count=lambda: 6,
            _save_memory=lambda: True,
        )
        ok = await comp._compress_locked(ctx, "g-fail-1", force=False)
        self.assertFalse(ok)
        self.assertEqual(km.failures, [])
        stats = comp.get_compression_stats("g-fail-1")
        self.assertGreater(stats["last_failure"], 0)
        calls_before = km.calls
        ok2 = await comp._compress_locked(ctx, "g-fail-1", force=False)
        self.assertFalse(ok2)
        self.assertEqual(km.calls, calls_before)

    async def test_oversized_history_batches_oldest_prefix_and_truncates_single_message(self):
        comp, _ = _build_compressor([_valid_summary()])
        history = []
        for i in range(11):
            history.append({"role": "user", "content": f"R{i}-" + ("X" * 5500)})
            history.append({"role": "assistant", "content": f"A{i}-" + ("Y" * 5500)})
        ctx = SimpleNamespace(
            history=history,
            max_rounds=30,
            get_message_count=lambda: 11,
            _save_memory=lambda: True,
        )
        materials_single = comp._build_summary_materials(history[:2])
        self.assertIn("...[已截断]", materials_single)
        ok = await comp._compress_locked(ctx, "g-batch", force=True)
        self.assertTrue(ok)
        self.assertLess(len(ctx.history), len(history))

    def test_build_summary_materials_filters_invisible_messages(self):
        comp, _ = _build_compressor([])
        materials = comp._build_summary_materials([
            {"role": "user", "content": "可见消息"},
            {"role": "assistant", "content": "隐藏内部消息", "_memory_author_visible": False},
        ])
        self.assertIn("可见消息", materials)
        self.assertNotIn("隐藏内部消息", materials)

    async def test_summary_plus_huge_round_avoids_best_k_one_infinite_loop(self):
        comp, km = _build_compressor([_valid_summary("旧摘要合并超大第二轮")])
        history = [
            {"role": "assistant", "content": ROUNDS.SUMMARY_PREFIX + "2轮] 旧摘要"},
            {"role": "user", "content": "U1-" + ("A" * 17000)},
            {"role": "assistant", "content": "A1-" + ("B" * 17000)},
            {"role": "user", "content": "U2-正常对话"},
            {"role": "assistant", "content": "A2-正常回答"},
            {"role": "user", "content": "R3-提问"},
            {"role": "assistant", "content": "R3-回答"},
            {"role": "user", "content": "最近提问"},
            {"role": "assistant", "content": "最近回答"},
        ]
        ctx = SimpleNamespace(
            history=history,
            max_rounds=20,
            get_message_count=lambda: 5,
            _save_memory=lambda: True,
        )
        ok = await comp._compress_locked(ctx, "g-summary-huge", force=True)
        self.assertTrue(ok)
        self.assertEqual(km.calls, 1)
        self.assertEqual(len(ctx.history), 5)
        self.assertIn("旧摘要合并超大第二轮", ctx.history[0]["content"])

    async def test_single_round_many_large_tools_compresses_via_dynamic_cap(self):
        comp, km = _build_compressor([_valid_summary("工具摘要")])
        history = [{"role": "user", "content": "请批量分析"}]
        for idx in range(10):
            history.append({"role": "assistant", "content": f"tool_call_{idx}", "tool_calls": [{"id": f"c{idx}"}]})
            history.append({"role": "tool", "tool_call_id": f"c{idx}", "content": f"TOOL{idx}-" + ("T" * 4000)})
        history.append({"role": "assistant", "content": "工具汇总完成"})
        history.extend([
            {"role": "user", "content": "old-2"},
            {"role": "assistant", "content": "old-a2"},
            {"role": "user", "content": "old-3"},
            {"role": "assistant", "content": "old-a3"},
            {"role": "user", "content": "recent-1"},
            {"role": "assistant", "content": "recent-a1"},
            {"role": "user", "content": "recent-2"},
            {"role": "assistant", "content": "recent-a2"},
        ])
        ctx = SimpleNamespace(
            history=history,
            max_rounds=20,
            get_message_count=lambda: 5,
            _save_memory=lambda: True,
        )
        ok = await comp._compress_locked(ctx, "g-many-tools", force=True)
        self.assertTrue(ok)
        self.assertEqual(km.calls, 1)
        self.assertIn("工具摘要", ctx.history[0]["content"])

    async def test_unsplittable_oversized_summary_records_failure_cooldown(self):
        comp, km = _build_compressor([])
        huge_summary = ROUNDS.SUMMARY_PREFIX + "2轮] " + ("Z" * 35000)
        history = [
            {"role": "assistant", "content": huge_summary},
            {"role": "user", "content": "U1"},
            {"role": "assistant", "content": "A1"},
            {"role": "user", "content": "U2"},
            {"role": "assistant", "content": "A2"},
            {"role": "user", "content": "R1"},
            {"role": "assistant", "content": "RA1"},
            {"role": "user", "content": "R2"},
            {"role": "assistant", "content": "RA2"},
        ]
        ctx = SimpleNamespace(
            history=history,
            max_rounds=20,
            get_message_count=lambda: 5,
            _save_memory=lambda: True,
        )
        ok1 = await comp._compress_locked(ctx, "g-unsplittable", force=False)
        self.assertFalse(ok1)
        self.assertEqual(km.calls, 0)
        self.assertGreater(comp.get_compression_stats("g-unsplittable")["last_failure"], 0)
        # 冷却期内再次触发直接跳过
        self.assertFalse(await comp._compress_locked(ctx, "g-unsplittable", force=False))

    def test_summary_budget_small_increment_stays_below_shortening_cap(self):
        comp, _ = _build_compressor([])
        prev_summary = ROUNDS.SUMMARY_PREFIX + "10轮] " + ("A" * 1780)
        limit, source_chars, msg_count = comp._summary_budget([
            {"role": "assistant", "content": prev_summary},
            {"role": "user", "content": "新的短提问细节" * 8},
            {"role": "assistant", "content": "新的短回答细节" * 8},
        ])
        self.assertEqual(msg_count, 2)
        self.assertLess(limit, source_chars)
        self.assertGreaterEqual(limit, len(prev_summary))


PNG_PREFIX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Zl1sAAAAASUVORK5CYII="
)


def _data_url(suffix: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(PNG_PREFIX + suffix).decode("ascii")


class ImageHistoryAndGroupContextTests(unittest.TestCase):
    def test_describe_retained_images_skips_recalled_latest_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ImageHistoryStore(Path(tmp))
            ctx = SimpleNamespace(context_type="private", chat_id="1", history=[])
            scope = scope_for(ctx)
            ref_old = store.put_data_url(scope, _data_url(b"old"), author_id="10", message_id="1")
            ref_new = store.put_data_url(scope, _data_url(b"new"), author_id="10", message_id="2")
            self.assertIsNotNone(ref_old)
            self.assertIsNotNone(ref_new)
            ctx.history = [
                {"role": "user", "content": "旧图", "image_refs": [ref_old]},
                {"role": "user", "content": "新图", "image_refs": [ref_new]},
            ]
            relayed = []

            async def fake_relay(c, prompt, urls):
                relayed.append((prompt, list(urls)))
                return "旧图是一只蓝猫", 10, 5, 5

            usage = asyncio.run(
                describe_retained_images(
                    ctx, store, relay=fake_relay,
                    visible=lambda r: r["sha256"] == ref_old["sha256"] if "sha256" in r else True,
                )
            )
            self.assertEqual(usage, (10, 5, 5))
            self.assertEqual(len(relayed), 1)
            self.assertEqual(relayed[0][0], "旧图")
            self.assertIn("旧图是一只蓝猫", store.describe_refs(scope, [ref_old]))

    def test_describe_refs_multi_image_does_not_hit_legacy_single_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ImageHistoryStore(Path(tmp))
            scope = scope_for(SimpleNamespace(context_type="private", chat_id="1"))
            r1 = store.put_data_url(scope, _data_url(b"1"), author_id="10", message_id="1")
            r2 = store.put_data_url(scope, _data_url(b"2"), author_id="10", message_id="2")
            self.assertIsNotNone(r1)
            self.assertIsNotNone(r2)
            member1 = {k: r1[k] for k in ("scope", "sha256", "author_id", "message_id") if k in r1}
            legacy_path = store._path(scope, r1["sha256"], ".json")
            legacy_path.parent.mkdir(parents=True, exist_ok=True)
            legacy_path.write_text(json.dumps({"description": "单图旧缓存", "sources": [member1]}, ensure_ascii=False), encoding="utf-8")
            self.assertEqual(store.describe_refs(scope, [r1, r2]), "")
            self.assertEqual(store.describe_refs(scope, [r1]), "单图旧缓存")

    def test_group_context_rollback_only_restores_matching_record_images(self):
        mgr = GroupChatContextManager(default_max_cnt=50)
        mgr.record("100", "第一条", image_urls=["https://img/1.png"])
        res1 = mgr.reserve_for_inject("100", mgr.record("100", "trigger1"))
        mgr.record("100", "第二条", image_urls=["https://img/2.png"])
        self.assertEqual([x["url"] for x in mgr._latest_images.values()], ["https://img/2.png"])
        self.assertTrue(mgr.rollback(res1))
        res2 = mgr.reserve_for_inject("100", mgr.record("100", "trigger2"))
        self.assertEqual([x["url"] for x in res2.images], ["https://img/2.png"])

    def test_history_user_message_dedups_refs_and_tracks_injected_download_refs_plus_base_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ImageHistoryStore(Path(tmp))
            scope = scope_for(SimpleNamespace(context_type="private", chat_id="123"))
            env = {
                "_text_blocks_for_history": lambda *a, **kw: [],
                "chat_memory": SimpleNamespace(image_history=store),
            }
            exec(compile(ast.Module(body=[HISTORY_USER_MSG_NODE], type_ignores=[]), "<hum>", "exec"), env)
            fn = env["_history_user_message"]
            ctx = SimpleNamespace(context_type="private", chat_id="123", _image_download_refs=[])
            r1 = {"scope": scope, "sha256": "abc"}
            r1_dup = {"scope": scope, "sha256": "abc"}
            r2 = {"scope": scope, "sha256": "def"}
            msg = fn(
                ctx, "hi", [r1, r1_dup, r2],
                injected=[{"role": "user", "content": [{"type": "image_url", "image_url": {"url": _data_url(b"inj")}}]}],
            )
            self.assertGreaterEqual(len(msg["image_refs"]), 3)
            self.assertEqual(len(ctx._image_download_refs), 1)
            self.assertTrue(any(r.get("sha256") for r in ctx._image_download_refs))

            # LimitedDeepSeekContext.agen_content finally 必须清理 _image_download_refs
            basic_cls = next(n for n in MAIN_TREE.body if isinstance(n, ast.ClassDef) and n.name == "LimitedDeepSeekContext")
            base_env = {
                "asyncio": asyncio,
                "threading": threading,
                "re": __import__("re"),
                "time": time,
                "user_cfg": {},
                "_normal_ai_state": lambda: None,
                "key_manager": SimpleNamespace(
                    get_attempt_count=lambda: 1,
                    get_next_for_request=lambda **kw: None,
                    get_next_multimodal_for_request=lambda **kw: None,
                ),
                "chat_memory": SimpleNamespace(_image_refs_lock=threading.Lock(), _all_disk_image_refs=lambda: [], image_history=store),
                "get_multimodal_image_mode": lambda: "relay",
                "_format_exc_text": lambda e: str(e),
                "print": lambda *a, **kw: None,
            }
            exec(compile(ast.Module(body=[basic_cls], type_ignores=[]), "<basic-ctx>", "exec"), base_env)
            bctx = base_env["LimitedDeepSeekContext"]("sys")
            inj_ref = ctx._image_download_refs[0]
            bctx._image_download_refs = [inj_ref]
            bctx._image_pending_refs = [inj_ref]
            with self.assertRaises(Exception):
                asyncio.run(bctx.agen_content("hello"))
            self.assertEqual(bctx._image_download_refs, [])
            self.assertFalse(store._path(scope, inj_ref["sha256"], ".img").exists())

    def test_webui_chatroom_cleanup_with_empty_history_removes_ephemeral_images(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ImageHistoryStore(Path(tmp))
            scope = scope_for(SimpleNamespace(context_type="webui", chat_id="web_1"))
            ref_ephemeral = store.put_data_url(scope, _data_url(b"ephemeral"), author_id="u1", message_id="m1")
            ref_kept = store.put_data_url(scope, _data_url(b"kept"), author_id="u1", message_id="m2")
            store.set_description(scope, [ref_ephemeral], "临时图片")
            store.set_description(scope, [ref_kept], "磁盘保留图片")

            memory = SimpleNamespace(
                _image_refs_lock=threading.Lock(),
                image_history=store,
                _all_disk_image_refs=lambda: [ref_kept],
            )
            deleted = store.cleanup_downloads(memory, [ref_ephemeral], ())
            self.assertGreaterEqual(deleted, 1)
            self.assertFalse(store._path(scope, ref_ephemeral["sha256"], ".img").exists())
            self.assertTrue(store._path(scope, ref_kept["sha256"], ".img").exists())

    def test_relay_images_partial_failure_preserves_attribution_and_data_url_avoids_misattribution(self):
        from bot import message_runtime
        from unittest.mock import patch as _patch

        relay_node = next(
            n for n in MAIN_TREE.body
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "relay_images_with_multimodal_model"
        )
        seen_stored = []

        async def fake_prepare(url):
            if "fail" in url:
                return ""
            if url.startswith("data:"):
                return url
            return _data_url(b"ok")

        class FakeImageHistory:
            def put_data_url(self, scope, data_url, author_id="", message_id="", **extra):
                seen_stored.append((scope, str(author_id), str(message_id)))
                return {"scope": scope, "sha256": f"sha-{author_id}", "author_id": author_id, "message_id": message_id}

            def describe_refs(self, scope, refs, visible=None):
                return ""

            def set_description(self, scope, refs, desc):
                seen_stored.append(("desc", refs, desc))

        class FakeKM:
            def get_attempt_count(self):
                return 1

            def get_next_multimodal_for_request(self, **kw):
                return ("https://v.invalid/v1", "KEY-1", "vision-m", True, 5, "p", "", {})

            def make_attempt_identity(self, *args):
                return args

            def mark_success(self, *args, **kw):
                pass

            def mark_failure(self, *args, **kw):
                pass

        client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(
                    create=lambda **kw: SimpleNamespace(
                        choices=[SimpleNamespace(message=SimpleNamespace(content="猫猫图"))],
                        usage=SimpleNamespace(total_tokens=10, prompt_tokens=5, completion_tokens=5),
                    )
                )
            )
        )
        env = {
            "asyncio": asyncio,
            "key_manager": FakeKM(),
            "chat_memory": SimpleNamespace(image_history=FakeImageHistory()),
            "get_configured_multimodal_model": lambda: "vision-m",
            "convert_image_url_to_data_url": fake_prepare,
            "build_openai_message_content": lambda text, image_urls=None, supports_multimodal=True: text,
            "build_image_relay_prompt": lambda t: t,
            "_relay_client": lambda *a, **kw: client,
            "_memory_author_visible": lambda ref: True,
            "split_reasoning_output": lambda msg: (msg.content, ""),
            "_format_exc_text": lambda e: str(e),
            "log_api_request": lambda *a, **kw: None,
            "log_api_success": lambda *a, **kw: None,
            "log_api_failure": lambda *a, **kw: None,
            "add_token_usage": lambda *a, **kw: None,
            "_VisionRelayConfigError": Exception,
        }
        exec(compile(ast.Module(body=[relay_node], type_ignores=[]), "<relay>", "exec"), env)
        relay_fn = env["relay_images_with_multimodal_model"]

        idx = message_runtime.MessageIndex()
        idx.record("1", True, "1", "1101", "101", text="", media=[{"type": "image", "url": "https://img/fail.png"}])
        idx.record("1", True, "1", "1202", "202", text="", media=[{"type": "image", "url": "https://img/ok.png"}])
        req_state = message_runtime.RequestState(
            self_id="1", is_group=True, chat_id="1",
            decision=message_runtime.ReplyDecision("1", True, "1", source="normal"),
            event=SimpleNamespace(user_id="999", message_id="1999"),
        )
        ctx = SimpleNamespace(session_id="g1", context_type="group", chat_id="1", history=[], _image_download_refs=[])

        # 1) 两张 HTTP 图里 1 张下载失败，第 2 张必须保留原始归属 (author 202, msg 1202)
        with _patch.object(message_runtime, "INDEX", idx), message_runtime.bind_request(req_state):
            asyncio.run(relay_fn(ctx, "hi", ["https://img/fail.png", "https://img/ok.png"]))
        self.assertTrue(any(item[1:] == ("202", "1202") for item in seen_stored if item[0] != "desc"))
        desc_entry = next(item for item in seen_stored if item[0] == "desc")
        self.assertEqual([str(r["author_id"]) for r in desc_entry[1]], ["202"])

        # 2) 纯 data:image/... 无对应已记录来源时不得误归属当前触发人 (999)
        seen_stored.clear()
        with _patch.object(message_runtime, "INDEX", idx), message_runtime.bind_request(req_state):
            asyncio.run(relay_fn(ctx, "hi", [_data_url(b"unknown")]))
        self.assertEqual(seen_stored, [])

    def test_save_compression_stats_uses_state_lock_getter(self):
        save_node = next(
            n for n in MAIN_TREE.body
            if isinstance(n, ast.FunctionDef) and n.name == "save_compression_stats"
        )
        comp, _ = _build_compressor([])
        comp.compression_count["s1"] = 3
        comp.last_compression_time["s1"] = 1234.5
        seen_locked = []
        real_lock = comp._state_lock

        class _TrackedLock:
            def __enter__(self):
                seen_locked.append(True)
                return real_lock.__enter__()

            def __exit__(self, *args):
                return real_lock.__exit__(*args)

        comp._state_lock = _TrackedLock()

        with tempfile.TemporaryDirectory() as tmp:
            stats_path = Path(tmp) / "data" / "compression" / "compression_stats.json"
            env = {
                "os": __import__("os"),
                "json": json,
                "time": time,
                "atomic_write_json": lambda p, d, indent=2: Path(p).write_text(json.dumps(d, indent=indent), encoding="utf-8"),
                "cmc": SimpleNamespace(compressor=comp),
                "BASE_DIR": Path(tmp),
                "print": lambda *a, **kw: None,
            }
            exec(compile(ast.Module(body=[save_node], type_ignores=[]), "<save-comp-stats>", "exec"), env)
            env["save_compression_stats"]()
            self.assertEqual(seen_locked, [True])
            written = json.loads(stats_path.read_text(encoding="utf-8"))
            self.assertEqual(written["sessions"]["s1"], 3)
            self.assertEqual(written["last_compression_times"]["s1"], 1234.5)


if __name__ == "__main__":
    unittest.main()
