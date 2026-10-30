"""真实 main 社交接线离线测试；仅提取函数，不启动入口。"""
import ast
import asyncio
import json
from pathlib import Path
import threading
import types
import unittest
from unittest.mock import patch

from bot import agent
from bot.message_runtime import MessageIndex, ReplyDecision, RequestState
from bot.social_tools import register_social_tools

TREE = ast.parse((Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8"))


def load(names, env):
    nodes = [node for node in TREE.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "main-social-integration", "exec"), env)
    return env


class SocialDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.index = MessageIndex()
        self.key = ("999", True, "123")
        self.index.record(*self.key, "1", "222", text="别人消息", timestamp=10)
        self.index.record(*self.key, "2", "999", text="自己消息", timestamp=20,
                          is_bot=True, sent_at=__import__("time").time() - 20)
        self.addCleanup(patch.stopall)
        patch("bot.message_runtime.INDEX", self.index).start()
        self.enabled = True
        self.deleted = []

        async def delete(mid):
            self.deleted.append(mid)
            return {"status": "ok", "retcode": 0}

        self.blacklist = set()
        self.env = load({"is_user_blacklisted", "_hard_blacklisted_author",
                         "_receipt_succeeded", "_make_social_context", "_generate_proactive_content"}, {
            "get_all_blacklist": lambda: self.blacklist,
            "_proactive_target_allowed": lambda *args: True,
            "normalize_bool_config": lambda value, default=False: bool(value),
            "get_runtime_setting": lambda path, default=None: self.enabled if path.endswith("enabled") else default,
            "_ai_reply_allowed": lambda request, **kwargs: request.decision.check(True, ["222"], 0, **kwargs),
            "asyncio": asyncio, "time": __import__("time"),
            "is_trace_recording": lambda: False,
        })
        self.request = RequestState(*self.key, ReplyDecision(*self.key, source="proactive"),
                                    actions=types.SimpleNamespace(del_message=delete))
        self.social = self.env["_make_social_context"](self.request)
        self.request.social = self.social

    async def test_real_context_dynamic_hard_author_filter_and_selection(self):
        self.request.decision.source = "normal"
        self.env["_ai_reply_allowed"] = lambda *args, **kwargs: True
        self.assertFalse((await self.social.select_reply("1")).startswith("error:"))
        self.blacklist.add("222,备注")
        self.assertEqual([m["id"] for m in json.loads(self.social.list_visible_messages())], ["2"])
        self.assertTrue((await self.social.select_reply("1")).startswith("error:"))
        self.assertIsNone(self.social.consume_reply())
        rebuilt = self.env["_make_social_context"](self.request)
        self.assertNotIn("1", rebuilt._snapshot)
        self.assertIsNotNone(self.index.resolve(*self.key, "1"))
        self.blacklist.add("999,当前账号")
        self.assertEqual([m["id"] for m in json.loads(rebuilt.list_visible_messages())], ["2"])

    async def test_group_other_reference_weak_denial_is_sticky(self):
        result = await self.social.select_reply("1")
        self.assertTrue(result.startswith("error:"))
        self.assertTrue(self.request.decision.denied)
        self.assertTrue((await self.social.select_reply("2")).startswith("error:"))
        self.assertIsNone(self.social.consume_reply())

    async def test_own_reference_and_no_reference_do_not_roll_weak_author(self):
        self.assertIsNone(self.social.consume_reply())
        result = await self.social.select_reply("2")
        self.assertFalse(result.startswith("error:"))
        self.assertEqual(self.social.consume_reply(), "2")
        self.assertIsNone(self.social.consume_reply())
        self.assertIsNone(self.request.decision.sample)

    async def test_recall_checks_author_live_switch_and_remote_receipt(self):
        self.assertTrue((await self.social.recall_own("1")).startswith("error:"))
        self.assertFalse(self.deleted)
        self.assertEqual(await self.social.recall_own("2"), "已撤回自己的消息。")
        self.assertEqual(self.deleted, [2])
        self.assertTrue(self.index.resolve(*self.key, "2")["recalled"])

    async def test_switch_off_recall_rejected_even_after_snapshot(self):
        self.enabled = False
        self.assertTrue((await self.social.recall_own("2")).startswith("error:"))
        self.assertFalse(self.deleted)

    async def test_aged_message_rejected_by_live_index(self):
        self.index.record(*self.key, "3", "999", is_bot=True,
                          sent_at=__import__("time").time() - 121, text="已过期")
        self.assertTrue((await self.social.recall_own("3")).startswith("error:"))
        self.assertFalse(self.deleted)

    async def test_real_sdk_delete_branch_fetches_success_receipt(self):
        from bot.message_runtime import CURRENT_REQUEST, INDEX, bind_request
        from collections import OrderedDict
        called = []

        async def delete_msg(**kwargs):
            called.append(kwargs)
            return "echo_test"

        async def fetch(echo):
            self.assertEqual(echo, "echo_test")
            return {"status": "ok", "retcode": 0}

        cls = next(node for node in TREE.body if isinstance(node, ast.ClassDef) and node.name == "LoggedActions")
        env = {"asyncio": asyncio, "threading": threading, "OrderedDict": OrderedDict,
               "CURRENT_REQUEST": CURRENT_REQUEST, "INDEX": INDEX, "_agent_fetch_ret": fetch,
               "_receipt_succeeded": lambda ret: ret.get("status") == "ok"}
        exec(compile(ast.Module(body=[cls], type_ignores=[]), "logged-actions", "exec"), env)
        raw = types.SimpleNamespace(custom=types.SimpleNamespace(delete_msg=delete_msg))
        actions = env["LoggedActions"](raw, self_id="999")
        with bind_request(self.request):
            ret = await actions.delete_with_receipt("2")
        self.assertEqual(ret["status"], "ok")
        self.assertEqual(called, [{"message_id": 2}])
        self.assertTrue(INDEX.resolve(*self.key, "2")["recalled"])

    async def test_background_writer_only_gets_social_tools(self):
        registry = agent.ToolRegistry()
        register_social_tools(registry)
        patch.object(agent, "REGISTRY", registry).start()
        model_calls = []

        class Manager:
            def get_attempt_count(self):
                return 1
            def get_next_for_request(self, **kwargs):
                return ("https://offline.invalid/v1", "fake", "chat", False, 10, "p/chat", "", {})
            def make_attempt_identity(self, *args):
                return args
            def mark_success(self, *args, **kwargs):
                pass
            def mark_failure(self, *args, **kwargs):
                pass

        class Writer:
            def _build_messages(self, text):
                return [{"role": "system", "content": "人格"}, {"role": "user", "content": text}]
            async def _request_completion(inner, url, key, timeout, proxy, kwargs):
                model_calls.append(kwargs)
                if len(model_calls) == 1:
                    call = types.SimpleNamespace(id="q1", type="function", function=types.SimpleNamespace(name="select_reply", arguments='{"message_id":"2"}'))
                    msg = types.SimpleNamespace(content="", tool_calls=[call])
                else:
                    msg = types.SimpleNamespace(content="主动正文", tool_calls=[])
                return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)], usage=types.SimpleNamespace(total_tokens=3,prompt_tokens=2,completion_tokens=1))

        self.env.update(key_manager=Manager(), split_reasoning_output=lambda message: (message.content, ""))
        text, total = await self.env["_generate_proactive_content"](Writer(), "继续聊天", self.request)
        self.assertEqual(text, "主动正文")
        self.assertEqual(total, 6)
        names = {item["function"]["name"] for item in model_calls[0]["tools"]}
        self.assertEqual(names, {"list_visible_messages", "select_reply", "wait_for_messages", "finish_observation", "recall_own_message"})
        self.assertNotIn("execute_shell", names)
        self.assertNotIn("send_message", names)
        self.assertEqual(self.social.consume_reply(), "2")


if __name__ == "__main__":
    unittest.main()
