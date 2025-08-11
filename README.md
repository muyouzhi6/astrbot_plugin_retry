# AstrBot 错误/空值重试插件 (Intelligent Retry)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

一个为 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 设计的增强型插件，旨在解决与大语言模型（LLM）交互时不稳定的问题（如空回复/网关错误）。当LLM返回空内容或已知的错误信息时，本插件会自动进行多次重试，并保持原有上下文与人设，显著提升可靠性。

## ✨ 功能特性

- 自动重试：在最终装饰阶段介入，直接替换结果，不打断事件流程。
- 智能检测：
  1) 最终回复为空；
  2) 文本包含错误关键词（如“请求失败”“api 返回的内容为空”等）。
- 保持人设与上下文：
  - 始终带上完整的会话上下文（包含当前轮）。
  - 如果上下文内已含 system 角色消息，则不再重复注入 provider 的 system_prompt；否则自动补位。
- 工具调用兼容：检测到 finish_reason=tool_calls 时不干预，避免打断工具链。
- 重试退避：支持指数退避（初始 retry_delay，随后×2，封顶 30s）。
- 失败兜底：所有重试失败后返回可配置的兜底提示，避免“无响应”。

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
| **触发重试的错误关键词** | `文本` | 当LLM的回复中包含这些关键词时，将触发重试。每行一个，不区分大小写。 | (见默认值) |
| **允许重试的HTTP状态码** | `文本` | 错误文本中出现这些码时允许重试，每行一个。 | 400, 429, 502, 503, 504 |
| **禁止重试的HTTP状态码** | `文本` | 错误文本中出现这些码时直接跳过重试。 | 空 |
| **兜底回复** | `文本` | 达到最大重试次数仍失败时，发送给用户的友好提示。留空则不发送消息。 | 抱歉，刚才遇到服务波动... |

配置修改后会自动保存并生效。

## 📝 使用方法

本插件无需命令，安装并启用后自动工作。它会在结果装饰阶段介入，对用户透明。

验证人设/上下文是否被带上：观察调试日志中如下行：
- “上下文长度: N, 系统提示词存在: True/False, 上下文含system: True/False，示例: …”
- 若“上下文含system: True”，说明历史对话里有人设消息；否则若系统提示词存在，则会补位 system_prompt。

提示：若希望始终携带固定人设，建议在 Provider 中配置 system_prompt；若人设写在会话开头（system 角色），插件会自动沿用。

## 🤝 贡献

欢迎通过提交 Pull Request 或 Issue 来为本项目做出贡献，您的Star🌟是对我最大的鼓励！！。

## 📄 开源许可证

本项目基于 [MIT License](LICENSE) 开源。

## ✍️ 作者

- [@木有知](https://github.com/your-github-username)
- [@长安某](https://github.com/your-github-username)
