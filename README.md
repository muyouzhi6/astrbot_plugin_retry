# AstrBot 智能重试插件 (Intelligent Retry) v2.8.0

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Version](https://img.shields.io/badge/Version-2.8.0-blue.svg)](https://github.com/muyouzhi6/astrabot_plugin_retry)
[![AstrBot](https://img.shields.io/badge/AstrBot-Compatible-green.svg)](https://github.com/AstrBotDevs/AstrBot)

一个为 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 设计的智能重试插件，专门解决与大语言模型（LLM）交互时的不稳定问题。当 LLM 响应为空、出现特定错误或状态码时，会自动检测并智能重试，让您的对话体验更加流畅。

**🎉 v2.8.0 重大更新**：大幅优化默认配置，涵盖更多实际使用场景，界面体验全面升级！

## ✨ 核心特性

### 🎯 智能检测与重试
- **空回复检测**：自动检测并重试空的LLM响应
- **错误关键词匹配**：支持11种常见错误关键词，包括APITimeoutError、语音转换失败等
- **HTTP状态码识别**：智能处理429/500/502/503/504/524等服务器错误
- **优先级策略**：禁止重试 > 允许重试 > 错误关键词

### 🛡️ 安全与稳定
- **LLM响应验证**：只处理真正的LLM响应，不会误判正常空消息
- **工具调用兼容**：检测到工具调用时自动跳过，不干扰工具链
- **上下文保持**：完整保留对话历史和人设信息
- **指数退避**：支持重试间隔递增，避免频繁请求

### 🎨 用户体验
- **零配置使用**：开箱即用的优化默认配置
- **友好界面**：emoji图标 + 详细说明的配置界面
- **透明工作**：后台自动处理，用户无感知
- **兜底机制**：重试失败时提供友好提示

## 📦 安装方式

### 方法1：插件市场安装（推荐）
1. 进入 AstrBot WebUI
2. 打开插件市场
3. 搜索 `intelligent_retry`
4. 点击安装并启用

### 方法2：手动安装
```bash
cd your_astrbot_directory/plugins
git clone https://github.com/muyouzhi6/astrabot_plugin_retry.git
```

安装完成后重启 AstrBot 即可。

## ⚙️ 配置说明

**✨ v2.8.0 优化了所有配置项，提供更友好的界面和更实用的默认值**

所有配置均可在 **AstrBot WebUI → 插件管理 → Intelligent Retry → 配置** 中进行设置：

### 📝 错误关键词配置

默认包含11种常见错误场景：
```
api 返回的内容为空
API 返回的内容为空
APITimeoutError
错误类型: Exception
API 返回的 completion 由于内容安全过滤被拒绝(非 AstrBot)
语音转换失败，请稍后再试
语音转换失败
网络连接超时
服务器暂时不可用
请求频率过高
连接失败
调用失败
```

💡 **配置技巧**：
- 每行输入一个关键词，按回车换行
- 不区分大小写，支持中英文
- 空行会被自动忽略
- 可以直接使用默认配置，也可以根据需要自定义

### 🔢 状态码配置

**✅ 允许重试的状态码**：
- `429` - 请求频率过高
- `500` - 内部服务器错误
- `502` - 网关错误
- `503` - 服务不可用
- `504` - 网关超时
- `524` - Cloudflare超时

**🚫 禁止重试的状态码**：
- `400` - 请求格式错误
- `401` - 未授权
- `403` - 禁止访问
- `404` - 资源不存在

### ⚙️ 其他配置

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| 最大重试次数 | 整数 | 3 | 设置为0禁用重试 |
| 重试间隔(秒) | 整数 | 2 | 支持指数退避 |
| 强制使用Provider人设 | 布尔 | true | 防止人设污染 |
| 备用人设 | 文本 | 空 | Provider无人设时使用 |
| 兜底回复 | 文本 | 友好提示 | 重试失败时的用户提示 |

## 📝 使用说明

### 🚀 开箱即用
插件安装启用后自动工作，无需任何命令：
- ✅ 自动检测LLM响应异常
- ✅ 智能重试并保持上下文
- ✅ 对用户完全透明

### 🔍 工作原理
1. **监听LLM响应**：插件在结果装饰阶段介入
2. **智能判断**：检查是否需要重试（空回复/错误关键词/状态码）
3. **保持上下文**：重试时完整保留对话历史和人设
4. **指数退避**：避免频繁请求，保护API配额
5. **兜底处理**：多次重试失败时提供友好提示

### 📊 重试优先级
```
1. 检查LLM响应来源（只处理真正的LLM调用）
2. 检查工具调用（跳过工具调用场景）
3. 检查禁止重试状态码（直接跳过）
4. 检查允许重试状态码（触发重试）
5. 检查错误关键词（触发重试）
6. 检查空回复（触发重试）
```

## 🔧 调试与排查

### 启用调试日志
在配置中开启"调试：输出上下文预览"，可以看到：
```
[重试插件] 检测到空回复，需要重试
[重试插件] 上下文长度: 5, 系统提示词存在: True
[重试插件] 上下文预览(最近 3 条):
#1 [user] 你好
#2 [assistant] 你好！有什么我可以帮助你的吗？
#3 [user] 今天天气怎么样
```

### 常见问题
- **插件不工作**：检查是否正确安装在plugins目录下
- **配置不生效**：修改配置后重启AstrBot
- **误重试正常消息**：v2.8.0已修复，只处理LLM响应
- **工具调用被打断**：v2.8.0已优化，自动跳过工具调用

## 🆕 版本更新

### v2.8.0（当前版本）
- 🎯 **配置大优化**：默认包含11种错误关键词，覆盖更多实际场景
- 🎨 **界面全升级**：emoji图标 + 详细说明，配置更友好
- 🔢 **状态码优化**：新增500/524支持，优化默认禁止重试列表
- 🛡️ **安全增强**：更严格的LLM响应验证，避免误判
- 📚 **文档完善**：详细的配置指南和使用说明

### v2.7.x
- 🐛 修复关键逻辑错误，确保重试行为符合配置预期
- 🔧 优化LLM响应检测机制，避免误判正常空消息
- ⚡ 性能优化，10倍性能提升

### v2.6.x
- 🎯 基础重试功能
- 🔄 上下文保持
- 🛠️ 工具调用兼容

## 🤝 贡献

欢迎通过以下方式为项目做贡献：
- 🐛 [报告Bug](https://github.com/muyouzhi6/astrabot_plugin_retry/issues)
- 💡 [功能建议](https://github.com/muyouzhi6/astrabot_plugin_retry/issues)
- 🔧 [提交PR](https://github.com/muyouzhi6/astrabot_plugin_retry/pulls)
- ⭐ [给项目点星](https://github.com/muyouzhi6/astrabot_plugin_retry)

## 📄 开源许可

本项目基于 [MIT License](LICENSE) 开源。

## ✍️ 作者

- 木有知 [@muyouzhi6](https://github.com/muyouzhi6)
- 长安某 [@ChanganZhou](https://github.com/ChanganZhou)

---

💝 **如果这个插件对您有帮助，请给个Star支持一下！**

🔗 **相关链接**：
- [AstrBot官方](https://github.com/AstrBotDevs/AstrBot)
- [插件文档](https://github.com/muyouzhi6/astrabot_plugin_retry/wiki)
- [问题反馈](https://github.com/muyouzhi6/astrabot_plugin_retry/issues)
