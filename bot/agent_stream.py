"""单次公开 Chat Completions 流的展示与生命周期；不解析或执行工具参数。"""
from __future__ import annotations

import asyncio
import logging
import queue
import re
import threading
import time

from .agent_protocol import inspect_tool_markup, markup_prefix_pending
from .reasoning import ThinkStreamSplitter, extract_field_reasoning, split_model_output

_LOG = logging.getLogger(__name__)


class VisibleTextGuard:
    """使用同一纯协议检查器，只放行不可撤回的正文前缀。"""

    def __init__(self, inspect_markup=inspect_tool_markup):
        self._inspect = inspect_markup
        self._raw = ""
        self._sent = ""
        self._blocked = False

    def feed(self, text: str) -> str:
        self._raw += text
        safe = self._raw
        if markup_prefix_pending(safe):
            safe = safe[:safe.rfind("<")]
        _cleaned, detected = self._inspect(safe)
        if detected:
            # 闭合反引号/引号可能在后续 chunk 才到：不能提前认作真调用，
            # 也不能先发清洗后缀再重发正文。只放行首个 '<' 前的原文，
            # 歧义处等引用闭合或正常完成后再由共享检查器处理。
            prefix = safe.split("<", 1)[0]
            return self._advance(prefix) if prefix.startswith(self._sent) else ""
        return self._advance(safe)

    def finish(self) -> str:
        """仅在 SDK 成功给出最终 completion 后调用，不用于异常收尾。"""
        return self._advance(self._raw)

    def _advance(self, text: str) -> str:
        if self._blocked:
            return ""
        cleaned, _detected = self._inspect(text)
        if not cleaned.startswith(self._sent):
            # 已发前缀不可撤销；绝不能从头重发 cleaned 来“修复”它。
            self._blocked = True
            _LOG.warning("工具协议清洗改变了已发送前缀，停止本次流式正文展示；最终消息保留原文")
            return ""
        delta = cleaned[len(self._sent):]
        self._sent = cleaned
        return delta


class _InitialThinkGuard:
    """初始 prefix 可能是缺失开标签的推理，判定前不交给正文 splitter。"""

    _BOUNDARY = re.compile(r"<(?P<close>/?)(?:think|thinking|thought)>", re.IGNORECASE)

    def __init__(self):
        self._pending = ""
        self._decided = False
        self._splitter = ThinkStreamSplitter()

    def feed(self, content, *, field_reasoning=False):
        if self._decided:
            return self._splitter.feed(content)
        self._pending += content
        boundary = self._BOUNDARY.search(self._pending)
        if boundary is None and not field_reasoning:
            return "", ""
        self._decided = True
        pending, self._pending = self._pending, ""
        if boundary is not None and boundary.group("close"):
            # 首个边界是闭标签：此前全部是推理，即使早几个 chunk 已经到达。
            visible, reasoning = self._splitter.feed(pending[boundary.end():])
            return visible, pending[:boundary.start()] + reasoning
        return self._splitter.feed(pending)

    def finish(self):
        # 无明确字段/think 边界的普通模型牺牲逐 token 展示；成功完成后
        # 使用与非流式相同的分类规则，不靠最终 content 赋值补救已泄漏正文。
        if not self._decided:
            pending, self._pending = self._pending, ""
            return split_model_output(None, raw_content=pending)
        return self._splitter.flush()


# 不用默认 executor 关流：close 可能永不返回，不能拖住 loop/asyncio.run 退出，
# 也不能每次取消都新建线程。固定两个 daemon；创建流前预留 64 个清理名额，
# 活动流+正在关闭+排队的总数有界。耗尽时拒绝新流，不丢资源也不在 loop 自关。
_CLOSE_QUEUE = queue.Queue(maxsize=64)
_CLOSE_SLOTS = threading.BoundedSemaphore(64)
_CLOSE_WORKERS_LOCK = threading.Lock()
_CLOSE_WORKERS_STARTED = False


