from __future__ import annotations

import os
import queue
import shutil
import subprocess
import threading
import tkinter as tk
from datetime import datetime, timezone
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk

from .collectors import ClaudeCollector, CodexCollector
from .models import CodexAccount, GrantBatch, LimitWindow, QuotaSnapshot, utc_now
from .pet import PetWindow
from .service import GrantService
from .storage import AccountStore


BG = "#F3F0E8"
CARD = "#FBF9F4"
CARD_ALT = "#F7F3EC"
TEXT = "#34332F"
MUTED = "#918B82"
BORDER = "#E5DFD5"
ACCENT = "#FF8A4C"
ACCENT_SOFT = "#FDE4D3"
GREEN = "#66A36B"
RED = "#B85C4A"


def format_countdown(target: datetime | None, now: datetime | None = None) -> str:
    if target is None:
        return "重置时间未知"
    now = now or utc_now()
    seconds = int((target - now).total_seconds())
    if seconds <= 0:
        return "等待服务端刷新"
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes = seconds // 60
    parts = []
    if days:
        parts.append(f"{days}天")
    if hours or days:
        parts.append(f"{hours}小时")
    parts.append(f"{minutes}分")
    return "后重置 · " + " ".join(parts)


def local_time(value: datetime | None) -> str:
    return value.astimezone().strftime("%Y-%m-%d %H:%M") if value else "--"


def quota_bar_style(used_percent: float) -> str:
    if used_percent >= 100:
        return "Danger.Horizontal.TProgressbar"
    if used_percent >= 80:
        return "Warning.Horizontal.TProgressbar"
    return "Safe.Horizontal.TProgressbar"


class QuotaCard(ttk.Frame):
    def __init__(self, master: tk.Misc, title: str, symbol: str) -> None:
        super().__init__(master, style="Card.TFrame", padding=22)
        heading = ttk.Frame(self, style="CardInner.TFrame")
        heading.pack(fill="x")
        self.badge = tk.Label(
            heading, text=symbol, bg=ACCENT_SOFT, fg=ACCENT, font=("Segoe UI Symbol", 15, "bold"),
            width=2, height=1, bd=0,
        )
        self.badge.pack(side="left", padx=(0, 10))
        title_box = ttk.Frame(heading, style="CardInner.TFrame")
        title_box.pack(side="left", fill="x", expand=True)
        self.title = ttk.Label(title_box, text=title, style="Title.TLabel")
        self.title.pack(anchor="w")
        self.meta = ttk.Label(title_box, text="等待刷新…", style="Muted.TLabel")
        self.meta.pack(anchor="w", pady=(1, 0))
        self.body = ttk.Frame(self, style="CardInner.TFrame")
        self.body.pack(fill="both", expand=True, pady=(18, 0))
        self.footer = ttk.Label(self, text="", style="Muted.TLabel")
        self.footer.pack(anchor="w", pady=(6, 0))

    def render(self, snapshot: QuotaSnapshot) -> None:
        for child in self.body.winfo_children():
            child.destroy()
        identity = snapshot.account_email or snapshot.plan or "未知方案"
        if snapshot.account_email and snapshot.plan:
            identity += f" · {snapshot.plan}"
        self.meta.configure(text=f"{identity} · {snapshot.source}")
        if snapshot.error:
            ttk.Label(self.body, text=snapshot.error, style="Error.TLabel", wraplength=420).pack(anchor="w", pady=16)
        elif not snapshot.windows:
            ttk.Label(self.body, text="暂时没有额度数据", style="Muted.TLabel").pack(anchor="w", pady=16)
        else:
            for window in snapshot.windows:
                self._window(window)
        extras = []
        if snapshot.credit_balance is not None:
            extras.append(f"Credits {snapshot.credit_balance}")
        if snapshot.reset_credits_count is not None:
            extras.append(f"赠送重置 {snapshot.reset_credits_count} 次")
        extras.append(f"更新于 {snapshot.sampled_at.astimezone():%H:%M:%S}")
        self.footer.configure(text="   ·   ".join(extras))

    def placeholder(self, text: str) -> None:
        for child in self.body.winfo_children():
            child.destroy()
        self.meta.configure(text="尚未连接")
        ttk.Label(self.body, text=text, style="Muted.TLabel", wraplength=420).pack(anchor="center", pady=34)
        self.footer.configure(text="")

    def _window(self, window: LimitWindow) -> None:
        row = ttk.Frame(self.body, style="CardInner.TFrame")
        row.pack(fill="x", pady=(0, 17))
        header = ttk.Frame(row, style="CardInner.TFrame")
        header.pack(fill="x")
        ttk.Label(header, text=window.label, style="Body.TLabel").pack(side="left")
        ttk.Label(
            header, text=f"剩余 {window.remaining_percent:.0f}%", style="Value.TLabel"
        ).pack(side="right")
        ttk.Progressbar(
            row, maximum=100, value=window.used_percent, style=quota_bar_style(window.used_percent)
        ).pack(fill="x", pady=(7, 5))
        ttk.Label(
            row,
            text=f"已用 {window.used_percent:.0f}%  ·  {format_countdown(window.resets_at)}  ·  {local_time(window.resets_at)}",
            style="Muted.TLabel",
        ).pack(anchor="w")


