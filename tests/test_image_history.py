"""Offline image history: temporary stores/fake download, never import main."""
import ast
import asyncio
import base64
import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from bot.group_chat_context import GroupChatContextManager
from bot.image_history import (ImageHistoryStore, archive_message, clean_content, content_text,
                               prepare_turn_images, refs_from, ref_visible, scope_for)
from bot.memory import ChatMemoryManager
from bot.rounds import extract_agent_trail, fix_messages, keep_recent_rounds
from bot.message_runtime import MessageIndex

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Zl1sAAAAASUVORK5CYII=")


def data(letter):
    return "data:image/png;base64," + base64.b64encode(PNG + letter.encode()).decode()


def block(letter):
    return {"type": "image_url", "image_url": {"url": data(letter)}}


def state(account="1", chat="3", author="8", mid="4"):
    return SimpleNamespace(self_id=account, chat_id=chat, is_group=True, records={},
                           event=SimpleNamespace(user_id=author, message_id=mid))


def context():
    return SimpleNamespace(context_type="group", chat_id="3", history=[])


class EavesdropImageTests(unittest.TestCase):
    def setUp(self):
        self.manager = GroupChatContextManager()

    def record(self, letter, **kwargs):
        return self.manager.record("3", letter, author_id="8", self_id="1", message_id="1",
                                   image_urls=[f"https://example.invalid/{letter}.png"], **kwargs)

    def test_a_b_c_only_c_without_download_and_text_unchanged(self):
        for letter in "ABC":
            self.record(letter)
        self.assertEqual(len(self.manager._latest_images), 1)
        self.assertEqual(self.manager._latest_images["3"]["url"], "https://example.invalid/C.png")
        trigger = self.manager.record("3", "trigger")
        reserved = self.manager.reserve_for_inject("3", trigger)
        self.assertEqual(list(reserved.records), ["A", "B", "C"])
        self.assertEqual([item["url"] for item in reserved.images], ["https://example.invalid/C.png"])
        self.assertNotIn("data:image", str(reserved))
        self.assertTrue(self.manager.commit(reserved))

    def test_rollback_restores_media_without_overwriting_new_f(self):
        self.record("C")
        reservation = self.manager.reserve_for_inject("3", self.manager.record("3", "trigger"))
        self.record("F")
        self.assertTrue(self.manager.rollback(reservation))
        next_reservation = self.manager.reserve_for_inject("3", self.manager.record("3", "next"))
        self.assertEqual(next_reservation.images[0]["url"], "https://example.invalid/F.png")
        self.assertEqual(list(next_reservation.records), ["C", "F"])

    def test_rollback_media_is_usable_again_and_clear_invalidates(self):
        self.record("C")
        reservation = self.manager.reserve_for_inject("3", self.manager.record("3", "trigger"))
        self.assertTrue(self.manager.rollback(reservation))
        again = self.manager.reserve_for_inject("3", self.manager.record("3", "next"))
        self.assertEqual(again.images, reservation.images)
        self.manager.clear("3")
        self.assertFalse(self.manager.rollback(again))
        self.assertEqual(self.manager._latest_images, {})

    def test_blacklist_and_trim_do_not_revive_previous_image(self):
        self.record("B")
        self.record("C")
        reservation = self.manager.reserve_for_inject("3", self.manager.record("3", "trigger"),
                                                      visible=lambda row: getattr(row, "author_id", None) != "8")
        self.assertEqual(reservation.images, ())
        self.assertEqual(reservation.records, ())
        self.record("F", max_cnt=1)
        self.manager.record("3", "text", max_cnt=1)
        self.assertEqual(self.manager._latest_images, {})


class ImageStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.memory = ChatMemoryManager(self.temporary.name)
        self.store = self.memory.image_history
        self.ctx = context()
        self.state = state()
        self.scope = scope_for(self.ctx, self.state)

    def archive(self, letter):
        return archive_message(self.store, self.scope, {"role": "user", "content": [
            {"type": "text", "text": letter}, block(letter)]}, self_id="1", is_group=True, chat_id="3")

    def test_hash_dedup_and_reject_untrusted_paths_and_other_accounts(self):
        first = self.archive("A")
        duplicate = self.archive("A")
        self.assertEqual(refs_from([first]), refs_from([duplicate]))
        self.assertEqual(len(list(Path(self.temporary.name).rglob("*.img"))), 1)
        ref = first["image_refs"][0]
        self.assertEqual(self.store.read_data_url(self.scope, {**ref, "path": "C:/Windows/win.ini"}), data("A"))
        self.assertEqual(self.store.read_data_url(self.scope, {**ref, "sha256": "../win.ini"}), "")
        self.assertEqual(self.store.read_data_url("0" * 64, ref), "")
        other_scope = scope_for(context(), state(account="2"))
        self.assertNotEqual(self.scope, other_scope)
        self.assertEqual(self.store.rebuild(other_scope, [first], supports_multimodal=True)[0]["content"], "A\n[图片]")
        self.assertIsNone(self.store.put_data_url(self.scope, "file:///C:/Windows/win.ini"))
        self.assertIsNone(self.store.put_data_url(self.scope, "data:image/png;base64," + base64.b64encode(b"not image").decode()))

    def test_cross_round_native_blocks_and_no_full_history_replay(self):
        a, b = self.archive("A"), self.archive("B")
        history = [a, {"role": "assistant", "content": "A reply"}, b,
                   {"role": "assistant", "content": "B reply"}]
        before = copy.deepcopy(history)
        rebuilt = self.store.rebuild(self.scope, history, supports_multimodal=True)
        self.assertNotIn(data("A"), str(rebuilt))
        self.assertIn(data("B"), str(rebuilt))
        self.assertIsInstance(rebuilt[2]["content"], list)
        self.assertNotIn("image_refs", str(rebuilt))
        self.assertEqual(history, before)
        self.assertEqual(fix_messages(history), before)
        self.assertEqual(keep_recent_rounds(history, 1)[0]["image_refs"], b["image_refs"])

    def test_compression_prune_keeps_retained_shared_and_other_disk_refs(self):
        a, b, c = [self.archive(letter) for letter in "ABC"]
        self.assertTrue(self.memory.save_group_memory(3, [a, b, c]))
        # Failed memory write cannot make prune delete the previous durable refs.
        with patch("bot.memory.atomic_write_json", side_effect=OSError("disk failed")):
            self.assertFalse(self.memory.save_group_memory(3, [c]))
        self.assertEqual(self.memory.prune_history_images(self.scope, [c]), 0)
        self.assertTrue(self.store.read_data_url(self.scope, a["image_refs"][0]))
        # A second owner protects A; latest complete retained round protects C.
        self.assertTrue(self.memory.save_private_memory(9, [a]))
        self.assertTrue(self.memory.save_group_memory(3, [c]))
        self.assertEqual(self.memory.prune_history_images(self.scope, [c]), 1)
        self.assertEqual(self.store.read_data_url(self.scope, b["image_refs"][0]), "")
        self.assertTrue(self.store.read_data_url(self.scope, a["image_refs"][0]))
        self.assertTrue(self.store.read_data_url(self.scope, c["image_refs"][0]))
        self.assertTrue(self.memory.delete_private_memory(9))
        self.assertEqual(self.store.read_data_url(self.scope, a["image_refs"][0]), "")
        self.assertTrue(self.store.read_data_url(self.scope, c["image_refs"][0]))

    def test_unsaved_inflight_reference_protects_bytes_until_release(self):
        old, pending = self.archive("A"), self.archive("F")
        self.store.pin("request", pending["image_refs"])
        self.assertEqual(self.store.prune(self.scope, old["image_refs"]), 0)
        self.assertTrue(self.store.read_data_url(self.scope, pending["image_refs"][0]))
        self.store.release("request")
        self.assertEqual(self.store.prune(self.scope, old["image_refs"]), 1)
        self.assertTrue(self.store.read_data_url(self.scope, old["image_refs"][0]))

    def test_restart_json_has_sidecars_no_base64_and_reset_cleans_bytes(self):
        message = self.archive("C")
        self.store.set_description(self.scope, message["image_refs"], "缓存描述")
        self.assertTrue(self.memory.save_group_memory(3, [message], 42))
        disk = (Path(self.temporary.name) / "group_3.json").read_text(encoding="utf-8")
        self.assertNotIn("base64", disk)
        restarted = ChatMemoryManager(self.temporary.name)
        history, tokens, _ = restarted.load_group_memory(3)
        self.assertEqual(tokens, 42)
        self.assertEqual(history[0]["image_refs"], message["image_refs"])
        scope = scope_for(SimpleNamespace(context_type="group", chat_id="3", history=history))
        self.assertEqual(scope, self.scope)
        rebuilt = restarted.image_history.rebuild(scope, history, supports_multimodal=True)
        self.assertIn(data("C"), str(rebuilt))
        self.assertIn("缓存描述", str(restarted.image_history.rebuild(scope, history)))
        self.assertTrue(restarted.delete_group_memory(3))
        self.assertEqual(list(Path(self.temporary.name).rglob("*.img")), [])

    def test_recall_blacklist_session_scope_are_applied_to_refs(self):
        index = MessageIndex()
        index.record("1", True, "3", "4", "8")
        ref = self.store.put_data_url(self.scope, data("C"), self_id="1", chat_id="3",
                                      is_group=True, message_id="4", author_id="8")
        self.assertTrue(ref_visible(ref, self.state, index=index))
        self.assertFalse(ref_visible(ref, state(account="2"), index=index))
        self.assertFalse(ref_visible(ref, self.state, visible=lambda item: item["author_id"] != "8"))
        index.tombstone("1", True, "3", "4")
        self.assertFalse(ref_visible(ref, self.state, index=index))
        visible = lambda item: ref_visible(item, self.state, index=index)
        rebuilt = self.store.rebuild(self.scope, [{"role": "user", "content": "文字", "image_refs": [ref]}],
                                     supports_multimodal=True, visible=visible)
        self.assertNotIn("image_url", str(rebuilt))

    def test_clean_blocks_and_retry_trail_keep_sidecar_without_bytes(self):
        original = {"role": "user", "content": [{"type": "text", "text": "Follow-Up"}, block("F")]}
        message = archive_message(self.store, self.scope, original)
        trail = fix_messages(extract_agent_trail([message], request_user_messages=[message]))
        self.assertEqual(trail[0]["image_refs"], message["image_refs"])
        self.assertIsInstance(trail[0]["content"], list)
        self.assertNotIn("base64", str(trail))
        self.assertIn(data("F"), str(self.store.rebuild(self.scope, trail, supports_multimodal=True)))
        self.assertIn(data("F"), str(original))
        self.assertIsInstance(clean_content(original["content"]), list)


class PrepareTurnTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = ImageHistoryStore(self.temporary.name)
        self.context = context()
        self.state = state()
        self.download = AsyncMock(side_effect=lambda url: data(Path(url).stem))
        self.relay = AsyncMock(return_value=("C、D、E 图片描述", 9, 6, 3))

    async def prepare(self, message, **kwargs):
        return await prepare_turn_images(self.context, message, self.store, download=self.download,
                                         relay=self.relay, state=self.state, **kwargs)

    async def test_trigger_d_e_saves_c_d_e_new_f_does_not_delete_old(self):
        manager = GroupChatContextManager()
        for letter in "ABC":
            manager.record("3", letter, image_urls=[f"https://example.invalid/{letter}.png"],
                           author_id="8", self_id="1", message_id="1")
        reservation = manager.reserve_for_inject("3", manager.record("3", "trigger"))
        message = {"text": "触发", "image_urls": ["https://example.invalid/D.png", "https://example.invalid/E.png"],
                   "context_images": list(reservation.images)}
        text, urls, refs, usage = await self.prepare(message, supports_multimodal=True)
        self.assertEqual(set(urls), {data(letter) for letter in "CDE"})
        self.assertEqual(len(refs), 3)
        self.assertEqual(self.download.await_count, 3)
        manager.commit(reservation)
        manager.record("3", "F", image_urls=["https://example.invalid/F.png"])
        self.assertEqual(self.download.await_count, 3)
        for ref in refs:
            self.assertTrue(self.store.read_data_url(ref["scope"], ref))
        self.assertEqual(usage, (0, 0, 0))

    async def test_model_retry_reuses_download_and_cached_description(self):
        message = {"text": "看图", "image_urls": ["https://example.invalid/D.png", "https://example.invalid/E.png"]}
        _, urls, refs, _ = await self.prepare(message, supports_multimodal=True)
        text, text_urls, again_refs, usage = await self.prepare(message, supports_multimodal=False)
        self.assertEqual(text_urls, [])
        self.assertIn("C、D、E 图片描述", text)
        self.assertEqual(usage, (9, 6, 3))
        self.assertEqual(refs, again_refs)
        self.assertEqual(self.download.await_count, 2)
        self.assertEqual(self.relay.await_count, 1)
        _, _, _, usage = await self.prepare(message, supports_multimodal=False)
        self.assertEqual(usage, (0, 0, 0))
        self.assertEqual(self.relay.await_count, 1)
        # A fresh request uses the persisted hash description, not another relay.
        fresh = {"text": "再看", "image_urls": message["image_urls"]}
        text, _, _, _ = await self.prepare(fresh, supports_multimodal=False)
        self.assertIn("C、D、E 图片描述", text)
        self.assertEqual(self.relay.await_count, 1)

    async def test_download_and_no_vision_failure_keep_text_and_no_external_model_url(self):
        self.download.side_effect = OSError("fake failure")
        message = {"text": "保留正文", "image_urls": ["https://example.invalid/D.png"]}
        text, urls, refs, _ = await self.prepare(message, supports_multimodal=True)
        self.assertIn("保留正文", text)
        self.assertIn("[图片]", text)
        self.assertEqual((urls, refs), ([], []))
        self.assertEqual(list(Path(self.temporary.name).rglob("*.img")), [])
        self.download.side_effect = lambda url: data("D")
        self.relay.side_effect = RuntimeError("no vision model")
        fresh = {"text": "原文字", "image_urls": message["image_urls"]}
        text, urls, refs, _ = await self.prepare(fresh, supports_multimodal=False)
        self.assertIn("[图片]", text)
        self.assertEqual(urls, [])
        self.assertEqual(len(refs), 1)
        self.assertNotIn("data:", text)
        # Failure remains bounded across retries; multimodal can still recover.
        self.assertEqual(self.relay.await_count, 1)
        await self.prepare(fresh, supports_multimodal=False)
        self.assertEqual(self.relay.await_count, 1)
        _, urls, _, _ = await self.prepare(fresh, supports_multimodal=True)
        self.assertEqual(urls, [data("D")])


class MainSidecarIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """Execute only selected real methods/helper nodes, no startup side effects."""
    def setUp(self):
        tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
        base = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "LimitedDeepSeekContext")
        enhanced = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "EnhancedLimitedDeepSeekContext")
        methods = [copy.deepcopy(node) for owner in (base, enhanced) for node in owner.body
                   if isinstance(node, ast.FunctionDef) and node.name in {
                       "_build_messages", "_clean_content", "_prune_history_images", "_save_memory"}]
        for method in methods:
            method.decorator_list = []
        helpers = [copy.deepcopy(node) for node in tree.body
                   if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in {
                       "_history_user_message", "_prepare_history_turn_images"}]
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.memory = ChatMemoryManager(self.temp.name)
        self.env = {"chat_memory": self.memory, "fix_messages": fix_messages,
                    "build_llm_system_prompt": lambda value: value, "_memory_author_visible": lambda ref: True,
                    "convert_image_url_to_data_url": AsyncMock(side_effect=lambda url: data(Path(url).stem)),
                    "relay_images_with_multimodal_model": AsyncMock(return_value=("描述", 3, 2, 1))}
        exec(compile(ast.Module(body=methods + helpers, type_ignores=[]), "offline-sidecars", "exec"), self.env)
        self.ctx = context()
        self.ctx.system_prompt = "system"
        self.ctx.total_tokens = 0
        self.ctx._prune_history_images = lambda: self.env["_prune_history_images"](self.ctx)

    async def test_actual_prepare_persist_build_save_prune_restart_flow(self):
        text, urls, refs, _ = await self.env["_prepare_history_turn_images"](
            self.ctx, {"text": "D/E", "image_urls": ["https://example.invalid/D.png", "https://example.invalid/E.png"]}, True)
        user = self.env["_history_user_message"](self.ctx, text, refs)
        self.ctx.history = [user, {"role": "assistant", "content": "answer"}]
        self.assertTrue(self.env["_save_memory"](self.ctx))
        rebuilt = self.env["_build_messages"](self.ctx, supports_multimodal=True)
        self.assertEqual(sum(block.get("type") == "image_url" for block in rebuilt[1]["content"]), 2)
        self.assertNotIn("image_refs", str(rebuilt))
        before = copy.deepcopy(self.ctx.history)
        self.assertEqual(self.env["_build_messages"](self.ctx)[1]["content"], "D/E\n[图片]")
        self.assertEqual(self.ctx.history, before)
        # Switching a later text-only request describes retained images once.
        _, _, _, usage = await self.env["_prepare_history_turn_images"](self.ctx, {"text": "下一轮"}, False)
        self.assertEqual(usage, (3, 2, 1))
        self.assertIn("描述", str(self.env["_build_messages"](self.ctx)))
        await self.env["_prepare_history_turn_images"](self.ctx, {"text": "再一轮"}, False)
        self.assertEqual(self.env["relay_images_with_multimodal_model"].await_count, 1)
        # Failed save and compressor rollback retain every old file.
        self.ctx.history = [{"role": "assistant", "content": "compressed"}]
        with patch("bot.memory.atomic_write_json", side_effect=OSError("disk full")):
            self.assertFalse(self.env["_save_memory"](self.ctx))
        self.assertEqual(self.ctx._prune_history_images(), 0)
        self.assertEqual(len(list(Path(self.temp.name).rglob("*.img"))), 2)
        self.assertTrue(self.env["_save_memory"](self.ctx))
        self.assertEqual(list(Path(self.temp.name).rglob("*.img")), [])
        self.ctx._discarded = True
        self.assertFalse(self.env["_save_memory"](self.ctx))

    async def test_actual_follow_up_sidecar_survives_text_adaptation(self):
        user = self.env["_history_user_message"](self.ctx, "期间补充", injected=[
            {"role": "user", "content": [{"type": "text", "text": "Follow-Up"}, block("F")]}])
        self.assertEqual(len(user["image_refs"]), 1)
        self.assertNotIn("base64", str(user))
        self.ctx.history = [user]
        self.assertIn(data("F"), str(self.env["_build_messages"](self.ctx, supports_multimodal=True)))


class SafeDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_helper_blocks_private_redirect_invalid_body_and_caps(self):
        import aiohttp
        import ipaddress
        import urllib.parse
        tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
        node = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
                    and node.name == "convert_image_url_to_data_url")
        responses = []
        calls = []

        class Response:
            def __init__(self, status=200, body=PNG, headers=None, length=None):
                self.status, self.body = status, body
                self.headers = headers or {}
                self.content_length = len(body) if length is None else length
                self.content = self
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                return False
            async def iter_chunked(self, size):
                yield self.body

        class Session:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                return False
            def get(self, url, **kwargs):
                calls.append((url, kwargs))
                return responses.pop(0)

        env = {"aiohttp": aiohttp, "asyncio": asyncio, "ipaddress": ipaddress,
               "urllib": __import__("urllib"), "base64": base64}
        exec(compile(ast.Module(body=[node], type_ignores=[]), "offline-download", "exec"), env)
        download = env["convert_image_url_to_data_url"]
        with patch.object(aiohttp, "ClientSession", Session), patch.object(aiohttp, "TCPConnector", return_value=object()):
            self.assertEqual(await download("http://127.0.0.1/private.png"), "")
            self.assertEqual(calls, [])
            responses.append(Response(302, headers={"Location": "http://169.254.169.254/metadata"}))
            self.assertEqual(await download("https://public.invalid/a.png"), "")
            self.assertEqual(len(calls), 1)
            self.assertFalse(calls[0][1]["allow_redirects"])
            responses.append(Response(body=b"not image"))
            self.assertEqual(await download("https://public.invalid/a.png"), "")
            responses.append(Response(length=8 * 1024 * 1024 + 1))
            self.assertEqual(await download("https://public.invalid/a.png"), "")
            responses.append(Response())
            self.assertEqual(await download("https://public.invalid/a.png"), "data:image/png;base64," + base64.b64encode(PNG).decode())
            self.assertEqual(await download("data:image/png;base64,bm90IGltYWdl"), "")


if __name__ == "__main__":
    unittest.main()
