# Changelog

## 1.3.0 - 2026-08-20

- 新增本地反向代理：把 Claude Code / Codex 订阅暴露成本地 API，供自己的项目调用。
- 同时提供 OpenAI 兼容端点 `/v1/chat/completions` 与 Anthropic 原生端点 `/v1/messages`，支持流式输出与多模态图片。
- Claude 默认走更安全的 `claude -p` CLI 后端：调用官方 Claude Code headless 模式，令牌与刷新由 CLI 自管（含 Windows 凭据库），不碰凭据文件，也无需为反代单独重新登录，被判定为滥用的风险更低；用 `AQM_CLAUDE_BACKEND=cli|oauth` 切换。
- CLI 后端支持 `sonnet`/`opus`/`fable` 别名，并用 `--tools ""` + 调用方 system 提示裁剪为普通聊天模型。
- OAuth 直连后端保留：自动刷新 Claude OAuth 令牌并写回 `~/.claude/.credentials.json`，与 Claude Code 本体保持同步（refresh token 会轮换）。
- Codex/ChatGPT 提供实验性原始透传端点 `/codex/responses`（后端协议不稳定，仅作最佳努力）。
- 可选 `AQM_PROXY_KEY` 本地访问密钥；默认仅监听 127.0.0.1。
- 界面新增「🔌 反代 API」开关，弹窗与状态栏显示当前后端；也可用 `python -m ai_quota_monitor.proxy` 或 `start-proxy.bat` 启动。
- 提示：用订阅令牌当通用 API 违反 Anthropic / OpenAI 使用条款，可能导致封号，仅限个人低频自用。

## 1.2.0 - 2026-06-27

- Codex 赠送重置优先读取后端明细，显示真实授予时间和到期时间。
- 后端明细可用时自动同步本地赠送记录，替换旧的估算记录。
- 后端接口不可用时继续按首次观察 + 30 天估算，保留手工校正能力。
- UI 区分 Codex 后端真实时间、估算记录和手工确认记录。

## 1.1.0 - 2026-06-21

- 新增桌面悬浮宠物：无边框置顶可拖动的状态条，同屏显示 Claude Code 与 Codex 的运行状态。
- 正在运行的任务实时显示已运行时长；同一软件并发多个任务时逐行列出并用 ①②③ 编号。
- 状态判定只读本地会话日志（不耗额度、不联网）：Claude 依据最后的提问/工具调用，Codex 依据 `task_started`/`task_complete` 标记。
- 30 分钟存活判定：长时间无写入的崩溃/异常退出会话归为已完成，避免一直显示“运行中”。
- 主界面新增“桌面宠物”开关；双击宠物唤回主窗口，右键提供菜单。

## 1.0.1 - 2026-06-19

- 修复 Claude 用量端点因客户端标识不兼容而返回 HTTP 429。
- 区分请求限流与登录失效错误，避免错误提示重新登录。
- 增加“登录 Claude”入口，支持重新授权或切换账号。

## 1.0.0 - 2026-06-19

- 同屏展示 Claude Code 与多个 Codex 账号的 5 小时、7 天额度和重置时间。
- 支持独立 `CODEX_HOME` 的 Codex 多账号登录与并行刷新。
- 支持 Codex 赠送重置次数及 30 天失效估算、手工校正。
- 暖米色 Windows 桌面界面，额度条使用绿、橙、红三级状态色。
- 提供单文件 Windows EXE 和源码运行方式。
