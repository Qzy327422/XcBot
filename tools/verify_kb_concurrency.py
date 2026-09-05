# -*- coding: utf-8 -*-
"""C3 验证：同一文档并发上传时的去重与互相覆盖。

不联网、不碰真实数据：KB_ROOT/KB_DB/KB_FILES 全部改指临时目录，
向量模式关掉（vector_mode_enabled=False），所以不需要嵌入提供商。

关注两件事：
  1) 去重窗口内（_INDEXING_STALE_SECONDS 默认 600s）第二次上传应直接返回，不重复建索引。
  2) 窗口过期后两次上传会真的并发跑。此时失败分支会不会把另一边的成功结果清掉。

用法：python tools/verify_kb_concurrency.py
退出码 0 = 全部符合预期。
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import bot.knowledge_base as kb

results: list[tuple[str, str, str]] = []


def ok(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    results.append((status, name, detail))
    print(f"[{status}] {name}" + (f" - {detail}" if detail else ""))


CFG = {"enabled": True, "vector_mode_enabled": False, "chunk_size": 400, "chunk_overlap": 0}
RAW = ("段落一，用来切出至少一个 chunk。\n\n" * 5).encode("utf-8")


def use_temp_kb(tmp: Path) -> None:
    kb.KB_ROOT = tmp
    kb.KB_DB = tmp / "knowledge.sqlite3"
    kb.KB_FILES = tmp / "documents"


def fresh_kb() -> Path:
    tmp = Path(tempfile.mkdtemp())
    use_temp_kb(tmp)
    return tmp


def check_single_upload() -> None:
    """先确认基线：正常上传能建好索引。"""
    fresh_kb()
    doc = kb.add_document("a.txt", RAW, CFG, [])
    ok("单次上传建索引成功", doc.get("status") == "ready" and int(doc.get("chunks") or 0) > 0,
       f"status={doc.get('status')} chunks={doc.get('chunks')}")
    ok("单次上传原文留在磁盘", Path(doc["path"]).exists())


def check_dedupe_in_window() -> None:
    """窗口内并发：第二个调用者应当直接拿到 indexing 行，不重复建索引。"""
    fresh_kb()
    real_read = kb._read_text
    entered = threading.Event()
    release = threading.Event()
    calls: list[int] = []

    def slow_read(path, ext):
        calls.append(1)
        entered.set()
        release.wait(10)
        return real_read(path, ext)

    kb._read_text = slow_read
    out: dict[str, dict] = {}
    try:
        t = threading.Thread(target=lambda: out.update(a=kb.add_document("a.txt", RAW, CFG, [])))
        t.start()
        entered.wait(10)          # A 已进入索引阶段，documents 行是 indexing
        out["b"] = kb.add_document("a.txt", RAW, CFG, [])   # B 在窗口内上传
        release.set()
        t.join(10)
    finally:
        kb._read_text = real_read

    ok("窗口内第二次上传不重复建索引", len(calls) == 1, f"_read_text 被调用 {len(calls)} 次")
    ok("窗口内第二次上传返回 indexing 行", out.get("b", {}).get("status") == "indexing",
       f"b.status={out.get('b', {}).get('status')}")
    ok("A 最终仍然建成 ready", out.get("a", {}).get("status") == "ready",
       f"a.status={out.get('a', {}).get('status')}")


def check_stale_window_clobber() -> None:
    """窗口过期后并发：A 失败的清理分支会不会抹掉 B 的成功结果。

    把 _INDEXING_STALE_SECONDS 设为 0，让 B 直接穿过去重判断，
    模拟真实世界里「A 索引超过 600s 还没完，用户又传了一次」。

    A 的失败是真实可达的：add_document 在 _LOCK 里 write_bytes（:299），
    但 _read_text 在锁外（:302）。B 拿到锁后 write_bytes 会先截断文件，
    此刻正在锁外读文件的 A 就可能读到空/半截内容而抛错。
    这里用受控的 _read_text 复现「A 读失败、B 读成功」这个交错。
    """
    fresh_kb()
    real_read = kb._read_text
    original_stale = kb._INDEXING_STALE_SECONDS
    kb._INDEXING_STALE_SECONDS = 0

    a_reading = threading.Event()
    b_done = threading.Event()
    state = {"n": 0}
    lock = threading.Lock()

    def racy_read(path, ext):
        with lock:
            state["n"] += 1
            mine = state["n"]
        if mine == 1:                       # A：等 B 整个跑完，然后模拟读到被截断的文件
            a_reading.set()
            b_done.wait(10)
            raise RuntimeError("文本文件编码无法识别，请转换为 UTF-8 或 GBK")
        return real_read(path, ext)         # B：正常读

    kb._read_text = racy_read
    a_err: list[str] = []
    a_ret: list[dict] = []

    def run_a():
        try:
            a_ret.append(kb.add_document("a.txt", RAW, CFG, []))
        except Exception as e:  # noqa: BLE001
            a_err.append(str(e))

    try:
        ta = threading.Thread(target=run_a)
        ta.start()
        a_reading.wait(10)
        b = kb.add_document("a.txt", RAW, CFG, [])   # B 全程跑完并成功
        ok("窗口过期后第二次上传会真的重建", b.get("status") == "ready",
           f"b.status={b.get('status')} chunks={b.get('chunks')}")
        b_done.set()
        ta.join(10)
    finally:
        kb._read_text = real_read
        kb._INDEXING_STALE_SECONDS = original_stale

    # A 的索引确实失败了，但它发现自己已被 B 接管，于是不上抛异常，
    # 而是返回当前（B 写好的）文档状态。发起 A 的那个用户看到的是「已就绪」——
    # 这是对的，文档此刻确实可用，报错反而误导。
    ok("A 被接管后不上抛异常", not a_err, f"a_err={a_err}")
    ok("A 返回的是接管后的 ready 状态",
       bool(a_ret) and a_ret[0].get("status") == "ready",
       f"a_ret={a_ret[0].get('status') if a_ret else None}")

    final = kb.list_documents()
    doc = final[0] if final else {}
    status = doc.get("status")
    chunks = int(doc.get("chunks") or 0)
    file_alive = bool(doc.get("path")) and Path(doc["path"]).exists()

    # 这一条是本次验证的核心问题：B 成功了，但 A 的失败分支后跑，
    # 它会 DELETE chunks + status='failed' + path.unlink，把 B 的成果一并抹掉。
    ok("B 的成功结果没被 A 的失败清理抹掉",
       status == "ready" and chunks > 0 and file_alive,
       f"status={status} chunks={chunks} 原文在磁盘={file_alive}")

    # 无论哪边赢，DB 状态至少不能自相矛盾（ready 却没有 chunks，或 failed 却留着 chunks）
    consistent = (status == "ready" and chunks > 0) or (status == "failed" and chunks == 0)
    ok("最终状态自身不矛盾（status 与 chunks 一致）", consistent,
       f"status={status} chunks={chunks}")


def check_plain_failure_still_reports() -> None:
    """回归：没有并发接管时，普通索引失败必须照旧报错 + 删原文 + status=failed。

    上面那条修复是「被接管才撒手」，很容易过度收敛成「失败一律不报」。
    这条守住边界。
    """
    fresh_kb()
    real_read = kb._read_text
    kb._read_text = lambda p, e: (_ for _ in ()).throw(RuntimeError("文档没有可建立索引的文本"))
    err = ""
    try:
        kb.add_document("a.txt", RAW, CFG, [])
    except Exception as e:  # noqa: BLE001
        err = str(e)
    finally:
        kb._read_text = real_read

    ok("普通失败仍然上抛异常", "没有可建立索引" in err, f"err={err}")
    docs = kb.list_documents()
    doc = docs[0] if docs else {}
    ok("普通失败 status=failed 且 chunks=0",
       doc.get("status") == "failed" and int(doc.get("chunks") or 0) == 0,
       f"status={doc.get('status')} chunks={doc.get('chunks')}")
    ok("普通失败仍然删掉磁盘原文",
       bool(doc.get("path")) and not Path(doc["path"]).exists())


def main() -> int:
    try:
        check_single_upload()
        check_dedupe_in_window()
        check_stale_window_clobber()
        check_plain_failure_still_reports()
    finally:
        # 只清临时目录，绝不碰真实 data/knowledge_base
        if "Temp" in str(kb.KB_ROOT) or "tmp" in str(kb.KB_ROOT).lower():
            shutil.rmtree(kb.KB_ROOT, ignore_errors=True)

    fails = [r for r in results if r[0] == "FAIL"]
    print("\n=== SUMMARY ===")
    print(f"total={len(results)} pass={len(results) - len(fails)} fail={len(fails)}")
    if fails:
        print("FAILURES:")
        for _, name, detail in fails:
            print(f"  - {name}: {detail}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
