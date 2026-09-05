# -*- coding: utf-8 -*-
"""Small local knowledge base with optional OpenAI-compatible embeddings."""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

from bot.paths import BASE_DIR
from bot.llm_config import normalize_proxy_url

KB_ROOT = BASE_DIR / "data" / "knowledge_base"
KB_DB = KB_ROOT / "knowledge.sqlite3"
KB_FILES = KB_ROOT / "documents"
_ALLOWED = {".txt", ".md", ".markdown", ".pdf"}
_MAX_BYTES = 16 * 1024 * 1024
_EMBED_BATCH_SIZE = 64
_INDEXING_STALE_SECONDS = 10 * 60
_LOCK = threading.RLock()


def _db() -> sqlite3.Connection:
    KB_ROOT.mkdir(parents=True, exist_ok=True)
    KB_FILES.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(KB_DB), timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript("""
    CREATE TABLE IF NOT EXISTS documents (
      id TEXT PRIMARY KEY, name TEXT NOT NULL, path TEXT NOT NULL UNIQUE,
      extension TEXT NOT NULL, sha256 TEXT NOT NULL UNIQUE, size_bytes INTEGER NOT NULL,
      status TEXT NOT NULL, error TEXT NOT NULL DEFAULT '', chunks INTEGER NOT NULL DEFAULT 0,
      created_at REAL NOT NULL, updated_at REAL NOT NULL,
      index_mode TEXT NOT NULL DEFAULT 'fts', embedding_ref TEXT NOT NULL DEFAULT '',
      warning TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS chunks (
      id TEXT PRIMARY KEY, document_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
      page INTEGER, content TEXT NOT NULL, embedding TEXT NOT NULL DEFAULT '',
      embedding_model TEXT NOT NULL DEFAULT '',
      embedding_provider_id TEXT NOT NULL DEFAULT '', embedding_dimensions INTEGER NOT NULL DEFAULT 0,
      FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE
    );
    CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
      content, document_id UNINDEXED, chunk_id UNINDEXED
    );
    """)
    document_columns = {row["name"] for row in db.execute("PRAGMA table_info(documents)")}
    if "index_mode" not in document_columns:
        db.execute("ALTER TABLE documents ADD COLUMN index_mode TEXT NOT NULL DEFAULT 'fts'")
    if "embedding_ref" not in document_columns:
        db.execute("ALTER TABLE documents ADD COLUMN embedding_ref TEXT NOT NULL DEFAULT ''")
    if "warning" not in document_columns:
        # warning 与 error 分开：降级成功（回退 FTS）属于 ready 而不是 failed，
        # 混用 error 列会让前端把成功的降级索引显示成失败。
        db.execute("ALTER TABLE documents ADD COLUMN warning TEXT NOT NULL DEFAULT ''")
    chunk_columns = {row["name"] for row in db.execute("PRAGMA table_info(chunks)")}
    if "embedding_provider_id" not in chunk_columns:
        db.execute("ALTER TABLE chunks ADD COLUMN embedding_provider_id TEXT NOT NULL DEFAULT ''")
    if "embedding_dimensions" not in chunk_columns:
        db.execute("ALTER TABLE chunks ADD COLUMN embedding_dimensions INTEGER NOT NULL DEFAULT 0")
    db.commit()
    return db


def _bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on", "启用", "开启", "是"}:
        return True
    if text in {"0", "false", "no", "n", "off", "禁用", "关闭", "否"}:
        return False
    return default


def _int(raw: dict, key: str, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(int(raw.get(key, default)), high))
    except (TypeError, ValueError):
        return default


