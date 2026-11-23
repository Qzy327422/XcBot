# -*- coding: utf-8 -*-
"""统一角色解析：纯函数，不读写文件、不启动后台任务。

`resolve_persona` 逐条对齐 `main.EnhancedContextManager._get_system_prompt`
的预设/跟随/覆盖/空内容报错语义；额外给出稳定的 `role_id` 与只随角色自身
人设模板变化的 `revision`，供角色生活按 (账号, 角色) 隔离存储。

revision 绝不包含聊天对方的称呼：同一个人设在私聊 A 和 B 中是同一个角色，
不能让生活任务因称呼不同而互相作废。渲染称呼走 `Persona.render`。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Mapping

# 与 webui.sync_personality_presets 的首项、config_migrate 的旧人格映射一致。
DEFAULT_ROLE_ID = "default"

_SAFE_PREFIX_RE = re.compile(r"^[A-Za-z0-9_-]{0,32}$")


@dataclass(frozen=True)
class Persona:
    """一次解析结果。

    `raw_prompt` 是**未渲染**的人设模板（保留 `{bot_name}` / `{user_name}`
    占位符）；`revision` 只由角色身份与模板决定，与具体聊天对象无关。
    """

    role_id: str
    raw_prompt: str
    revision: str
    preset_id: str
    name: str

    def render(self, user_name: str = "", bot_name: str = "") -> str:
        prompt = self.raw_prompt
        if bot_name:
            prompt = prompt.replace("{bot_name}", bot_name)
        if user_name:
            prompt = prompt.replace("{user_name}", user_name)
        return prompt


def render_persona(persona: Persona, user_name: str = "", bot_name: str = "") -> str:
    return persona.render(user_name=user_name, bot_name=bot_name)


def _preset_map(presets: Any) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    if not isinstance(presets, list):
        return result
    for item in presets:
        if not isinstance(item, dict):
            continue
        pid = str(item.get("id", "") or "").strip()
        if not pid or pid in result:
            continue
        result[pid] = {
            "prompt": str(item.get("prompt", "") or ""),
            "name": str(item.get("name", "") or pid),
        }
    return result


def _revision(role_id: str, prompt: str, bot_name: str) -> str:
    digest = hashlib.sha256()
    digest.update(role_id.encode("utf-8"))
    digest.update(b"\x00")
    # bot_name 属于角色共同配置（同一角色所有会话一致），不含对方称呼。
    digest.update(str(bot_name or "").encode("utf-8"))
    digest.update(b"\x00")
    digest.update(prompt.encode("utf-8"))
    return digest.hexdigest()[:16]


def resolve_persona(
    others: Mapping[str, Any],
    *,
    bot_name: str,
    user_name: str = "",
    context_type: str = "",
    preset_id_override: str = "",
) -> Persona:
    """按会话类型解析当前角色。

    - `group`/`private` 使用对应键；空串表示跟随默认人格。
    - `preset_id_override` 只在该预设仍存在时生效，已删除则静默回退默认。
    - 选中的预设存在但内容为空时抛 ValueError（与 main 的消息一致），
      不静默回退到 personality_prompt。
    - 没有选中预设时使用 personality_prompt，`role_id` 固定为 `default`。
    - `user_name` 只在本调用返回的 Persona 上用于渲染，不参与 revision；
      需要渲染时用 `persona.render(user_name=...)`。
    """
    presets = others.get("personality_presets", []) if isinstance(others, Mapping) else []
    preset_map = _preset_map(presets)
    active_key = (
        "group_personality_preset" if context_type == "group"
        else "private_personality_preset" if context_type == "private"
        else "active_personality_preset"
    )
    selected = str(others.get(active_key, "") or "").strip() \
        or str(others.get("active_personality_preset", "") or "").strip()
    override = str(preset_id_override or "").strip()
    if override and override in preset_map:
        selected = override

    if selected in preset_map:
        custom_prompt = preset_map[selected]["prompt"].strip()
        if not custom_prompt:
            name = preset_map[selected]["name"] or selected
            raise ValueError(
                f"人格预设「{name}」内容为空：请在 WebUI 人格设定页面填写人设，或改用其他预设"
            )
        role_id = selected
        preset_name = preset_map[selected]["name"] or selected
    else:
        custom_prompt = str(others.get("personality_prompt", "") or "").strip()
        role_id = DEFAULT_ROLE_ID
        preset_name = "默认"

    if not custom_prompt:
        raise ValueError("主对话系统提示词为空：请在 WebUI 人格设定页面填写人设")

    return Persona(
        role_id=role_id,
        raw_prompt=custom_prompt,
        revision=_revision(role_id, custom_prompt, bot_name),
        preset_id=selected if selected in preset_map else "",
        name=preset_name,
    )


def persona_role_key(role_id: str) -> str:
    """把角色 ID 编码成有界、不可碰撞的目录名。

    所有非空 role_id 都用 sha256(UTF-8) 的 32 位十六进制编码，短安全前缀
    只用于人眼识别，不参与唯一性。空或非法输入直接报错，绝不落到共享
    `_invalid` 目录把不同角色混在一起。
    """
    raw = str(role_id or "")
    if not raw:
        raise ValueError("role_id must be non-empty")
    if len(raw.encode("utf-8")) > 512:
        raise ValueError("role_id too long")
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    prefix = re.sub(r"[^A-Za-z0-9_-]", "_", raw)[:16].strip("_")
    prefix = prefix if _SAFE_PREFIX_RE.match(prefix) else ""
    return f"{prefix}-{digest}" if prefix else digest
