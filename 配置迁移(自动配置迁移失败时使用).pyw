# -*- coding: utf-8 -*-
"""
XcBot 配置迁移 GUI（零外部依赖）

只需点一下"选择旧 config.json"，再点"迁移到新版本 X.X.X"，
程序就会把旧配置合并进当前目录下的 config.json，并自动备份。

特点：
    * 纯 tkinter，标准库就够，不需要 pip install 任何东西
    * 新模板固定为脚本所在目录的 config.json，无需用户挑路径
    * 备份目录固定为同目录下 config_backup/，自动创建
    * 合并算法复用 config_migrate.deep_merge：
        - dict 深合并
        - list：旧值非空保留用户自定义；旧值为空采用新模板默认
        - 标量：旧值优先（version_name / project_name 除外）

使用方式：
    python migrate_gui.py
"""

from __future__ import annotations

import json
import shutil
import sys
import threading
import traceback
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

BASE_DIR = Path(__file__).resolve().parent
NEW_CONFIG_PATH = BASE_DIR / "config.json"
BACKUP_DIR = BASE_DIR / "config_backup"

# 复用项目里已经测试过的合并函数
try:
    from config_migrate import deep_merge  # type: ignore
except Exception as e:  # pragma: no cover
    print(f"无法导入 config_migrate.deep_merge: {e}")
    sys.exit(1)


def _read_json_safe(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} 顶层不是对象/字典")
    return data


def _collect_keys(d: dict, prefix: str = "") -> set:
    keys = set()
    if not isinstance(d, dict):
        return keys
    for k, v in d.items():
        if str(k).startswith("_comment"):
            continue
        full = f"{prefix}.{k}" if prefix else k
        keys.add(full)
        if isinstance(v, dict):
            keys |= _collect_keys(v, full)
    return keys


def _detect_new_version(template: dict) -> str:
    try:
        v = str((template.get("Others") or {}).get("version_name", "") or "").strip()
        if v:
            return v
    except Exception:
        pass
    return ""