def _close_worker():
    while True:
        close = _CLOSE_QUEUE.get()
        try:
            close()
        except BaseException:
            _LOG.warning("关闭本次 chat completion stream 失败", exc_info=True)
        finally:
            _CLOSE_SLOTS.release()
            _CLOSE_QUEUE.task_done()
            del close


def _reserve_close():
    if not _CLOSE_SLOTS.acquire(blocking=False):
        _LOG.error("本次 stream 被拒绝：64 个流清理名额已耗尽，可能有 close 长期阻塞")
        raise RuntimeError("流清理队列已满，本次未创建 stream，请等待已挂起的流关闭")


def _schedule_close(close):
    global _CLOSE_WORKERS_STARTED
    with _CLOSE_WORKERS_LOCK:
        if not _CLOSE_WORKERS_STARTED:
            for index in range(2):
                threading.Thread(target=_close_worker, name=f"agent-stream-close-{index}",
                                 daemon=True).start()
            _CLOSE_WORKERS_STARTED = True
    _CLOSE_QUEUE.put_nowait(close)  # 已持有 admission 名额，容量必然足够


class _RequestGate:
    """锁只保护派发许可/生命周期；绝不在锁内执行外部 callback 或 close。"""

    def __init__(self, timeout, abort=None):
        self._lock = threading.Lock()
        self._active = True
        self._close = None
        self._close_scheduled = False
        self._abort = abort
        self._deadline = time.monotonic() + timeout if timeout is not None else None

    def _allowed(self):
        return (self._active
                and not (self._abort is not None and self._abort.is_set())
                and (self._deadline is None or time.monotonic() < self._deadline))

    def check(self):
        with self._lock:
            if not self._active or (self._abort is not None and self._abort.is_set()):
                raise asyncio.CancelledError()
            if self._deadline is not None and time.monotonic() >= self._deadline:
                raise TimeoutError("Chat completion timed out")

    def emit(self, callback, text):
        if text and callable(callback):
            with self._lock:
                allowed = self._allowed()
            # 许可是派发边界：已获许可/已经进入的外部回调无法撤销或杀线程。
            # stop 不等待它；之后每次派发都必须重新检查许可。
            if allowed:
                callback(text)

    def attach(self, manager):
        def close():
            # 公开 manager.__exit__ 负责关闭本次 stream；只入队一次，避免
            # worker 收尾与取消路径同时对同一响应执行两次阻塞 close。
            manager.__exit__(None, None, None)

        with self._lock:
            self._close = close
            allowed = self._allowed()
        if not allowed:
            self.close_stream()
        return allowed

    def close_stream(self):
        with self._lock:
            if self._close_scheduled or self._close is None:
                return
            self._close_scheduled = True
            close, self._close = self._close, None
        _schedule_close(close)

    def stop(self):
        with self._lock:
            self._active = False
        self.close_stream()


def _repair_tool_indexes(chunk):
    """保留公开 chunk 原对象可见时的位置补齐，不触碰 SDK 私有状态。"""
    repaired = False
    for choice in getattr(chunk, "choices", None) or []:
        for index, call in enumerate(getattr(choice.delta, "tool_calls", None) or []):
            if getattr(call, "index", None) is None:
                try:
                    call.index = index
                except Exception as exc:
                    raise RuntimeError("tool_call.index 缺失且公开 chunk 不可修改") from exc
                repaired = True
    if repaired:
        _LOG.warning(
            "已补齐公开 chunk 的 tool_call.index；公开 SDK 在 chunk 事件前已聚合，"
            "此补齐无法保证修复此前的聚合，请兼容接口提供 index 或改用非流式请求"
        )


