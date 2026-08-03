# -*- coding: utf-8 -*-
"""Agent 工具元数据 —— WebUI 渲染与主程序默认值的唯一来源。

level 两档语义（与项目自身的权限模型一致，main.py 的 load_admin_lists_from_config
把 ROOT_User / Super_User / Manage_User 统一成同一份管理员名单，没有更高一级）：
  user  —— 任何人都能让机器人调用。只放不作用于他人的查询类和当前会话内的操作
  admin —— 仅「权限/名单」里配置的管理用户可用，不设额外限制

default_enabled 全部为 True：工具默认全开，能力完整可用。真正的约束来自 level——
危险工具即使开着，也只有管理用户能让 AI 调用，普通群友调用会被拒绝并收到解释。
用户在 WebUI 改过之后以 config.json 里的值为准。

注意：这里不放插件功能的复制品。插件是独立生态，agent 通过 list_plugins /
call_plugin 两个工具去调用已加载的插件，插件本体不做任何改动。
"""

AGENT_TOOL_META = [
    # ---------- 基础能力：默认全开且不在 WebUI 展示 ----------
    # hidden=True 的工具既不出现在 Agent 页，也不写进 config.json 的 tools 段。
    # 它们要么无副作用（查时间、算数、查群信息），要么只作用于当前会话，
    # 逐个给开关只会让页面和配置都变乱。要临时关掉的话，关 Agent 总开关即可。
    {"key": "get_current_time", "title": "获取当前时间", "level": "user", "default_enabled": True,
     "hidden": True, "group": "基础查询", "desc": "让 AI 知道真实的日期时间，避免瞎猜今天几号"},
    {"key": "calculate", "title": "数学计算", "level": "user", "default_enabled": True,
     "hidden": True, "group": "基础查询", "desc": "精确计算数学表达式，比模型心算可靠"},

    # 联网搜索与读网页共用一张卡片：搜索给摘要、读网页拿全文，是同一件事的两步，
    # 分成两个开关没有意义（只开一个都会让模型半途卡住）。
    {"key": "web_search", "title": "联网搜索", "level": "user", "default_enabled": True,
     "group": "联网", "card": "web",
     "desc": "让 AI 搜索实时信息并打开网页读正文。优先用下方配置的 Tavily/博查，否则回落百度/DuckDuckGo。已禁止访问内网地址"},
    {"key": "fetch_webpage", "title": "读取网页", "level": "user", "default_enabled": True,
     "group": "联网", "card": "web", "desc": ""},

    # ---------- 插件桥接：两个工具是一套动作，合成一张卡片 ----------
    {"key": "call_plugin", "title": "调用插件", "level": "user", "default_enabled": True,
     "group": "插件", "card": "plugin",
     "desc": "让 AI 自主调用 plugins/ 里已加载的插件（如 /天气 /生图），不必让用户自己打命令。含列出插件清单的能力"},
    {"key": "list_plugins", "title": "列出可用插件", "level": "user", "default_enabled": True,
     "group": "插件", "card": "plugin", "desc": ""},

    # ---------- QQ 查询：只读、无副作用，隐藏 ----------
    {"key": "get_group_member_info", "title": "查群成员信息", "level": "user", "default_enabled": True,
     "hidden": True, "group": "QQ 查询", "desc": "查询本群某成员的群名片、群等级、加群时间和身份"},
    {"key": "get_group_info", "title": "查群信息", "level": "user", "default_enabled": True,
     "hidden": True, "group": "QQ 查询", "desc": "查询本群群名和成员数量"},
    {"key": "get_group_member_list", "title": "查群成员列表", "level": "admin", "default_enabled": True,
     "hidden": True, "group": "QQ 查询", "desc": "拉取本群完整成员名单。人多时返回内容很长"},

    # ---------- QQ 操作：只作用于当前会话，隐藏 ----------
    {"key": "send_image", "title": "发送图片", "level": "user", "default_enabled": True,
     "hidden": True, "group": "QQ 操作", "desc": "往当前会话发图，支持网络直链和 AI 自己生成的图片"},
    {"key": "send_file", "title": "发送文件", "level": "user", "default_enabled": True,
     "hidden": True, "group": "QQ 操作", "desc": "把文件发给用户下载。普通用户触发时只能发会话工作目录内的文件"},
    {"key": "send_poke", "title": "拍一拍", "level": "user", "default_enabled": True,
     "hidden": True, "group": "QQ 操作", "desc": "拍一拍当前会话里的某个人"},
    {"key": "send_message", "title": "主动汇报消息", "level": "user", "default_enabled": True,
     "hidden": True, "group": "QQ 操作", "desc": "让 AI 在长任务中间汇报进度、或把长结果分批发到当前会话"},

    # ---------- 群管理：有真实且难撤销的副作用，保留独立开关 ----------
    {"key": "set_group_ban", "title": "禁言群成员", "level": "admin", "default_enabled": True,
     "group": "群管理", "desc": "禁言本群成员，最长 30 天"},
    {"key": "recall_message", "title": "撤回消息", "level": "admin", "default_enabled": True,
     "group": "群管理", "desc": "撤回指定消息（需要机器人是管理员）"},
    {"key": "set_group_card", "title": "改群名片", "level": "admin", "default_enabled": True,
     "group": "群管理", "desc": "修改本群某成员的群名片"},
    {"key": "set_essence_msg", "title": "设精华消息", "level": "admin", "default_enabled": True,
     "group": "群管理", "desc": "把指定消息设为群精华"},

    # ---------- 定时任务 ----------
    {"key": "future_task", "title": "定时任务", "level": "user", "default_enabled": True,
     "group": "其他", "desc": "让 AI 设置定时提醒（「明天早上8点叫我」「每天中午提醒喝水」），到点会主动发消息"},

    # ---------- 文件与代码：五个文件工具 + 两个执行工具共用一张卡片 ----------
    # 它们是一套连贯能力（读→改→跑→看结果），单独开关意义不大，
    # 风险等级也一致：全是 admin，且都限制在会话工作区内。
    {"key": "file_write", "title": "文件与代码", "level": "admin", "default_enabled": True,
     "group": "其他", "card": "fs",
     "desc": "⚠️ 让 AI 在自己的工作目录里读写文件、搜索内容、执行 Python 和系统命令。"
             "无沙箱，等同于给出机器权限，仅在完全信任时开启"},
    {"key": "file_read", "level": "admin", "default_enabled": True,
     "group": "其他", "card": "fs", "title": "读文件", "desc": ""},
    {"key": "file_edit", "level": "admin", "default_enabled": True,
     "group": "其他", "card": "fs", "title": "编辑文件", "desc": ""},
    {"key": "file_list", "level": "admin", "default_enabled": True,
     "group": "其他", "card": "fs", "title": "列目录", "desc": ""},
    {"key": "grep_files", "level": "admin", "default_enabled": True,
     "group": "其他", "card": "fs", "title": "搜索文件内容", "desc": ""},
    {"key": "execute_python", "level": "admin", "default_enabled": True,
     "group": "其他", "card": "fs", "title": "执行 Python", "desc": ""},
    {"key": "execute_shell", "level": "admin", "default_enabled": True,
     "group": "其他", "card": "fs", "title": "执行 Shell", "desc": ""},

    # ---------- MCP：需要配合下方的服务器配置，保留 ----------
    {"key": "mcp_tools", "title": "MCP 外部工具", "level": "admin", "default_enabled": True,
     "group": "其他", "desc": "接入 data/mcp_server.json 里配置的 MCP 服务器，把它们的工具暴露给 AI。开启后在下方配置服务器"},

    # ---------- 机器人自身：只读，隐藏 ----------
    {"key": "read_bot_config", "title": "读机器人配置", "level": "admin", "default_enabled": True,
     "hidden": True, "group": "机器人自身", "desc": "读取非敏感配置项。已屏蔽 API Key、Token 等字段"},
    {"key": "get_bot_status", "title": "查运行状态", "level": "admin", "default_enabled": True,
     "hidden": True, "group": "机器人自身", "desc": "查询机器人运行时长、内存占用、当前模型"},
]

