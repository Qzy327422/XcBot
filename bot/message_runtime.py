# -*- coding: utf-8 -*-
"""有界的可见消息索引与请求级回复决策。

本模块不依赖传输层、配置或 main 模块。只有出站发送成功后的记录调用可以传入
``sent_at``，其值必须在调用传输接口之前采集；入站回流不得设置此字段。
平台时间戳不能建立撤回资格。集成层仍须在发送边界复核功能开关、强制拒绝条件、
候选引用及传输回执。
"""

from __future__ import annotations

import copy
import math
import random
import re
import sys
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Iterator
from urllib.parse import urlsplit

SessionKey = tuple[str, bool, str]


def session_key(self_id, is_group, chat_id) -> SessionKey:
    return str(self_id), bool(is_group), str(chat_id)


def _message_id(value) -> str | None:
    """规范化真实的带符号整数 ID，拒绝布尔值、浮点数、UUID 及 CQ 文本。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        try:
            return str(value)
        except ValueError:
            return None
    if not isinstance(value, str) or re.fullmatch(r"-?[0-9]+", value) is None:
        return None
    digits = value.lstrip("-").lstrip("0") or "0"
    return "-" + digits if value.startswith("-") and digits != "0" else digits


def _finite_number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _text(value, limit: int) -> str:
    # 不将二进制缓冲区、传输对象或任意载荷转换成字符串。
    return value[:limit] if isinstance(value, str) else ""


def _media_metadata(media) -> list[dict]:
    """最多保留五条小型公开描述信息，不保留原始传输数据。"""
    if isinstance(media, dict):
        media = [media]
    if not isinstance(media, (list, tuple)):
        return []
    result = []
    for item in media[:5]:
        if not isinstance(item, dict):
            continue
        cleaned = {}
        for name, limit in (("type", 64), ("id", 128), ("summary", 256), ("url", 2048)):
            value = item.get(name)
            if name == "id" and isinstance(value, int) and not isinstance(value, bool):
                value = _message_id(value)
            if not isinstance(value, str) or not value:
                continue
            if name == "url" and len(value) > limit:
                continue
            value = value[:limit]
            if value.lstrip().lower().startswith(("data:", "base64:")):
                continue
            if name == "url":
                try:
                    parsed = urlsplit(value)
                    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
                        continue
                except ValueError:
                    continue
            cleaned[name] = value[:limit]
        if cleaned:
            result.append(cleaned)
    return result


def _deep_size(value, seen: set[int] | None = None) -> int:
    """保守估算驻留 Python 对象的字节数，包含嵌套元数据。"""
    if isinstance(value, (str, int, float, type(None))):
        # 标量按每次出现计费，包括布尔值，避免 True/False 单例去重
        # 导致设置撤回标记时已核算的占用量发生增长。
        return sys.getsizeof(value)
    if seen is None:
        seen = set()
    identity = id(value)
    if identity in seen:
        return 0
    seen.add(identity)
    size = sys.getsizeof(value)
    if isinstance(value, dict):
        size += sum(_deep_size(k, seen) + _deep_size(v, seen) for k, v in value.items())
    elif isinstance(value, (tuple, list)):
        size += sum(_deep_size(v, seen) for v in value)
    return size


@dataclass(slots=True)
class _Entry:
    record: dict
    size: int


@dataclass(slots=True)
class _Session:
    messages: OrderedDict[str, _Entry] = field(default_factory=OrderedDict)
    size: int = 0


class MessageIndex:
    """线程安全的会话 LRU 索引，会话内优先淘汰最早记录的消息。

    记录采用字符串 ID，包含 self_id/is_group/chat_id、message_id、author_id、
    text（最多 256 字）、nickname、timestamp、sent_at、media、reply_to、
    is_bot、known 和 recalled。``known`` 表示作者通过自身身份校验，且显式提供了
    可信出站时间；仅观察到自身回流不能建立此标记。
    所有返回记录均为独立副本，包括嵌套的媒体字典。
    """

    def __init__(self, max_sessions=1000, max_messages=100,
                 max_bytes=64 * 1024 * 1024, clock=time.time):
        for name, value in (("max_sessions", max_sessions), ("max_messages", max_messages),
                            ("max_bytes", max_bytes)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} 必须为非负整数")
        self.max_sessions = max_sessions
        self.max_messages = max_messages
        self.max_bytes = max_bytes
        self._clock = clock
        self._lock = threading.RLock()
        self._sessions: OrderedDict[SessionKey, _Session] = OrderedDict()
        self._total_bytes = 0

    @property
    def total_bytes(self) -> int:
        with self._lock:
            return self._total_bytes

    @property
    def session_count(self) -> int:
        with self._lock:
            return len(self._sessions)

    @staticmethod
    def _session_size(key: SessionKey) -> int:
        return _deep_size(key) + sys.getsizeof(OrderedDict()) + 256

    @staticmethod
    def _entry_size(record: dict) -> int:
        # 同时计入条目对象及 OrderedDict 节点的开销，而非仅计算文本长度。
        return _deep_size(record) + 256

    def _drop_session(self, key: SessionKey) -> None:
        self._total_bytes -= self._sessions.pop(key).size

    def _drop_oldest_message(self, session: _Session, protected_id: str) -> None:
        # 即使是在补充旧消息，也须保留本次刚写入的条目。
        mid = next(mid for mid in session.messages if mid != protected_id)
        entry = session.messages.pop(mid)
        session.size -= entry.size
        self._total_bytes -= entry.size

    def record(self, self_id, is_group, chat_id, message_id, author_id, *,
               text='', nickname='', timestamp=None, sent_at=None, media=None,
               reply_to=None, is_bot=False) -> dict | None:
        mid = _message_id(message_id)
        if mid is None or not self.max_sessions or not self.max_messages or not self.max_bytes:
            return None
        key = session_key(self_id, is_group, chat_id)
        author = str(author_id) if author_id is not None else ""
        own = (is_bot is True and bool(key[0].strip()) and key[0] != "None"
               and bool(author.strip()) and author == key[0])
        trusted_time = _finite_number(sent_at) if own else None
        record = {
            "self_id": key[0], "is_group": key[1], "chat_id": key[2],
            "message_id": mid, "author_id": author,
            "text": _text(text, 256), "nickname": _text(nickname, 128),
            "timestamp": _finite_number(timestamp), "sent_at": trusted_time,
            "media": _media_metadata(media), "reply_to": _message_id(reply_to),
            "is_bot": own, "known": trusted_time is not None, "recalled": False,
        }
        with self._lock:
            session = self._sessions.get(key)
            old = session.messages.get(mid) if session is not None else None
            if old is not None:
                previous = old.record
                # 重复消息可以补充内容，但不能变更原作者。
                if previous["author_id"] and previous["author_id"] != author:
                    self._sessions.move_to_end(key)
                    return copy.deepcopy(previous)
                for name in ("text", "nickname", "media", "reply_to", "timestamp"):
                    if record[name] is None or record[name] == "" or record[name] == []:
                        record[name] = previous[name]
                record["is_bot"] = previous["is_bot"] or own
                if previous["sent_at"] is not None:
                    record["sent_at"] = previous["sent_at"]
                    if trusted_time is not None:
                        record["sent_at"] = min(record["sent_at"], trusted_time)
                record["known"] = record["is_bot"] and record["sent_at"] is not None
                record["recalled"] = previous["recalled"]
            size = self._entry_size(record)
            if self._session_size(key) + size > self.max_bytes:
                return None  # 单条超大记录不得挤出无关会话。
            if session is None:
                session = _Session(size=self._session_size(key))
                self._sessions[key] = session
                self._total_bytes += session.size
            delta = size - (old.size if old is not None else 0)
            session.messages[mid] = _Entry(record, size)
            session.size += delta
            self._total_bytes += delta
            self._sessions.move_to_end(key)
            while len(session.messages) > self.max_messages or session.size > self.max_bytes:
                self._drop_oldest_message(session, mid)
            while len(self._sessions) > self.max_sessions or self._total_bytes > self.max_bytes:
                self._drop_session(next(iter(self._sessions)))
            return copy.deepcopy(record)

    def snapshot(self, self_id, is_group, chat_id, limit=30, before_id=None) -> list[dict]:
        """按观察顺序返回最近 ``limit`` 条可见记录，顺序为旧到新。

        before_id 是不包含自身的分页锚点；未知或无效时返回空列表。
        不按 ID 数值范围查询，因为平台真实 ID 不一定单调递增。
        结果有意保留撤回标记记录，工具层必须拒绝使用已撤回消息。
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            return []
        anchor = _message_id(before_id) if before_id is not None else None
        if before_id is not None and anchor is None:
            return []
        key = session_key(self_id, is_group, chat_id)
        with self._lock:
            session = self._sessions.get(key)
            if session is None or (anchor is not None and anchor not in session.messages):
                return []
            self._sessions.move_to_end(key)
            records = []
            for mid, entry in session.messages.items():
                if mid == anchor:
                    break
                records.append(entry.record)
            return copy.deepcopy(records[-min(limit, self.max_messages):])

    def resolve(self, self_id, is_group, chat_id, message_id) -> dict | None:
        mid = _message_id(message_id)
        key = session_key(self_id, is_group, chat_id)
        with self._lock:
            session = self._sessions.get(key)
            entry = session.messages.get(mid) if session is not None else None
            if entry is None:
                return None
            self._sessions.move_to_end(key)
            return copy.deepcopy(entry.record)

    def tombstone(self, self_id, is_group, chat_id, message_id) -> bool:
        mid = _message_id(message_id)
        key = session_key(self_id, is_group, chat_id)
        with self._lock:
            session = self._sessions.get(key)
            entry = session.messages.get(mid) if session is not None else None
            if entry is None:
                return False
            entry.record["recalled"] = True
            size = self._entry_size(entry.record)
            delta = size - entry.size
            entry.size = size
            session.size += delta
            self._total_bytes += delta
            self._sessions.move_to_end(key)
            return True

    def clear(self, self_id, is_group, chat_id) -> None:
        key = session_key(self_id, is_group, chat_id)
        with self._lock:
            if key in self._sessions:
                self._drop_session(key)

    def clear_all(self) -> int:
        """原子清空所有账号、群聊和私聊的索引，返回清理的消息记录数。

        返回数量包含已撤回记录；重复清理空索引返回 0。
        清理后不保留任何会话，字节计数归零，旧消息不再具备索引撤回资格。
        """
        with self._lock:
            count = sum(len(session.messages) for session in self._sessions.values())
            self._sessions.clear()
            self._total_bytes = 0
            return count

    def can_recall(self, self_id, is_group, chat_id, message_id, now=None) -> tuple[dict | None, str | None]:
        """返回（独立记录副本，None）或（None，稳定的错误码）。

        此处仅判断撤回资格，不授予删除权限；调用方仍须检查功能开关、
        校验删除回执，并在删除成功后设置撤回标记。错误码保持英文标识，
        供集成层稳定判断；面向用户的提示由集成层转换。
        """
        if _message_id(message_id) is None:
            return None, "invalid_message_id"
        with self._lock:
            record = self.resolve(self_id, is_group, chat_id, message_id)
            if record is None:
                return None, "message_not_found"
            if record["recalled"]:
                return None, "already_recalled"
            if (not str(self_id or "").strip() or str(self_id) == "None"
                    or not record["author_id"].strip() or not record["is_bot"]
                    or record["author_id"] != str(self_id)):
                return None, "not_bot_message"
            if not record["known"] or record["sent_at"] is None:
                return None, "unknown_sent_at"
            current = _finite_number(self._clock() if now is None else now)
            if current is None:
                return None, "invalid_now"
            if not 0 <= current - record["sent_at"] <= 120:
                return None, "outside_recall_window"
            return record, None


