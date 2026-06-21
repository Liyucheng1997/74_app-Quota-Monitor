"""桌面悬浮宠物：实时显示 Claude Code 与 Codex 的运行状态。

通过读取各自的会话日志判断每个 Agent 是在工作还是已完成，
正在运行的任务会显示已运行时长；同一软件并发多个任务时逐个列出。
全程只读本地文件，不消耗额度、不需要联网。
"""

from __future__ import annotations

import json
import os
import tkinter as tk
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

from .models import parse_datetime, utc_now

# 与主界面一致的暖色调
BG_TRANS = "#FF00FF"  # 透明遮罩色（Windows transparentcolor）
CARD = "#FBF9F4"
BORDER = "#E5DFD5"
TEXT = "#34332F"
MUTED = "#918B82"
ACCENT = "#FF8A4C"
GREEN = "#66A36B"
GREEN_SOFT = "#A9CBAB"
GREY = "#C7C1B6"

# 读取日志尾部的字节数，足够覆盖最后若干条事件
TAIL_BYTES = 96 * 1024
# 存活判定：被判为运行中、但这么久没有任何写入的会话视为已结束（崩溃/异常退出）。
# 比单条长命令/构建的间隔宽松得多，又能清掉死会话。
LIVENESS_SECONDS = 30 * 60
# 仅为性能：超过此时长未写入的会话不再解析（绝不会有任务连续跑这么久）
RECENT_SCAN_SECONDS = 24 * 3600

WORKING = "working"
DONE = "done"
NONE = "none"

_CIRCLED = "①②③④⑤⑥⑦⑧⑨"


@dataclass(slots=True)
class ToolActivity:
    running: list[datetime] = field(default_factory=list)  # 每个正在运行任务的开始时间
    last_done: datetime | None = None
    has_logs: bool = False


def _read_tail_lines(path: Path, size: int = TAIL_BYTES) -> list[str]:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            file_size = handle.tell()
            handle.seek(max(0, file_size - size))
            chunk = handle.read()
    except OSError:
        return []
    text = chunk.decode("utf-8", errors="ignore")
    return [line for line in text.splitlines() if line.strip()]


def _read_all_lines(path: Path) -> list[str]:
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            return [line for line in handle if line.strip()]
    except OSError:
        return []


def _loads(line: str) -> dict | None:
    try:
        value = json.loads(line)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _local(ts: float) -> datetime:
    return datetime.fromtimestamp(ts).astimezone()


def _claude_status(lines: list[str]) -> str:
    """最后一条 user/assistant 事件决定状态（始终在文件尾部，尾读即可）。"""
    for line in reversed(lines):
        event = _loads(line)
        if event is None or event.get("type") not in ("user", "assistant"):
            continue  # 跳过 ai-title / queue-operation 等元数据
        if event.get("type") == "assistant":
            content = (event.get("message") or {}).get("content")
            blocks = content if isinstance(content, list) else []
            has_tool = any(isinstance(b, dict) and b.get("type") == "tool_use" for b in blocks)
            return WORKING if has_tool else DONE  # 工具运行中 vs 收尾完成
        return WORKING  # 刚提交输入或工具结果回填，模型即将/正在响应
    return DONE


def _claude_start(lines: list[str]) -> datetime | None:
    """当前任务开始 = 最后一条真实用户提问（排除工具结果回填与子代理）。"""
    for line in reversed(lines):
        event = _loads(line)
        if event is None or event.get("type") != "user" or event.get("isSidechain"):
            continue
        content = (event.get("message") or {}).get("content")
        is_tool_result = isinstance(content, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content
        )
        if not is_tool_result:
            return parse_datetime(event.get("timestamp"))
    return None


def _analyze_claude_file(path: Path) -> tuple[str, datetime | None]:
    tail = _read_tail_lines(path)
    if _claude_status(tail) != WORKING:
        return DONE, None
    # 长任务里真实提问可能已滚出尾部，找不到再回读整个文件
    start = _claude_start(tail) or _claude_start(_read_all_lines(path))
    return WORKING, start