# WebUI 只渲染这些分组，且只渲染其中 hidden 不为 True 的工具。
AGENT_TOOL_GROUPS = ["联网", "插件", "群管理", "其他"]


def visible_tools() -> list:
    """WebUI 要展示的工具。同一 card 的多个工具只保留第一个作为代表。"""
    out, seen_cards = [], set()
    for meta in AGENT_TOOL_META:
        if meta.get("hidden"):
            continue
        card = meta.get("card")
        if card:
            if card in seen_cards:
                continue
            seen_cards.add(card)
        out.append(meta)
    return out


def card_members(key: str) -> list:
    """某个代表工具背后真正联动的所有工具名。"""
    target = next((m for m in AGENT_TOOL_META if m["key"] == key), None)
    if target is None:
        return [key]
    card = target.get("card")
    if not card:
        return [key]
    return [m["key"] for m in AGENT_TOOL_META if m.get("card") == card]


def configurable_keys() -> set:
    """会写进 config.json 的工具名。隐藏工具不落配置，缺失时按默认值处理。"""
    return {m["key"] for m in AGENT_TOOL_META if not m.get("hidden")}

DEFAULT_AGENT_CONFIG = {
    "enabled": True,
    "max_rounds": 30,
    "tool_result_max_chars": 8000,
    "tool_timeout": 120,
    "parallel_tools": True,
    "exec_timeout": 60,
    "search": {
        "provider": "auto",
        "tavily_api_key": "",
        "bocha_api_key": "",
    },
    # 只列可配置的工具。隐藏工具不写进 config —— 读取时找不到条目就用
    # AGENT_TOOL_META 里的 default_enabled，效果一样，但配置文件干净得多。
    "tools": {
        meta["key"]: {"enabled": meta["default_enabled"], "level": meta["level"]}
        for meta in AGENT_TOOL_META if not meta.get("hidden")
    },
}
