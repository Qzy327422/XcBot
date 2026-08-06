# -*- coding: utf-8 -*-
"""群聊上下文感知（旁听缓冲）。

对齐 AstrBot group_icl：
- 记录群内普通消息到内存环形缓冲
- 触发 LLM 时注入「当前触发消息之前」的旁听内容
- 注入后消费，避免下次重复发送
- 旁听块只影响当次请求，不写入对话持久化历史
"""

from __future__ import annotations

import datetime
import threading
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass

DEFAULT_GROUP_MESSAGE_MAX_CNT = 30

GROUP_HISTORY_HEADER = (
    "<system_reminder>"
    "You are in a group chat. "
    "Belows are group chat context after your last reply:\n"
    "--- BEGIN CONTEXT---\n"
)
GROUP_HISTORY_FOOTER = "\n--- END CONTEXT ---\n</system_reminder>"

_MAX_REPLY_TEXT_LENGTH = 200


def positive_int(value, fallback: int = DEFAULT_GROUP_MESSAGE_MAX_CNT) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback


def format_group_message(
    nickname: str,
    text: str = "",
    *,
    has_image: bool = False,
    at_bot: bool = False,
    quote_preview: str | None = None,
    now: datetime.datetime | None = None,
) -> str:
    """格式化为旁听缓冲条目。"""
    ts = (now or datetime.datetime.now()).strftime("%H:%M:%S")
    nick = str(nickname or "用户").strip() or "用户"
    parts = [f"[{nick}/{ts}]: "]
    if at_bot:
        parts.append("⚠️[DIRECTED AT YOU] ")

    cleaned = str(text or "").strip()
    if cleaned:
        parts.append(f" {cleaned}")
    if has_image:
        parts.append(" [Image]")
    if quote_preview:
        preview = _truncate_reply_text(str(quote_preview).strip())
        if preview:
            parts.append(f" [Quote: {preview}]")
        else:
            parts.append(" [Quote]")
    return "".join(parts)


def format_history_block(records: list[str]) -> str:
    if not records:
        return ""
    return GROUP_HISTORY_HEADER + "\n".join(records) + GROUP_HISTORY_FOOTER


def _truncate_reply_text(text: str) -> str:
    if len(text) <= _MAX_REPLY_TEXT_LENGTH:
        return text
    return text[:_MAX_REPLY_TEXT_LENGTH] + "..."


def _trim_left(
    records: deque[str],
    max_records: int,
    record_ids: deque[str] | None = None,
) -> None:
    while len(records) > max_records:
        records.popleft()
        if record_ids is not None and record_ids:
            record_ids.popleft()


@dataclass(frozen=True)
class GroupContextReservation:
    """一次请求暂存的旁听内容，成功确认或最终失败时恢复。"""

    token: str
    key: str
    records: tuple[str, ...]
    generation: int
    max_cnt: int


