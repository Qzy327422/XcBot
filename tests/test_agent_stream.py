"""Public stream fakes only: no main import, sockets, QQ, or tool execution."""
import ast
import asyncio
import pathlib
import threading
import time
import types
import unittest
from unittest.mock import patch

from bot.agent_protocol import inspect_tool_markup
from bot.agent_stream import VisibleTextGuard, request_chat_completion
from bot.reasoning import split_model_output

ROOT = pathlib.Path(__file__).resolve().parents[1]
NS = types.SimpleNamespace
DSML = '<｜｜DSML｜｜ calls><｜｜DSML｜｜ invoke name="execute_shell">{"command":"SECRET"}</｜｜DSML｜｜ invoke></｜｜DSML｜｜ calls>'
NATIVE = '<｜tool▁calls▁begin｜><｜tool▁call▁begin｜>function<｜tool▁sep｜>execute_shell\n{"command":"SECRET"}<｜tool▁call▁end｜><｜tool▁calls▁end｜>'
ASCII = '<|tool_calls|>{"name":"execute_shell","arguments":{"command":"SECRET"}}</|tool_calls|>'


def chunk(content=None, reasoning=None, calls=None):
    return NS(choices=[NS(index=0, delta=NS(content=content, reasoning_content=reasoning,
                                          tool_calls=calls))], usage=None)


def event(value):
    return NS(type="chunk", chunk=value)


def completion(content="", calls=None, reasoning=None):
    message = NS(content=content, tool_calls=calls, reasoning_content=reasoning)
    return NS(choices=[NS(message=message)], usage=NS(total_tokens=9, prompt_tokens=5,
              completion_tokens=4, prompt_tokens_details=NS(cached_tokens=2)))


class FakeStream:
    def __init__(self, events=(), final=None, hook=None, failure=None):
        self.events = list(events)
        self.final = final or completion()
        self.hook = hook
        self.failure = failure
        self.closed = 0
        self.final_calls = 0
        self.exited = threading.Event()
        self.iter_finished = threading.Event()
        self.close_hook = None

    def __iter__(self):
        try:
            for index, item in enumerate(self.events):
                if self.hook:
                    self.hook(index)
                yield item
            if self.failure:
                raise self.failure
        finally:
            self.iter_finished.set()

    def get_final_completion(self):
        self.final_calls += 1
        return self.final

    def close(self):
        self.closed += 1
        if self.close_hook:
            self.close_hook()


class FakeManager:
    def __init__(self, stream, enter_hook=None):
        self.value = stream
        self.enter_hook = enter_hook
        self.entered = 0

    def __enter__(self):
        self.entered += 1
        if self.enter_hook:
            self.enter_hook()
        return self.value

    def __exit__(self, *args):
        self.value.close()
        self.value.exited.set()


class FakeCompletions:
    def __init__(self, manager=None, final=None):
        self.manager = manager
        self.final = final or completion()
        self.stream_calls = []
        self.create_calls = []
        self.create_hook = None
        if manager is None:
            self.stream = None

    def stream(self, **kwargs):
        if "stream" in kwargs:
            raise AssertionError("public .stream() cannot take stream=")
        self.stream_calls.append(kwargs)
        return self.manager

    def create(self, **kwargs):
        self.create_calls.append(kwargs)
        if self.create_hook:
            self.create_hook()
        return self.final


class FakeClient:
    def __init__(self, completions):
        self.chat = NS(completions=completions)
        self.closed = 0

    def close(self):
        self.closed += 1
        raise AssertionError("shared client must not be closed")


