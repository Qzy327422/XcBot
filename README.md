<p align="center">
  <img src="https://img.shields.io/badge/Python-3.12+-blue?logo=python&logoColor=white" alt="Python Version">
  <img src="https://img.shields.io/badge/License-MIT-green" alt="License">
  <img src="https://img.shields.io/badge/Framework-Hyper-red" alt="Framework">
  <img src="https://img.shields.io/badge/QQ_Group-1009790417-12B7F5?logo=tencentqq&logoColor=white" alt="QQ Group">
</p>


![WebUI](https://img.cdn1.vip/i/69e3a22f885ab_1776525871.webp)

# XcBot 快速开始

XcBot 是一款基于 NapCat + OneBot + hyper-bot 的轻量级 QQ 机器人，支持 AI 对话、群/私聊、多模型切换、WebUI 管理及外部插件热加载。

**官方 QQ 群：** 1009790417

## 核心特性

*   多模型 AI 对话（支持使用 Openai 格式的模型）
*   群聊和私聊智能响应
*   WebUI 可视化管理界面
*   外部插件系统（支持热重载）

## 推荐协议端

*   **NapCatQQ**

## 快速开始

1.  安装 Python 3.12 或更高版本
2.  执行命令：`pip install -r requirements.txt`
3.  下载并启动 NapCatQQ，登录机器人 QQ 并开启 OneBot WebSocket 服务
4.  运行 `main.py`，复制控制台输出的 WebUI 网址，粘贴到浏览器打开
5.  进入 WebUI，在“连接”选项卡中选择和 napcat 相同的地址和端口
6.  在 QQ 中发送 `/帮助` 测试机器人是否回复
7.  在 AI 配置选项卡中填写大模型接口

## 运行要求

*   Python 3.12+
*   NapCatQQ 协议端（强烈推荐）
*   一个可用的大模型接口（LLM）

## 安装依赖

先升级 pip：

```bash
python -m pip install --upgrade pip
```

然后安装依赖：

```bash
pip install -r requirements.txt
```

## 接入 NapCatQQ（最重要步骤）

### Windows 用户（推荐一键版）

1.  下载 `NapCat.Shell.Windows.OneKey.zip`
2.  解压后运行 `NapCatInstaller.exe`
3.  启动 `NapCatWinBootMain.exe`

### Linux 用户

```bash
curl -o napcat.sh https://nclatest.znin.net/NapNeko/NapCat-Installer/main/script/install.sh && bash napcat.sh
```

### 配置 OneBot WebSocket（关键）

1.  打开 NapCat WebUI（默认 `http://localhost:6099`）
2.  前往 **网络配置 → 添加 WebSocket 服务器**
3.  **推荐配置：**
    *   Host：`127.0.0.1`（同机部署推荐）
    *   Port：建议使用 `5004` 或 `3333`（必须与 `config.json` 中的端口一致）
4.  确保机器人 QQ 已登录且状态为在线

> **推荐部署方式：** NapCatQQ 和 XcBot 放在同一台机器，使用 `127.0.0.1` 本地连接，最稳定。

## 配置机器人

### 连接 NapCat

*   连接地址: `"127.0.0.1"`
*   连接端口: `3333` (必须与 NapCat WebSocket 端口一致)
*   监听地址: `"127.0.0.1"`
*   监听端口: `3333`

### LLM 接口配置（AI 核心）

示例：

```json
"base_url": "https://api.deepseek.com/v1",
"model": "deepseek-chat",
"keys": ["sk-你的真实Key"]
```

### 其他常用配置

*   人格设定（设置机器人人格）
*   管理用户（填你的 QQ 号，获得管理权限）

## WebUI

*   **默认地址：** `http://127.0.0.1:7891/`
*   **功能包括：** 查看运行状态、修改配置、管理 LLM 接口、管理插件、查看日志

## 常用命令（前缀 /）

*   **基础命令：** `/帮助`、`/关于`、`/天气 北京`、`/生图 关键词`、`/大头照`、`/名言`
*   **记忆相关：** `/reset`、`/压缩状态`、`/立即压缩`
*   **管理命令：** `/重载插件`、`/model`、`/重启`、`/感知`

## 常见问题

### 机器人完全不回复

1.  检查 NapCat 是否在线、QQ 是否登录成功
2.  OneBot WebSocket 是否已开启
3.  `config.json` 中的 host 和 port 是否与 NapCat 一致

### AI 不回复

1.  `llm_endpoints` 配置是否正确
2.  API Key 是否有效且有余额
