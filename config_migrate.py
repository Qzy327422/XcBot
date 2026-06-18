import json
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


CONFIG_VERSION_KEY = "config_version"
CURRENT_CONFIG_VERSION = 2
DEFAULT_TEMPLATE_NAME = "config.default.json"


VERSION_COMMENTS = {}


def deep_merge(new_template: dict, old_config: dict, prefix: str = "") -> dict:
    """递归合并：以新版模板为骨架，保留旧配置里的用户自定义值。"""
    result = new_template.copy()
    protected_keys = {CONFIG_VERSION_KEY, "Others.version_name", "Others.project_name"}
    for key, old_value in old_config.items():
        key = str(key)
        full_key = f"{prefix}.{key}" if prefix else key
        if key.startswith("_comment"):
            continue
        if full_key in protected_keys:
            continue
        if key in result:
            new_value = result[key]
            if isinstance(old_value, dict) and isinstance(new_value, dict):
                result[key] = deep_merge(new_value, old_value, full_key)
            elif isinstance(new_value, list) and isinstance(old_value, list):
                result[key] = old_value if old_value else new_value
            elif old_value is None and new_value is not None:
                continue
            else:
                result[key] = old_value
        else:
            result[key] = old_value
    return result


def _is_empty_provider_list(value: Any) -> bool:
    if not isinstance(value, list) or not value:
        return True
    for item in value:
        if not isinstance(item, dict):
            continue
        provider_id = str(item.get("id", "") or "").strip()
        base_url = str(item.get("base_url", "") or "").strip()
        keys = item.get("keys", [])
        models = item.get("models", [])
        has_keys = isinstance(keys, list) and any(str(x).strip() for x in keys)
        has_models = isinstance(models, list) and any(
            (str(m).strip() if isinstance(m, str) else str((m or {}).get("name", "") or (m or {}).get("model", "")).strip())
            for m in models
        )
        if provider_id and base_url and has_keys and has_models:
            return False
    return True


def _normalize_keys(value: Any) -> list[str]:
    if isinstance(value, str):
        return [x.strip() for x in value.splitlines() if x.strip()]
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    return []


def migrate_v14_llm_config(config: dict) -> bool:
    """把 v1.4 的 Others.llm_endpoints 自动转换成 v1.5 提供商结构。"""
    if not isinstance(config, dict):
        return False
    others = config.get("Others")
    if not isinstance(others, dict):
        return False

    changed = False
    endpoints = others.get("llm_endpoints", [])
    providers = others.get("llm_providers", [])
    if isinstance(endpoints, list) and endpoints and _is_empty_provider_list(providers):
        grouped: dict[tuple[str, tuple[str, ...]], dict] = {}
        order: list[tuple[str, tuple[str, ...]]] = []
        for ep in endpoints:
            if not isinstance(ep, dict):
                continue
            base_url = str(ep.get("base_url", "") or "").strip()
            model = str(ep.get("model", "") or "").strip()
            keys = _normalize_keys(ep.get("keys", []))
            if not base_url or not model or not keys:
                continue
            key = (base_url, tuple(keys))
            if key not in grouped:
                provider_id = f"provider{len(order) + 1}"
                grouped[key] = {"id": provider_id, "base_url": base_url, "keys": keys, "models": [], "detected_models": []}
                order.append(key)
            try:
                timeout = int(float(ep.get("timeout_seconds", others.get("api_request_timeout_seconds", 60)) or 60))
            except Exception:
                timeout = int(others.get("api_request_timeout_seconds", 60) or 60)
            grouped[key]["models"].append({
                "name": model,
                "enabled": True,
                "supports_multimodal": bool(ep.get("supports_multimodal", False)),
                "timeout_seconds": max(1, timeout),
            })
        new_providers = [grouped[key] for key in order]
        if new_providers:
            others["llm_providers"] = new_providers
            others["llm_rotation"] = [
                {"provider_id": provider["id"], "model": model["name"]}
                for provider in new_providers
                for model in provider.get("models", [])
            ]
            changed = True

    prompt = str(others.get("personality_prompt", "") or "")
    presets = others.get("personality_presets", [])
    if prompt and (not isinstance(presets, list) or not presets):
        others["personality_presets"] = [{"id": "default", "name": "默认", "prompt": prompt}]
        others["active_personality_preset"] = "default"
        changed = True

    return changed


def collect_keys(d: dict, prefix: str = "") -> set[str]:
    keys = set()
    for k, v in d.items():
        if str(k).startswith("_comment"):
            continue
        full = f"{prefix}.{k}" if prefix else str(k)
        keys.add(full)
        if isinstance(v, dict):
            keys |= collect_keys(v, full)
    return keys


def load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, dict) else {}


def write_json(path: Path, data: dict) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)
        f.write("\n")


def get_program_version(data: dict) -> str:
    others = data.get("Others", {}) if isinstance(data, dict) else {}
    if isinstance(others, dict):
        return str(others.get("version_name", "") or "").strip()
    return ""


def get_config_version(data: dict) -> int:
    if not isinstance(data, dict):
        return 0
    try:
        return int(data.get(CONFIG_VERSION_KEY, 0) or 0)
    except (TypeError, ValueError):
        return 0


def looks_like_v14_config(data: dict) -> bool:
    others = data.get("Others", {}) if isinstance(data, dict) else {}
    if not isinstance(others, dict):
        return False
    if get_config_version(data) > 0:
        return False
    endpoints = others.get("llm_endpoints", [])
    providers = others.get("llm_providers", [])
    return isinstance(endpoints, list) and bool(endpoints) and _is_empty_provider_list(providers)