class GuardTests(unittest.TestCase):
    def test_all_two_chunk_boundaries_and_truncated_protocol(self):
        for markup in (DSML, NATIVE, ASCII):
            for text in ("正文 " + markup + " 尾文", "正文 " + markup[:len(markup) // 2]):
                expected, detected = inspect_tool_markup(text)
                self.assertTrue(detected)
                self.assertNotIn("SECRET", expected)
                for split in range(len(text) + 1):
                    with self.subTest(text=text[:24], split=split):
                        guard = VisibleTextGuard()
                        visible = guard.feed(text[:split]) + guard.feed(text[split:]) + guard.finish()
                        self.assertEqual(visible, expected)
                        self.assertNotIn("SECRET", visible)

    def test_each_character_markers_and_partial_headers(self):
        for markup in (DSML, NATIVE, ASCII, '<｜｜DS', '<|tool_ca', '<｜tool▁call▁beg'):
            guard = VisibleTextGuard()
            text = "正常内容 " + markup
            outputs = [guard.feed(char) for char in text]
            outputs.append(guard.finish())
            self.assertEqual("".join(outputs), inspect_tool_markup(text)[0])
            self.assertEqual("".join(outputs), "正常内容 ")

    def test_quotes_inline_and_fenced_examples_at_every_boundary(self):
        for markup in (DSML, NATIVE, ASCII):
            for text in ("前文 `" + markup + "` 后文", '前文 “' + markup + '” 后文',
                         '前文 "' + markup + '" 后文', "```xml\n" + markup + "\n```\n后文",
                         "> " + markup.replace("\n", "\n> ") + "\n后文",
                         "    " + markup.replace("\n", "\n    ") + "\n后文"):
                self.assertEqual(inspect_tool_markup(text), (text, False))
                for split in range(len(text) + 1):
                    with self.subTest(example=text[:20], split=split):
                        guard = VisibleTextGuard()
                        output = guard.feed(text[:split]) + guard.feed(text[split:]) + guard.finish()
                        self.assertEqual(output, text)

    def test_normal_text_is_immediate(self):
        guard = VisibleTextGuard()
        for text in ("你好", "，普通正文", " keeps spaces\n", "2 < 3 and ", "<html>"):
            self.assertEqual(guard.feed(text), text)
        self.assertEqual(guard.finish(), "")

    def test_prose_before_markup_in_same_chunk_is_immediate(self):
        guard = VisibleTextGuard()
        self.assertEqual(guard.feed("先说结论 " + DSML), "先说结论 ")
        self.assertEqual(guard.feed(" 后文"), "")
        self.assertEqual(guard.finish(), " 后文")

    def test_changed_cleaned_prefix_is_never_reemitted(self):
        def inspect(text):
            return (text, False) if text == "prefix" else ("replacement", False)
        guard = VisibleTextGuard(inspect)
        self.assertEqual(guard.feed("prefix"), "prefix")
        with self.assertLogs("bot.agent_stream", "WARNING"):
            self.assertEqual(guard.feed(" later"), "")
        self.assertEqual(guard.finish(), "")


class RequestTests(unittest.IsolatedAsyncioTestCase):
    def setup_stream(self, events=(), final=None, hook=None, failure=None, enter_hook=None):
        stream = FakeStream(events, final, hook, failure)
        manager = FakeManager(stream, enter_hook)
        resource = FakeCompletions(manager)
        client = FakeClient(resource)
        return client, resource, manager, stream

    async def request(self, client, visible, reasoning=None, **kwargs):
        response = await request_chat_completion(
            client, {"model": "offline", "messages": [], "stream": False},
            timeout=kwargs.pop("timeout", 2), stream_callback=visible.append,
            reasoning_callback=reasoning.append if reasoning is not None else None, **kwargs)
        api = client.chat.completions
        if api.manager is not None:
            await self.wait_event(api.manager.value.exited)
        return response

    async def wait_event(self, event):
        self.assertTrue(await asyncio.to_thread(event.wait, 2), "fake worker failed to reach checkpoint")

    async def test_public_context_chunks_once_and_native_final_unchanged(self):
        calls = [NS(id="call_real", type="function", function=NS(name="file_read", arguments='{"path":"real"}'))]
        final = completion("正文 " + DSML, calls=calls)
        first = chunk("正文 ", "reason ")
        second = chunk(DSML, "more", [NS(index=0, function=NS(arguments='{"path":'))])
        visible, reasoning = [], []
        client, api, manager, stream = self.setup_stream([
            event(first), NS(type="content.delta", delta="正文 "), event(second),
            NS(type="tool_calls.function.arguments.delta", arguments_delta='"GUESS"}')], final)
        result = await self.request(client, visible, reasoning)
        self.assertIs(result, final)
        self.assertIs(result.choices[0].message.tool_calls, calls)
        self.assertEqual(result.choices[0].message.content, "正文 " + DSML)
        self.assertEqual("".join(visible), "正文 ")
        self.assertEqual("".join(reasoning), "reason more")
        self.assertEqual(manager.entered, 1)
        self.assertEqual(stream.closed, 1)
        self.assertEqual(stream.final_calls, 1)
        self.assertEqual(client.closed, 0)
        self.assertEqual(len(api.stream_calls), 1)
        self.assertNotIn("stream", api.stream_calls[0])
        self.assertEqual(api.create_calls, [])

    async def test_plain_content_with_explicit_reasoning_field_streams_immediately(self):
        visible = []
        def hook(index):
            if index == 1:
                self.assertEqual(visible, ["first"])
        client, _, _, _ = self.setup_stream(
            [event(chunk("first", "separate reasoning")), event(chunk(" second"))], hook=hook)
        await self.request(client, visible)
        self.assertEqual(visible, ["first", " second"])

    async def test_plain_content_without_reasoning_field_waits_for_completion(self):
        visible = []
        def hook(_):
            self.assertEqual(visible, [])
        client, _, _, stream = self.setup_stream(
            [event(chunk("first")), event(chunk(" second"))], completion("first second"), hook=hook)
        await self.request(client, visible)
        self.assertEqual(visible, ["first second"])
        self.assertEqual(stream.final_calls, 1)

    async def test_closed_only_think_never_emits_initial_private_chunks(self):
        for tag in ("think", "thinking", "thought"):
            for closing_parts in ((f"</{tag}>",), ("</", tag[:2], tag[2:] + ">")):
                with self.subTest(tag=tag, closing_parts=closing_parts):
                    visible, reasoning = [], []
                    chunks = ["私密推理", " 更多私密", *closing_parts, "最终答案", " 后续正文"]
                    first_answer = len(chunks) - 2
                    def hook(index):
                        if index <= first_answer:
                            self.assertEqual(visible, [])
                        else:
                            self.assertEqual(visible, ["最终答案"])
                    raw = "私密推理 更多私密" + f"</{tag}>最终答案 后续正文"
                    client, _, _, _ = self.setup_stream(
                        [event(chunk(text)) for text in chunks], completion(raw), hook=hook)
                    response = await self.request(client, visible, reasoning)
                    self.assertEqual("".join(visible), "最终答案 后续正文")
                    self.assertNotIn("私密", "".join(visible))
                    self.assertEqual("".join(reasoning), "私密推理 更多私密")
                    self.assertEqual(response.choices[0].message.content, raw)

    async def test_explicit_think_close_resumes_streaming_before_final_completion(self):
        visible, reasoning = [], []
        def hook(index):
            if index < 3:
                self.assertEqual(visible, [])
            else:
                self.assertEqual(visible, ["answer"])
        raw = "<think>private</think>answer tail"
        client, _, _, _ = self.setup_stream([
            event(chunk("<thi")), event(chunk("nk>private</thi")),
            event(chunk("nk>answer")), event(chunk(" tail"))], completion(raw), hook=hook)
        await self.request(client, visible, reasoning)
        self.assertEqual("".join(visible), "answer tail")
        self.assertEqual("".join(reasoning), "private")

    async def test_unknown_initial_prefix_is_not_flushed_on_failure(self):
        visible, reasoning = [], []
        client, _, _, stream = self.setup_stream(
            [event(chunk("私密推理")), event(chunk(" 未分类前缀"))], failure=RuntimeError("broken"))
        with self.assertRaisesRegex(RuntimeError, "broken"):
            await self.request(client, visible, reasoning)
        await self.wait_event(stream.exited)
        self.assertEqual((visible, reasoning), ([], []))
        self.assertEqual(stream.final_calls, 0)

    async def test_empty_reasoning_placeholder_does_not_trust_initial_content(self):
        visible = []
        def hook(index):
            if index == 1:
                self.assertEqual(visible, [])
        client, _, _, _ = self.setup_stream(
            [event(chunk("私密推理", "")), event(chunk("</think>最终答案"))],
            completion("私密推理</think>最终答案"), hook=hook)
        await self.request(client, visible)
        self.assertEqual(visible, ["最终答案"])

    async def test_split_protocol_and_quotes_through_think_splitter(self):
        for markup in (DSML, NATIVE, ASCII):
            for raw in ("前文 " + markup + " 后文", "`" + markup + "`", "“" + markup + "”",
                        "```\n" + markup + "\n```", "> " + markup,
                        "前文 " + markup[:23]):
                visible, reasoning = [], []
                client, _, _, stream = self.setup_stream(
                    [event(chunk(char)) for char in raw], completion(raw))
                result = await self.request(client, visible, reasoning)
                self.assertEqual("".join(visible), inspect_tool_markup(raw)[0])
                self.assertEqual(result.choices[0].message.content, raw)
                self.assertEqual(stream.closed, 1)

    async def test_quoted_example_resumes_streaming_after_quote_closes(self):
        visible = []
        raw = "前文 `" + ASCII + "`"
        def hook(index):
            if index == 2:
                self.assertEqual("".join(visible), raw)
        client, _, _, _ = self.setup_stream(
            [event(chunk("前文 `" + ASCII, "separate reasoning")), event(chunk("`")), event(chunk(" 后文"))], hook=hook)
        await self.request(client, visible)
        self.assertEqual("".join(visible), raw + " 后文")

    async def test_think_split_and_success_only_tail(self):
        visible, reasoning = [], []
        client, _, _, stream = self.setup_stream([
            event(chunk("<thi")), event(chunk("nk>thinking </thi")),
            event(chunk("nk>正文 <html>"))], completion("<think>thinking </think>正文 <html>"))
        await self.request(client, visible, reasoning)
        self.assertEqual("".join(visible), "正文 <html>")
        self.assertEqual("".join(reasoning), "thinking ")
        self.assertEqual(stream.closed, 1)

    async def test_missing_index_on_exposed_chunk_repaired_and_warned(self):
        calls = [NS(index=None), NS(index=None)]
        client, _, _, stream = self.setup_stream([event(chunk(calls=calls))])
        with self.assertLogs("bot.agent_stream", "WARNING") as logs:
            await self.request(client, [])
        self.assertEqual([call.index for call in calls], [0, 1])
        self.assertIn("聚合", "".join(logs.output))
        self.assertEqual(stream.closed, 1)

    async def test_sdk_failure_before_chunk_reports_tradeoff_no_replay(self):
        visible = []
        client, api, _, stream = self.setup_stream([event(chunk("sent", "separate reasoning"))],
                                                  failure=TypeError("indices must be integers, not NoneType"))
        with self.assertRaisesRegex(RuntimeError, r"tool_call.index.*chunk"):
            await self.request(client, visible)
        self.assertEqual(visible, ["sent"])
        self.assertEqual(api.create_calls, [])
        self.assertEqual(len(api.stream_calls), 1)
        self.assertEqual(stream.closed, 1)

    async def test_missing_public_stream_falls_back_once_with_raw_content(self):
        final = completion("正文 " + DSML, reasoning="reason")
        api = FakeCompletions(final=final)
        client = FakeClient(api)
        visible, reasoning = [], []
        with self.assertLogs("bot.agent_stream", "WARNING") as logs:
            result = await self.request(client, visible, reasoning)
        self.assertIn("回退非流式", "".join(logs.output))
        self.assertIs(result, final)
        self.assertEqual(final.choices[0].message.content, "正文 " + DSML)
        self.assertEqual("".join(visible), "正文 ")
        self.assertEqual(reasoning, ["reason"])
        self.assertEqual(len(api.create_calls), 1)
        self.assertFalse(api.create_calls[0]["stream"])
        self.assertEqual(client.closed, 0)

    async def test_nonstream_no_visible_callback_still_preserves_raw_message(self):
        final = completion("<think>private</think>正文 " + DSML)
        api = FakeCompletions(final=final)
        reasoning = []
        result = await request_chat_completion(FakeClient(api), {}, timeout=2,
                                               reasoning_callback=reasoning.append)
        self.assertIs(result, final)
        self.assertEqual(result.choices[0].message.content, "<think>private</think>正文 " + DSML)
        self.assertEqual(reasoning, ["private"])

    async def test_error_does_not_flush_splitter_or_guard_residue(self):
        for partial in ("safe <thi", "safe <｜｜DS", "safe <|tool_calls|>{\"command\":\"SECRET\"}",
                        "<think>reason </thi"):
            visible, reasoning = [], []
            client, _, _, stream = self.setup_stream([event(chunk(partial))], failure=RuntimeError("broken"))
            with patch("bot.agent_stream.ThinkStreamSplitter.flush", side_effect=AssertionError("must not flush")):
                with self.assertRaisesRegex(RuntimeError, "broken"):
                    await self.request(client, visible, reasoning)
            self.assertNotIn("<", "".join(visible))
            self.assertNotIn("SECRET", "".join(visible))
            self.assertNotIn("</thi", "".join(reasoning))
            self.assertEqual(stream.closed, 1)

    async def test_close_admission_full_rejects_before_creating_stream(self):
        from bot import agent_stream
        client, api, manager, stream = self.setup_stream([event(chunk("answer"))])
        with patch.object(agent_stream._CLOSE_SLOTS, "acquire", return_value=False):
            with self.assertLogs("bot.agent_stream", "ERROR"):
                with self.assertRaisesRegex(RuntimeError, "队列已满"):
                    await self.request(client, [])
        self.assertEqual(api.stream_calls, [])
        self.assertEqual(manager.entered, 0)
        self.assertEqual(stream.closed, 0)

    async def test_enter_failure_releases_cleanup_admission(self):
        from bot import agent_stream
        def enter():
            raise RuntimeError("enter failed")
        client, _, _, stream = self.setup_stream(enter_hook=enter)
        slots = agent_stream._CLOSE_SLOTS
        with patch.object(slots, "acquire", wraps=slots.acquire) as acquire:
            with patch.object(slots, "release", wraps=slots.release) as release:
                with self.assertRaisesRegex(RuntimeError, "enter failed"):
                    await self.request(client, [])
                acquire.assert_called_once_with(blocking=False)
                release.assert_called_once_with()
        self.assertEqual(stream.closed, 0)

    def test_close_workers_are_fixed_daemons_and_queue_is_bounded(self):
        from bot import agent_stream
        self.assertEqual(agent_stream._CLOSE_QUEUE.maxsize, 64)
        workers = [worker for worker in threading.enumerate()
                   if worker.name.startswith("agent-stream-close-")]
        self.assertLessEqual(len(workers), 2)
        self.assertTrue(all(worker.daemon for worker in workers))

    async def test_blocking_callback_does_not_block_cancel_or_emit_tail(self):
        for kind in ("visible", "reasoning"):
            with self.subTest(callback=kind):
                entered, release, callback_done = (threading.Event() for _ in range(3))
                visible, reasoning = [], []
                def blocked(text):
                    target = visible if kind == "visible" else reasoning
                    target.append(text)
                    entered.set()
                    try:
                        if not release.wait(3):
                            raise AssertionError("callback was not released")
                    finally:
                        callback_done.set()
                client, _, _, stream = self.setup_stream([
                    event(chunk("first <thi", "separate reasoning")),
                    event(chunk("nk>SECRET</think>tail", "LATE reasoning"))])
                task = asyncio.create_task(request_chat_completion(
                    client, {}, timeout=5,
                    stream_callback=blocked if kind == "visible" else visible.append,
                    reasoning_callback=blocked if kind == "reasoning" else reasoning.append))
                # 即使回归成持锁 callback，也由独立定时器释放，避免测试死锁。
                watchdog = threading.Timer(2, release.set)
                watchdog.daemon = True
                watchdog.start()
                try:
                    await self.wait_event(entered)
                    started = time.monotonic()
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    self.assertLess(time.monotonic() - started, 0.75)
                    self.assertFalse(release.is_set(), "cancel waited for external callback")
                    before = (list(visible), list(reasoning))
                    release.set()
                    await self.wait_event(callback_done)
                    await self.wait_event(stream.iter_finished)
                    await self.wait_event(stream.exited)
                    self.assertEqual((visible, reasoning), before)
                    self.assertEqual(stream.final_calls, 0)
                    self.assertEqual(stream.closed, 1)
                    self.assertEqual(client.closed, 0)
                finally:
                    release.set()
                    watchdog.cancel()
                    if not task.done():
                        task.cancel()

    async def test_blocking_callback_does_not_delay_timeout_or_send_after_release(self):
        entered, release = threading.Event(), threading.Event()
        visible, reasoning = [], []
        def callback(text):
            visible.append(text)
            entered.set()
            if not release.wait(3):
                raise AssertionError("callback was not released")
        client, _, _, stream = self.setup_stream([
            event(chunk("first", "before")), event(chunk("LATE", "LATE"))])
        task = asyncio.create_task(request_chat_completion(
            client, {}, timeout=0.25, stream_callback=callback, reasoning_callback=reasoning.append))
        watchdog = threading.Timer(2, release.set)
        watchdog.daemon = True
        watchdog.start()
        try:
            await self.wait_event(entered)
            started = time.monotonic()
            with self.assertRaises(TimeoutError):
                await task
            self.assertLess(time.monotonic() - started, 0.75)
            self.assertFalse(release.is_set())
            release.set()
            await self.wait_event(stream.iter_finished)
            await self.wait_event(stream.exited)
            self.assertEqual((visible, reasoning), (["first"], ["before"]))
            self.assertEqual(stream.final_calls, 0)
        finally:
            release.set()
            watchdog.cancel()
            if not task.done():
                task.cancel()

    async def test_blocking_close_does_not_block_cancel_and_is_scheduled_once(self):
        entered, release_next, close_entered, release_close = (threading.Event() for _ in range(4))
        visible, reasoning = [], []
        def next_hook(index):
            if index == 1:
                entered.set()
                if not release_next.wait(3):
                    raise AssertionError("iterator was not released")
        def close_hook():
            close_entered.set()
            if not release_close.wait(3):
                raise AssertionError("close was not released")
        client, _, _, stream = self.setup_stream([
            event(chunk("first", "before")), event(chunk("LATE", "LATE"))], hook=next_hook)
        stream.close_hook = close_hook
        task = asyncio.create_task(request_chat_completion(
            client, {}, timeout=5, stream_callback=visible.append, reasoning_callback=reasoning.append))
        watchdog = threading.Timer(2, release_close.set)
        watchdog.daemon = True
        watchdog.start()
        try:
            await self.wait_event(entered)
            started = time.monotonic()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertLess(time.monotonic() - started, 0.75)
            await self.wait_event(close_entered)
            self.assertFalse(release_close.is_set())
            release_next.set()
            await self.wait_event(stream.iter_finished)
            self.assertEqual((visible, reasoning), (["first"], ["before"]))
            self.assertEqual(stream.closed, 1)
            self.assertEqual(stream.final_calls, 0)
            self.assertFalse(stream.exited.is_set())
            release_close.set()
            await self.wait_event(stream.exited)
            self.assertEqual(client.closed, 0)
        finally:
            release_next.set()
            release_close.set()
            watchdog.cancel()
            if not task.done():
                task.cancel()

    async def test_blocking_close_on_success_does_not_delay_response(self):
        close_entered, release = threading.Event(), threading.Event()
        client, _, _, stream = self.setup_stream([event(chunk("answer"))], completion("answer"))
        def close_hook():
            close_entered.set()
            if not release.wait(3):
                raise AssertionError("close was not released")
        stream.close_hook = close_hook
        visible = []
        watchdog = threading.Timer(2, release.set)
        watchdog.daemon = True
        watchdog.start()
        try:
            started = time.monotonic()
            response = await request_chat_completion(client, {}, timeout=5, stream_callback=visible.append)
            self.assertLess(time.monotonic() - started, 0.75)
            await self.wait_event(close_entered)
            self.assertFalse(release.is_set())
            self.assertEqual(visible, ["answer"])
            self.assertEqual(response.choices[0].message.content, "answer")
            release.set()
            await self.wait_event(stream.exited)
            self.assertEqual(stream.closed, 1)
        finally:
            release.set()
            watchdog.cancel()

    async def test_cancel_during_blocking_next_disables_both_callbacks(self):
        entered, release = threading.Event(), threading.Event()
        visible, reasoning = [], []
        def hook(index):
            if index == 1:
                entered.set()
                if not release.wait(2):
                    raise AssertionError("worker was not released")
        client, _, _, stream = self.setup_stream([
            event(chunk("safe <thi", "before")), event(chunk("nk>SECRET", "LATE"))], hook=hook)
        task = asyncio.create_task(self.request(client, visible, reasoning))
        try:
            await self.wait_event(entered)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await self.wait_event(stream.exited)
            self.assertGreaterEqual(stream.closed, 1)
            release.set()
            await self.wait_event(stream.iter_finished)
            self.assertEqual(visible, ["safe "])
            self.assertEqual(reasoning, ["before"])
            self.assertEqual(stream.final_calls, 0)
            self.assertEqual(client.closed, 0)
        finally:
            release.set()

    async def test_cancel_while_entering_closes_late_created_stream(self):
        entered, release = threading.Event(), threading.Event()
        def enter():
            entered.set()
            if not release.wait(2):
                raise AssertionError("enter was not released")
        client, _, _, stream = self.setup_stream([event(chunk("LATE", "LATE"))], enter_hook=enter)
        visible, reasoning = [], []
        task = asyncio.create_task(self.request(client, visible, reasoning))
        try:
            await self.wait_event(entered)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(stream.closed, 0)
            release.set()
            await self.wait_event(stream.exited)
            self.assertGreaterEqual(stream.closed, 1)
            self.assertEqual(visible, [])
            self.assertEqual(reasoning, [])
            self.assertEqual(stream.final_calls, 0)
            self.assertEqual(client.closed, 0)
        finally:
            release.set()

    async def test_cancel_while_stream_factory_blocks_closes_late_context(self):
        entered, release = threading.Event(), threading.Event()
        visible, reasoning = [], []
        client, api, manager, stream = self.setup_stream([event(chunk("LATE", "LATE"))])
        original = api.stream
        def factory(**kwargs):
            entered.set()
            if not release.wait(2):
                raise AssertionError("stream factory was not released")
            return original(**kwargs)
        api.stream = factory
        task = asyncio.create_task(self.request(client, visible, reasoning))
        try:
            await self.wait_event(entered)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            release.set()
            await self.wait_event(stream.exited)
            self.assertEqual(manager.entered, 1)
            self.assertGreaterEqual(stream.closed, 1)
            self.assertEqual((visible, reasoning), ([], []))
            self.assertEqual(client.closed, 0)
        finally:
            release.set()

    async def test_timeout_while_entering_closes_late_stream(self):
        entered, release = threading.Event(), threading.Event()
        visible, reasoning = [], []
        def enter():
            entered.set()
            if not release.wait(2):
                raise AssertionError("stream enter was not released")
        client, _, _, stream = self.setup_stream([event(chunk("LATE", "LATE"))], enter_hook=enter)
        task = asyncio.create_task(self.request(client, visible, reasoning, timeout=0.1))
        try:
            await self.wait_event(entered)
            with self.assertRaises(TimeoutError):
                await task
            release.set()
            await self.wait_event(stream.exited)
            self.assertGreaterEqual(stream.closed, 1)
            self.assertEqual((visible, reasoning), ([], []))
            self.assertEqual(stream.final_calls, 0)
        finally:
            release.set()

    async def test_timeout_closes_stream_and_drops_late_chunks_without_flush(self):
        entered, release = threading.Event(), threading.Event()
        def hook(_):
            entered.set()
            if not release.wait(2):
                raise AssertionError("worker was not released")
        client, _, _, stream = self.setup_stream([event(chunk("<|tool_calls|>SECRET", "LATE"))], hook=hook)
        visible, reasoning = [], []
        task = asyncio.create_task(self.request(client, visible, reasoning, timeout=0.1))
        try:
            await self.wait_event(entered)
            with patch("bot.agent_stream.ThinkStreamSplitter.flush", side_effect=AssertionError("must not flush")):
                with self.assertRaises(TimeoutError):
                    await task
                release.set()
                await self.wait_event(stream.exited)
            self.assertGreaterEqual(stream.closed, 1)
            self.assertEqual((visible, reasoning), ([], []))
            self.assertEqual(stream.final_calls, 0)
        finally:
            release.set()

    async def test_abort_event_blocks_callbacks_without_waiting_for_task_cancel(self):
        abort = threading.Event()
        def hook(index):
            if index == 1:
                abort.set()
        visible, reasoning = [], []
        client, _, _, stream = self.setup_stream([
            event(chunk("safe", "before")), event(chunk("LATE", "LATE"))], hook=hook)
        with self.assertRaises(asyncio.CancelledError):
            await self.request(client, visible, reasoning, abort=abort)
        self.assertEqual(visible, ["safe"])
        self.assertEqual(reasoning, ["before"])
        await self.wait_event(stream.exited)
        self.assertGreaterEqual(stream.closed, 1)
        self.assertEqual(stream.final_calls, 0)

    async def test_abort_before_request_prevents_create_and_stream(self):
        abort = threading.Event()
        abort.set()
        client, api, manager, stream = self.setup_stream([event(chunk("LATE", "LATE"))])
        with self.assertRaises(asyncio.CancelledError):
            await self.request(client, [], [], abort=abort)
        self.assertEqual(api.stream_calls, [])
        self.assertEqual(api.create_calls, [])
        self.assertEqual(manager.entered, 0)
        self.assertEqual(stream.closed, 0)

    async def test_nonstream_cancel_ignores_worker_result_and_callbacks(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        api = FakeCompletions(final=completion("SECRET " + DSML, reasoning="LATE"))
        def create():
            entered.set()
            if not release.wait(2):
                raise AssertionError("create was not released")
            finished.set()
        api.create_hook = create
        client = FakeClient(api)
        visible, reasoning = [], []
        with self.assertLogs("bot.agent_stream", "WARNING"):
            task = asyncio.create_task(self.request(client, visible, reasoning))
            try:
                await self.wait_event(entered)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                release.set()
                await self.wait_event(finished)
            finally:
                release.set()
        self.assertEqual((visible, reasoning), ([], []))
        self.assertEqual(client.closed, 0)
        self.assertEqual(len(api.create_calls), 1)


class MainContractTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (ROOT / "main.py").read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.source)
        owner = next(node for node in cls.tree.body if isinstance(node, ast.ClassDef)
                     and node.name == "EnhancedLimitedDeepSeekContext")
        method = next(node for node in owner.body if isinstance(node, ast.AsyncFunctionDef)
                      and node.name == "agen_content")
        cls.node = next(node for node in ast.walk(method)
                        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_complete")

    async def test_ast_extracted_complete_passes_public_helper_raw_message_and_usage(self):
        abort = threading.Event()
        visible, reasoning = [], []
        env = {"model": "offline", "timeout_seconds": 2, "model_extra": {"reasoning_effort": "low"},
               "stream_callback": visible.append, "reasoning_callback": reasoning.append,
               "nonlocal_reasoning": {"text": ""}, "abort_event": abort,
               "split_reasoning_output": split_model_output}
        final = completion("正文 " + DSML)
        stream = FakeStream([event(chunk("正文 ", "reason")), event(chunk(DSML))], final)
        api = FakeCompletions(FakeManager(stream))
        env["client"] = FakeClient(api)
        exec(compile(ast.Module(body=[self.node], type_ignores=[]), str(ROOT / "main.py"), "exec"), env)
        tools = [{"type": "function", "function": {"name": "file_read"}}]
        msg, usage = await env["_complete"]([{"role": "user", "content": "offline"}], tools)
        self.assertIs(msg, final.choices[0].message)
        self.assertEqual(msg.content, "正文 " + DSML)
        self.assertEqual(usage, {"total": 9, "prompt": 5, "completion": 4, "cached": 2})
        self.assertEqual(api.stream_calls[0]["tools"], tools)
        self.assertEqual(api.stream_calls[0]["tool_choice"], "auto")
        self.assertEqual(api.stream_calls[0]["reasoning_effort"], "low")
        self.assertEqual("".join(visible), "")
        self.assertEqual(reasoning, ["reason"])
        self.assertEqual(env["nonlocal_reasoning"]["text"], "reason")

    async def test_complete_tool_round_only_commits_visible_text_without_protocol_or_native_calls(self):
        for body, calls, expected in (
            ("我来看看文件", [NS(id="call_1", type="function", function=NS(name="read", arguments="{}"))], ""),
            ("我来看看文件" + DSML, None, ""),
            ("最终回答", None, "最终回答"),
        ):
            with self.subTest(body=body):
                visible = []
                final = completion(body, calls=calls)
                stream = FakeStream([event(chunk(body, "独立思考"))], final)
                env = {"model": "offline", "timeout_seconds": 2, "model_extra": {},
                       "stream_callback": visible.append, "reasoning_callback": None,
                       "nonlocal_reasoning": {"text": ""}, "abort_event": None,
                       "client": FakeClient(FakeCompletions(FakeManager(stream))),
                       "split_reasoning_output": split_model_output}
                exec(compile(ast.Module(body=[self.node], type_ignores=[]), str(ROOT / "main.py"), "exec"), env)
                message, _ = await env["_complete"]([], [{"type": "function", "function": {"name": "read"}}])
                self.assertEqual("".join(visible), expected)
                self.assertEqual(message.content, body)

    async def test_complete_strips_think_keeps_protocol_and_reasoning_once(self):
        for streamed in (True, False):
            with self.subTest(streamed=streamed):
                raw = "<think>private</think>正文 " + DSML
                final = completion(raw)
                visible, reasoning = [], []
                if streamed:
                    stream = FakeStream([event(chunk("<thi")), event(chunk("nk>private</think>正文 ")),
                                         event(chunk(DSML))], final)
                    api = FakeCompletions(FakeManager(stream))
                else:
                    api = FakeCompletions(final=final)
                env = {"model": "offline", "timeout_seconds": 2, "model_extra": {},
                       "stream_callback": visible.append if streamed else None,
                       "reasoning_callback": reasoning.append, "nonlocal_reasoning": {"text": ""},
                       "abort_event": None, "client": FakeClient(api),
                       "split_reasoning_output": split_model_output}
                exec(compile(ast.Module(body=[self.node], type_ignores=[]), str(ROOT / "main.py"), "exec"), env)
                msg, _ = await env["_complete"]([], None)
                self.assertEqual(msg.content, "正文 " + DSML)
                self.assertEqual("".join(reasoning), "private")
                self.assertEqual(env["nonlocal_reasoning"]["text"], "private")
                if streamed:
                    self.assertEqual("".join(visible), "正文 ")

    async def test_complete_closed_only_think_never_leaks_before_final_sanitizing(self):
        visible, reasoning = [], []
        raw = "私密推理</think>最终答案"
        def hook(index):
            if index == 1:
                self.assertEqual(visible, [])
        stream = FakeStream([event(chunk("私密推理")), event(chunk("</thi")),
                             event(chunk("nk>最终答案"))], completion(raw), hook=hook)
        env = {"model": "offline", "timeout_seconds": 2, "model_extra": {},
               "stream_callback": visible.append, "reasoning_callback": reasoning.append,
               "nonlocal_reasoning": {"text": ""}, "abort_event": None,
               "client": FakeClient(FakeCompletions(FakeManager(stream))),
               "split_reasoning_output": split_model_output}
        exec(compile(ast.Module(body=[self.node], type_ignores=[]), str(ROOT / "main.py"), "exec"), env)
        msg, _ = await env["_complete"]([], None)
        self.assertEqual(msg.content, "最终答案")
        self.assertEqual(visible, ["最终答案"])
        self.assertEqual(reasoning, ["私密推理"])
        self.assertEqual(env["nonlocal_reasoning"]["text"], "私密推理")
        self.assertTrue(await asyncio.to_thread(stream.exited.wait, 2))

    def test_source_contract_raw_final_and_no_private_sdk(self):
        source = ast.get_source_segment(self.source, self.node)
        self.assertNotIn("ChatCompletionStreamState", source)
        self.assertNotIn("openai.lib", source)
        self.assertIn("_msg.content = _visible", source)
        self.assertNotIn("inspect_tool_markup(_msg", source)
        self.assertIn("request_chat_completion", source)
        self.assertIn("abort=abort_event", source)
        helper = (ROOT / "bot" / "agent_stream.py").read_text(encoding="utf-8")
        self.assertNotIn("openai.lib", helper)
        self.assertNotIn("ChatCompletionStreamState", helper)
        self.assertNotIn("client.close(", helper)
        tree = ast.parse(helper)
        for node in ast.walk(tree):
            if isinstance(node, ast.Try):
                for cleanup in node.finalbody:
                    self.assertFalse(any(isinstance(call, ast.Attribute) and call.attr == "flush"
                                         for call in ast.walk(cleanup)))


if __name__ == "__main__":
    unittest.main()