class ReplySuppressed(Exception):
    """预期内的静默取消信号；main 应捕获此异常，不输出错误消息。"""

    silent = True

    def __init__(self, reason: str = "reply_suppressed"):
        self.reason = reason
        super().__init__(reason)


class ReplyDecision:
    """管理一次逻辑请求的惰性随机样本、主体冻结及不可逆拒绝状态。

    普通请求的主体不可更换；主动私聊请求默认以目标用户为主体。
    主动群聊请求可以从无主体开始，仅在选择他人消息并传入 freeze_actor=True
    时绑定主体。引用自身不能替换已绑定的主体，也不能豁免已被拒绝的请求。
    """

    def __init__(self, self_id, is_group, chat_id, *, actor_id='', source='normal',
                 event_id='', sample=None, random_fn=random.random):
        self.key = session_key(self_id, is_group, chat_id)
        self.self_id, self.is_group, self.chat_id = self.key
        self.source = source
        self.event_id = str(event_id)
        self._group_proactive = source == "proactive" and self.is_group
        actor = str(actor_id) if actor_id is not None else ""
        if source == "proactive" and not self.is_group and not actor:
            actor = self.chat_id
        self._actor_id = actor
        self._actor_frozen = not self._group_proactive or bool(actor and actor != self.self_id)
        self._sample = self._validate_sample(sample) if sample is not None else None
        self._random_fn = random_fn
        self._denied = False
        self._lock = threading.RLock()

    @staticmethod
    def _validate_sample(value) -> float:
        value = _finite_number(value)
        if value is None or not 0 <= value < 1:
            raise ValueError("随机样本必须位于 [0, 1) 区间")
        return value

    @property
    def actor_id(self) -> str:
        with self._lock:
            return self._actor_id

    @property
    def actor_frozen(self) -> bool:
        with self._lock:
            return self._actor_frozen

    @property
    def sample(self) -> float | None:
        with self._lock:
            return self._sample

    @property
    def denied(self) -> bool:
        with self._lock:
            return self._denied

    def check(self, enabled, weak_users, probability, *, actor_id=None, freeze_actor=False) -> bool:
        with self._lock:
            if self._denied:
                return False
            candidate = str(actor_id) if actor_id is not None else ""
            if self._group_proactive:
                if candidate and candidate != self.self_id:
                    if self._actor_frozen and candidate != self._actor_id:
                        return False
                    if not self._actor_frozen:
                        if not freeze_actor:
                            return False  # 必须在选择引用时显式绑定主体。
                        self._actor_id = candidate
                        self._actor_frozen = True
            elif actor_id is not None and candidate != self._actor_id:
                return False
            actor = self._actor_id
            if not enabled or not actor or actor == self.self_id:
                return True
            users = (weak_users,) if isinstance(weak_users, (str, int)) else (weak_users or ())
            if not any(str(user) == actor for user in users if not isinstance(user, bool)):
                return True
            probability = _finite_number(probability)
            if probability is None or not 0 <= probability <= 1:
                self._denied = True  # 生效策略无效时安全拒绝，不抽取随机数。
                return False
            if probability == 1:
                return True
            if probability == 0:
                self._denied = True
                return False
            if self._sample is None:
                self._sample = self._validate_sample(self._random_fn())
            if self._sample < probability:
                return True
            self._denied = True
            return False


