# -*- coding: utf-8 -*-
"""XcBot 全局常量配置

集中管理硬编码的魔法数字，方便维护和调整。
"""

# ==================== Token 统计相关 ====================
# 滚动窗口时间（秒）
TOKEN_WINDOW_SECONDS = 24 * 3600  # 24 小时

# Token 统计保存节流间隔（秒）
TOKEN_SAVE_INTERVAL_SECONDS = 5.0

# 每个会话最多保留的详细统计条数（0 表示不限制）
TOKEN_DETAIL_LIMIT = 0

# ==================== AI 对话相关 ====================
# 单个会话最多保留的上下文消息数
CONTEXT_MAX_MESSAGES = 60

# 消息达到多少条后允许触发上下文压缩
COMPRESSION_THRESHOLD = 40

# 压缩时保留最近多少条消息不压缩
COMPRESSION_KEEP_RECENT = 20

# 自动压缩触发阈值
AUTO_COMPRESS_AFTER_MESSAGES = 44

# 每个群每天允许总结的次数
SUMMARY_PER_DAY_LIMIT = 2

# 每次最多可总结多少条消息
SUMMARY_MAX_MESSAGES = 600

# LLM/API 请求超时时间（秒）
API_REQUEST_TIMEOUT_SECONDS = 120

# ==================== 交互功能相关 ====================
# 表情 +1 的防抖时间（秒）
EMOJI_PLUS_ONE_COOLDOWN_SECONDS = 1

# 拍一拍回复防抖时间（秒）
POKE_COOLDOWN_SECONDS = 8

# 弱黑名单用户被拦截的概率（0 到 1）
WEAK_BLACKLIST_TRIGGER_PROBABILITY = 0.3

# 普通群消息下机器人概率触发对话的概率（0 到 1）
GROUP_RANDOM_REPLY_PROBABILITY = 0.01

# API 调用失败后的冷却时间（秒）
API_FAILURE_COOLDOWN_SECONDS = 5

# ==================== WebUI 相关 ====================
# WebUI 默认监听地址
WEBUI_DEFAULT_HOST = "127.0.0.1"

# WebUI 默认端口
WEBUI_DEFAULT_PORT = 7891

# ==================== 连接相关 ====================
# 连接失败后的重试次数
CONNECTION_RETRIES = 5

# 默认连接端口
DEFAULT_CONNECTION_PORT = 3333