async def request_chat_completion(client, kwargs, *, timeout, stream_callback=None,
                                  reasoning_callback=None, abort=None, inspect_markup=None):
    """返回 SDK 原始 completion；仅展示层 clean，取消后不再许可新回调。

    .stream() 缺失才回退本次非流式请求。SDK 聚合异常不重放请求，避免
    重复展示已发送前缀。阻塞 create/enter/已派发 callback 不可杀，stop 不等它们。
    无明确独立推理字段时先缓冲到首个 think 边界或完整响应，安全优先于首字延迟。
    """
    gate = _RequestGate(timeout, abort)
    completions = client.chat.completions
    public_stream = getattr(completions, "stream", None)
    use_stream = callable(stream_callback) and callable(public_stream)
    if callable(stream_callback) and not use_stream:
        _LOG.warning("公开 client.chat.completions.stream 不可用，本次明确回退非流式 create")
    if inspect_markup is None:
        inspect_markup = inspect_tool_markup

    def worker():
        gate.check()
        if not use_stream:
            create_kwargs = dict(kwargs, stream=False)
            response = completions.create(**create_kwargs)
            gate.check()
            if getattr(response, "choices", None):
                visible, reasoning = split_model_output(response.choices[0].message)
                gate.emit(reasoning_callback, reasoning)
                if callable(stream_callback):
                    guard = VisibleTextGuard(inspect_markup)
                    # 非流式也只清洗展示副本，不改最终 message。
                    gate.emit(stream_callback, guard.feed(visible))
                    gate.emit(stream_callback, guard.finish())
            return response

        stream_kwargs = dict(kwargs)
        stream_kwargs.pop("stream", None)  # 公开 .stream() 没有 stream= 参数
        think_guard = _InitialThinkGuard()
        guard = VisibleTextGuard(inspect_markup)

        def emit(visible, reasoning):
            gate.emit(stream_callback, guard.feed(visible))
            gate.emit(reasoning_callback, reasoning)

        # 使用公开 context manager 生命周期，但退出交给有界 daemon 工作者，
        # 不让阻塞的 __exit__/close 卡住请求 worker 或取消所在的事件循环。
        _reserve_close()
        try:
            gate.check()
            manager = public_stream(**stream_kwargs)
            stream = manager.__enter__()
        except BaseException:
            _CLOSE_SLOTS.release()  # 尚未成功进入公开 context manager，没有流可移交
            raise
        attached = gate.attach(manager)
        try:
            gate.check()
            if not attached:
                raise asyncio.CancelledError()
            iterator = iter(stream)
            while True:
                gate.check()
                try:
                    event = next(iterator)
                except StopIteration:
                    break
                except (TypeError, KeyError, IndexError) as exc:
                    raise RuntimeError(
                        "公开 SDK 流聚合失败（兼容接口可能缺少 tool_call.index）；"
                        "chunk 事件发生在 SDK 聚合之后，无法在此提前补齐。"
                        "未重放请求或使用私有 SDK，请让接口提供 index 或使用非流式"
                    ) from exc
                gate.check()
                # 只消费原始 chunk，忽略 content.delta / arguments.delta 等派生
                # 事件，既避免双发，也绝不把部分工具参数猜成可执行调用。
                if getattr(event, "type", None) != "chunk":
                    continue
                chunk = event.chunk
                _repair_tool_indexes(chunk)
                choice = chunk.choices[0] if getattr(chunk, "choices", None) else None
                delta = getattr(choice, "delta", None)
                field_reasoning = extract_field_reasoning(delta, strip=False)
                gate.emit(reasoning_callback, field_reasoning)
                content = getattr(delta, "content", None)
                if isinstance(content, list):
                    content = "".join(str(item.get("text", "") or "")
                                      for item in content if isinstance(item, dict))
                if isinstance(content, str) or field_reasoning:
                    emit(*think_guard.feed(content if isinstance(content, str) else "",
                                           field_reasoning=bool(field_reasoning)))
            gate.check()
            response = stream.get_final_completion()
            gate.check()
            # 只有正常完成才能放出 splitter/初始正文尾部；finally 绝不 flush。
            emit(*think_guard.finish())
            gate.emit(stream_callback, guard.finish())
            return response
        finally:
            gate.close_stream()
        # 仅关闭本次 context manager/stream，从不关闭共享 client。

    try:
        response = await asyncio.wait_for(asyncio.to_thread(worker), timeout=timeout)
        gate.check()
        return response
    finally:
        # wait_for/cancel 不会杀工作线程，必须先原子封住所有晚到回调。
        gate.stop()
