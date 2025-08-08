# AstrBot 错误/空值重试插件 (Intelligent Retry)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

一个为 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 设计的增强型插件，旨在解决与大语言模型（LLM）交互时不稳定的问题（主要是gemini最近老空返回）。当LLM返回空内容或已知的错误信息时，本插件会自动进行多次重试，以获取有效的回复，从而显著提升用户体验和机器人服务的可靠性。

## ✨ 功能特性

- **自动重试**：在后台静默运行，对用户透明，无需任何额外指令。
- **智能检测**：能够检测两种常见的失败情况：
  1.  LLM 返回了完全空的消息。
  2.  LLM 返回的文本中包含了预设的错误关键词（如 "请求失败", "api 返回的内容为空" 等）。
- **高度可配置**：
  -  可自定义最大重试次数。
  -  可自定义每次重试的间隔时间。
  -  可自定义触发重试的错误关键词列表。
- **日志清晰**：在重试过程中会输出详细的日志，方便管理员追踪问题。

## 📦 安装

请在 AstrBot 的插件市场中搜索 `intelligent_retry` 进行安装，或通过以下方式手动安装：

1.  进入 AstrBot 主目录。
2.  打开 `data/plugins` 文件夹。
3.  执行 `git clone https://github.com/muyouzhi6/astrabot_plugin_retry.git`。
4.  重启 AstrBot 或在 WebUI 中重载插件。

## ⚙️ 配置

安装并重载插件后，你可以在 AstrBot 的 WebUI -> 插件管理 -> Intelligent Retry -> 管理 -> 配置 中对插件进行设置。

| 配置项 | 类型 | 描述 | 默认值 |
| :--- | :--- | :--- | :--- |
| **最大重试次数** | `整数` | 当LLM回复无效时，插件尝试重新请求的最大次数。设置为0则禁用重试。 | `3` |
| **重试间隔（秒）** | `整数` | 每次重试之间的等待时间，单位为秒。 | `2` |
| **触发重试的错误关键词** | `文本` | 当LLM的回复中包含这些关键词时，将触发重试。请每行输入一个关键词，不区分大小写。 | (列表见默认值) |

配置修改后会自动保存并生效。

## 📝 使用方法

本插件无需任何命令，安装并启用后即可自动工作。它会监听所有经过LLM处理的消息，并在必要时介入。
如遇不生效的情况关闭其余报错相关插件

## 🤝 贡献

欢迎通过提交 Pull Request 或 Issue 来为本项目做出贡献。

## 📄 开源许可证

本项目基于 [MIT License](LICENSE) 开源。

## ✍️ 作者

- [@木有知](https://github.com/your-github-username)
- [@长安某](https://github.com/your-github-username)