def sync_personality_prompt_to_presets(config: dict) -> bool:
    others = config.get("Others") if isinstance(config, dict) else None
    if not isinstance(others, dict):
        return False
    prompt = str(others.get("personality_prompt", "") or "")
    if not prompt:
        return False
    presets = others.get("personality_presets", [])
    changed = False
    if not isinstance(presets, list) or not presets:
        others["personality_presets"] = [{"id": "default", "name": "默认", "prompt": prompt}]
        others["active_personality_preset"] = "default"
        return True
    active = str(others.get("active_personality_preset", "") or "default")
    target = None
    for item in presets:
        if isinstance(item, dict) and str(item.get("id", "") or "") == active:
            target = item
            break
    if target is None:
        target = next((item for item in presets if isinstance(item, dict)), None)
    if isinstance(target, dict) and target.get("prompt") != prompt:
        target["prompt"] = prompt
        changed = True
    if not others.get("active_personality_preset") and isinstance(target, dict):
        others["active_personality_preset"] = str(target.get("id", "default") or "default")
        changed = True
    return changed


def default_template_path_for(config_path: Path) -> Path:
    local_template = config_path.resolve().parent / DEFAULT_TEMPLATE_NAME
    if local_template.exists():
        return local_template
    return Path(__file__).resolve().parent / DEFAULT_TEMPLATE_NAME


def ensure_config_up_to_date(config_path: str = "config.json", template_path: str | None = None,
                             backup_dir: str | None = None) -> bool:
    """启动时自动补齐旧 config.json 缺失字段，并写回最新配置版本号。

    返回 True 表示配置文件被修改；False 表示无需迁移或迁移失败。
    迁移原则：新版模板提供结构、注释和默认值；用户已有配置优先保留。
    """
    config_file = Path(config_path)
    template_file = Path(template_path) if template_path else default_template_path_for(config_file)

    if not config_file.exists():
        if template_file.exists():
            shutil.copy2(template_file, config_file)
            print(f"[配置升级] 未找到 config.json，已从默认模板创建: {config_file}")
            return True
        return False

    if not template_file.exists():
        print(f"[配置升级] 未找到默认配置模板，跳过自动补齐: {template_file}")
        return False

    try:
        current = load_json(config_file)
        template = load_json(template_file)
    except Exception as e:
        print(f"[配置升级] 读取配置失败，跳过自动补齐: {e}")
        return False

    template_config_version = get_config_version(template) or CURRENT_CONFIG_VERSION
    current_config_version = get_config_version(current)
    template_program_version = get_program_version(template)
    current_program_version = get_program_version(current)

    merged = deep_merge(template, current)
    provider_migrated = migrate_v14_llm_config(merged)
    personality_migrated = sync_personality_prompt_to_presets(merged)
    merged[CONFIG_VERSION_KEY] = max(template_config_version, CURRENT_CONFIG_VERSION)
    for key, value in VERSION_COMMENTS.items():
        merged.setdefault(key, value)

    current_keys = collect_keys(current)
    template_keys = collect_keys(template)
    missing_keys = sorted(template_keys - current_keys)
    legacy_migrated = looks_like_v14_config(current)
    version_upgraded = current_config_version < merged[CONFIG_VERSION_KEY]

    if not version_upgraded and not missing_keys and not provider_migrated and not personality_migrated and not legacy_migrated:
        return False

    try:
        backup_root = Path(backup_dir) if backup_dir else config_file.resolve().parent / "config_backup"
        backup_root.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = backup_root / f"config-before-auto-upgrade_{timestamp}.json"
        shutil.copy2(config_file, backup_path)

        write_json(config_file, merged)
        version_text = f"{current_config_version or 'legacy'} -> {merged[CONFIG_VERSION_KEY]}"
        program_text = f"，程序版本 {current_program_version or 'unknown'} -> {template_program_version}" if template_program_version and current_program_version != template_program_version else ""
        print(
            f"[配置升级] 已自动迁移/补齐 config.json，配置版本 {version_text}{program_text}，"
            f"新增字段 {len(missing_keys)} 个，备份: {backup_path}"
        )
        return True
    except Exception as e:
        print(f"[配置升级] 写回配置失败: {e}")
        return False


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

    old_config = load_json(old_file)
    new_template = load_json(new_file)
    merged = deep_merge(new_template, old_config)
    migrate_v14_llm_config(merged)
    sync_personality_prompt_to_presets(merged)
    merged[CONFIG_VERSION_KEY] = max(get_config_version(new_template) or CURRENT_CONFIG_VERSION, CURRENT_CONFIG_VERSION)
    for key, value in VERSION_COMMENTS.items():
        merged.setdefault(key, value)

    backup.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    old_backup_name = f"config-old_{timestamp}.json"
    new_backup_name = f"config-new_{timestamp}.json"

    shutil.copy2(old_file, backup / old_backup_name)
    shutil.copy2(new_file, backup / new_backup_name)

    print(f"[备份] 老配置 -> {backup / old_backup_name}")
    print(f"[备份] 新模板 -> {backup / new_backup_name}")

    write_json(new_file, merged)
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
        if sys.argv[1] in {"--auto", "auto"}:
            target = sys.argv[2] if len(sys.argv) > 2 else new_path
            ensure_config_up_to_date(target)
            sys.exit(0)
        old_path = sys.argv[1]
    if len(sys.argv) > 2:
        new_path = sys.argv[2]
    if len(sys.argv) > 3:
        backup_dir = sys.argv[3]

    migrate(old_path, new_path, backup_dir)