@dataclass
class RequestState:
    """显式传递的请求状态；待引用目标不得存放在共享会话上。

    ``records`` 为请求私有快照，不是共享索引。候选有效性及作者弱名单检查
    仍由集成层负责。跨线程应使用 pending_reply / consume_once，
    不要直接修改 reply_id。social 仅保存集成层上下文的引用。
    """

    self_id: str
    is_group: bool
    chat_id: str
    decision: ReplyDecision
    event: object = None
    actions: object = None
    handled: bool = False
    observation_ticket: object = None
    normal_reply_sent: bool = False
    normal_reply_bookkeeping_failed: bool = False
    social: object = None
    quoted_text: str = ''
    image_urls: list[str] = field(default_factory=list)
    reply_id: str | None = None
    allowed_reply_ids: set[str] = field(default_factory=set)
    records: dict[str, dict] = field(default_factory=dict)
    wait_seconds: float | None = None
    finish: bool = False
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False, compare=False)

    def __post_init__(self):
        self.self_id, self.is_group, self.chat_id = self.key
        if self.key != self.decision.key:
            raise ValueError("请求会话键必须与决策会话键一致")
        self.allowed_reply_ids = {mid for value in self.allowed_reply_ids
                                  if (mid := _message_id(value)) is not None}
        self.records = copy.deepcopy(self.records)
        self.image_urls = list(self.image_urls)
        if self.reply_id is not None:
            self.pending_reply = self.reply_id

    @property
    def key(self) -> SessionKey:
        return session_key(self.self_id, self.is_group, self.chat_id)

    @property
    def pending_reply(self) -> str | None:
        with self._lock:
            return self.reply_id

    @pending_reply.setter
    def pending_reply(self, value) -> None:
        mid = _message_id(value) if value is not None else None
        if value is not None and mid is None:
            raise ValueError("待引用目标必须为真实消息 ID")
        with self._lock:
            self.reply_id = mid

    def select_reply(self, message_id) -> bool:
        mid = _message_id(message_id)
        with self._lock:
            if mid is None or mid not in self.allowed_reply_ids:
                return False
            self.reply_id = mid
            return True

    def consume_once(self) -> str | None:
        with self._lock:
            reply_id = self.reply_id
            self.reply_id = None
            return reply_id


INDEX = MessageIndex()
CURRENT_REQUEST: ContextVar[RequestState | None] = ContextVar("current_request", default=None)
current_request = CURRENT_REQUEST


@contextmanager
def bind_request(state: RequestState | None) -> Iterator[RequestState | None]:
    """绑定请求状态，退出时即使发生异常也恢复；线程须显式传入并绑定状态。

    异步任务按 ContextVar 的常规语义继承上下文；每个独立逻辑请求应绑定独立的
    RequestState。显式共享同一状态时，其原子待引用槽与决策也会共享，
    适用于同一请求内部的工作线程。
    """
    token = CURRENT_REQUEST.set(state)
    try:
        yield state
    finally:
        CURRENT_REQUEST.reset(token)
