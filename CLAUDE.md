# CLAUDE.md

## 项目开发原则

### 消息内容不做截断

历史消息（tool_use 摘要、tool_result 文本、用户/助手消息）一律保留完整内容，不在解析层做任何字符数截断。长文本的处理统一交给发送层：通过 `split_message` 按 Telegram 4096 字符限制分页，配合 inline keyboard 翻页浏览。

### 历史分页默认显示最新内容

`/history` 默认显示最后一页（最新消息），用户通过 "◀ Older" 按钮向前翻阅更早的内容。

### 遵循 Telegram Bot 最佳实践

Bot 的交互设计应参考 Telegram Bot 平台的最佳实践：优先使用 inline keyboard 而非 reply keyboard；翻页/操作通过 `edit_message_text` 原地更新而非发送新消息；callback data 保持精简以适应 64 字节限制；合理使用 `answer_callback_query` 提供即时反馈。

### 代码质量检查

每次修改代码后运行 `pyright src/ccmux/` 检查类型错误，确保 0 errors 后再提交。

### 消息格式化统一使用 MarkdownV2

所有发送到 Telegram 的消息统一使用 `parse_mode="MarkdownV2"`。通过 `telegramify-markdown` 库将标准 Markdown 转换为 Telegram MarkdownV2 格式。所有发送/编辑消息的调用都必须经过 `_safe_reply`/`_safe_edit`/`_safe_send` helper 函数，这些函数会自动完成 MarkdownV2 转换并在解析失败时 fallback 到纯文本。不要直接调用 `reply_text`/`edit_message_text`/`send_message`。

### 以 Window 为核心单位

所有逻辑（session 列表、消息发送、历史查看、通知等）均以 tmux window 为核心单位进行处理，而非以项目目录（cwd）为单位。同一个目录可以有多个 window（名称自动加后缀如 `cc:project-2`），每个 window 独立关联自己的 Claude session。

### Telegram Flood Control 防护

Bot 实现了消息发送速率限制，避免触发 Telegram 的 flood control（频率限制）：
- 每个用户的消息发送间隔至少 1.1 秒
- Status 轮询间隔设为 1 秒（发送层有 rate limiting 保护）
- 所有 `send_message` 调用都经过 `_rate_limit_send()` 检查并等待

### 消息队列架构

Bot 使用 per-user 消息队列 + worker 模式处理所有发送任务，确保：
- 消息按接收顺序发送（FIFO）
- Status 消息始终在 content 消息之后
- 多用户并发处理互不干扰

队列溢出保护（`MAX_QUEUE_SIZE = 5`）：当队列消息数超过阈值时，自动 compact：
- 保留第一条消息（提供上下文）
- 保留最后 N 条消息（最新内容）
- 丢弃中间消息，并向用户发送警告

### Status 消息处理

Status 消息（Claude 状态行）采用特殊处理优化用户体验：

**去重**：入队前移除同一 window 的旧 status 消息，确保队列中每个 window 只有一条 status。

**转换**：将 status 消息编辑为第一条 content 消息，减少消息数量：
- 有 status 消息时，第一条 content 通过 edit 更新 status 消息
- 后续 content 作为新消息发送

**轮询**：后台任务以 1 秒间隔轮询所有 active window 的终端状态，发送层的 rate limiting 确保不会触发 flood control。

### Session 生命周期管理

Session monitor 通过 `session_map.json`（hook 写入）追踪 window → session_id 映射：

**启动清理**：Bot 启动时清理所有不在 session_map 中的 tracked session，避免监控已关闭的 session。

**运行时变更检测**：每次轮询时检测 session_map 变化：
- Window 的 session_id 改变（如执行 `/clear`）→ 清理旧 session
- Window 被删除 → 清理对应 session

### 性能优化实践

**mtime 缓存**：监控循环维护内存中的文件 mtime 缓存，跳过未修改的文件读取。

**Byte offset 增量读取**：每个 tracked session 记录 `last_byte_offset`，只读取新增内容。检测文件截断（offset > file_size）自动重置。

**Status 去重**：入队前移除同 window 旧 status，减少队列占用和发送次数。

### 多字体 Fallback

截图功能使用三级字体 fallback 链确保所有字符正确显示：
1. **JetBrains Mono** — Latin、符号、box-drawing
2. **Noto Sans Mono CJK SC** — 中日韩字符、全角标点
3. **Symbola** — 其他特殊符号、dingbats

通过 `_font_tier()` 函数按字符 codepoint 判断使用哪级字体。支持完整的 ANSI 颜色解析（16 色 + 256 色 + RGB）。

### Hook 配置

用户需在 `~/.claude/settings.json` 中配置：

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [{ "type": "command", "command": "ccmux hook", "timeout": 5 }]
      }
    ]
  }
}
```
