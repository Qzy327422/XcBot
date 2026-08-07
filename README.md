<p align="center">
  <img src="https://img.shields.io/badge/Python-3.12+-blue?logo=python&logoColor=white" alt="Python Version">
  <img src="https://img.shields.io/badge/License-MIT-green" alt="License">
  <img src="https://img.shields.io/badge/Framework-Hyper-red" alt="Framework">
</p>


<img src="https://picui.ogmua.cn/s1/2026/08/03/6a70b19af2774.webp" alt="XcBot WebUI" />

# XcBot

XcBot 是一款基于 NapCat + OneBot + hyper-bot 的 QQ 机器人。除了 AI 对话、
多模型切换、WebUI 管理和插件热加载之外，v3.0 起内置了 **Agent 能力**——
AI 会自己判断要不要联网搜索、调用插件、操作 QQ、读写文件、执行代码，
不需要用户先记住命令。


## 核心特性

* **Agent 工具调用** —— AI 自主决定调用哪些工具，多步任务自动串起来
* 多模型 AI 对话（OpenAI 格式接口）
* 群聊和私聊智能响应
* WebUI 可视化管理（配置、日志、统计、追踪、聊天室）
* 外部插件系统（热重载，AI 也能直接调用插件）

## Agent 能力

开启后 AI 在对话中会自行判断是否需要工具。你问「最近有什么新闻」，
它会去搜索再回答；你说「帮我把这些数据做成表格」，它会写文件、
生成图表并直接发给你。

**具体能做什么**

| 类别 | 能力 |
| --- | --- |
| 联网 | 搜索实时信息、打开网页读正文 |
| 插件 | 调用 `plugins/` 下已装的插件，不必让用户自己打命令 |
| QQ 查询 | 查群信息、群成员资料 |
| QQ 操作 | 发图、发文件、拍一拍、任务过程中主动汇报进度 |
| 群管理 | 禁言、撤回、改群名片、设精华 |
| 定时提醒 | 「明天早上八点叫我」，到点用 AI 人格说出提醒语 |
| 文件与代码 | 在工作目录里读写文件、搜索内容、执行 Python 和 Shell |
| MCP | 接入外部 MCP 服务器提供的工具 |
| 其他 | 获取真实时间、精确计算 |

**权限有两档**

* **所有人** —— 查询类、只作用于当前对话的操作
* **仅管理员** —— 群管理、文件读写、代码执行、MCP


**每个会话有独立工作区**

AI 的文件读写默认落在 `data/agent_workspace/群号或QQ号/`，
群与群之间、群和私聊之间互不可见。目录只在真的要写文件时才创建。


## 快速开始

1. 安装 Python 3.12 或更高版本
2. 执行 `pip install -r requirements.txt`
3. 下载并启动 NapCatQQ，登录机器人 QQ 并开启 OneBot WebSocket 服务
4. 运行 `main.py`，复制控制台输出的 WebUI 网址，粘贴到浏览器打开
5. 进入 WebUI 的「连接」页，填写与 NapCat 相同的地址和端口
6. 在「提供商」页填写大模型接口和 API Key
7. 在 QQ 中发送 `/帮助` 测试机器人是否回复

## 运行要求

* Python 3.12+
* NapCatQQ 协议端（强烈推荐）
* 一个可用的大模型接口（LLM）

Agent 的联网搜索在没有配置 Tavily / 博查 Key 时会回落到百度和 DuckDuckGo，
不额外花钱也能用。MCP 需要 `pip install mcp`，不装则自动跳过。

## 安装依赖

```bash
python -m pip install --upgrade pip
pip install -r requirements.txt
```

## 接入 NapCatQQ（最重要步骤）

### Windows 用户（推荐一键版）

1. 下载 `NapCat.Shell.Windows.OneKey.zip`
2. 解压后运行 `NapCatInstaller.exe`
3. 启动 `NapCatWinBootMain.exe`

### Linux 用户

```bash
curl -o napcat.sh https://nclatest.znin.net/NapNeko/NapCat-Installer/main/script/install.sh && bash napcat.sh
```

### 配置 OneBot WebSocket（关键）

1. 打开 NapCat WebUI（默认 `http://localhost:6099`）
2. 前往 **网络配置 → 添加 WebSocket 服务器**
3. 推荐配置：
   * Host：`127.0.0.1`（同机部署推荐）
   * Port：建议 `5004` 或 `3333`（必须与 XcBot 配置一致）
4. 确保机器人 QQ 已登录且状态为在线

> **推荐部署方式：** NapCatQQ 和 XcBot 放在同一台机器，用 `127.0.0.1` 连接最稳定。

## 配置机器人

### 连接 NapCat

* 连接地址 `127.0.0.1`，连接端口 `3333`（必须与 NapCat WebSocket 端口一致）
* 监听地址 `127.0.0.1`，监听端口 `3333`

### LLM 接口

在 WebUI 的「提供商」页填写，可以配多个并设置轮换顺序：

```
Base URL: https://api.deepseek.com/v1
模型:     deepseek-chat
Keys:     sk-你的真实Key
```

### 其他常用配置

* 人格设定（决定机器人的说话风格）
* 管理用户（填你的 QQ 号，获得管理权限和 Agent 的管理员工具）
* 触发词（群里提到这个词会触发回复）

## WebUI

**默认地址：** `http://127.0.0.1:7891/`

| 页面 | 用途 |
| --- | --- |
| 欢迎 | 运行状态、连接状态、更新 |
| 数据统计 | 消息数、模型调用历史、Token 排名 |
| Agent | 工具开关、权限、搜索源、MCP 服务器 |
| 追踪 | 每次对话的完整链路：提示词、工具调用明细、Token 用量 |
| 聊天室 | 直接在网页里和 AI 对话 |
| 实时日志 | 完整运行日志 |

**部署在远程服务器时：** 把「访问 Token」设上，否则任何能访问该端口的人
都能读到你的 API Key。首次打开页面会弹窗引导你设置。

## 常用命令（前缀 `/`）

* **基础：** `/帮助`、`/关于`、`/大头照`、`/名言`
* **内置插件：** `/天气 北京`、`/生图 关键词`、`/开 QQ号`
* **记忆：** `/reset`（或「重置」）、`/压缩状态`、`/立即压缩`
* **Agent：** `/停止`（或 `/stop`）中断正在执行的工具
* **管理：** `/重载插件`、`/model`、`/重启`、`/感知`

群里需要 @ 机器人、说出触发词，或使用 `/` 前缀才会触发回复。

## 常见问题

### 机器人完全不回复

1. 检查 NapCat 是否在线、QQ 是否登录成功
2. OneBot WebSocket 是否已开启
3. 配置里的 host 和 port 是否与 NapCat 一致

### 群里发消息没反应

群聊默认不会回复所有消息。需要 @ 机器人、说出触发词，或用 `/` 前缀。

### AI 不回复

1. 提供商配置是否正确
2. API Key 是否有效且有余额

### AI 不调用工具

1. Agent 总开关是否开启
2. 对应工具在 Agent 页是否开启
3. 如果是管理员工具，你的 QQ 号是否在「管理用户」里
4. 打开「追踪」页看这次对话的链路，能看到工具调用的完整明细

### AI 说文件写好了，但我拿不到

用户看不到服务器上的工作目录。让它用「发送文件」把结果发出来，
或者直接说「把文件发给我」。

## 致谢

部分功能灵感来源于 jianer。
