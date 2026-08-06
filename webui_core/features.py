# -*- coding: utf-8 -*-
"""WebUI feature switch metadata."""

# 入群欢迎正文默认值。config.default.json 与 main.py 的兜底都引用这里，避免三处不一致。
# 支持的字面占位符：{bot_name} {user_nickname} {user_id} {group_id}
DEFAULT_GROUP_JOIN_WELCOME_TEXT = (
    " 加入{bot_name}的大家庭，{bot_name}是你最可爱的好朋友\n"
    "经常@{bot_name} 看看{bot_name}又学会做什么新事情啦~o((>ω< ))o\n"
    "祝你在{bot_name}的大家庭里生活愉快！♪(≧∀≦)ゞ☆"
)

GROUP_JOIN_WELCOME_PLACEHOLDERS = ("bot_name", "user_nickname", "user_id", "group_id")

FEATURE_META = [
    {"key": "ai_chat", "title": "AI 对话", "desc": "AI 回复总开关", "group": "对话"},
    {"key": "private_chat", "title": "私聊响应", "desc": "允许私聊直接触发 AI", "group": "对话"},
    {"key": "group_chat", "title": "群聊响应", "desc": "允许群内 @ / 名字 / 前缀触发 AI", "group": "对话"},
    {"key": "sensitive_filter", "title": "屏蔽词过滤", "desc": "对消息、人设、日志展示等文本执行敏感词替换", "group": "对话"},
    {"key": "plugin_admin_commands", "title": "插件/模型命令", "desc": "允许使用 /插件视角、/model、/重载插件 等命令", "group": "功能配置"},
    {"key": "group_chat_context", "title": "群聊上下文感知", "desc": "旁听群消息，触发 AI 时注入上次回复后的群聊内容；默认关闭", "group": "功能配置"},
    {"key": "summary", "title": "群聊总结", "desc": "总结群聊记录与数据看板", "group": "功能配置"},
    {"key": "compression_commands", "title": "记忆压缩", "desc": "自动压缩上下文，并允许使用压缩相关命令", "group": "功能配置"},
    {"key": "emoji_plus_one", "title": "表情 +1", "desc": "单个表情自动复读", "group": "功能配置"},
    {"key": "split_reply_quote", "title": "分段首段引用", "desc": "开启后：仅多段回复默认首段引用发送者消息", "group": "功能配置"},
    {"key": "weak_blacklist", "title": "弱黑名单", "desc": "按概率拦截触发", "group": "功能配置"},
    {"key": "poke_reply", "title": "拍一拍回复", "desc": "收到拍一拍时自动回复", "group": "功能配置"},
    {"key": "group_join_welcome", "title": "入群欢迎", "desc": "新成员入群时发送头像 + @ + 欢迎语，欢迎语可在下方自定义", "group": "功能配置"},
    {"key": "weather", "title": "天气查询", "desc": "允许使用 /天气 [城市] 查询天气", "group": "功能配置"},
    {"key": "quote", "title": "名言生成", "desc": "允许使用 /名言 引用消息生成名言图片", "group": "功能配置"},
    {"key": "image_generation", "title": "生图/搜图", "desc": "允许使用 /生图 [关键词] 搜索并发送图片", "group": "功能配置"},
    {"key": "check_account", "title": "QQ 资料查询", "desc": "允许使用 /开 [QQ号] 查询 QQ 资料", "group": "功能配置"},
    {"key": "plugins_external", "title": "外部插件加载", "desc": "是否继续加载 plugins 目录中的第三方插件", "group": "功能配置"},
    {"key": "http_send_api", "title": "HTTP 消息发送", "desc": "允许外部程序/插件通过 POST /api/send 推送 QQ 消息，需先设置 WebUI 访问 Token", "group": "外部接口"},
]

# 绝大多数功能默认开；仅下列明确默认关（与 config.default.json 保持一致）
_FEATURE_DEFAULT_OFF = frozenset({
    "group_chat_context",  # 旁听会增 token，默认关，需在功能配置里手动开
})

DEFAULT_FEATURE_SWITCHES = {
    item["key"]: (item["key"] not in _FEATURE_DEFAULT_OFF)
    for item in FEATURE_META
}