def _codex_scan(lines: list[str]) -> tuple[str, datetime | None] | None:
    for line in reversed(lines):
        event = _loads(line)
        if event is None:
            continue
        ptype = (event.get("payload") or {}).get("type")
        if ptype == "task_complete":
            return DONE, None
        if ptype in ("task_started", "user_message"):
            return WORKING, parse_datetime(event.get("timestamp"))
    return None


def _analyze_codex_file(path: Path) -> tuple[str, datetime | None]:
    """依据 task_started / task_complete 标记；尾部无标记时回读整个文件。"""
    result = _codex_scan(_read_tail_lines(path)) or _codex_scan(_read_all_lines(path))
    return result if result is not None else (DONE, None)


def _aggregate(roots: Iterable[Path], analyze: Callable[[Path], tuple[str, datetime | None]]) -> ToolActivity:
    files: list[tuple[float, Path]] = []
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*.jsonl"):
            try:
                files.append((path.stat().st_mtime, path))
            except OSError:
                continue
    if not files:
        return ToolActivity()
    files.sort(key=lambda item: item[0], reverse=True)
    now = utc_now().timestamp()
    running: list[datetime] = []
    for mtime, path in files:
        if mtime < now - RECENT_SCAN_SECONDS:
            break  # 已按时间倒序，更早的会话不可能仍在运行
        if mtime < now - LIVENESS_SECONDS:
            continue  # 太久没写入，视为已结束（崩溃/异常退出的死会话）
        status, start = analyze(path)
        if status == WORKING:
            running.append(start or _local(mtime))
    return ToolActivity(running=running, last_done=_local(files[0][0]), has_logs=True)


def claude_tasks(home: Path | None = None) -> ToolActivity:
    home = home or Path.home()
    return _aggregate([home / ".claude" / "projects"], _analyze_claude_file)


def codex_tasks(codex_homes: Iterable[Path], home: Path | None = None) -> ToolActivity:
    home = home or Path.home()
    roots = [Path(h) / "sessions" for h in codex_homes] or [home / ".codex" / "sessions"]
    return _aggregate(roots, _analyze_codex_file)


def _circled(n: int) -> str:
    return _CIRCLED[n - 1] if 1 <= n <= len(_CIRCLED) else f"({n})"


def _elapsed(start: datetime) -> str:
    secs = max(0, int(utc_now().timestamp() - start.timestamp()))
    hours, rest = divmod(secs, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


def _ago(value: datetime | None) -> str:
    if value is None:
        return "无记录"
    seconds = int(utc_now().timestamp() - value.timestamp())
    if seconds < 60:
        return "刚刚"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}分钟前"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}小时前"
    return value.strftime("%m-%d %H:%M")