class MigrateApp:
    PAD = 12

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.old_path_var = tk.StringVar(value="")
        self.btn_label_var = tk.StringVar(value="迁移到新版本")
        self.new_version = ""
        self._running = False

        self._build_ui()
        self._refresh_new_version()

    # ---------- UI ----------

    def _build_ui(self) -> None:
        self.root.title("XcBot 配置迁移工具")
        self.root.geometry("680x520")
        self.root.minsize(560, 440)

        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("Drop.TButton", padding=24, font=("Segoe UI", 11))
        style.configure("Primary.TButton", padding=10, font=("Segoe UI", 11, "bold"))
        style.configure("Hint.TLabel", foreground="#666")
        style.configure("Path.TLabel", foreground="#1f6feb")

        outer = ttk.Frame(self.root, padding=self.PAD)
        outer.pack(fill="both", expand=True)

        ttk.Label(outer, text="XcBot 配置迁移", font=("Segoe UI", 15, "bold")).pack(anchor="w")
        ttk.Label(
            outer,
            text="把旧版本的 config.json 合并到当前安装目录下的 config.json，"
                 "旧配置和当前文件会先备份到 config_backup/。",
            style="Hint.TLabel",
            wraplength=640,
            justify="left",
        ).pack(anchor="w", pady=(2, 12))

        # 大按钮："点击选择旧 config.json"
        self.pick_btn = ttk.Button(
            outer,
            text="📂  点击选择旧 config.json",
            style="Drop.TButton",
            command=self._pick_old,
        )
        self.pick_btn.pack(fill="x", pady=(0, 12))

        # 当前已选
        row1 = ttk.Frame(outer)
        row1.pack(fill="x", pady=2)
        ttk.Label(row1, text="旧配置：", width=10).pack(side="left")
        ttk.Label(row1, textvariable=self.old_path_var, style="Path.TLabel", anchor="w").pack(
            side="left", fill="x", expand=True
        )

        # 新模板（固定，只展示不可改）
        row2 = ttk.Frame(outer)
        row2.pack(fill="x", pady=2)
        ttk.Label(row2, text="目标文件：", width=10).pack(side="left")
        ttk.Label(row2, text=str(NEW_CONFIG_PATH), style="Path.TLabel", anchor="w").pack(
            side="left", fill="x", expand=True
        )

        row3 = ttk.Frame(outer)
        row3.pack(fill="x", pady=2)
        ttk.Label(row3, text="备份目录：", width=10).pack(side="left")
        ttk.Label(row3, text=str(BACKUP_DIR), style="Path.TLabel", anchor="w").pack(
            side="left", fill="x", expand=True
        )

        # 主按钮 & 状态
        action = ttk.Frame(outer)
        action.pack(fill="x", pady=(14, 6))
        self.run_btn = ttk.Button(
            action,
            textvariable=self.btn_label_var,
            style="Primary.TButton",
            command=self._on_migrate,
            state="disabled",
        )
        self.run_btn.pack(side="left")
        ttk.Button(action, text="清空日志", command=self._clear_log).pack(side="left", padx=8)
        ttk.Button(action, text="退出", command=self.root.destroy).pack(side="right")

        # 日志
        ttk.Label(outer, text="迁移日志：").pack(anchor="w", pady=(8, 2))
        log_wrap = ttk.Frame(outer)
        log_wrap.pack(fill="both", expand=True)
        self.log = tk.Text(log_wrap, height=12, wrap="word", state="disabled")
        self.log.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(log_wrap, command=self.log.yview)
        scroll.pack(side="right", fill="y")
        self.log.configure(yscrollcommand=scroll.set)

    # ---------- 状态 ----------

    def _refresh_new_version(self) -> None:
        if not NEW_CONFIG_PATH.exists():
            self.new_version = ""
            self.btn_label_var.set("迁移到新版本（未找到当前 config.json）")
            self._log(f"⚠️ 未找到目标文件：{NEW_CONFIG_PATH}")
            return
        try:
            data = _read_json_safe(NEW_CONFIG_PATH)
            self.new_version = _detect_new_version(data)
            label_ver = self.new_version or "（未识别版本号）"
            self.btn_label_var.set(f"迁移到新版本 {label_ver}")
        except Exception as e:
            self.new_version = ""
            self.btn_label_var.set("迁移到新版本（目标文件解析失败）")
            self._log(f"⚠️ 解析目标文件失败：{e}")

    def _pick_old(self) -> None:
        p = filedialog.askopenfilename(
            title="选择旧 config.json",
            filetypes=[("JSON 配置", "*.json"), ("所有文件", "*.*")],
        )
        if not p:
            return
        path = Path(p).resolve()
        if path == NEW_CONFIG_PATH.resolve():
            messagebox.showwarning(
                "提示",
                "你选的就是当前目录下的 config.json 本身，不需要迁移。\n请选择旧版本的 config.json。",
            )
            return
        try:
            _read_json_safe(path)
        except Exception as e:
            messagebox.showerror("无法读取", f"{path}\n\n{e}")
            return
        self.old_path_var.set(str(path))
        self.run_btn.configure(state="normal")
        self._log(f"已选择旧配置：{path}")

    # ---------- 迁移 ----------

    def _on_migrate(self) -> None:
        if self._running:
            return
        old_path_str = self.old_path_var.get().strip()
        if not old_path_str:
            messagebox.showerror("错误", "请先选择旧 config.json")
            return
        old_path = Path(old_path_str)
        if not old_path.exists():
            messagebox.showerror("错误", f"旧 config.json 不存在：\n{old_path}")
            return
        if not NEW_CONFIG_PATH.exists():
            messagebox.showerror("错误", f"目标文件不存在：\n{NEW_CONFIG_PATH}")
            return

        ver_text = self.new_version or "新版本"
        if not messagebox.askyesno(
            "确认迁移",
            f"将把\n  {old_path}\n合并到当前目录下的\n  {NEW_CONFIG_PATH}\n\n"
            f"两个文件都会先备份到\n  {BACKUP_DIR}\n\n是否迁移到 {ver_text}？",
        ):
            return

        self._running = True
        self.run_btn.configure(state="disabled")
        threading.Thread(
            target=self._do_migrate_async,
            args=(old_path,),
            daemon=True,
        ).start()

    def _do_migrate_async(self, old_path: Path) -> None:
        try:
            self._migrate(old_path)
        except Exception as e:
            self._log("迁移失败：" + str(e))
            self._log(traceback.format_exc())
            self._ui(lambda: messagebox.showerror("迁移失败", str(e)))
        finally:
            self._running = False
            self._ui(lambda: self.run_btn.configure(state="normal"))

    def _migrate(self, old_path: Path) -> None:
        self._log("=" * 56)
        self._log(f"开始迁移：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        self._log(f"旧配置   : {old_path}")
        self._log(f"目标文件 : {NEW_CONFIG_PATH}")

        old_config = _read_json_safe(old_path)
        new_template = _read_json_safe(NEW_CONFIG_PATH)
        ver = _detect_new_version(new_template) or "未识别版本号"

        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        bak_old = BACKUP_DIR / f"config-old_{ts}.json"
        bak_new = BACKUP_DIR / f"config-new_{ts}.json"
        bak_latest = BACKUP_DIR / "config-old_latest.json"
        shutil.copy2(old_path, bak_old)
        shutil.copy2(NEW_CONFIG_PATH, bak_new)
        shutil.copy2(old_path, bak_latest)
        self._log(f"已备份旧配置 → {bak_old.name}")
        self._log(f"已备份目标文件 → {bak_new.name}")

        merged = deep_merge(new_template, old_config)
        with NEW_CONFIG_PATH.open("w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=4)
            f.write("\n")
        self._log(f"✅ 合并完成，已写入 {NEW_CONFIG_PATH}")

        # 统计
        old_keys = _collect_keys(old_config)
        new_keys = _collect_keys(new_template)
        migrated = old_keys & new_keys
        added = new_keys - old_keys
        removed = old_keys - new_keys
        self._log("--- 合并统计 ---")
        self._log(f"从旧配置迁移的字段数: {len(migrated)}")
        self._log(f"新版本新增的字段数  : {len(added)}（使用新默认值）")
        if removed:
            self._log(f"旧配置有但新版已移除: {len(removed)}")
            for k in sorted(removed):
                self._log(f"  - {k}")
        self._log(f"✅ 已成功迁移到新版本 {ver}")

        self._ui(lambda: messagebox.showinfo(
            "迁移完成",
            f"已成功迁移到新版本 {ver}\n\n字段迁移 {len(migrated)} 项，新增 {len(added)} 项。\n\n"
            f"原 config.json 已备份至：\n{bak_new.name}",
        ))

    # ---------- 日志/线程 ----------

    def _log(self, text: str) -> None:
        line = str(text)

        def _do():
            self.log.configure(state="normal")
            self.log.insert("end", line + "\n")
            self.log.see("end")
            self.log.configure(state="disabled")

        self._ui(_do)

    def _clear_log(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _ui(self, fn) -> None:
        # 后台线程里安全切回 UI 线程
        try:
            self.root.after(0, fn)
        except Exception:
            try:
                fn()
            except Exception:
                pass


def main() -> int:
    root = tk.Tk()
    MigrateApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
