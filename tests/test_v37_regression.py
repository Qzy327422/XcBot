"""3.7 日志在 4.0 的行为回归：假回调、临时会话，不启动机器人。"""
import ast
import copy
import errno
import json
import pathlib
import re
import socket
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

import webui
from bot import agent

ROOT = pathlib.Path(__file__).resolve().parents[1]


class FeatureDefaultTests(unittest.TestCase):
    def test_group_context_default_matches_template_and_proactive_remains_off(self):
        from webui_core.features import DEFAULT_FEATURE_SWITCHES
        cfg = json.loads((ROOT / "config.default.json").read_text(encoding="utf-8"))
        self.assertTrue(DEFAULT_FEATURE_SWITCHES["group_chat_context"])
        self.assertTrue(cfg["FeatureSwitches"]["group_chat_context"])
        self.assertFalse(cfg["ProactiveReply"]["group"]["enabled"])
        self.assertFalse(cfg["ProactiveReply"]["private"]["enabled"])

    def test_runtime_fallback_on_preserves_explicit_off(self):
        tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                    and n.name == "is_group_chat_context_enabled")
        for configured, expected in ((None, True), (False, False), (True, True)):
            env = {"is_feature_enabled": lambda key, default: default if configured is None else configured}
            exec(compile(ast.Module(body=[node], type_ignores=[]), "context-default", "exec"), env)
            self.assertEqual(env["is_group_chat_context_enabled"](), expected)


class ChatroomRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.session_path = pathlib.Path(self.temp.name) / "session.json"
        self.addCleanup(patch.stopall)
        patch.object(webui, "_chatroom_path", lambda session_id: self.session_path).start()
        patch.object(webui, "_chatroom_others", lambda: {"context_max_messages": 60}).start()
        patch.object(webui, "_chatroom_trim_agent_history", lambda obj: None).start()
        patch.object(webui, "_chatroom_split_callback", lambda text: text.split("<split>")).start()
        patch.object(webui, "_chatroom_filter_callback", None).start()
        self.obj = {"id": "s1", "model": "provider/model", "persona": "p2", "messages": [
            {"role": "user", "content": "请帮忙", "request_id": "r1", "ts": 1}
        ]}
        webui._chatroom_save(self.obj)

    def test_error_is_persisted_user_kept_and_excluded_from_model(self):
        result = webui._chatroom_append_error(self.obj, "模拟接口失败")
        self.assertEqual(result["messages"][0]["content"], "请帮忙")
        loaded = webui._chatroom_load("s1")
        self.assertTrue(loaded["messages"][-1]["error"])
        self.assertEqual(loaded["messages"][-1]["request_id"], "r1")
        loaded["messages"].append({"role": "user", "content": "重新提问"})
        with patch.object(webui, "_chatroom_endpoint_for_model", return_value={"supports_multimodal": False}), \
             patch.object(webui, "_chatroom_system_prompt", return_value="人格"):
            messages = webui._chatroom_build_llm_messages(loaded, "provider/model")
        text = json.dumps(messages, ensure_ascii=False)
        self.assertNotIn("模拟接口失败", text)
        self.assertIn("重新提问", text)

    def test_send_failure_path_keeps_request_and_error_after_reload(self):
        with patch.object(webui, "_chatroom_prepare_user_message", return_value=(copy.deepcopy(self.obj), "请帮忙")), \
             patch.object(webui, "_chatroom_handle_command", return_value=None), \
             patch.object(webui, "get_webui_config", return_value={"access_token": "offline"}), \
             patch.object(webui, "_chatroom_agent_callback", side_effect=RuntimeError("模拟生成失败")):
            with self.assertRaisesRegex(RuntimeError, "模拟生成失败"):
                webui._chatroom_send("s1", "provider/model", "请帮忙")
        loaded = webui._chatroom_load("s1")
        self.assertEqual([m["role"] for m in loaded["messages"]], ["user", "assistant"])
        self.assertTrue(loaded["messages"][1]["error"])

    def test_split_bubbles_each_keep_reasoning_outside_content(self):
        result = webui._chatroom_append_assistant(self.obj, "第一段<split>第二段", reasoning="独立思考")
        replies = [m for m in result["messages"] if m["role"] == "assistant"]
        self.assertEqual([m["content"] for m in replies], ["第一段", "第二段"])
        self.assertEqual([m["reasoning"] for m in replies], ["独立思考", "独立思考"])
        with patch.object(webui, "_chatroom_endpoint_for_model", return_value={"supports_multimodal": False}), \
             patch.object(webui, "_chatroom_system_prompt", return_value="人格"):
            context = webui._chatroom_build_llm_messages(result, "provider/model")
        self.assertNotIn("独立思考", json.dumps(context, ensure_ascii=False))

    def test_actual_persona_resolution_override_fallback_and_empty(self):
        cfg = {"personality_presets": [{"id": "p1", "prompt": "默认人格"}, {"id": "p2", "prompt": "会话人格"}, {"id": "empty", "prompt": ""}],
               "active_personality_preset": "p1", "private_personality_preset": "", "personality_prompt": "旧默认"}
        with patch.object(webui, "_chatroom_others", return_value=cfg):
            self.assertEqual(webui._chatroom_system_prompt("p2"), "会话人格")
            self.assertEqual(webui._chatroom_system_prompt("removed"), "默认人格")
            with self.assertRaises(ValueError):
                webui._chatroom_system_prompt("empty")

    def test_persona_selection_reaches_prompt_builder(self):
        selected = []
        with patch.object(webui, "_chatroom_endpoint_for_model", return_value={}), \
             patch.object(webui, "_chatroom_system_prompt", side_effect=lambda preset: selected.append(preset) or "会话人格"):
            result = webui._chatroom_build_llm_messages(self.obj, "provider/model")
        self.assertEqual(selected, ["p2"])
        self.assertEqual(result[0]["content"], "会话人格")