def _config(cfg: dict | None) -> dict:
    raw = cfg if isinstance(cfg, dict) else {}
    chunk_size = _int(raw, "chunk_size", 1000, 200, 5000)
    overlap = _int(raw, "chunk_overlap", 150, 0, 1000)
    overlap = min(overlap, chunk_size // 2)
    return {
        "enabled": _bool(raw.get("enabled"), True),
        "vector_mode_enabled": _bool(raw.get("vector_mode_enabled"), True),
        "top_k": _int(raw, "top_k", 5, 1, 20),
        "chunk_size": chunk_size,
        "chunk_overlap": overlap,
        "max_context_chars": _int(raw, "max_context_chars", 8000, 500, 30000),
        "embedding_provider_id": str(raw.get("embedding_provider_id", "") or "").strip(),
        "embedding_model": str(raw.get("embedding_model", "") or "").strip(),
        "embedding_model_ref": str(raw.get("embedding_model_ref", "") or "").strip(),
    }


def _chunks(text: str, size: int, overlap: int) -> list[str]:
    overlap = max(0, min(int(overlap), max(0, int(size) // 2)))
    paragraphs = [x.strip() for x in re.split(r"\n\s*\n+", text) if x.strip()]
    out: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if len(paragraph) <= size and len(current) + len(paragraph) + 1 <= size:
            current = (current + "\n" + paragraph).strip()
            continue
        if current:
            out.append(current)
            # 段落边界只在能和下一段合并时保留尾部，避免产生纯 overlap 小片段。
            current = current[-overlap:] if overlap and len(current) > overlap else ""
        if len(paragraph) <= size:
            combined = (current + "\n" + paragraph).strip() if current else paragraph
            if len(combined) <= size:
                current = combined
            else:
                # 尾部与新段落合并后超限，丢弃尾部，避免额外产生重复小片段。
                current = paragraph
            continue
        # 超长单段落：滑动切分，同样带 overlap。
        if current:
            out.append(current)
            current = ""
        step = size - overlap
        out.extend(paragraph[i:i + size] for i in range(0, len(paragraph), step))
    if current:
        out.append(current)
    return out


def _read_text(path: Path, ext: str) -> list[tuple[str, int | None]]:
    if ext == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as e:
            raise RuntimeError("解析 PDF 需要安装 pypdf") from e
        pages = []
        for no, page in enumerate(PdfReader(str(path)).pages, start=1):
            text = str(page.extract_text() or "").strip()
            if text:
                pages.append((text, no))
        if not pages:
            raise RuntimeError("PDF 没有可提取的文本（扫描 PDF 暂不支持 OCR）")
        return pages
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return [(raw.decode(encoding), None)]
        except UnicodeDecodeError:
            continue
    raise RuntimeError("文本文件编码无法识别，请转换为 UTF-8 或 GBK")


def _embed_with_key(texts: list[str], provider: dict, model: str, key: str) -> list[list[float]]:
    base_url = str(provider.get("base_url", "") or "").strip().rstrip("/")
    if not base_url or not key or not model:
        raise RuntimeError("未配置可用的嵌入提供商、Key 或模型")
    req = urllib.request.Request(
        base_url + "/embeddings",
        data=json.dumps({"model": model, "input": texts}).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    # 嵌入走该提供商自己的代理；留空则显式禁用代理（ProxyHandler({})），
    # 避免继承环境变量里的 HTTP_PROXY 造成"没配代理却走了代理"。
    proxy = normalize_proxy_url(provider.get("http_proxy", ""))
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {})
    )
    with opener.open(req, timeout=60) as response:
        obj = json.loads(response.read().decode("utf-8", errors="replace"))
    rows = sorted(obj.get("data", []), key=lambda x: int(x.get("index", 0)))
    vectors = [x.get("embedding") for x in rows]
    if len(vectors) != len(texts) or any(not isinstance(v, list) or not v for v in vectors):
        raise RuntimeError("嵌入接口返回的数据格式不正确")
    converted = [[float(n) for n in vector] for vector in vectors]
    dimensions = len(converted[0])
    if dimensions <= 0 or any(len(vector) != dimensions for vector in converted):
        raise RuntimeError("嵌入接口返回的向量维度不一致")
    if any(not math.isfinite(number) for vector in converted for number in vector):
        raise RuntimeError("嵌入接口返回了非有限数值")
    return converted


def _embed(texts: list[str], provider: dict, model: str) -> list[list[float]]:
    keys = [str(key or "").strip() for key in provider.get("keys", []) if str(key or "").strip()]
    if not keys:
        raise RuntimeError("未配置可用的嵌入 Key")
    output: list[list[float]] = []
    expected_dimensions = 0
    for start in range(0, len(texts), _EMBED_BATCH_SIZE):
        batch = texts[start:start + _EMBED_BATCH_SIZE]
        errors = []
        vectors = None
        for key in keys:
            try:
                vectors = _embed_with_key(batch, provider, model, key)
                break
            except Exception as e:
                errors.append(str(e))
        if vectors is None:
            raise RuntimeError("所有嵌入 Key 均失败：" + "；".join(errors[-3:]))
        dimensions = len(vectors[0])
        if expected_dimensions and dimensions != expected_dimensions:
            raise RuntimeError("分批嵌入返回的向量维度不一致")
        expected_dimensions = dimensions
        output.extend(vectors)
    return output


def _provider(settings: dict, providers: list[dict]) -> tuple[dict | None, str, str]:
    pid = settings["embedding_provider_id"]
    model = settings["embedding_model"]
    ref = settings.get("embedding_model_ref", "")
    if ref:
        matches = []
        for item in providers:
            provider_id = str(item.get("id", "") or "").strip()
            for model_cfg in item.get("embedding_models", []) if isinstance(item.get("embedding_models"), list) else []:
                if isinstance(model_cfg, str):
                    name, enabled = model_cfg.strip(), True
                elif isinstance(model_cfg, dict):
                    name = str(model_cfg.get("name", "") or model_cfg.get("model", "") or "").strip()
                    enabled = _bool(model_cfg.get("enabled"), True)
                else:
                    continue
                if enabled and name and f"{provider_id}/{name}" == ref:
                    matches.append((item, provider_id, name))
        if len(matches) == 1:
            return matches[0]
        return None, "", ""
    for item in providers:
        if str(item.get("id", "") or "").strip() == pid and item.get("base_url") and item.get("keys"):
            return item, pid, model
    return None, "", ""


def _embedding_ref(provider_id: str, model: str) -> str:
    return f"{provider_id}/{model}" if provider_id else model


def add_document(name: str, raw: bytes, cfg: dict, providers: list[dict]) -> dict:
    settings = _config(cfg)
    display_name = Path(str(name or "")).name or "未命名文档"
    print(f"[KnowledgeBase] 开始处理文档：{display_name}")
    safe_name = Path(str(name or "")).name
    ext = Path(safe_name).suffix.lower()
    if ext not in _ALLOWED:
        raise ValueError("只支持 TXT、Markdown 和 PDF 文件")
    if not raw or len(raw) > _MAX_BYTES:
        raise ValueError("文件为空或超过 16MB 限制")

    digest = hashlib.sha256(raw).hexdigest()
    doc_id = digest[:24]
    path = KB_FILES / f"{digest}{ext}"
    provider, provider_id, model = _provider(settings, providers)
    wanted_ref = _embedding_ref(provider_id, model) if settings["vector_mode_enabled"] and provider and model else ""

    with _LOCK:
        db = _db()
        old = db.execute("SELECT * FROM documents WHERE sha256=?", (digest,)).fetchone()
        if old and old["status"] == "indexing" and (time.time() - float(old["updated_at"] or 0)) < _INDEXING_STALE_SECONDS:
            db.close()
            return dict(old)
        if old and old["status"] == "ready" and str(old["embedding_ref"] or "") == wanted_ref:
            db.close()
            return dict(old)
        now = time.time()
        if old:
            doc_id = old["id"]
            path = Path(old["path"])
            # updated_at 兼作本次尝试的凭据（见下方 _still_mine）。必须严格大于旧值，
            # 否则同一毫秒内的两次上传会拿到相同的 now，凭据失去区分能力。
            if now <= float(old["updated_at"] or 0):
                now = float(old["updated_at"] or 0) + 0.001
            db.execute(
                "UPDATE documents SET name=?,extension=?,size_bytes=?,status='indexing',error='',warning='',updated_at=? WHERE id=?",
                (safe_name, ext, len(raw), now, doc_id),
            )
        else:
            db.execute(
                "INSERT INTO documents(id,name,path,extension,sha256,size_bytes,status,error,chunks,created_at,updated_at,index_mode,embedding_ref) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (doc_id, safe_name, str(path), ext, digest, len(raw), "indexing", "", 0, now, now, "fts", ""),
            )
        db.commit()
        db.close()
        print(f"[KnowledgeBase] 文档已登记，开始建立索引：{display_name}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)

    # 本次尝试的凭据。同一文档被并发上传（前一次索引超过 _INDEXING_STALE_SECONDS
    # 还没结束，用户又传了一遍）时，后来者会把 updated_at 改成它自己的值。
    # 收尾前比对一次：不相等说明这行已被后来者接管，本次尝试的结果作废，
    # 什么都不能写、不能删——否则慢的那次收尾会把快的那次的成果一起抹掉。
    my_stamp = now

    def _still_mine(db) -> bool:
        row = db.execute("SELECT updated_at FROM documents WHERE id=?", (doc_id,)).fetchone()
        return bool(row) and abs(float(row["updated_at"] or 0) - my_stamp) < 1e-6

    try:
        pages = _read_text(path, ext)
        chunks: list[tuple[str, int | None]] = []
        for text, page in pages:
            chunks.extend((content, page) for content in _chunks(text, settings["chunk_size"], settings["chunk_overlap"]))
        if not chunks:
            raise RuntimeError("文档没有可建立索引的文本")

        vectors: list[list[float]] = []
        warning = ""
        if settings["vector_mode_enabled"]:
            if provider and model:
                try:
                    vectors = _embed([item[0] for item in chunks], provider, model)
                except Exception as e:
                    warning = f"向量索引失败，已回退 SQLite FTS：{e}"
            else:
                warning = "未选择可用的嵌入模型，已使用 SQLite FTS"
        dimensions = len(vectors[0]) if vectors else 0
        index_mode = "vector" if vectors else "fts"
        actual_ref = wanted_ref if vectors else ""

        with _LOCK:
            db = _db()
            exists = db.execute("SELECT 1 FROM documents WHERE id=?", (doc_id,)).fetchone()
            if not exists:
                db.close()
                raise RuntimeError("文档在索引期间已被删除")
            if not _still_mine(db):
                # 已被后来的上传接管：那次会写自己的 chunks，这里不能再插一遍
                # （chunks.id 是 f"{doc_id}_{ordinal}"，两次一起写会主键冲突或串味）。
                db.close()
                print(f"[KnowledgeBase] 索引结果作废（同文档已被新的上传接管）：{display_name}")
                return get_document(doc_id)
            db.execute("DELETE FROM chunks_fts WHERE document_id=?", (doc_id,))
            db.execute("DELETE FROM chunks WHERE document_id=?", (doc_id,))
            for ordinal, (item, page) in enumerate(chunks):
                chunk_id = f"{doc_id}_{ordinal}"
                vector = json.dumps(vectors[ordinal], ensure_ascii=False) if vectors else ""
                db.execute(
                    "INSERT INTO chunks(id,document_id,ordinal,page,content,embedding,embedding_model,embedding_provider_id,embedding_dimensions) VALUES (?,?,?,?,?,?,?,?,?)",
                    (chunk_id, doc_id, ordinal, page, item, vector, model if vectors else "", provider_id if vectors else "", dimensions),
                )
                db.execute("INSERT INTO chunks_fts(content,document_id,chunk_id) VALUES (?,?,?)", (item, doc_id, chunk_id))
            db.execute(
                "UPDATE documents SET status='ready',error='',warning=?,chunks=?,updated_at=?,index_mode=?,embedding_ref=? WHERE id=?",
                (warning, len(chunks), time.time(), index_mode, actual_ref, doc_id),
            )
            db.commit()
            db.close()
        print(f"[KnowledgeBase] 索引完成：{display_name}，{len(chunks)} 个片段，模式={index_mode}")
    except Exception as e:
        with _LOCK:
            db = _db()
            superseded = not _still_mine(db)
            if not superseded:
                db.execute("DELETE FROM chunks_fts WHERE document_id=?", (doc_id,))
                db.execute("DELETE FROM chunks WHERE document_id=?", (doc_id,))
                db.execute("UPDATE documents SET status='failed',error=?,warning='',chunks=0,updated_at=? WHERE id=?", (str(e), time.time(), doc_id))
                db.commit()
            db.close()
        if superseded:
            # 同文档已被新的上传接管（且很可能已经成功）。这里必须完全撒手：
            # 早期版本无条件走下面的清理，导致「慢的那次失败收尾」把「快的那次的
            # 成功结果」连 chunks 带磁盘原文一起抹掉，界面显示索引失败但其实刚成功过。
            # 本次的异常也不再上抛——它属于一次已被取代的尝试，报给用户只会误导。
            print(f"[KnowledgeBase] 索引失败但结果已作废（同文档已被新的上传接管）：{display_name}：{e}")
            return get_document(doc_id)
        # documents 行保留（前端要显示失败原因），但磁盘原文要删。
        # 留着它没有任何用处：_read_text 只在本函数里调用，而本函数只由上传接口
        # 触发——没有独立的「重新索引」入口。用户重试必然重新上传，上面的
        # path.write_bytes(raw) 会把文件重写一遍。检索侧只读 chunks 表，不碰原文。
        # 不删的话，反复索引失败的大文件会一直占着磁盘，直到删除文档才清。
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        print(f"[KnowledgeBase] 索引失败：{display_name}：{e}")
        raise
    return get_document(doc_id)


def get_document(doc_id: str) -> dict:
    with _LOCK:
        db = _db()
        row = db.execute("SELECT * FROM documents WHERE id=?", (str(doc_id),)).fetchone()
        db.close()
    return dict(row) if row else {}


def list_documents() -> list[dict]:
    with _LOCK:
        db = _db()
        rows = db.execute("SELECT * FROM documents ORDER BY updated_at DESC").fetchall()
        db.close()
    return [dict(row) for row in rows]


def delete_document(doc_id: str) -> bool:
    with _LOCK:
        db = _db()
        row = db.execute("SELECT path FROM documents WHERE id=?", (str(doc_id),)).fetchone()
        if not row:
            db.close()
            return False
        db.execute("DELETE FROM chunks_fts WHERE document_id=?", (str(doc_id),))
        db.execute("DELETE FROM documents WHERE id=?", (str(doc_id),))
        db.commit()
        db.close()
    try:
        Path(row["path"]).unlink(missing_ok=True)
    except OSError:
        pass
    return True


def _fts_match(query: str) -> str:
    words = [x for x in re.findall(r"[\w\u4e00-\u9fff]+", str(query)) if x]
    return " OR ".join(f'"{word.replace(chr(34), chr(34) * 2)}"' for word in words[:12])


def search(query: str, cfg: dict, providers: list[dict]) -> dict:
    settings = _config(cfg)
    if not settings["enabled"] or not str(query or "").strip():
        return {"text": "", "mode": "disabled", "hits": []}

    scored_rows: list[tuple[float, sqlite3.Row]] = []
    mode = "fts"
    provider, provider_id, model = _provider(settings, providers)
    if settings["vector_mode_enabled"] and provider and model:
        try:
            q = _embed([str(query)], provider, model)[0]
            with _LOCK:
                db = _db()
                candidates = db.execute(
                    "SELECT c.*,d.name FROM chunks c JOIN documents d ON d.id=c.document_id "
                    "WHERE d.status='ready' AND c.embedding_provider_id=? AND c.embedding_model=? AND c.embedding_dimensions=?",
                    (provider_id, model, len(q)),
                ).fetchall()
                db.close()

            def score(row):
                vector = json.loads(row["embedding"] or "[]")
                if len(vector) != len(q):
                    return 0.0
                den = math.sqrt(sum(x * x for x in q) * sum(x * x for x in vector))
                return sum(a * b for a, b in zip(q, vector)) / den if den else 0.0

            scored_rows = sorted(((score(row), row) for row in candidates), key=lambda x: x[0], reverse=True)[:settings["top_k"]]
            scored_rows = [(max(0.0, min(1.0, value)), row) for value, row in scored_rows if value > 0]
            if scored_rows:
                mode = "vector"
        except Exception:
            scored_rows = []

    if not scored_rows:
        match = _fts_match(str(query))
        if match:
            with _LOCK:
                db = _db()
                ranked = db.execute(
                    "SELECT c.*,d.name,bm25(chunks_fts) AS fts_rank FROM chunks_fts f "
                    "JOIN chunks c ON c.id=f.chunk_id JOIN documents d ON d.id=c.document_id "
                    "WHERE d.status='ready' AND chunks_fts MATCH ? ORDER BY fts_rank ASC LIMIT ?",
                    (match, settings["top_k"]),
                ).fetchall()
                db.close()
            if ranked:
                strengths = [-float(row["fts_rank"]) for row in ranked]
                strongest, weakest = max(strengths), min(strengths)
                span = strongest - weakest
                scored_rows = [
                    ((strength - weakest) / span if span > 1e-12 else 1.0, row)
                    for strength, row in zip(strengths, ranked)
                ]
        mode = "fts"

    pieces, hits, total = [], [], 0
    for relevance, row in scored_rows:
        source = f"{row['name']}" + (f"，第 {row['page']} 页" if row["page"] else "")
        prefix = f"[来源：{source}｜相关度：{relevance:.2f}]\n"
        remaining = settings["max_context_chars"] - total
        if remaining <= len(prefix):
            break
        content = str(row["content"] or "")
        truncated = len(prefix) + len(content) > remaining
        if truncated:
            content = content[:max(0, remaining - len(prefix) - 1)] + "…"
        piece = prefix + content
        pieces.append(piece)
        hits.append({
            "document": row["name"],
            "page": row["page"],
            "chunk_id": row["id"],
            "relevance": round(relevance, 4),
        })
        total += len(piece)
        if truncated:
            break
    return {"text": "\n\n".join(pieces), "mode": mode, "hits": hits}
