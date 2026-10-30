"""聊天室离线回归：执行真实持久化函数和 HTTP handler，不导入 bot/main 或启用网络。"""
import ast
import io
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import traceback
import types
import unittest
from unittest.mock import Mock
import urllib.parse
import uuid
import weakref


ROOT = Path(__file__).resolve().parents[1]
WEBUI = ROOT / "webui.py"
TREE = ast.parse(WEBUI.read_text(encoding="utf-8"), filename=str(WEBUI))


def execute_nodes(nodes, namespace, filename):
    # 只编译选中的真实函数和常量，避开模块导入、配置读取和服务启动副作用。
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    module = ast.fix_missing_locations(ast.Module(body=[future, *nodes], type_ignores=[]))
    exec(compile(module, str(filename), "exec"), namespace)


def load_chatroom(directory):
    namespace = dict(
        BASE_DIR=Path(directory), Path=Path, json=json, os=os, re=re, uuid=uuid,
        threading=threading, weakref=weakref, traceback=traceback, urllib=urllib,
        time=types.SimpleNamespace(time=lambda: 1700000000),
    )
    nodes = []
    for node in TREE.body:
        if isinstance(node, ast.FunctionDef) and node.name.startswith("_chatroom_"):
            nodes.append(node)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(target, ast.Name) and target.id.startswith(("CHATROOM_", "_chatroom_", "ABORT_")) for target in targets):
                nodes.append(node)
    execute_nodes(nodes, namespace, WEBUI)
    io_path = ROOT / "webui_core" / "json_io.py"
    io_tree = ast.parse(io_path.read_text(encoding="utf-8"), filename=str(io_path))
    execute_nodes([node for node in io_tree.body if isinstance(node, ast.FunctionDef)
                   and node.name == "atomic_write_text"], namespace, io_path)
    namespace["_atomic_write_text"] = namespace["atomic_write_text"]
    handler = next(node for node in ast.walk(TREE) if isinstance(node, ast.ClassDef)
                   and node.name == "WebUIHandler")
    execute_nodes([node for node in handler.body if isinstance(node, ast.FunctionDef)
                   and node.name in ("do_POST", "do_GET")], namespace, WEBUI)
    namespace.update(
        get_webui_config=lambda: {}, _chatroom_reminder=lambda: "/",
        _chatroom_others=lambda: {}, build_llm_endpoints_from_providers=lambda cfg: [],
        _normalize_webui_llm_endpoints=lambda eps: eps,
        _chatroom_endpoint_for_model=lambda model: None,
        _chatroom_system_prompt=lambda persona: "离线系统提示",
        _append_log=Mock(),
        _json_response=lambda request, value, status=200: request.responses.append((status, value)),
    )

    def no_model(*args, **kwargs):
        raise AssertionError("离线测试不得调用真实模型")

    namespace["_chatroom_complete"] = no_model
    namespace["_chatroom_stream_complete"] = no_model
    return namespace


