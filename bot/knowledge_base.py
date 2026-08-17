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

KB_ROOT = BASE_DIR / "data" / "knowledge_base"
KB_DB = KB_ROOT / "knowledge.sqlite3"
KB_FILES = KB_ROOT / "documents"
_ALLOWED = {".txt", ".md", ".markdown", ".pdf"}
_MAX_BYTES = 16 * 1024 * 1024
_EMBED_BATCH_SIZE = 64
_LOCK = threading.RLock()


# schema 迁移只需在进程生命周期内执行一次，用 flag 避免每次 _db() 都跑 executescript + commit
_SCHEMA_INITIALIZED = False


def _db() -> sqlite3.Connection:
    global _SCHEMA_INITIALIZED
    KB_ROOT.mkdir(parents=True, exist_ok=True)
    KB_FILES.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(KB_DB), timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    if not _SCHEMA_INITIALIZED:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS documents (
          id TEXT PRIMARY KEY, name TEXT NOT NULL, path TEXT NOT NULL UNIQUE,
          extension TEXT NOT NULL, sha256 TEXT NOT NULL UNIQUE, size_bytes INTEGER NOT NULL,
          status TEXT NOT NULL, error TEXT NOT NULL DEFAULT '', chunks INTEGER NOT NULL DEFAULT 0,
          created_at REAL NOT NULL, updated_at REAL NOT NULL,
          index_mode TEXT NOT NULL DEFAULT 'fts', embedding_ref TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS chunks (
          id TEXT PRIMARY KEY, document_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
          page INTEGER, content TEXT NOT NULL, embedding TEXT NOT NULL DEFAULT '',
          embedding_model TEXT NOT NULL DEFAULT '',
          embedding_provider_id TEXT NOT NULL DEFAULT '', embedding_dimensions INTEGER NOT NULL DEFAULT 0,
          FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
          content, document_id UNINDEXED, chunk_id UNINDEXED, tokenize='trigram'
        );
        """)
        document_columns = {row["name"] for row in db.execute("PRAGMA table_info(documents)")}
        if "index_mode" not in document_columns:
            db.execute("ALTER TABLE documents ADD COLUMN index_mode TEXT NOT NULL DEFAULT 'fts'")
        if "embedding_ref" not in document_columns:
            db.execute("ALTER TABLE documents ADD COLUMN embedding_ref TEXT NOT NULL DEFAULT ''")
        chunk_columns = {row["name"] for row in db.execute("PRAGMA table_info(chunks)")}
        if "embedding_provider_id" not in chunk_columns:
            db.execute("ALTER TABLE chunks ADD COLUMN embedding_provider_id TEXT NOT NULL DEFAULT ''")
        if "embedding_dimensions" not in chunk_columns:
            db.execute("ALTER TABLE chunks ADD COLUMN embedding_dimensions INTEGER NOT NULL DEFAULT 0")
        # 检测 chunks_fts 是否使用 trigram 分词器（通过 sqlite_master 的建表语句判断）
        fts_sql_row = db.execute(
            "SELECT sql FROM sqlite_master WHERE name='chunks_fts' AND type='table'"
        ).fetchone()
        fts_sql = str(fts_sql_row[0] if fts_sql_row else "")
        if "trigram" not in fts_sql:
            # 重建 FTS 表并从 chunks 重新填充
            db.execute("DROP TABLE IF EXISTS chunks_fts")
            db.execute(
                "CREATE VIRTUAL TABLE chunks_fts USING fts5("
                "content, document_id UNINDEXED, chunk_id UNINDEXED, tokenize='trigram')"
            )
            db.execute(
                "INSERT INTO chunks_fts(content, document_id, chunk_id) "
                "SELECT content, document_id, id FROM chunks"
            )
        db.commit()
        _SCHEMA_INITIALIZED = True
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
            # 把前一块的尾部作为下一块的前导，实现段落边界处的 overlap
            if overlap and len(current) > overlap:
                current = current[-overlap:]
            else:
                current = ""
        if len(paragraph) <= size:
            if current and len(current) + len(paragraph) + 1 <= size:
                current = (current + "\n" + paragraph).strip()
            else:
                if current:
                    out.append(current)
                current = paragraph
            continue
        # 超长单段落：滑动切分，同样带 overlap
        step = size - overlap
        for i in range(0, len(paragraph), step):
            out.append(paragraph[i:i + size])
        current = ""
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
    with urllib.request.urlopen(req, timeout=60) as response:
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
        if old and old["status"] == "indexing":
            db.close()
            return dict(old)
        if old and old["status"] == "ready" and str(old["embedding_ref"] or "") == wanted_ref:
            db.close()
            return dict(old)
        now = time.time()
        if old:
            doc_id = old["id"]
            path = Path(old["path"])
            db.execute(
                "UPDATE documents SET name=?,extension=?,size_bytes=?,status='indexing',error='',updated_at=? WHERE id=?",
                (safe_name, ext, len(raw), now, doc_id),
            )
        else:
            db.execute(
                "INSERT INTO documents(id,name,path,extension,sha256,size_bytes,status,error,chunks,created_at,updated_at,index_mode,embedding_ref) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (doc_id, safe_name, str(path), ext, digest, len(raw), "indexing", "", 0, now, now, "fts", ""),
            )
        db.commit()
        db.close()

    # 写文件移出全局锁：16MB 写盘期间不应阻塞 list/search/delete
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)

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
                "UPDATE documents SET status='ready',error=?,chunks=?,updated_at=?,index_mode=?,embedding_ref=? WHERE id=?",
                (warning, len(chunks), time.time(), index_mode, actual_ref, doc_id),
            )
            db.commit()
            db.close()
    except Exception as e:
        with _LOCK:
            db = _db()
            db.execute("UPDATE documents SET status='failed',error=?,updated_at=? WHERE id=?", (str(e), time.time(), doc_id))
            db.commit()
            db.close()
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
    """把查询词转成 FTS5 trigram MATCH 表达式。

    trigram 分词器以任意连续三字符为索引单元，支持中英文混合查询，
    最短有效查询为 3 个字符（1-2 字符查询因不能生成三元组，返回空跳过 FTS）。
    """
    chars = [c for c in str(query or "") if not c.isspace()]
    if len(chars) < 3:
        return ""
    trigrams: list[str] = []
    seen: set[str] = set()
    for i in range(len(chars) - 2):
        t = "".join(chars[i:i + 3])
        if t not in seen:
            seen.add(t)
            trigrams.append(f'"{t.replace(chr(34), chr(34) * 2)}"')
    return " OR ".join(trigrams[:20])


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
        except Exception as _vec_err:
            print(f"[KnowledgeBase] 向量检索失败，已回退 SQLite FTS：{_vec_err}")
            scored_rows = []

    # 问题2：向量命中数 < top_k 时用 FTS 补位，同时覆盖换模型后旧文档不可见的情况
    need_fts = not scored_rows or len(scored_rows) < settings["top_k"]
    fts_rows: list[tuple[float, sqlite3.Row]] = []
    if need_fts:
        match = _fts_match(str(query))
        if match:
            already_ids = {row["id"] for _, row in scored_rows}
            fts_limit = settings["top_k"] - len(scored_rows)
            with _LOCK:
                db = _db()
                ranked = db.execute(
                    "SELECT c.*,d.name,bm25(chunks_fts) AS fts_rank FROM chunks_fts f "
                    "JOIN chunks c ON c.id=f.chunk_id JOIN documents d ON d.id=c.document_id "
                    "WHERE d.status='ready' AND chunks_fts MATCH ? ORDER BY fts_rank ASC LIMIT ?",
                    (match, fts_limit + len(already_ids)),  # 多取一些抵消去重损耗，最终 [:fts_limit] 截断
                ).fetchall()
                db.close()
            ranked = [row for row in ranked if row["id"] not in already_ids][:fts_limit]
            if ranked:
                strengths = [-float(row["fts_rank"]) for row in ranked]
                strongest, weakest = max(strengths), min(strengths)
                span = strongest - weakest
                fts_rows = [
                    ((strength - weakest) / span if span > 1e-12 else 1.0, row)
                    for strength, row in zip(strengths, ranked)
                ]

    if not scored_rows and fts_rows:
        scored_rows = fts_rows
        mode = "fts"
    elif fts_rows:
        # 向量命中不足时追加 FTS 补位，相关度降权为向量最低分的一半，然后重新排序
        floor = min(v for v, _ in scored_rows) * 0.5 if scored_rows else 0.5
        scored_rows = sorted(
            scored_rows + [(floor * r, row) for r, row in fts_rows],
            key=lambda x: x[0],
            reverse=True,
        )


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
