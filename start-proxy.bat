@echo off
cd /d "%~dp0"
REM 把 Claude Code / Codex 订阅反代成本地 API（仅供个人自用，注意封号风险）。
REM 如需设置访问密钥，取消下一行注释并改成你自己的密钥：
REM set AQM_PROXY_KEY=change-me
python -m ai_quota_monitor.proxy %*
pause