class GroupChatContextManager:
    """线程安全的群聊旁听缓冲管理器。"""

    def __init__(self, default_max_cnt: int = DEFAULT_GROUP_MESSAGE_MAX_CNT) -> None:
        self._default_max_cnt = positive_int(default_max_cnt, DEFAULT_GROUP_MESSAGE_MAX_CNT)
        self._lock = threading.RLock()
        self._records: dict[str, deque[str]] = defaultdict(deque)
        self._record_ids: dict[str, deque[str]] = defaultdict(deque)
        self._generations: dict[str, int] = defaultdict(int)
        self._reservations: dict[str, GroupContextReservation] = {}

    @staticmethod
    def _key(group_id) -> str:
        return str(group_id)

    def record(self, group_id, formatted_text: str, max_cnt: int | None = None) -> str:
        """追加一条旁听消息，返回 record_id。"""
        text = str(formatted_text or "").strip()
        if not text:
            return ""
        key = self._key(group_id)
        limit = positive_int(max_cnt, self._default_max_cnt) if max_cnt is not None else self._default_max_cnt
        record_id = uuid.uuid4().hex
        with self._lock:
            records = self._records[key]
            record_ids = self._record_ids[key]
            records.append(text)
            record_ids.append(record_id)
            _trim_left(records, limit, record_ids)
        return record_id

    def reserve_for_inject(self, group_id, record_id: str | None = None,
                           max_cnt: int | None = None) -> GroupContextReservation | None:
        """暂存当前触发条之前的旁听，待 LLM 成功后确认或失败后恢复。"""
        key = self._key(group_id)
        limit = positive_int(max_cnt, self._default_max_cnt) if max_cnt is not None else self._default_max_cnt
        with self._lock:
            records = self._records.get(key)
            if not records:
                return None

            raw_list = list(records)
            id_list = list(self._record_ids.get(key, deque()))
            if not isinstance(record_id, str) or not record_id or record_id not in id_list:
                if record_id:
                    print(f"[GroupContext] 触发记录已被裁剪，跳过旁听注入: group={key}")
                return None

            prompt_idx = id_list.index(record_id)
            if prompt_idx >= len(raw_list):
                return None

            to_inject = tuple(raw_list[:prompt_idx])
            remaining = raw_list[prompt_idx + 1 :]
            remaining_ids = id_list[prompt_idx + 1 :]
            records.clear()
            records.extend(remaining)
            stored_ids = self._record_ids[key]
            stored_ids.clear()
            stored_ids.extend(remaining_ids)

            generation = self._generations[key] + 1
            self._generations[key] = generation
            reservation = GroupContextReservation(
                token=uuid.uuid4().hex,
                key=key,
                records=to_inject,
                generation=generation,
                max_cnt=limit,
            )
            self._reservations[reservation.token] = reservation
            return reservation

    def commit(self, reservation: GroupContextReservation | None) -> bool:
        """确认 LLM 已处理暂存的旁听。"""
        if reservation is None:
            return False
        with self._lock:
            return self._reservations.pop(reservation.token, None) is not None

    def rollback(self, reservation: GroupContextReservation | None) -> bool:
        """请求最终失败时恢复旁听；后续已有消费时不恢复过期内容。"""
        if reservation is None:
            return False
        with self._lock:
            pending = self._reservations.pop(reservation.token, None)
            if pending is None:
                return False
            if self._generations.get(pending.key, 0) != pending.generation:
                print(f"[GroupContext] 已有后续触发消费，放弃恢复过期旁听: group={pending.key}")
                return False
            if not pending.records:
                return True
            records = self._records[pending.key]
            for text in reversed(pending.records):
                records.appendleft(text)
            # 已恢复的记录不需要再定位为触发条，使用空标识与文本队列保持对齐。
            record_ids = self._record_ids[pending.key]
            for _ in pending.records:
                record_ids.appendleft("")
            _trim_left(records, pending.max_cnt, record_ids)
            return True

    def consume_for_inject(self, group_id, record_id: str | None = None) -> list[str]:
        """兼容旧调用：立即确认消费并返回旁听记录。"""
        reservation = self.reserve_for_inject(group_id, record_id)
        if reservation is None:
            return []
        self.commit(reservation)
        return list(reservation.records)

    def discard(self, group_id, record_id: str | None) -> bool:
        """仅删除指定 record，保留其前后条目。

        用于 Follow-Up 等「已记录但不走 agen_content 注入」的路径，
        避免触发条本身残留到下一次正式注入。
        """
        if not isinstance(record_id, str) or not record_id:
            return False
        key = self._key(group_id)
        with self._lock:
            record_ids = self._record_ids.get(key)
            records = self._records.get(key)
            if not record_ids or not records:
                return False
            try:
                idx = list(record_ids).index(record_id)
            except ValueError:
                return False
            # deque 不支持按索引删，重建一次（缓冲很短，成本可忽略）
            new_records = deque()
            new_ids = deque()
            for i, (text, rid) in enumerate(zip(list(records), list(record_ids))):
                if i == idx:
                    continue
                new_records.append(text)
                new_ids.append(rid)
            records.clear()
            records.extend(new_records)
            record_ids.clear()
            record_ids.extend(new_ids)
            return True

    def clear(self, group_id) -> int:
        key = self._key(group_id)
        with self._lock:
            cnt = len(self._records.get(key, ()))
            self._records.pop(key, None)
            self._record_ids.pop(key, None)
            self._generations[key] += 1
            for token, reservation in list(self._reservations.items()):
                if reservation.key == key:
                    self._reservations.pop(token, None)
            return cnt

    def clear_all(self) -> int:
        with self._lock:
            cnt = sum(len(records) for records in self._records.values())
            keys = set(self._records) | set(self._generations)
            self._records.clear()
            self._record_ids.clear()
            for key in keys:
                self._generations[key] += 1
            self._reservations.clear()
            return cnt

    def size(self, group_id) -> int:
        key = self._key(group_id)
        with self._lock:
            return len(self._records.get(key, ()))


# 进程级单例，供 main.py 直接使用
group_chat_context = GroupChatContextManager()
