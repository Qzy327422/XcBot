import json
import shutil
import os
import sys
from datetime import datetime
from pathlib import Path


def deep_merge(new_template: dict, old_config: dict) -> dict:
    result = new_template.copy()
    for key, old_value in old_config.items():
        if key.startswith("_comment"):
            continue
        if key in result:
            if isinstance(old_value, dict) and isinstance(result[key], dict):
                result[key] = deep_merge(result[key], old_value)
            else:
                result[key] = old_value
    return result


def migrate(old_path: str, new_path: str, backup_dir: str, *, remove_old: bool = False):
    old_file = Path(old_path)
    new_file = Path(new_path)
    backup = Path(backup_dir)

    if not old_file.exists():
        print(f"[错误] 找不到老配置文件: {old_path}")
        raise FileNotFoundError(old_path)

    if not new_file.exists():
        print(f"[错误] 找不到新配置模板: {new_path}")
        raise FileNotFoundError(new_path)

    with open(old_file, "r", encoding="utf-8") as f:
        old_config = json.load(f)

    with open(new_file, "r", encoding="utf-8") as f:
        new_template = json.load(f)

    merged = deep_merge(new_template, old_config)

    backup.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    old_backup_name = f"config-old_{timestamp}.json"
    new_backup_name = f"config-new_{timestamp}.json"

    shutil.copy2(old_file, backup / old_backup_name)
    shutil.copy2(new_file, backup / new_backup_name)

    print(f"[备份] 老配置 -> {backup / old_backup_name}")
    print(f"[备份] 新模板 -> {backup / new_backup_name}")

    with open(new_file, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)

    print(f"[完成] 合并后的配置已写入: {new_path}")

    latest_backup_path = backup / "config-old_latest.json"
    shutil.copy2(old_file, latest_backup_path)
    if remove_old:
        try:
            old_file.unlink()
        except Exception:
            pass
        print(f"[移动] 老配置文件已移至: {latest_backup_path}")
    else:
        print(f"[保留] 老配置文件已备份到: {latest_backup_path}")

    print("\n--- 合并统计 ---")
    old_keys = set()
    new_keys = set()

    def collect_keys(d, prefix=""):
        keys = set()
        for k, v in d.items():
            if k.startswith("_comment"):
                continue
            full = f"{prefix}.{k}" if prefix else k
            keys.add(full)
            if isinstance(v, dict):
                keys |= collect_keys(v, full)
        return keys

    old_keys = collect_keys(old_config)
    new_keys = collect_keys(new_template)

    migrated = old_keys & new_keys
    added = new_keys - old_keys
    removed = old_keys - new_keys

    print(f"从老配置迁移的字段数: {len(migrated)}")
    print(f"新配置新增的字段数:   {len(added)} (使用新默认值)")
    if removed:
        print(f"老配置有但新版已移除: {len(removed)}")
        for k in sorted(removed):
            print(f"  - {k}")


if __name__ == "__main__":
    script_dir = Path(__file__).parent
    old_path = str(script_dir / "config-old.json")
    new_path = str(script_dir / "config.json")
    backup_dir = str(script_dir / "config_backup")

    if len(sys.argv) > 1:
        old_path = sys.argv[1]
    if len(sys.argv) > 2:
        new_path = sys.argv[2]
    if len(sys.argv) > 3:
        backup_dir = sys.argv[3]

    migrate(old_path, new_path, backup_dir)