class CandidateFailoverTests(unittest.TestCase):
    def test_chatroom_signature_invalidates_old_selection_before_request(self):
        with tempfile.TemporaryDirectory() as directory:
            ns = load_chatroom(directory)
            signature = [["a", "one"], ["b", "two"]]
            ns["_chatroom_rotation_signature"] = lambda: list(signature)
            obj = ns["_chatroom_new_session"]("b/two")
            same, _ = ns["_chatroom_prepare_user_message"](obj["id"], "b/two", "offline one")
            self.assertEqual(same["model"], "b/two")
            signature.reverse()
            changed, _ = ns["_chatroom_prepare_user_message"](obj["id"], "b/two", "offline two")
            self.assertEqual(changed["model"], "")
            self.assertEqual(changed["_rotation_signature"], signature)

    def test_plain_and_stream_skip_all_keys_without_leaking_bad_body(self):
        from bot.reasoning import split_model_output as split_reasoning_output, ThinkStreamSplitter
        for stream in (False, True):
            with self.subTest(stream=stream), tempfile.TemporaryDirectory() as directory:
                ns = load_chatroom(directory)
                nodes = [node for node in TREE.body if isinstance(node, ast.FunctionDef)
                         and node.name in {"_chatroom_complete", "_chatroom_stream_complete"}]
                execute_nodes(nodes, ns, WEBUI)
                attempts = []
                class Response:
                    def __init__(self, text):
                        self.text = text
                    def __enter__(self):
                        return self
                    def __exit__(self, *args):
                        pass
                    def read(self):
                        return json.dumps({"choices": [{"message": {"content": self.text}}]}).encode()
                    def __iter__(self):
                        for chunk in (self.text[:2], self.text[2:]):
                            yield ("data: " + json.dumps({"choices": [{"delta": {"content": chunk}}]}) + "\n").encode()
                        yield b"data: [DONE]\n"
                class Opener:
                    def __init__(self, ep):
                        self.ep = ep
                    def open(self, req, **kwargs):
                        attempts.append(self.ep["model"])
                        return Response("bad-body" if self.ep["model"] == "first" else "safe-body")
                endpoints = [dict(model=m, keys=["fake1", "fake2"], base_url="https://offline.invalid")
                             for m in ("first", "second")]
                ns.update(_chatroom_rotation_endpoints=lambda model: endpoints,
                          _make_provider_opener=Opener, _provider_display_model=lambda p, m: m,
                          split_reasoning_output=split_reasoning_output, ThinkStreamSplitter=ThinkStreamSplitter,
                          _chatroom_filter_callback=lambda text: "bad" if "bad-body" in text else "")
                if stream:
                    text = "".join(chunk[0] for chunk in ns["_chatroom_stream_complete"]("", []))
                else:
                    text, _ = ns["_chatroom_complete"]("", [])
                self.assertEqual(text, "safe-body")
                self.assertEqual(attempts, ["first", "second"])
                attempts.clear()
                ns["_chatroom_filter_callback"] = lambda text: "all"
                with self.assertRaisesRegex(RuntimeError, "切换关键词"):
                    if stream:
                        list(ns["_chatroom_stream_complete"]("", []))
                    else:
                        ns["_chatroom_complete"]("", [])
                self.assertEqual(attempts, ["first", "second"])


class BrowserWriter(io.BytesIO):
    def __init__(self, failure=None, fail_on="write", fail_at=1):
        super().__init__()
        self.failure = failure
        self.fail_on = fail_on
        self.fail_at = fail_at
        self.write_calls = 0
        self.flush_calls = 0

    def write(self, value):
        self.write_calls += 1
        if self.failure and self.fail_on == "write" and self.write_calls >= self.fail_at:
            raise self.failure("浏览器写入失败")
        return super().write(value)

    def flush(self):
        self.flush_calls += 1
        if self.failure and self.fail_on == "flush" and self.flush_calls >= self.fail_at:
            raise self.failure("浏览器刷新失败")
        return super().flush()


class Request:
    # 不创建 socket；只替换传输层，do_POST/do_GET 及 SSE 分支保持真实执行。
    def __init__(self, payload=None, writer=None, path="/api/chat/send-stream"):
        self.payload = payload
        self.path = path
        self.wfile = writer if writer is not None else BrowserWriter()
        self.responses = []
        self.headers_sent = []
        self.status = None

    def _guard(self):
        return True

    def _read_body_json(self, max_bytes):
        return self.payload, None

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.headers_sent.append((name, value))

    def end_headers(self):
        pass

    def events(self):
        result = []
        for raw in self.wfile.getvalue().decode("utf-8").split("\n\n"):
            if not raw:
                continue
            event, data = raw.split("\n", 1)
            result.append((event.removeprefix("event: "), json.loads(data.removeprefix("data: "))))
        return result


class ChatroomRegressionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="xcbot-chatroom-test-")
        self.addCleanup(self.directory.cleanup)
        self.ns = load_chatroom(self.directory.name)
        self.session = self.call("_chatroom_new_session", "offline-model")
        self.sid = self.session["id"]
        self.payload = {"id": self.sid, "model": "offline-model", "text": "请保留这个提问", "stream": True}

    def call(self, name, *args, **kwargs):
        return self.ns[name](*args, **kwargs)

    def send(self, callback, writer=None, **payload):
        self.ns["_chatroom_agent_callback"] = callback
        request = Request({**self.payload, **payload}, writer)
        self.call("do_POST", request)
        self.assertEqual(request.status, 200)
        self.assertIn(("Content-Type", "text/event-stream; charset=utf-8"), request.headers_sent)
        self.assertFalse(self.call("_chatroom_is_generating", self.sid))
        return request

    def reload(self):
        # 重新建命名空间并走真实 GET，确保显示内容确实来自文件，不是原对象或 SSE 缓存。
        namespace = load_chatroom(self.directory.name)
        request = Request(path="/api/chat/session?id=" + self.sid)
        namespace["do_GET"](request)
        self.assertEqual(request.responses[0][0], 200)
        self.assertTrue(request.responses[0][1]["ok"])
        return request.responses[0][1]["data"]

    def assert_failure(self, request, partial=None, connected=True):
        session = self.reload()
        messages = session["messages"]
        self.assertEqual(messages[0]["role"], "user")
        self.assertEqual(messages[0]["content"], self.payload["text"])
        errors = [message for message in messages if message.get("error")]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["content"], "模型网络异常")
        replies = [message for message in messages if message["role"] == "assistant" and not message.get("error")]
        self.assertEqual([message["content"] for message in replies], [partial] if partial else [])
        ids = [message["id"] for message in messages]
        self.assertTrue(all(ids))
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len({message["request_id"] for message in messages}), 1)
        context = self.call("_chatroom_build_llm_messages", session, "offline-model")
        self.assertNotIn("模型网络异常", json.dumps(context, ensure_ascii=False))
        self.assertNotIn("reasoning", json.dumps(context))
        self.assertEqual([m["role"] for m in context], ["system", "user", "assistant"] if partial else ["system"])
        if connected:
            events = request.events()
            self.assertEqual(sum(event == "error" for event, _ in events), 1)
            self.assertNotIn("done", [event for event, _ in events])
            self.assertEqual(events[-1][1]["session"], session)
        self.assertFalse(self.call("_chatroom_is_generating", self.sid))
        return session

    def test_agent_network_errors_keep_question_and_persist_error(self):
        for failure in (BrokenPipeError, ConnectionResetError, RuntimeError):
            for stream in (True, False):
                with self.subTest(failure=failure.__name__, stream=stream):
                    self.call("_chatroom_save", {**self.session, "messages": []})

                    def callback(payload):
                        self.assertTrue(self.call("_chatroom_is_generating", self.sid))
                        raise failure("模型网络异常")

                    self.assert_failure(self.send(callback, stream=stream))

    def test_agent_partial_then_error_is_saved_once_with_reasoning(self):
        for failure in (BrokenPipeError, ConnectionResetError, RuntimeError):
            with self.subTest(failure=failure.__name__):
                self.call("_chatroom_save", {**self.session, "messages": []})

                def callback(payload):
                    payload["reasoning_callback"]("整轮思考")
                    payload["stream_callback"]("安全可见")
                    payload["stream_callback"]("半篇回复")
                    raise failure("模型网络异常")

                request = self.send(callback)
                session = self.assert_failure(request, "安全可见半篇回复")
                self.assertEqual(session["messages"][1]["reasoning"], "整轮思考")

    def test_segmented_partial_and_progress_are_saved_once_then_error(self):
        append = Mock(wraps=self.ns["_chatroom_append_assistant"])
        self.ns["_chatroom_append_assistant"] = append

        def callback(payload):
            payload["progress_callback"]("工具前说明")
            payload["reasoning_callback"]("同一份整轮思考")
            payload["stream_callback"]("分段一<split>")
            payload["stream_callback"]("分段二")
            raise ConnectionResetError("模型网络异常")

        request = self.send(callback)
        messages = self.reload()["messages"]
        self.assertEqual([m["content"] for m in messages], [self.payload["text"], "工具前说明", "分段一", "分段二", "模型网络异常"])
        self.assertEqual([m["reasoning"] for m in messages[2:4]], ["同一份整轮思考"] * 2)
        self.assertEqual(len({m["id"] for m in messages}), 5)
        self.assertTrue(messages[-1]["error"])
        self.assertEqual(request.events()[-1][1]["session"]["messages"], messages)
        append.assert_called_once()

    def test_nonstream_fallback_network_failure_persists_error(self):
        self.ns["_chatroom_complete"] = Mock(side_effect=BrokenPipeError("模型网络异常"))
        self.assert_failure(self.send(None, stream=False))
        self.ns["_chatroom_complete"].assert_called_once()

    def test_browser_failure_on_done_preserves_completed_reply_without_error(self):
        writer = BrowserWriter(BrokenPipeError)
        self.send(lambda payload: {"reply": "完整回复"}, writer)
        self.assertEqual([m["content"] for m in self.reload()["messages"]], [self.payload["text"], "完整回复"])
        self.assertEqual(writer.write_calls, 1)

    def test_browser_write_or_flush_failure_does_not_interrupt_success(self):
        for failure in (BrokenPipeError, ConnectionResetError, OSError):
            for fail_on in ("write", "flush"):
                with self.subTest(failure=failure.__name__, fail_on=fail_on):
                    self.call("_chatroom_save", {**self.session, "messages": []})
                    writer = BrowserWriter(failure, fail_on)

                    def callback(payload):
                        payload["stream_callback"]("第一段")
                        payload["reasoning_callback"]("同一份整轮思考")
                        payload["stream_callback"]("第二段")
                        return {"reply": "第一段<split>第二段", "history": [{"role": "user", "content": "隐藏历史"}]}

                    self.send(callback, writer)
                    session = self.reload()
                    self.assertEqual([m["content"] for m in session["messages"]], [self.payload["text"], "第一段", "第二段"])
                    self.assertEqual([m["reasoning"] for m in session["messages"][1:]], ["同一份整轮思考"] * 2)
                    self.assertNotIn("_agent_history", session)
                    self.assertEqual(writer.write_calls, 1)
                    self.assertEqual(writer.flush_calls, int(fail_on == "flush"))

    def test_browser_disconnected_still_persists_model_failure_and_partial(self):
        for failure in (BrokenPipeError, ConnectionResetError, RuntimeError):
            for partial in (None, "安全半篇"):
                with self.subTest(failure=failure.__name__, partial=partial):
                    self.call("_chatroom_save", {**self.session, "messages": []})
                    writer = BrowserWriter(ConnectionResetError)

                    def callback(payload):
                        # 即使尚无正文，思考事件也可先触发浏览器断开。
                        payload["reasoning_callback"]("整轮思考")
                        if partial:
                            payload["stream_callback"](partial)
                        raise failure("模型网络异常")

                    request = self.send(callback, writer)
                    self.assert_failure(request, partial, connected=False)
                    self.assertEqual(writer.write_calls, 1)

    def test_fallback_stream_partial_then_network_failure_uses_same_error_path(self):
        for failure in (BrokenPipeError, ConnectionResetError, RuntimeError):
            with self.subTest(failure=failure.__name__):
                self.call("_chatroom_save", {**self.session, "messages": []})

                def stream(model, messages):
                    yield "安全", "整轮思考"
                    yield "半篇", ""
                    raise failure("模型网络异常")

                self.ns["_chatroom_stream_complete"] = stream
                self.assert_failure(self.send(None), "安全半篇")

    def test_reply_filter_is_respected_when_recovering_partial(self):
        self.ns["_chatroom_filter_callback"] = lambda text: "禁止词" if "禁止词" in text else ""

        def callback(payload):
            payload["stream_callback"]("含禁止词的半篇")
            raise RuntimeError("模型网络异常")

        request = self.send(callback)
        messages = self.reload()["messages"]
        self.assertEqual(len(messages), 3)
        self.assertTrue(all(message.get("error") for message in messages[1:]))
        self.assertNotIn("含禁止词的半篇", json.dumps(messages, ensure_ascii=False))
        self.assertEqual(request.events()[-1][0], "error")
        self.assertEqual(len(self.call("_chatroom_build_llm_messages", self.reload(), "offline-model")), 1)

    def test_error_persistence_failure_never_removes_saved_question(self):
        self.ns["_chatroom_append_error"] = Mock(side_effect=OSError("磁盘写入失败"))

        def callback(payload):
            raise BrokenPipeError("模型网络异常")

        request = self.send(callback)
        self.assertEqual([m["content"] for m in self.reload()["messages"]], [self.payload["text"]])
        self.assertEqual(request.events()[-1][0], "error")
        self.assertIsNone(request.events()[-1][1]["session"])

    def test_partial_save_failure_still_attempts_error_persistence(self):
        self.ns["_chatroom_append_assistant"] = Mock(side_effect=OSError("半篇保存失败"))

        def callback(payload):
            payload["stream_callback"]("安全半篇")
            raise RuntimeError("模型网络异常")

        self.assert_failure(self.send(callback))
        self.ns["_chatroom_append_assistant"].assert_called_once()

    def test_saved_reply_is_not_appended_again_if_done_event_fails(self):
        # 模拟正常落盘后编码 done 事件失败，恢复分支不能重复追加同一回复。
        public = self.ns["_chatroom_public"]
        calls = 0

        def fail_once(session):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("模型网络异常")
            return public(session)

        self.ns["_chatroom_public"] = fail_once
        self.assert_failure(self.send(lambda payload: {"reply": "最终回复"}), "最终回复")

    def test_message_ids_are_unique_persistent_and_shared_reasoning_is_unchanged(self):
        def callback(payload):
            payload["reasoning_callback"]("同一份整轮思考")
            return {"reply": "相同分段<split>相同分段", "progress_messages": ["工具前说明"]}

        request = self.send(callback)
        session = self.reload()
        self.assertEqual(request.events()[-1], ("done", {"session": session}))
        messages = session["messages"]
        self.assertEqual(len(messages), 4)
        self.assertEqual(len({m["ts"] for m in messages}), 1)
        self.assertEqual(len({m["request_id"] for m in messages}), 1)
        self.assertEqual(len({m["id"] for m in messages}), 4)
        self.assertEqual([m["reasoning"] for m in messages[-2:]], ["同一份整轮思考"] * 2)
        self.assertEqual(messages, self.reload()["messages"])
        self.ns["CHATROOM_MAX_MESSAGES"] = 2
        self.call("_chatroom_trim_history", session)
        self.call("_chatroom_save", session)
        self.assertEqual(self.reload()["messages"], messages[-2:])

    def test_legacy_session_without_message_ids_remains_loadable(self):
        old = {**self.session, "messages": [
            {"role": "user", "content": "旧问题", "ts": 1, "request_id": "old"},
            {"role": "assistant", "content": "旧回复", "ts": 1, "request_id": "old", "reasoning": "旧思考"},
        ]}
        self.call("_chatroom_save", old)
        self.assertEqual(self.reload()["messages"], old["messages"])
        self.send(lambda payload: {"reply": "新回复"})
        messages = self.reload()["messages"]
        self.assertEqual(messages[:2], old["messages"])
        self.assertTrue(all(m.get("id") for m in messages[2:]))

    def test_generating_flag_is_cleared_inside_original_session_lock(self):
        mark = self.ns["_chatroom_mark_generating"]
        calls = []

        def checked_mark(sid, running):
            lock = self.call("_chatroom_session_lock", sid)
            self.assertTrue(lock._is_owned(), "清理必须先于释放本轮会话锁")
            calls.append(running)
            mark(sid, running)

        self.ns["_chatroom_mark_generating"] = checked_mark

        def callback(payload):
            self.assertTrue(self.call("_chatroom_is_generating", self.sid))
            raise ConnectionResetError("模型网络异常")

        self.send(callback)
        self.assertEqual(calls, [True, False])

    def test_validation_failure_also_clears_generating_without_deleting_history(self):
        request = self.send(Mock(side_effect=AssertionError("不应调用回调")), text="")
        self.assertEqual(self.reload()["messages"], [])
        self.assertEqual(request.events()[-1][0], "error")
        self.assertFalse(self.call("_chatroom_is_generating", self.sid))


if __name__ == "__main__":
    unittest.main(verbosity=2)