class PetWindow(tk.Toplevel):
    """无边框、置顶、可拖动的状态条；每个运行中的任务单独成行并显示时长。"""

    WIDTH = 244
    ROW_H = 30
    TOP = 14
    BOT = 12
    POLL_MS = 2000   # 扫描日志状态
    TICK_MS = 1000   # 刷新时长 / 呼吸动画

    def __init__(
        self,
        master: tk.Misc,
        codex_homes_provider: Callable[[], list[str]],
        on_open_main: Callable[[], None],
        on_close: Callable[[], None],
    ) -> None:
        super().__init__(master)
        self._codex_homes_provider = codex_homes_provider
        self._on_open_main = on_open_main
        self._on_close = on_close
        self._phase = 0
        self._claude = ToolActivity()
        self._codex = ToolActivity()
        self._cur_h = -1
        self._poll_job: str | None = None
        self._tick_job: str | None = None

        self.overrideredirect(True)
        self.attributes("-topmost", True)
        try:
            self.attributes("-transparentcolor", BG_TRANS)
        except tk.TclError:
            pass
        self.configure(bg=BG_TRANS)
        self.canvas = tk.Canvas(self, width=self.WIDTH, bg=BG_TRANS, highlightthickness=0, bd=0)
        self.canvas.pack()
        self._build_menu()
        self._bind_events()

        self._place_top_right()
        self._poll()
        self._tick()

    # ---------- 绘制 ----------
    def _rounded(self, x1: int, y1: int, x2: int, y2: int, r: int, **kw) -> None:
        points = [
            x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
            x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
        ]
        self.canvas.create_polygon(points, smooth=True, **kw)

    def _build_menu(self) -> None:
        self.menu = tk.Menu(self, tearoff=0)
        self.menu.add_command(label="打开主窗口", command=self._on_open_main)
        self.menu.add_separator()
        self.menu.add_command(label="关闭宠物", command=self.close)

    def _bind_events(self) -> None:
        for seq, fn in (
            ("<Button-1>", self._start_drag),
            ("<B1-Motion>", self._on_drag),
            ("<Double-Button-1>", lambda _e: self._on_open_main()),
            ("<Button-3>", self._show_menu),
        ):
            self.canvas.bind(seq, fn)

    # ---------- 拖动 ----------
    def _start_drag(self, event: tk.Event) -> None:
        self._drag_x, self._drag_y = event.x, event.y

    def _on_drag(self, event: tk.Event) -> None:
        x = self.winfo_x() + event.x - self._drag_x
        y = self.winfo_y() + event.y - self._drag_y
        self.geometry(f"+{x}+{y}")

    def _show_menu(self, event: tk.Event) -> None:
        self.menu.tk_popup(event.x_root, event.y_root)

    def _place_top_right(self) -> None:
        self.update_idletasks()
        x = self.winfo_screenwidth() - self.WIDTH - 24
        self.geometry(f"+{x}+72")

    # ---------- 状态刷新 ----------
    def _poll(self) -> None:
        homes = [Path(h) for h in self._codex_homes_provider()]
        self._claude = claude_tasks()
        self._codex = codex_tasks(homes)
        self._render()
        self._poll_job = self.after(self.POLL_MS, self._poll)

    def _tick(self) -> None:
        self._phase = (self._phase + 1) % 3
        self._render()
        self._tick_job = self.after(self.TICK_MS, self._tick)

    def _rows(self) -> list[tuple[str, str, str, str]]:
        rows: list[tuple[str, str, str, str]] = []
        for symbol, name, act in (("✦", "Claude", self._claude), ("⚡", "Codex", self._codex)):
            if act.running:
                count = len(act.running)
                for index, start in enumerate(sorted(act.running)):
                    label = name if count == 1 else f"{name} {_circled(index + 1)}"
                    rows.append((symbol, label, WORKING, f"运行 {_elapsed(start)}"))
            elif not act.has_logs:
                rows.append((symbol, name, NONE, "未登录 / 无记录"))
            else:
                rows.append((symbol, name, DONE, f"完成 · {_ago(act.last_done)}"))
        return rows

    def _render(self) -> None:
        rows = self._rows()
        height = self.TOP + len(rows) * self.ROW_H + self.BOT
        if height != self._cur_h:
            self._cur_h = height
            self.canvas.configure(height=height)
            self.geometry(f"{self.WIDTH}x{height}+{self.winfo_x()}+{self.winfo_y()}")
        self.canvas.delete("all")
        self._rounded(1, 1, self.WIDTH - 1, height - 1, 16, fill=CARD, outline=BORDER, width=1)
        for index, (symbol, label, status, text) in enumerate(rows):
            y = self.TOP + index * self.ROW_H + 13
            self.canvas.create_text(18, y, text=symbol, fill=ACCENT, font=("Segoe UI Symbol", 13, "bold"))
            self.canvas.create_text(36, y, text=label, anchor="w", fill=TEXT, font=("Segoe UI", 10, "bold"))
            if status == WORKING:
                color = GREEN if self._phase != 1 else GREEN_SOFT
                text_fill = TEXT
            elif status == DONE:
                color, text_fill = GREY, MUTED
            else:
                color, text_fill = BORDER, MUTED
            self.canvas.create_oval(120, y - 5, 130, y + 5, fill=color, outline="")
            self.canvas.create_text(138, y, text=text, anchor="w", fill=text_fill, font=("Segoe UI", 9))

    # ---------- 关闭 ----------
    def close(self) -> None:
        for job in (self._poll_job, self._tick_job):
            if job:
                self.after_cancel(job)
        self._poll_job = self._tick_job = None
        self._on_close()
        self.destroy()
