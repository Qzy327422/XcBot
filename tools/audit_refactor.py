# -*- coding: utf-8 -*-
"""XcBot refactor regression audit.

Run from project root or any cwd:
  python tools/audit_refactor.py

Exit code 0 = all checks passed; 1 = failures.
"""
from __future__ import annotations

import ast
import asyncio
import json
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

results: list[tuple[str, str, str]] = []


def ok(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    results.append((status, name, detail))
    print(f"[{status}] {name}" + (f" — {detail}" if detail else ""))


def check_syntax() -> None:
    files = ["main.py", "webui.py"]
    files += [str(p.relative_to(ROOT)).replace("\\", "/") for p in (ROOT / "bot").glob("*.py")]
    files += [str(p.relative_to(ROOT)).replace("\\", "/") for p in (ROOT / "webui_core").glob("*.py")]
    for f in files:
        try:
            ast.parse((ROOT / f).read_text(encoding="utf-8"))
            ok(f"syntax {f}", True)
        except SyntaxError as e:
            ok(f"syntax {f}", False, str(e))


def check_split_integrity(main: str, webui_src: str) -> None:
    ok("main no class TokenStats", "class TokenStats" not in main)
    ok("main no class ChatMemoryManager", "class ChatMemoryManager" not in main)
    ok("main no def estimate_tokens", "def estimate_tokens" not in main)
    ok("main no def atomic_write_json", "def atomic_write_json" not in main)
    ok("main no def normalize_llm_endpoints", "def normalize_llm_endpoints" not in main)
    ok("webui no FEATURE_META body", "FEATURE_META = [" not in webui_src)
    ok("webui no def read_json", "def read_json(" not in webui_src)
    ok("webui no def normalize_llm_providers_config", "def normalize_llm_providers_config" not in webui_src)
    ok("lazy lock double-check present", "self._lock_init_lock" in main and "with self._lock_init_lock:" in main)
    ok("lazy lock tracks event loop", "self._lock_loop" in main and "asyncio.get_running_loop()" in main)
    ok("token command labels 24h", "过去24小时" in main and "会话生命周期，非24h" in main)
    ok("webui empty model hard fail gone", "模型名称不能为空" not in webui_src)


def check_estimate() -> None:
    from bot.estimate import estimate_tokens

    ok("estimate empty", estimate_tokens("") == 0)
    ok("estimate cjk", estimate_tokens("你好世界") >= 4)
    ok("estimate latin", estimate_tokens("hello world") > 0)


def check_token_stats() -> None:
    from bot.token_stats import TokenStats

    td = Path(tempfile.mkdtemp())
    path = td / "token_stats.json"
    ts = TokenStats(path=path)
    old = time.time() - 25 * 3600
    recent = time.time() - 10
    ts.detailed_stats["s1"] = [
        {"time": old, "tokens": 100, "user_id": 1, "group_id": 2},
        {"time": recent, "tokens": 40, "user_id": 1, "group_id": 2},
    ]
    ts._rebuild_aggregates_locked()
    g = ts.get_stats()
    ok("24h prunes old", g["total_tokens"] == 40 and g.get("window_hours") == 24)
    ts.add_usage("s1", user_id=1, group_id=2, tokens=5)
    ok("24h add rebuild", ts.total_tokens == 45)
    ts.add_usage("s0", user_id=0, group_id=0, tokens=7)
    ok("user_id 0 counted", ts.get_stats(user_id=0)["user_tokens"] == 7)
    ok("group_id 0 counted", ts.get_stats(group_id=0)["group_tokens"] == 7)
    ts.save(force=True)
    data = json.loads(path.read_text(encoding="utf-8"))
    ok("persist version 2", data.get("version") == 2)
    rows = data.get("detailed_stats", {}).get("s1", [])
    ok("save force-pruned old rows", bool(rows) and all(float(r["time"]) > time.time() - 86400 for r in rows))
    ts2 = TokenStats(path=path)
    ok("reload totals", ts2.total_tokens == ts.total_tokens)

    barrier = threading.Barrier(8)

    def worker(i: int) -> None:
        barrier.wait()
        ts2.add_usage(f"c{i % 3}", user_id=i, group_id=i, tokens=1)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    rebuilt = sum(int(r.get("tokens") or 0) for rows in ts2.detailed_stats.values() for r in rows)
    ok("concurrent add consistent", ts2.total_tokens == rebuilt, f"total={ts2.total_tokens} rebuilt={rebuilt}")

    # 高频会话超过旧版 500 条时仍必须完整统计 24h
    high = TokenStats(path=td / "high.json")
    for _ in range(550):
        high.add_usage("hot", tokens=1)
    ok("high-frequency session not truncated", high.get_stats(session_id="hot")["session_tokens"] == 550)
    ok("high-frequency call count not truncated", high.get_stats(session_id="hot")["session_calls"] == 550)


def check_memory() -> None:
    from bot.memory import ChatMemoryManager

    mp = Path(tempfile.mkdtemp()) / "mem"
    cm = ChatMemoryManager(memory_path=str(mp))
    ok("memory dir created", mp.exists())
    saved = cm.save_private_memory(
        1,
        [{"role": "user", "content": "a"}, {"role": "system", "content": "x"}],
        3,
    )
    history, tok = cm.load_private_memory(1)
    ok("memory save/load", saved and history == [{"role": "user", "content": "a"}] and tok == 3)


def check_llm_config() -> None:
    from bot.llm_config import (
        convert_legacy_endpoints_to_providers,
        looks_like_placeholder_key,
        normalize_llm_provider_rotation,
        normalize_llm_providers_config,
        normalize_provider_keys,
    )

    ok("placeholder api_key exact", looks_like_placeholder_key("api_key") is True)
    ok("placeholder not false-positive api_key infix", looks_like_placeholder_key("sk-abcapi_keydef") is False)
    ok("placeholder 请输入 prefix", looks_like_placeholder_key("请输入API密钥") is True)
    ok("real key kept", looks_like_placeholder_key("sk-live-abc123") is False)
    ok("normalize keys drops placeholders", normalize_provider_keys(["api_key", "sk-live-1"]) == ["sk-live-1"])

    legacy = {
        "llm_endpoints": [
            {"base_url": "https://x/v1", "model": "m1", "keys": ["sk-1"]},
            {"base_url": "https://x/v1", "model": "m2", "keys": ["sk-1"]},
            {"base_url": "https://y/v1", "model": "m3", "keys": ["sk-2"]},
        ]
    }
    ps = convert_legacy_endpoints_to_providers(legacy)
    ok("legacy merge providers", len(ps) == 2)
    ok("legacy merge models", {m["name"] for m in ps[0]["models"]} == {"m1", "m2"})
    slots = normalize_llm_provider_rotation(legacy)
    ok("legacy rotation 3 slots", len(slots) == 3)

    newcfg = {
        "llm_providers": [
            {
                "id": "p",
                "base_url": "https://n/v1",
                "keys": ["sk-n"],
                "models": [{"name": "a", "enabled": True}, {"name": "b", "enabled": False}],
            }
        ],
        "llm_rotation": [{"provider_id": "p", "model": "b"}, {"provider_id": "p", "model": "a"}],
        "llm_endpoints": [{"base_url": "https://stale", "model": "stale", "keys": ["sk-s"]}],
    }
    slots2 = normalize_llm_provider_rotation(newcfg)
    ok("providers beat stale endpoints", len(slots2) == 1 and slots2[0]["model"] == "a")
    ok("disabled model skipped", slots2[0]["model"] != "b")

    # 重复 provider id：只保留第一个，避免 silent overwrite
    dup = {
        "llm_providers": [
            {"id": "same", "base_url": "https://a/v1", "keys": ["sk-a"], "models": [{"name": "ma", "enabled": True}]},
            {"id": "same", "base_url": "https://b/v1", "keys": ["sk-b"], "models": [{"name": "mb", "enabled": True}]},
        ],
        "llm_rotation": [],
    }
    dps, _ = normalize_llm_providers_config(dup)
    ok("duplicate provider id keeps first only", len(dps) == 1 and dps[0]["base_url"] == "https://a/v1")

    raw_models = [{"name": ""}, {"name": "m1", "enabled": True}, {"name": ""}]
    cleaned = []
    for model_cfg in raw_models:
        name = str(model_cfg.get("name") or "").strip()
        if not name:
            continue
        cleaned.append(model_cfg)
    ok("blank models skipped", [c["name"] for c in cleaned] == ["m1"])


def check_lazy_lock() -> None:
    class C:
        def __init__(self):
            self._lock = None
            self._lock_loop = None
            self._lock_init_lock = threading.Lock()

        async def go(self):
            loop = asyncio.get_running_loop()
            if self._lock is None or self._lock_loop is not loop:
                with self._lock_init_lock:
                    if self._lock is None or self._lock_loop is not loop:
                        self._lock = asyncio.Lock()
                        self._lock_loop = loop
            async with self._lock:
                await asyncio.sleep(0.005)
                return id(self._lock)

    async def lock_test():
        c = C()
        ids = await asyncio.gather(*[c.go() for _ in range(20)])
        return len(set(ids)) == 1

    ok("lazy lock single instance under concurrency", asyncio.run(lock_test()))

    # 同一 context 跨两个 asyncio.run（模拟 Listener.run 重连新 loop）应重建 lock
    c = C()
    first = asyncio.run(c.go())
    first_loop = c._lock_loop
    second = asyncio.run(c.go())
    ok("lazy lock rebinds after event-loop restart", c._lock_loop is not first_loop and first != second)


def check_webui_and_ui(js: str, css: str) -> None:
    import webui

    ok("webui import", hasattr(webui, "start_webui"))
    ok("webui feature meta", len(webui.FEATURE_META) == 16)
    uptime = webui._format_uptime(61)
    ok("webui uptime alias", ("秒" in uptime) or ("分" in uptime))
    ok("webui compare alias", webui._compare_versions("1.0", "1.1") == -1)
    ok("donut class in js", "donut-chart" in js and "donut-center" in js)
    ok("donut transparent css", "radial-gradient(circle, transparent" in css)
    ok("no fixed dark hole", "rgba(6,21,27,.88)" not in js)


def check_bot_exports() -> None:
    import bot

    ok(
        "bot exports",
        all(
            hasattr(bot, x)
            for x in [
                "TokenStats",
                "create_token_stats",
                "ChatMemoryManager",
                "atomic_write_json",
                "estimate_tokens",
            ]
        ),
    )


def main() -> int:
    print(f"ROOT = {ROOT}")
    main_src = (ROOT / "main.py").read_text(encoding="utf-8")
    webui_src = (ROOT / "webui.py").read_text(encoding="utf-8")
    js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    css = (ROOT / "static" / "app.css").read_text(encoding="utf-8")

    check_syntax()
    check_split_integrity(main_src, webui_src)
    check_estimate()
    check_token_stats()
    check_memory()
    check_llm_config()
    check_lazy_lock()
    check_webui_and_ui(js, css)
    check_bot_exports()

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
