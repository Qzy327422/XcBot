# -*- coding: utf-8 -*-
"""Bounded, content-addressed image sidecars. Never read paths from history.

Only already-downloaded data URLs enter the store; this module performs no network
requests. Bytes are scoped to account/session and live only as long as retained
history references. Request reconstruction sends the latest image-bearing round,
not every stored image on every turn.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import tempfile
import threading
from copy import deepcopy
from pathlib import Path

MAX_IMAGE_BYTES = 8 * 1024 * 1024
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_DATA = re.compile(r"data:(image/[\w.+-]+);base64,([A-Za-z0-9+/=]+)\Z")


def image_mime(raw: bytes) -> str:
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if raw.startswith(b"BM"):
        return "image/bmp"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return ""


def content_text(content) -> str:
    if isinstance(content, list):
        return "\n".join(str(block.get("text") or "") for block in content
                         if isinstance(block, dict) and block.get("type") == "text")
    return str(content or "")


def clean_content(content):
    """Preserve native blocks; strip inline bytes without flattening the list."""
    if isinstance(content, list):
        result = []
        for block in content:
            if not isinstance(block, dict):
                continue
            item = deepcopy(block)
            if item.get("type") == "image_url":
                image = item.get("image_url")
                url = image.get("url", "") if isinstance(image, dict) else ""
                if not isinstance(url, str) or not url.startswith(("https://", "http://")):
                    result.append({"type": "text", "text": "[图片]"})
                    continue
            if item.get("type") == "text":
                item["text"] = clean_content(item.get("text", ""))
            result.append(item)
        return result
    if isinstance(content, str):
        return re.sub(r"data:image/[^;\s]+;base64,[A-Za-z0-9+/=]+", "[图片]", content)
    return content


def scope_for(context, state=None) -> str:
    account = str(getattr(state, "self_id", "") or getattr(context, "_image_account", ""))
    if not account:
        accounts = {str(ref["self_id"]) for ref in refs_from(getattr(context, "history", []))
                    if ref.get("self_id")}
        if len(accounts) == 1:
            account = accounts.pop()
    if account:
        context._image_account = account
    kind = str(getattr(context, "context_type", ""))
    chat = str(getattr(context, "chat_id", "") or getattr(context, "session_id", "AI"))
    return hashlib.sha256(json.dumps([account, kind, chat]).encode()).hexdigest()


def refs_from(messages) -> list[dict]:
    return [ref for msg in messages if isinstance(msg, dict)
            for ref in (msg.get("image_refs") or []) if isinstance(ref, dict)]


def history_signature(history) -> str:
    return hashlib.sha256(json.dumps(history, sort_keys=True, ensure_ascii=False,
                                     default=str).encode()).hexdigest()


def ref_visible(ref, state=None, *, visible=None, index=None) -> bool:
    """Scope + live blacklist/recall checks; no lookup outside the bound session."""
    if not isinstance(ref, dict) or (visible is not None and not visible(ref)):
        return False
    if state is not None:
        for name in ("self_id", "chat_id", "is_group"):
            if name in ref and str(ref[name]) != str(getattr(state, name, "")):
                return False
    if index is not None and ref.get("message_id") and ref.get("self_id"):
        row = index.resolve(ref["self_id"], ref.get("is_group", False),
                            ref.get("chat_id", ""), ref["message_id"])
        if row and row.get("recalled"):
            return False
        if row and ref.get("author_id") and str(row.get("author_id")) != str(ref["author_id"]):
            return False
    return True


def _metadata_for_url(state, url, source, index=None):
    if state is None:
        return {"source": source}
    metadata = {name: getattr(state, name) for name in ("self_id", "chat_id", "is_group")}
    event = getattr(state, "event", None)
    metadata.update(message_id=str(getattr(event, "message_id", "") or ""),
                    author_id=str(getattr(event, "user_id", "") or ""), source=source)
    # Quote parser has already verified these scoped records. Match URL to actual
    # source, not to the trigger author (important for blacklist/recall changes).
    rows = list((getattr(state, "records", {}) or {}).values())
    if index is not None:
        rows += index.snapshot(state.self_id, state.is_group, state.chat_id, limit=100)
    for row in reversed(rows):
        if any(item.get("url") == url for item in row.get("media", []) if isinstance(item, dict)):
            metadata.update(message_id=row.get("message_id", ""), author_id=row.get("author_id", ""))
            break
    return metadata


def capture_image_sources(urls, state, index=None):
    """在 Follow-Up 入队边界冻结当前图及已验证引用图的真实来源。"""
    from bot.message_runtime import ImageSource
    if state is None:
        return ()
    return tuple(ImageSource(url=url, **{name: str(metadata.get(name, ""))
                 for name in ("self_id", "chat_id", "author_id", "message_id")},
                 is_group=bool(metadata.get("is_group")))
                 for url in urls if isinstance(url, str) and url
                 for metadata in [_metadata_for_url(state, url, "follow_up", index)])


async def prepare_turn_images(context, message, store, *, download, relay,
                              supports_multimodal, direct=False, state=None,
                              visible=None, index=None):
    """Download current/quoted/all + at most one eavesdrop image, once per request.

    Returns text, model data URLs, sidecar refs, (total,prompt,completion).
    Failed downloads/relay are explicit text fallbacks, never external model URLs.
    The request-owned cache survives model retries and is never written to JSON.
    """
    text = str(message.get("text") or "")
    scope = scope_for(context, state)
    cache = message.setdefault("_image_turn_cache", {})
    if cache.get("scope") != scope:
        cache.clear()
        cache.update(scope=scope, items=[])
    from bot.message_runtime import ImageSource
    if "image_sources" in message:
        # 显式来源存在时绝不降级到触发作者；损坏来源宁可忽略。
        sources = [(item.url, {**item.metadata(), "source": "follow_up"})
                   for item in message["image_sources"] if isinstance(item, ImageSource)]
    else:
        sources = [(url, _metadata_for_url(state, url, "current", index))
                   for url in message.get("image_urls", []) if isinstance(url, str) and url]
    for item in (message.get("context_images") or [])[-1:]:
        if isinstance(item, dict) and item.get("url"):
            sources.append((item["url"], {**item, "source": "context"}))
    known = {item["url"] for item in cache["items"]}
    for url, metadata in sources:
        if url in known or not ref_visible(metadata, state, visible=visible, index=index):
            continue
        known.add(url)
        try:
            data = await download(url)
        except Exception:
            data = ""
        ref = store.put_data_url(scope, data, **metadata)
        cache["items"].append({"url": url, "ref": ref, "metadata": metadata})
    active = [item for item in cache["items"]
              if ref_visible(item["metadata"], state, visible=visible, index=index)]
    refs = [item["ref"] for item in active if item["ref"]]
    if getattr(context, "_discarded", False):
        # /reset can happen while the bounded download is awaited. Never make
        # the discarded turn an owner or write its references back afterwards.
        for stale_scope in {ref["scope"] for ref in refs}:
            memory = getattr(context, "_image_memory_manager", None)
            if memory is not None:
                memory.prune_history_images(stale_scope, [])
        return text + ("\n\n[图片]" if active else ""), [], [], (0, 0, 0)
    owner = f"context:{id(context)}"
    pending = list(getattr(context, "_image_pending_refs", []))
    pending.extend(ref for ref in refs if ref not in pending)
    context._image_pending_refs = pending
    downloaded = list(getattr(context, "_image_download_refs", []))
    downloaded.extend(ref for ref in refs if ref not in downloaded)
    context._image_download_refs = downloaded
    store.pin(owner, pending)
    urls = [data for ref in refs if (data := store.read_data_url(scope, ref))]
    usage = (0, 0, 0)
    active_signature = history_signature(refs)
    if cache.get("description_signature") != active_signature:
        cache.pop("description", None)
        cache.pop("relay_attempted", None)
        cache["description_signature"] = active_signature
    description = cache.get("description", "")
    allowed = lambda ref: ref_visible(ref, state, visible=visible, index=index)
    if refs and not description:
        description = store.describe_refs(scope, refs, visible=allowed)
    if not supports_multimodal and urls and not description and not direct and not cache.get("relay_attempted"):
        cache["relay_attempted"] = True
        try:
            description, *counts = await relay(context, text, urls)
            description = str(description or "").strip()
            usage = tuple(int(value or 0) for value in counts)
        except Exception:
            description = ""
        if description:
            cache["description"] = description
            store.set_description(scope, refs, description)
    if not supports_multimodal and active:
        text += "\n\n[图片转述]\n" + (description or "[图片]")
    elif len(urls) < len(active):
        text += "\n\n[图片]"
    return text, urls if supports_multimodal else [], refs, usage


async def describe_retained_images(context, store, *, relay, state=None, visible=None, index=None):
    """For a text-only model describe the latest retained turn with visible images."""
    scope = scope_for(context, state)
    allowed = lambda ref: ref_visible(ref, state, visible=visible, index=index)
    latest = None
    refs = []
    for msg in reversed(getattr(context, "history", [])):
        if not msg.get("image_refs") or (visible is not None and not visible(msg)):
            continue
        candidate_refs = [ref for ref in msg["image_refs"] if allowed(ref)]
        if candidate_refs:
            latest = msg
            refs = candidate_refs
            break
    if latest is None or not refs or store.describe_refs(scope, refs, visible=allowed):
        return (0, 0, 0)
    missing = refs
    signature = history_signature(missing)
    attempted = getattr(context, "_image_history_relay_attempts", set())
    if signature in attempted:
        return (0, 0, 0)
    # Bound retries to the retained turn, not an ever-growing process registry.
    context._image_history_relay_attempts = {signature}
    urls = [data for ref in missing if (data := store.read_data_url(scope, ref))]
    if not urls:
        return (0, 0, 0)
    try:
        description, *usage = await relay(context, content_text(latest.get("content")), urls)
        store.set_description(scope, missing, description)
        return tuple(int(value or 0) for value in usage)
    except Exception:
        return (0, 0, 0)


class ImageHistoryStore:
    def __init__(self, root):
        self.root = Path(root).absolute()
        self._lock = threading.RLock()
        # Unsaved/in-flight owners are strong references too (compression can run
        # in another callback before the turn is committed).
        self._pending: dict[str, list[dict]] = {}

    def pin(self, owner, refs):
        with self._lock:
            if refs:
                self._pending[str(owner)] = deepcopy(list(refs))
            else:
                self._pending.pop(str(owner), None)

    def release(self, owner):
        with self._lock:
            self._pending.pop(str(owner), None)

    def _path(self, scope, digest, suffix=".img", *, create=False):
        if not isinstance(scope, str) or not _HEX.fullmatch(scope):
            raise ValueError("invalid image scope")
        if not isinstance(digest, str) or not _HEX.fullmatch(digest):
            raise ValueError("invalid image digest")
        directory = self.root / scope
        # Reject symlink/junction escapes, including parents, before reads/writes.
        for parent in (self.root, *self.root.parents):
            if parent.is_symlink() or (hasattr(parent, "is_junction") and parent.is_junction()):
                raise ValueError("linked image root")
        if directory.is_symlink() or (hasattr(directory, "is_junction") and directory.is_junction()):
            raise ValueError("linked image scope")
        if create:
            directory.mkdir(parents=True, exist_ok=True)
        path = directory / (digest + suffix)
        if path.is_symlink() or path.resolve().parent != directory.resolve():
            raise ValueError("linked image file")
        return path

    def put_data_url(self, scope: str, data_url: str, **metadata) -> dict | None:
        if not isinstance(data_url, str) or len(data_url) > MAX_IMAGE_BYTES * 4 // 3 + 256:
            return None
        match = _DATA.fullmatch(data_url)
        if match is None:
            return None
        try:
            raw = base64.b64decode(match[2], validate=True)
        except ValueError:
            return None
        mime = image_mime(raw)
        if not mime or not raw or len(raw) > MAX_IMAGE_BYTES:
            return None
        digest = hashlib.sha256(raw).hexdigest()
        try:
            with self._lock:
                path = self._path(scope, digest, create=True)
                if not path.exists():
                    temporary = None
                    try:
                        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
                            temporary = stream.name
                            stream.write(raw)
                        os.replace(temporary, path)
                    finally:
                        if temporary and os.path.exists(temporary):
                            os.unlink(temporary)
            ref = {"sha256": digest, "scope": scope, "mime": mime}
            for key in ("self_id", "is_group", "chat_id", "message_id", "author_id", "source"):
                value = metadata.get(key)
                if isinstance(value, (str, bool, int)):
                    ref[key] = value
            return ref
        except (OSError, ValueError):
            return None

    def read_data_url(self, scope: str, ref: dict) -> str:
        if not isinstance(ref, dict) or ref.get("scope") != scope:
            return ""
        try:
            with self._lock:
                path = self._path(scope, ref.get("sha256"))
                with path.open("rb") as stream:
                    raw = stream.read(MAX_IMAGE_BYTES + 1)
            if (len(raw) > MAX_IMAGE_BYTES or not image_mime(raw)
                    or hashlib.sha256(raw).hexdigest() != ref["sha256"]):
                return ""
            return f"data:{image_mime(raw)};base64," + base64.b64encode(raw).decode("ascii")
        except (OSError, ValueError, TypeError):
            return ""

    @staticmethod
    def _description_members(scope, refs):
        # 内容相同但作者/消息不同也不能共用来源权限；顺序不同仍是同一组。
        members = []
        for ref in refs:
            if not isinstance(ref, dict) or ref.get("scope") != scope or not _HEX.fullmatch(str(ref.get("sha256", ""))):
                return []
            member = {key: ref[key] for key in ("scope", "sha256", "self_id", "is_group",
                      "chat_id", "message_id", "author_id") if key in ref}
            if member not in members:
                members.append(member)
        return sorted(members, key=lambda item: json.dumps(item, sort_keys=True))

    def _read_description(self, path, members, visible):
        try:
            if path.stat().st_size > 512 * 1024:
                return ""
            value = json.loads(path.read_text(encoding="utf-8"))
            sources = value.get("sources")
            if not isinstance(sources, list) or self._description_members(members[0]["scope"], sources) != members:
                return ""
            if visible and any(not visible(source) for source in sources):
                return ""
            text = value.get("description")
            return text[:16000] if isinstance(text, str) else ""
        except (OSError, ValueError, TypeError, AttributeError):
            return ""

    def describe_refs(self, scope, refs, *, visible=None) -> str:
        """只读可靠文字：精确匹配整组及来源；不下载、不识图，不对单图复用组描述。"""
        refs = list(refs)
        members = self._description_members(scope, refs)
        if not members or (visible and any(not visible(ref) for ref in refs)):
            return ""
        try:
            with self._lock:
                key = history_signature(members)
                text = self._read_description(self._path(scope, key, ".group.json"), members, visible)
                if text:
                    return text
                # 仅单图允许回退旧版 .json 单图缓存；多图组合必须走组签名，不能拼单图短路
                if len(members) != 1:
                    return ""
                member = members[0]
                single_key = history_signature([member])
                text = self._read_description(self._path(scope, single_key, ".group.json"), [member], visible)
                if not text:
                    text = self._read_description(self._path(scope, member["sha256"], ".json"), [member], visible)
                return text or ""
        except (OSError, ValueError, TypeError):
            return ""

    def description(self, scope, ref, *, visible=None) -> str:
        return self.describe_refs(scope, [ref], visible=visible)

    def set_description(self, scope, refs, description):
        from bot.io_json import atomic_write_json
        text = str(clean_content(str(description or ""))).strip()[:16000]
        members = self._description_members(scope, refs)
        if not text or not members:
            return
        with self._lock:
            try:
                path = self._path(scope, history_signature(members), ".group.json")
                atomic_write_json(str(path), {"description": text, "sources": members})
            except (OSError, ValueError):
                pass

    def prune(self, scope: str, retained_refs, *, other_refs=(), candidate_digests=None) -> int:
        """Call only after the new history has been durably saved."""
        keep = {ref.get("sha256") for ref in [*retained_refs, *other_refs]
                if isinstance(ref, dict) and ref.get("scope") == scope}
        removed = 0
        with self._lock:
            keep.update(ref.get("sha256") for refs in self._pending.values() for ref in refs
                        if ref.get("scope") == scope)
            try:
                directory = self._path(scope, "0" * 64).parent
                candidates = list(directory.glob("*.img"))
            except (OSError, ValueError):
                return 0
            allowed_candidates = None if candidate_digests is None else set(candidate_digests)
            removed_digests = set()
            for candidate in candidates:
                digest = candidate.stem
                if allowed_candidates is not None and digest not in allowed_candidates:
                    continue
                if digest in keep or not _HEX.fullmatch(digest):
                    continue
                try:
                    self._path(scope, digest).unlink(missing_ok=True)
                    self._path(scope, digest, ".json").unlink(missing_ok=True)
                    removed += 1
                    removed_digests.add(digest)
                except (OSError, ValueError):
                    pass
            # 组描述没有独立寿命，成员图片被回收后同步移除。
            for path in directory.glob("*.group.json"):
                try:
                    if not _HEX.fullmatch(path.name.removesuffix(".group.json")):
                        continue
                    safe = self._path(scope, path.name.removesuffix(".group.json"), ".group.json")
                    if safe.stat().st_size > 512 * 1024:
                        continue
                    value = json.loads(safe.read_text(encoding="utf-8"))
                    if any(ref.get("sha256") in removed_digests for ref in value.get("sources", [])):
                        safe.unlink(missing_ok=True)
                except (OSError, ValueError, TypeError, AttributeError):
                    pass
        return removed

    def cleanup_downloads(self, memory, refs, history=()):
        """仅回收本次下载且无磁盘/pending/当前历史引用的图片，读盘失败不删除。"""
        with memory._image_refs_lock:
            try:
                disk_refs = memory._all_disk_image_refs()
            except (OSError, ValueError, TypeError, AttributeError):
                return 0
            return sum(self.prune(scope, refs_from(history), other_refs=disk_refs,
                                  candidate_digests={ref.get("sha256") for ref in refs if ref.get("scope") == scope})
                       for scope in {ref.get("scope") for ref in refs if ref.get("scope")})

    def invalidate_refs(self, refs):
        """Explicit reset/recall can invalidate pending owners without resurrecting."""
        invalid = {(ref.get("scope"), ref.get("sha256")) for ref in refs if isinstance(ref, dict)}
        with self._lock:
            for owner, pending in list(self._pending.items()):
                kept = [ref for ref in pending if (ref.get("scope"), ref.get("sha256")) not in invalid]
                self.pin(owner, kept)

    def rebuild(self, scope, messages, *, supports_multimodal=False, visible=None):
        """API messages only: no sidecars/paths; at most latest image-bearing round."""
        result = deepcopy(messages)
        last = next((i for i in range(len(result) - 1, -1, -1)
                     if any(isinstance(ref, dict) and ref.get("scope") == scope
                            and (visible is None or visible(ref))
                            for ref in result[i].get("image_refs", []))), -1)
        for index, message in enumerate(result):
            refs = message.pop("image_refs", [])
            if not refs:
                continue
            text = content_text(message.get("content"))
            blocks = [{"type": "text", "text": text}] if text else []
            visible_refs = [ref for ref in refs if isinstance(ref, dict) and ref.get("scope") == scope
                            and (visible is None or visible(ref))]
            group_description = self.describe_refs(scope, visible_refs, visible=visible)
            descriptions = []
            for ref in refs:
                if not isinstance(ref, dict) or ref.get("scope") != scope or (visible and not visible(ref)):
                    continue
                data = self.read_data_url(scope, ref) if supports_multimodal and index == last else ""
                if data:
                    blocks.append({"type": "image_url", "image_url": {"url": data}})
                else:
                    description = group_description or self.description(scope, ref, visible=visible)
                    if description and description not in descriptions and description not in text:
                        descriptions.append(description)
                    elif not description and "[图片]" not in text and "[图片]" not in descriptions:
                        descriptions.append("[图片]")
            if descriptions:
                prefix = "[历史图片转述]\n" if any(value != "[图片]" for value in descriptions) else ""
                blocks.append({"type": "text", "text": prefix + "\n".join(descriptions)})
            message["content"] = blocks if any(b["type"] == "image_url" for b in blocks) else content_text(blocks)
        return result


def archive_message(store, scope, message, **metadata):
    """Convert actual injected blocks to refs before sanitizing; preserve sidecars."""
    result = deepcopy(message)
    refs = list(result.get("image_refs") or [])
    content = result.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "image_url":
                image = block.get("image_url")
                url = image.get("url") if isinstance(image, dict) else ""
                ref = store.put_data_url(scope, url, **metadata)
                if ref and ref not in refs:
                    refs.append(ref)
    result["content"] = clean_content(content)
    if refs:
        result["image_refs"] = refs
    return result
