# AI 额度 Monitor

当前版本：**1.3.0**

[下载 Windows 单文件版](https://github.com/Liyucheng1997/74_app-Quota-Monitor/releases/latest)

Windows 本地桌面工具，用一张面板查看 Claude Code 与 Codex 的额度、重置倒计时，以及 Codex 赠送重置次数的 30 天失效跟踪。

## 功能

- Claude Code：5 小时与 7 天额度、剩余比例、精确重置时间。
- Codex：5 小时与 7 天额度、Credits 余额、实时赠送重置可用次数。
- Codex 多账号：每个账号使用独立登录目录，并行刷新，在顶部下拉框切换查看。
- 多账号卡片：Claude 与 Codex 账号每行最多 3 张卡片并排显示，无需切换即可同时查看。
- 赠送重置：优先读取 Codex 后端真实授予和到期时间；接口不可用时按 30 天失效估算，并支持手工校正。
- 赠送总览：下方表格同时显示所有 Codex 账号的赠送重置次数和失效记录。
- 每 5 分钟自动刷新，也可手动刷新。
- 桌面悬浮宠物：无边框置顶可拖动的状态条，同屏显示 Claude Code 与 Codex 的运行状态；正在运行的任务实时显示已运行时长，同一软件并发多个任务时逐行列出。
- 全部数据保留在本机，不复制或保存 Claude/Codex 登录令牌。

## 运行

要求：Windows 10/11、Python 3.10+，并已登录 Claude Code 和/或 Codex Desktop。

双击 `start.bat`，或运行：

```powershell
python app.py
```

如果只使用其中一个 Agent，另一个卡片会显示明确的登录/数据源提示，不影响使用。

### 添加多个 Codex 账号

点击顶部“添加账号”，输入备注名称。工具会打开独立的 Codex 登录窗口；完成登录后返回工具并点击“立即刷新”。每个账号使用独立的 `CODEX_HOME`，不会覆盖 Codex Desktop 当前账号，也不会由本工具复制登录令牌。

## 本地反向代理（把订阅当 API 用）

> ⚠️ **风险须知**：用订阅（Claude Pro/Max、ChatGPT Plus/Pro）的 OAuth 令牌对外提供通用 API，**违反 Anthropic / OpenAI 使用条款，可能导致账号被封**。此功能仅供个人、低频、单人自用。请求特征已尽量模拟真实客户端，但不保证不被检测。是否使用由你自行承担风险。

反代复用 Monitor 已经在读取的本机登录令牌，起一个本地 HTTP 服务，让你自己的项目把它当普通 API 调用，无需再花钱买 API Key。

启动：

```powershell
python -m ai_quota_monitor.proxy
```

或双击 `start-proxy.bat`。默认监听 `http://127.0.0.1:8787`，提供三个端点：

| 端点 | 格式 | 说明 |
| --- | --- | --- |
| `/v1/chat/completions` | OpenAI 兼容 | 直接接 OpenAI SDK、LangChain 等现成工具，支持流式、工具调用、图片 |
| `/v1/messages` | Anthropic 原生 | 与官方 Claude API 一致，适合已用 `anthropic` SDK 的项目 |
| `/codex/responses` | ChatGPT 后端透传 | **实验性**，Codex Responses 协议原样转发，接口不稳定 |

在 OpenAI SDK 里这样用：

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8787/v1", api_key="unused")
resp = client.chat.completions.create(
    model="claude-sonnet-4-5",
    messages=[{"role": "user", "content": "你好"}],
)
print(resp.choices[0].message.content)
```

环境变量（可选）：

- `AQM_PROXY_HOST` / `AQM_PROXY_PORT`：监听地址与端口。
- `AQM_PROXY_KEY`：设置后客户端需在 `Authorization: Bearer <key>` 或 `x-api-key` 里携带；不设置则无鉴权，因此**默认只监听本机**，不要在未设密钥时绑定 `0.0.0.0`。
- `AQM_DEFAULT_MODEL`：客户端传入非 Claude 模型名（如 `gpt-4o`）时改用的默认模型。

令牌处理：反代自动检测 Claude 令牌是否过期，过期时用 refresh token 续期，并把新令牌写回 `~/.claude/.credentials.json`，与 Claude Code 本体保持同步（Anthropic 的 refresh token 会轮换，写回可避免本体被迫重新登录）。

## 打包为单文件 EXE

```powershell
.\build.ps1
```

生成文件位于 `dist\AI-Quota-Monitor.exe`。首次构建会安装 PyInstaller。

## 数据与准确性

- Claude 实时用量来自 Claude Code 本机 OAuth 会话使用的用量端点。该端点不是公开承诺的稳定 API，版本变化时界面会显示错误，不会静默伪造数据。
- Codex 优先调用本机 `app-server` 的只读 `account/rateLimits/read`；失败时降级到最新会话里的额度快照，并标记为“非实时”。
- Codex 0.142 当前协议中的 `RateLimitResetCreditsSummary` 只包含 `availableCount`，不返回授予日期、批次或失效日期。因此自动发现记录的日期是“首次观察时间”，默认失效时间为其后 30 天。请在已知真实授予日期时使用“校正获得时间”。
- 本工具状态文件位于 `%LOCALAPPDATA%\AIQuotaMonitor\state.json`。

## 测试

```powershell
python -m unittest discover -v
```

## 许可证

[MIT License](LICENSE)