class PortRegressionTests(unittest.TestCase):
    def test_real_busy_socket_falls_forward_without_starting_http_thread(self):
        busy = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(busy.close)
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            busy.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        port = busy.getsockname()[1]
        if port > 65515:
            self.skipTest("随机端口过于接近上限")
        with patch.object(webui, "_actual_port", None):
            server = webui._bind_webui_server("127.0.0.1", port, "")
            self.assertIsNotNone(server)
            try:
                self.assertGreater(server.server_address[1], port)
                self.assertEqual(webui.get_actual_webui_port(), server.server_address[1])
            finally:
                server.server_close()

    def test_busy_port_tries_next_and_tracks_actual_port(self):
        result = types.SimpleNamespace(server_address=("127.0.0.1", 7892))
        with patch.object(webui, "_actual_port", None), \
             patch.object(webui, "_WebUIServer", side_effect=[OSError(errno.EADDRINUSE, "used"), result]) as server:
            self.assertIs(webui._bind_webui_server("127.0.0.1", 7891, ""), result)
            self.assertEqual(server.call_args_list[1].args[0], ("127.0.0.1", 7892))
            self.assertEqual(webui.get_actual_webui_port(), 7892)

    def test_busy_maximum_port_does_not_overflow(self):
        with patch.object(webui, "_WebUIServer", side_effect=OSError(errno.EADDRINUSE, "used")) as server:
            self.assertIsNone(webui._bind_webui_server("127.0.0.1", 65535, ""))
            self.assertEqual(server.call_count, 1)
            self.assertEqual(server.call_args.args[0], ("127.0.0.1", 65535))

    def test_invalid_port_never_attempts_binding(self):
        for port in (-1, 0, 65536):
            with self.subTest(port=port), patch.object(webui, "_WebUIServer") as server:
                self.assertIsNone(webui._bind_webui_server("127.0.0.1", port, ""))
                server.assert_not_called()

    def test_permission_error_does_not_scan_other_ports(self):
        with patch.object(webui, "_WebUIServer", side_effect=PermissionError(errno.EACCES, "denied")) as server:
            self.assertIsNone(webui._bind_webui_server("127.0.0.1", 7891, ""))
            self.assertEqual(server.call_count, 1)


class EmptyReplyRegressionTests(unittest.TestCase):
    def test_real_generation_guards_reject_split_only_content(self):
        tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
        for class_name in ("LimitedDeepSeekContext", "EnhancedLimitedDeepSeekContext"):
            cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name)
            method = next(node for node in cls.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "agen_content")
            guard = next(node for node in ast.walk(method) if isinstance(node, ast.If)
                         and any(isinstance(item, ast.Raise) and "模型返回了空白回复" in ast.unparse(item) for item in node.body))
            code = compile(ast.Module(body=[guard], type_ignores=[]), "actual-empty-guard", "exec")
            for text in ("", "\n", "<split>", "< SPLIT >\n<split>"):
                with self.subTest(cls=class_name, text=text), self.assertRaises(ValueError):
                    exec(code, {"result": text, "re": re})
            exec(code, {"result": "第一行\n第二行<split>第三行", "re": re})


class SendNewlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_call_json_newlines_survive_to_send(self):
        tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
        fn = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "_agent_send_message")
        helpers = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                   and node.name in ("split_llm_reply_for_send", "build_final_send_parts")]
        fn.decorator_list = []
        sent = []

        async def send(**kwargs):
            sent.append(kwargs)
            return {"status": "ok", "retcode": 0}

        async def serial(fn, label):
            await fn()
            return True, ""

        from bot.message_runtime import CURRENT_REQUEST, ReplySuppressed
        env = {"CURRENT_REQUEST": CURRENT_REQUEST, "ReplySuppressed": ReplySuppressed,
               "_consume_social_reply": lambda state: None,
               "_receipt_succeeded": lambda value: bool(value and value.get("status") == "ok"),
               "_short_text": lambda text, length: text[:length],
               "_agent_actions": lambda ctx: types.SimpleNamespace(send=send),
               "re": re,
               "_get_llm_split_config_cached": lambda: {"enabled": True, "mode": "auto_prompt", "filter_regex": r"\n|\r"},
               "_agent_send_serialized": serial,
               "Manager": types.SimpleNamespace(Message=lambda *args: args),
               "Segments": types.SimpleNamespace(Text=lambda text: text),
               "random": __import__("random"), "asyncio": __import__("asyncio")}
        exec(compile(ast.Module(body=helpers + [fn], type_ignores=[]), "main-send-test", "exec"), env)
        ctx = agent.AgentContext(user_id="123")
        raw = '{"content":"第一行\\n第二行"}'
        spec = agent.ToolSpec("send_message", "测试", {"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"]}, env["_agent_send_message"])
        args, error, _ = agent.prepare_tool_args(spec, raw)
        self.assertFalse(error)
        result = await env["_agent_send_message"](args, ctx)
        self.assertFalse(result.startswith("error:"), result)
        self.assertEqual(sent[0]["message"], ("第一行\n第二行",))


if __name__ == "__main__":
    unittest.main()
