# -*- coding: utf-8 -*-
"""WebUI feature switch metadata."""

# 入群欢迎正文默认值。config.default.json 与 main.py 的兜底都引用这里，避免三处不一致。
# 支持的字面占位符：{at} @新成员、{bot_name} {user_nickname} {user_id} {group_id}
DEFAULT_GROUP_JOIN_WELCOME_TEXT = "{at}🎉 欢迎 {user_nickname} 加入本群！很高兴认识你～"

GROUP_JOIN_WELCOME_PLACEHOLDERS = ("bot_name", "user_nickname", "user_id", "group_id")

FEATURE_META = [
    {"key": "ai_chat", "title": "AI 对话", "desc": "AI 回复总开关", "group": "对话"},
    {"key": "private_chat", "title": "私聊响应", "desc": "私聊所有响应总开关，关闭后需从 WebUI 重新开启", "group": "对话"},
    {"key": "group_chat", "title": "群聊响应", "desc": "群聊所有响应总开关，关闭后需从 WebUI 重新开启", "group": "对话"},
    {"key": "sensitive_filter", "title": "屏蔽词过滤", "desc": "对用户发给机器人的消息执行屏蔽词替换", "group": "对话"},
    {"key": "plugin_admin_commands", "title": "插件/模型命令", "desc": "允许使用 /插件视角、/model、/重载插件 等命令", "group": "功能配置"},
    {"key": "group_chat_context", "title": "群聊上下文感知", "desc": "旁听群消息，触发 AI 时注入近期群聊内容及最新 1 张图片", "group": "功能配置"},
    {"key": "summary", "title": "群聊总结", "desc": "总结群聊记录与数据看板", "group": "功能配置"},
    {"key": "compression_commands", "title": "允许手动压缩命令", "desc": "允许使用手动压缩及压缩管理命令", "group": "功能配置"},
    {"key": "emoji_plus_one", "title": "表情 +1", "desc": "单个表情自动复读", "group": "功能配置"},
    {"key": "split_reply_quote", "title": "回复时引用", "desc": "普通 AI 回复时引用触发消息（仅群聊生效），多段仅首段引用", "group": "功能配置"},
    {"key": "weak_blacklist", "title": "弱黑名单", "desc": "按概率限制黑名单用户的 AI 回复", "group": "功能配置"},
    {"key": "poke_reply", "title": "拍一拍回复", "desc": "收到拍一拍时自动回复", "group": "功能配置"},
    {"key": "group_join_welcome", "title": "入群欢迎", "desc": "新成员入群时发送 @ + 欢迎语，可选附带头像", "group": "功能配置"},
    {"key": "weather", "title": "天气查询", "desc": "允许使用 /天气 [城市] 查询天气", "group": "功能配置"},
    {"key": "quote", "title": "名言生成", "desc": "允许使用 /名言 引用消息生成名言图片", "group": "功能配置"},
    {"key": "image_generation", "title": "生图/搜图", "desc": "允许使用 /生图 [关键词] 搜索并发送图片", "group": "功能配置"},
    {"key": "check_account", "title": "QQ 资料查询", "desc": "允许使用 /开 [QQ号] 查询 QQ 资料", "group": "功能配置"},
    {"key": "plugins_external", "title": "外部插件加载", "desc": "是否加载 plugins 目录中的第三方插件", "group": "功能配置"},
    {"key": "http_send_api", "title": "HTTP 消息发送", "desc": "允许外部程序通过 POST /api/send 推送 QQ 消息（需先设置访问 Token）", "group": "外部接口"},
]

def clamp_compression_trigger_rounds(trigger, max_rounds=None):
    """自动压缩触发轮数必须小于最大上下文轮数（max_rounds > 1 时），避免先被截断而永远压不到。"""
    val = max(1, int(trigger))
    if max_rounds is not None:
        try:
            cap = int(max_rounds)
        except (TypeError, ValueError):
            cap = 0
        if cap > 1 and val >= cap:
            return cap - 1
    return val


def compression_trigger_rounds(others):
    """旧双阈值取较大值；保存规范配置后只保留 auto_compress_after_messages。"""
    trigger = max(1, int(others.get("auto_compress_after_messages", 40)))
    if "compression_threshold" in others:
        trigger = max(trigger, int(others["compression_threshold"]))
    if "context_max_messages" in others:
        trigger = clamp_compression_trigger_rounds(trigger, others.get("context_max_messages"))
    return trigger


# 主动回复使用独立配置，仍默认关闭；这里的普通功能默认开启。
DEFAULT_FEATURE_SWITCHES = {item["key"]: True for item in FEATURE_META}
