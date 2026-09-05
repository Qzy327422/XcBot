# -*- coding: utf-8 -*-
"""配置备份归档到 config_backup/ 的验证（离线，临时目录，不碰真实文件）。

只 import webui_core.json_io，不 import main/webui，因此**不会**获取
目录独占锁 my_bot.lock，可以在 bot 运行时跑。

检查点：
  1) 首次写入不产生备份；后续写入的 .bak 落在同级 config_backup/，根目录不留 .bak；
  2) 只保留最近 5 份，且清理会把旧的根目录残留一起算进配额；
  3) 主文件损坏时能从新目录恢复，也能从旧的根目录位置恢复（升级前的备份不失效）；
  4) config_migrate 的 config-before-auto-upgrade_*.json 不被 .bak 清理规则误删；
  5) 备份目录跟随被备份文件所在目录（data/mcp_server.json → data/config_backup/）；
  6) 备份失败不阻断保存。

用法：PYTHONIOENCODING=utf-8 python tools/verify_config_backup.py
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from webui_core.json_io import (  # noqa: E402
    _BACKUP_DIR_NAME,
    _iter_backups,
    backup_dir_for,
    read_json,
    write_json,
)

results: list[tuple[str, str, str]] = []


def ok(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    results.append((status, name, detail))
    print(f"[{status}] {name}" + (f" - {detail}" if detail else ""))


def baks(d: Path) -> list[str]:
    if not d.exists():
        return []
    return sorted(p.name for p in d.iterdir() if p.name.endswith(".bak"))


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="xcbot_bak_"))
    try:
        cfg = tmp / "config.json"
        bdir = tmp / _BACKUP_DIR_NAME

        # 1) 首写无备份
        write_json(cfg, {"n": 0})
        ok("首次写入不产生备份", not bdir.exists() or not baks(bdir), f"{baks(bdir)}")
        ok("根目录无 .bak", not [p for p in tmp.glob("*.bak")])

        # 2) 连写后备份进子目录、根目录干净、只留 5 份
        for i in range(1, 9):
            time.sleep(0.01)
            write_json(cfg, {"n": i})
        ok("备份落在 config_backup/", len(baks(bdir)) > 0, f"{len(baks(bdir))} 份")
        ok("根目录仍无 .bak", not [p for p in tmp.glob("*.bak")],
           f"{[p.name for p in tmp.glob('*.bak')]}")
        ok("只保留 5 份", len(baks(bdir)) == 5, f"{len(baks(bdir))}")
        ok("主文件是最新内容", read_json(cfg, {}).get("n") == 8)

        # 3a) 损坏后从新目录恢复
        cfg.write_text('{"n": <<BROKEN', encoding="utf-8")
        recovered = read_json(cfg, {})
        ok("从 config_backup/ 恢复", recovered.get("n") == 7, f"n={recovered.get('n')}")

        # 3b) 旧的根目录残留也要认
        shutil.rmtree(bdir)
        legacy = tmp / "config.json.20200101000000_000000.bak"
        legacy.write_text('{"legacy":true}', encoding="utf-8")
        ok("认旧位置的备份", read_json(cfg, {}).get("legacy") is True)

        # 3c) 清理把旧位置一起算进配额
        for i in range(6):
            time.sleep(0.01)
            write_json(cfg, {"n": 100 + i})
        left_root = [p.name for p in tmp.glob("*.bak")]
        total = len(left_root) + len(baks(bdir))
        ok("清理含旧位置残留", total <= 5, f"根目录 {left_root} + 新目录 {len(baks(bdir))}")

        # 4) 迁移备份不被误删
        mig = bdir / "config-before-auto-upgrade_20260101_000000_000000.json"
        mig.write_text('{"migrated":true}', encoding="utf-8")
        for i in range(8):
            time.sleep(0.01)
            write_json(cfg, {"n": 200 + i})
        ok("迁移备份未被清理", mig.exists())
        ok("_iter_backups 只认 .bak",
           all(p.name.endswith(".bak") for p in _iter_backups(cfg)))

        # 5) 备份目录跟随文件所在目录
        sub = tmp / "data"
        sub.mkdir(exist_ok=True)
        mcp = sub / "mcp_server.json"
        write_json(mcp, {"mcpServers": {}})
        write_json(mcp, {"mcpServers": {"a": 1}})
        ok("备份目录随文件所在目录", backup_dir_for(mcp) == sub / _BACKUP_DIR_NAME,
           str(backup_dir_for(mcp)))
        ok("子目录备份不落到根目录", not [p for p in tmp.glob("*.bak")])
        ok("data/ 下生成备份", len(baks(sub / _BACKUP_DIR_NAME)) == 1)

        # 6) 备份失败不阻断保存：把备份目录位置占成普通文件，mkdir 必然失败
        cfg2 = tmp / "blocked" / "config.json"
        cfg2.parent.mkdir(parents=True, exist_ok=True)
        write_json(cfg2, {"v": 1})
        (cfg2.parent / _BACKUP_DIR_NAME).write_text("not a dir", encoding="utf-8")
        write_json(cfg2, {"v": 2})
        ok("备份失败仍完成保存", read_json(cfg2, {}).get("v") == 2)

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

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