class App(tk.Tk):
    REFRESH_MS = 5 * 60 * 1000

    def __init__(self) -> None:
        super().__init__()
        self.title("AI 额度 Monitor")
        self.geometry("1320x800")
        self.minsize(1080, 720)
        self.configure(bg=BG)
        self._configure_styles()

        self.account_store = AccountStore()
        self.accounts = self.account_store.load()
        self.grant_services = {
            account.id: GrantService(self.account_store.grant_store(account)) for account in self.accounts
        }
        self.current_account_id = self.accounts[0].id
        self.results: queue.Queue[tuple[str, QuotaSnapshot]] = queue.Queue()
        self.snapshots: dict[str, QuotaSnapshot] = {}
        self.pending: set[str] = set()
        self.refreshing = False
        self.auto_refresh_job: str | None = None
        self.pet: PetWindow | None = None

        self._build()
        self.after(150, self.refresh)
        self.after(60_000, self._tick)

    def _configure_styles(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Root.TFrame", background=BG)
        style.configure("Card.TFrame", background=CARD, bordercolor=BORDER, relief="solid", borderwidth=1)
        style.configure("CardInner.TFrame", background=CARD, borderwidth=0)
        style.configure("Header.TLabel", background=BG, foreground=TEXT, font=("Segoe UI", 24, "bold"))
        style.configure("Subtitle.TLabel", background=BG, foreground=MUTED, font=("Segoe UI", 10))
        style.configure("Title.TLabel", background=CARD, foreground=TEXT, font=("Segoe UI", 16, "bold"))
        style.configure("Body.TLabel", background=CARD, foreground=TEXT, font=("Segoe UI", 10, "bold"))
        style.configure("Value.TLabel", background=CARD, foreground=ACCENT, font=("Segoe UI", 11, "bold"))
        style.configure("Muted.TLabel", background=CARD, foreground=MUTED, font=("Segoe UI", 9))
        style.configure("Error.TLabel", background=CARD, foreground=RED, font=("Segoe UI", 10))
        style.configure("Section.TLabel", background=BG, foreground=TEXT, font=("Segoe UI", 13, "bold"))
        style.configure("Status.TLabel", background=BG, foreground=MUTED, font=("Segoe UI", 9))
        style.configure("Safe.Horizontal.TProgressbar", background=GREEN, troughcolor="#EDE7DD", borderwidth=0)
        style.configure("Warning.Horizontal.TProgressbar", background=ACCENT, troughcolor="#EDE7DD", borderwidth=0)
        style.configure("Danger.Horizontal.TProgressbar", background=RED, troughcolor="#EDE7DD", borderwidth=0)
        style.configure("Soft.TButton", background=CARD, foreground=TEXT, bordercolor=BORDER, padding=(13, 8))
        style.map("Soft.TButton", background=[("active", CARD_ALT)])
        style.configure("Accent.TButton", background=ACCENT, foreground="white", padding=(15, 8), font=("Segoe UI", 10, "bold"))
        style.map("Accent.TButton", background=[("active", "#F47D40")])
        style.configure("Warm.TCombobox", fieldbackground=CARD, background=CARD, foreground=TEXT, arrowcolor=ACCENT, padding=7)
        style.configure("Treeview", background=CARD, fieldbackground=CARD, foreground=TEXT, rowheight=30, bordercolor=BORDER)
        style.configure("Treeview.Heading", background="#EEE9E0", foreground=TEXT, font=("Segoe UI", 9, "bold"))
        style.map("Treeview", background=[("selected", ACCENT_SOFT)], foreground=[("selected", TEXT)])

    def _build(self) -> None:
        root = ttk.Frame(self, style="Root.TFrame", padding=28)
        root.pack(fill="both", expand=True)
        header = ttk.Frame(root, style="Root.TFrame")
        header.pack(fill="x")
        titles = ttk.Frame(header, style="Root.TFrame")
        titles.pack(side="left")
        ttk.Label(titles, text="AI 额度 Monitor", style="Header.TLabel").pack(anchor="w")
        ttk.Label(titles, text="安静地掌握每个 Agent 的可用额度", style="Subtitle.TLabel").pack(anchor="w")
        ttk.Button(header, text="立即刷新", style="Accent.TButton", command=self.refresh).pack(side="right")
        ttk.Button(header, text="登录 Claude", style="Soft.TButton", command=self._login_claude).pack(side="right", padx=8)
        self.pet_button = ttk.Button(header, text="🐾 桌面宠物", style="Soft.TButton", command=self._toggle_pet)
        self.pet_button.pack(side="right", padx=(0, 8))

        account_bar = ttk.Frame(root, style="Root.TFrame")
        account_bar.pack(fill="x", pady=(22, 10))
        ttk.Label(account_bar, text="Codex 账号", style="Section.TLabel").pack(side="left")
        self.account_combo = ttk.Combobox(account_bar, state="readonly", style="Warm.TCombobox", width=38)
        self.account_combo.pack(side="left", padx=(12, 7))
        self.account_combo.bind("<<ComboboxSelected>>", self._select_account)
        ttk.Button(account_bar, text="＋ 添加账号", style="Soft.TButton", command=self._add_account).pack(side="left")
        ttk.Button(account_bar, text="移除", style="Soft.TButton", command=self._remove_account).pack(side="left", padx=7)
        self._update_account_combo()

        self.cards_container = ttk.Frame(root, style="Root.TFrame")
        self.cards_container.pack(fill="x", pady=(0, 20))
        self.codex_cards: dict[str, QuotaCard] = {}
        self._rebuild_cards()

        grant_header = ttk.Frame(root, style="Root.TFrame")
        grant_header.pack(fill="x", pady=(0, 8))
        ttk.Label(grant_header, text="所有 Codex 账号赠送重置", style="Section.TLabel").pack(side="left")
        ttk.Button(grant_header, text="删除", style="Soft.TButton", command=self._delete_grant).pack(side="right")
        ttk.Button(grant_header, text="校正获得时间", style="Soft.TButton", command=self._edit_grant).pack(side="right", padx=7)
        ttk.Button(grant_header, text="手动添加", style="Soft.TButton", command=self._add_grant).pack(side="right")

        columns = ("account", "remaining", "granted", "expires", "countdown", "source")
        self.tree = ttk.Treeview(root, columns=columns, show="headings", height=6)
        for col, text, width in (
            ("account", "账号", 190), ("remaining", "可用/总数", 90), ("granted", "获得时间", 150),
            ("expires", "预计失效时间", 160), ("countdown", "剩余", 160), ("source", "时间可信度", 200),
        ):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor="center")
        self.tree.pack(fill="both", expand=True)
        self._render_grants()
        ttk.Label(
            root,
            text="Codex 只提供赠送重置的可用次数，不提供授予或到期时间。自动记录按首次观察 + 30 天估算，可在这里校正。",
            style="Status.TLabel",
        ).pack(anchor="w", pady=(8, 0))
        self.status = ttk.Label(root, text="准备刷新", style="Status.TLabel")
        self.status.pack(anchor="e", pady=(3, 0))

    def current_account(self) -> CodexAccount:
        return next(account for account in self.accounts if account.id == self.current_account_id)

    def current_grants(self) -> GrantService:
        return self.grant_services[self.current_account_id]

    def _account_label(self, account: CodexAccount) -> str:
        snapshot = self.snapshots.get(f"codex:{account.id}")
        suffix = snapshot.account_email if snapshot and snapshot.account_email else "未登录" if snapshot and snapshot.error else ""
        return f"{account.name}  ·  {suffix}" if suffix else account.name

    def _update_account_combo(self) -> None:
        self.account_combo["values"] = [self._account_label(account) for account in self.accounts]
        index = next((i for i, a in enumerate(self.accounts) if a.id == self.current_account_id), 0)
        self.account_combo.current(index)

    def _select_account(self, _event: object = None) -> None:
        index = self.account_combo.current()
        if index < 0:
            return
        self.current_account_id = self.accounts[index].id
        self._render_grants()

    def _rebuild_cards(self) -> None:
        for child in self.cards_container.winfo_children():
            child.destroy()
        self.codex_cards = {}
        cards: list[tuple[QuotaCard, QuotaSnapshot | None]] = []
        self.claude_card = QuotaCard(self.cards_container, "Claude Code", "✦")
        cards.append((self.claude_card, self.snapshots.get("claude")))
        for account in self.accounts:
            card = QuotaCard(self.cards_container, f"Codex · {account.name}", "⚡")
            self.codex_cards[account.id] = card
            cards.append((card, self.snapshots.get(f"codex:{account.id}")))
        for column in range(3):
            self.cards_container.columnconfigure(column, weight=1, uniform="cards")
        for index, (card, snapshot) in enumerate(cards):
            row, column = divmod(index, 3)
            card.grid(row=row, column=column, sticky="nsew", padx=6, pady=6)
            if snapshot:
                card.render(snapshot)
            else:
                card.placeholder("等待刷新额度数据")

    def refresh(self) -> None:
        if self.refreshing:
            return
        if self.auto_refresh_job:
            self.after_cancel(self.auto_refresh_job)
            self.auto_refresh_job = None
        self.refreshing = True
        keys = ["claude"] + [f"codex:{account.id}" for account in self.accounts]
        self.pending = set(keys)
        self.status.configure(text=f"正在刷新 Claude 和 {len(self.accounts)} 个 Codex 账号…")

        def worker(key: str, collector: object) -> None:
            self.results.put((key, collector.collect()))  # type: ignore[attr-defined]

        threading.Thread(target=worker, args=("claude", ClaudeCollector()), daemon=True).start()
        for account in self.accounts:
            collector = CodexCollector(codex_home=Path(account.codex_home))
            threading.Thread(target=worker, args=(f"codex:{account.id}", collector), daemon=True).start()
        self.after(100, self._poll_results)

    def _poll_results(self) -> None:
        while True:
            try:
                key, snapshot = self.results.get_nowait()
            except queue.Empty:
                break
            self.snapshots[key] = snapshot
            self.pending.discard(key)
            if key == "claude":
                self.claude_card.render(snapshot)
            else:
                account_id = key.split(":", 1)[1]
                if self.grant_services[account_id].reconcile(snapshot.reset_credits_count) and account_id == self.current_account_id:
                    self._render_grants()
                if account_id in self.codex_cards:
                    self.codex_cards[account_id].render(snapshot)
        self._update_account_combo()
        if self.pending:
            self.after(100, self._poll_results)
            return
        self.refreshing = False
        errors = sum(1 for snapshot in self.snapshots.values() if snapshot.error)
        self.status.configure(text="刷新完成" if not errors else f"刷新完成 · {errors} 个数据源不可用")
        self.auto_refresh_job = self.after(self.REFRESH_MS, self.refresh)

    def _tick(self) -> None:
        if "claude" in self.snapshots:
            self.claude_card.render(self.snapshots["claude"])
        for account_id, card in self.codex_cards.items():
            snapshot = self.snapshots.get(f"codex:{account_id}")
            if snapshot:
                card.render(snapshot)
        self._render_grants()
        self.after(60_000, self._tick)

    def _add_account(self) -> None:
        name = simpledialog.askstring("添加 Codex 账号", "账号备注名称：", parent=self)
        if not name or not name.strip():
            return
        account = self.account_store.create(name.strip())
        self.accounts.append(account)
        self.account_store.save(self.accounts)
        self.grant_services[account.id] = GrantService(self.account_store.grant_store(account))
        self.current_account_id = account.id
        self._update_account_combo()
        self._rebuild_cards()
        executable = CodexCollector()._find_executable()
        if not executable:
            messagebox.showerror("未找到 Codex", "请先安装或启动 Codex Desktop。", parent=self)
            return
        env = {**os.environ, "CODEX_HOME": account.codex_home}
        flags = subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0
        subprocess.Popen([str(executable), "login", "--device-auth"], env=env, creationflags=flags)
        messagebox.showinfo(
            "完成登录",
            "已为此账号打开独立的 Codex 登录窗口。按窗口提示完成登录后，回到这里点击“立即刷新”。",
            parent=self,
        )

    def _toggle_pet(self) -> None:
        if self.pet is not None:
            self.pet.close()
            return
        self.pet = PetWindow(
            self,
            codex_homes_provider=lambda: [account.codex_home for account in self.accounts],
            on_open_main=self._show_main_window,
            on_close=self._on_pet_closed,
        )
        self.pet_button.configure(text="🐾 隐藏宠物")

    def _on_pet_closed(self) -> None:
        self.pet = None
        self.pet_button.configure(text="🐾 桌面宠物")

    def _show_main_window(self) -> None:
        self.deiconify()
        self.lift()
        self.focus_force()

    def _login_claude(self) -> None:
        executable = shutil.which("claude")
        if not executable:
            messagebox.showerror("未找到 Claude Code", "请先安装 Claude Code CLI。", parent=self)
            return
        flags = subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0
        subprocess.Popen([executable, "auth", "login"], creationflags=flags)
        messagebox.showinfo(
            "Claude 登录",
            "已打开 Claude 登录窗口。完成授权后返回这里点击“立即刷新”。",
            parent=self,
        )

    def _remove_account(self) -> None:
        account = self.current_account()
        if account.is_default:
            messagebox.showinfo("默认账号", "默认 Codex 账号不能从监控器移除。", parent=self)
            return
        if not messagebox.askyesno("移除账号", f"停止监控“{account.name}”？登录目录会保留，不会删除。", parent=self):
            return
        self.accounts = [item for item in self.accounts if item.id != account.id]
        self.account_store.save(self.accounts)
        self.grant_services.pop(account.id, None)
        self.snapshots.pop(f"codex:{account.id}", None)
        self.current_account_id = self.accounts[0].id
        self._update_account_combo()
        self._render_grants()
        self._rebuild_cards()

    def _render_grants(self) -> None:
        if not hasattr(self, "tree"):
            return
        selected = self.tree.selection()
        for item in self.tree.get_children():
            self.tree.delete(item)
        for account in self.accounts:
            service = self.grant_services[account.id]
            snapshot = self.snapshots.get(f"codex:{account.id}")
            identity = snapshot.account_email if snapshot and snapshot.account_email else account.name
            grants = sorted(service.grants, key=lambda item: item.expires_at)
            if not grants:
                live_count = snapshot.reset_credits_count if snapshot and snapshot.reset_credits_count is not None else 0
                self.tree.insert(
                    "", "end", iid=f"empty:{account.id}",
                    values=(identity, f"{live_count}/{live_count}", "--", "--", "无可跟踪记录", "官方可用次数"),
                )
                continue
            for grant in grants:
                self.tree.insert(
                    "", "end", iid=f"{account.id}:{grant.id}",
                    values=(
                        identity, f"{grant.remaining}/{grant.count}", local_time(grant.granted_at),
                        local_time(grant.expires_at), self._expiry_text(grant),
                        "估算（可校正）" if grant.estimated else "手工确认",
                    ),
                )
        if selected and self.tree.exists(selected[0]):
            self.tree.selection_set(selected[0])

    @staticmethod
    def _expiry_text(grant: GrantBatch) -> str:
        if not grant.remaining:
            return "已使用/失效"
        if grant.expires_at <= utc_now():
            return "估算已过期"
        return format_countdown(grant.expires_at).replace("后重置", "后失效")

    def _selected_grant(self) -> tuple[str, str] | None:
        selection = self.tree.selection()
        if not selection:
            messagebox.showinfo("请选择记录", "请先选择一条赠送重置记录。", parent=self)
            return None
        item_id = selection[0]
        if item_id.startswith("empty:"):
            messagebox.showinfo("没有记录", "该账号当前没有可编辑的赠送记录。", parent=self)
            return None
        account_id, grant_id = item_id.split(":", 1)
        return account_id, grant_id

    def _add_grant(self) -> None:
        count = simpledialog.askinteger("手动添加", "赠送重置次数：", minvalue=1, maxvalue=100, parent=self)
        if count is None:
            return
        granted = self._ask_datetime("获得时间（本地时间）", datetime.now().strftime("%Y-%m-%d %H:%M"))
        if granted:
            self.current_grants().add(count, granted)
            self._render_grants()

    def _edit_grant(self) -> None:
        selected = self._selected_grant()
        if not selected:
            return
        account_id, grant_id = selected
        service = self.grant_services[account_id]
        grant = next(item for item in service.grants if item.id == grant_id)
        granted = self._ask_datetime("校正获得时间（本地时间）", grant.granted_at.astimezone().strftime("%Y-%m-%d %H:%M"))
        if granted:
            service.update_date(grant_id, granted)
            self._render_grants()

    def _delete_grant(self) -> None:
        selected = self._selected_grant()
        if selected and messagebox.askyesno("删除记录", "确定删除这条本地跟踪记录？", parent=self):
            account_id, grant_id = selected
            self.grant_services[account_id].delete(grant_id)
            self._render_grants()

    def _ask_datetime(self, title: str, initial: str) -> datetime | None:
        value = simpledialog.askstring(title, "格式：YYYY-MM-DD HH:MM", initialvalue=initial, parent=self)
        if value is None:
            return None
        try:
            return datetime.strptime(value.strip(), "%Y-%m-%d %H:%M").astimezone().astimezone(timezone.utc)
        except ValueError:
            messagebox.showerror("时间格式错误", "请输入例如：2026-06-19 18:30", parent=self)
            return None


def run() -> None:
    App().mainloop()
