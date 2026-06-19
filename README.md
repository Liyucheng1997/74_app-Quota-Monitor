# AI 额度 Monitor

当前版本：**1.0.0**

[下载 Windows 单文件版](https://github.com/Liyucheng1997/AI-Quota-Monitor/releases/latest)

Windows 本地桌面工具，用一张面板查看 Claude Code 与 Codex 的额度、重置倒计时，以及 Codex 赠送重置次数的 30 天失效跟踪。

## 功能

- Claude Code：5 小时与 7 天额度、剩余比例、精确重置时间。
- Codex：5 小时与 7 天额度、Credits 余额、实时赠送重置可用次数。
- Codex 多账号：每个账号使用独立登录目录，并行刷新，在顶部下拉框切换查看。
- 多账号卡片：Claude 与 Codex 账号每行最多 3 张卡片并排显示，无需切换即可同时查看。
- 赠送重置：自动发现次数增加，估算 30 天失效时间；支持手工校正获得日期。
- 赠送总览：下方表格同时显示所有 Codex 账号的赠送重置次数和失效记录。
- 每 5 分钟自动刷新，也可手动刷新。
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
