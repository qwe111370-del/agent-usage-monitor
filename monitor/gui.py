"""Agent 用量監控畫面；一般模式看完整卡片，懸浮模式左鍵拖曳、雙擊還原、右鍵選單。"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
import math
from pathlib import Path
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk

from .model import AgentStatus, QuotaWindow
from .ui_settings import load_settings, save_settings

try:
    from .fmt import format_clock, format_countdown, format_tokens
except ImportError:
    def format_tokens(n: int) -> str:
        """後端尚未備妥時，依規格顯示千分位、萬與億。"""
        n = max(0, n)
        if n < 10_000:
            return f"{n:,}"
        if n < 100_000_000:
            return f"{(n + 5_000) // 10_000:,} 萬"
        rounded = (Decimal(n) / 100_000_000).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
        return f"{rounded:,.1f} 億"

    def format_countdown(seconds: float) -> str:
        """後端尚未備妥時，依剩餘時間顯示重置倒數。"""
        if seconds <= 0 or not math.isfinite(seconds):
            return "已重置"
        if seconds < 3_600:
            return f"{math.ceil(seconds / 60)} 分"
        if seconds < 86_400:
            hours, minutes = divmod(math.ceil(seconds / 60), 60)
            return f"{hours} 小時" + (f" {minutes} 分" if minutes else "")
        days, hours = divmod(int(seconds // 3_600), 24)
        return f"{days} 天" + (f" {hours} 小時" if hours else "")

    def format_clock(ts: float) -> str:
        """後端尚未備妥時，顯示本機時間，跨日附上月日。"""
        value = datetime.fromtimestamp(ts)
        return value.strftime("%H:%M" if value.date() == datetime.now().date() else "%m/%d %H:%M")


FONT = "Microsoft JhengHei UI"
BACKGROUND = "#f2f4f7"
DARK = "#1e1e1e"
LIGHT = "#eeeeee"
MUTED = "#657080"


def quota_color(status: AgentStatus, window: QuotaWindow, now: float) -> str:
    """剩餘額度顏色；超過半小時的本機紀錄或快照改為灰色。"""
    if (status.quota_source in ("snapshot", "local")
            and status.quota_as_of is not None and now - status.quota_as_of > 1_800):
        return "#8a8a8a"
    if window.remaining_pct >= 50:
        return "#2e9e5b"
    if window.remaining_pct >= 20:
        return "#d99a1e"
    return "#d64545"


def source_text(status: AgentStatus) -> str:
    """將額度來源轉成畫面與提示共用的繁體中文。"""
    clock = format_clock(status.quota_as_of) if status.quota_as_of is not None else "時間未知"
    return {
        "live": f"即時查詢・{clock} 更新",
        "local": f"本機紀錄・{clock}（Codex 執行時才會更新）",
        "snapshot": f"桌面 App 快照・{clock}",
    }.get(status.quota_source, "沒有額度資料")


def token_text(status: AgentStatus) -> str:
    """同時呈現總量與扣除快取讀取後的用量。"""
    text = (f"今日 {format_tokens(status.tokens_today.total)} tokens"
            f"（不含快取讀取 {format_tokens(status.tokens_today.non_cache)}）")
    if status.tokens_window is not None:
        label = status.window_label or "近 5 小時"
        text += f"｜{label}視窗內 {format_tokens(status.tokens_window.total)}"
    return text


def reset_text(window: QuotaWindow, now: float) -> str:
    """未知重置時間留白，已到期時避免顯示『已重置後重置』。"""
    if window.resets_at is None:
        return ""
    remaining = window.resets_at - now
    return "已重置" if remaining <= 0 else f"{format_countdown(remaining)}後重置"


def _collect_loop(collector, interval: float, results: queue.Queue,
                  refresh: threading.Event, stop: threading.Event) -> None:
    """背景執行緒只處理收集與佇列，不持有或呼叫任何 tkinter 物件。"""
    while not stop.is_set():
        force = refresh.is_set()
        refresh.clear()
        try:
            statuses = collector.collect(force_remote=force)
            result = (statuses, time.time(), None)
        except Exception:
            result = (None, time.time(), "更新失敗，請稍後再試。")
        if stop.is_set():
            return
        results.put(result)
        refresh.wait(interval)


class MonitorApp:
    """主執行緒的視窗控制器，收集器僅由一個 daemon 執行緒呼叫。"""

    def __init__(self, root: tk.Tk, collector, config=None,
                 settings_path: Path | None = None):
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError("畫面必須在主執行緒建立")
        self.root = root
        self.settings_path = settings_path
        self.settings = load_settings(settings_path)
        self.mode = self.settings.mode
        self.statuses: list[AgentStatus] = []
        self._closed = False
        self._last_update: float | None = None
        self._collection_error: str | None = None
        self._save_error = False
        self._results = queue.Queue()
        self._refresh_event = threading.Event()
        self._stop_event = threading.Event()
        self._tooltip = None
        self._tooltip_after = None
        self._hover_agent = None
        self._drag_origin = None
        self._poll_after = None
        self._countdown_after = None
        self._topmost = tk.BooleanVar(root, value=self.settings.normal_topmost)
        self._alpha = tk.IntVar(root, value=round(self.settings.alpha * 100))
        self._footer_text = tk.StringVar(root, value="正在更新…")
        root.title("Agent 用量監控")
        root.option_add("*Font", (FONT, 10))
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.bind("<MouseWheel>", self._mousewheel)
        self._build_normal()
        self._build_compact()
        self._apply_mode()
        try:
            interval = max(1.0, float(getattr(config, "local_interval_s", 30)))
            if not math.isfinite(interval):
                interval = 30.0
        except (TypeError, ValueError):
            interval = 30.0
        self._worker = threading.Thread(
            target=_collect_loop,
            args=(collector, interval, self._results, self._refresh_event, self._stop_event),
            daemon=True, name="用量資料收集",
        )
        self._worker.start()
        self._poll_after = root.after(500, self._poll_results)
        self._countdown_after = root.after(30_000, self._tick_countdowns)

    def _build_normal(self):
        self.normal_frame = tk.Frame(self.root, bg=BACKGROUND)
        heading = tk.Frame(self.normal_frame, bg=BACKGROUND)
        heading.pack(fill="x", padx=18, pady=(14, 8))
        tk.Label(heading, text="Agent 用量監控", bg=BACKGROUND, fg="#202c3b",
                 font=(FONT, 17, "bold")).pack(side="left")
        tk.Label(heading, text="本機用量 · 官方剩餘額度", bg=BACKGROUND,
                 fg=MUTED).pack(side="right")
        scroll_area = tk.Frame(self.normal_frame, bg=BACKGROUND)
        scroll_area.pack(fill="both", expand=True)
        self.cards_canvas = tk.Canvas(scroll_area, bg=BACKGROUND, highlightthickness=0)
        scrollbar = ttk.Scrollbar(scroll_area, orient="vertical", command=self.cards_canvas.yview)
        self.cards_canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.cards_canvas.pack(side="left", fill="both", expand=True)
        self.cards_frame = tk.Frame(self.cards_canvas, bg=BACKGROUND)
        self._cards_window = self.cards_canvas.create_window((0, 0), window=self.cards_frame, anchor="nw")
        self.cards_frame.bind("<Configure>", lambda e: self.cards_canvas.configure(
            scrollregion=self.cards_canvas.bbox("all")))
        self.cards_canvas.bind("<Configure>", lambda e: self.cards_canvas.itemconfigure(
            self._cards_window, width=e.width))
        self.footer = tk.Frame(self.normal_frame, bg=BACKGROUND)
        self.footer.pack(fill="x", padx=18, pady=12)
        tk.Label(self.footer, textvariable=self._footer_text, bg=BACKGROUND, fg=MUTED,
                 anchor="w").pack(fill="x", pady=(0, 8))
        self.refresh_button = ttk.Button(self.footer, text="立即更新", command=self.refresh_now)
        self.refresh_button.pack(side="left")
        ttk.Button(self.footer, text="懸浮模式", command=lambda: self.set_mode("compact")).pack(
            side="left", padx=8)
        ttk.Checkbutton(self.footer, text="一般模式也置頂", variable=self._topmost,
                        command=self._change_topmost).pack(side="right")

    def _build_compact(self):
        self.compact_frame = tk.Frame(self.root, bg=DARK, padx=12, pady=8,
                                      highlightbackground="#454545", highlightthickness=1)
        self.menu = tk.Menu(self.root, tearoff=False, font=(FONT, 10))
        self.menu.add_command(label="一般模式", command=lambda: self.set_mode("normal"))
        self.menu.add_command(label="立即更新", command=self.refresh_now)
        opacity_menu = tk.Menu(self.menu, tearoff=False, font=(FONT, 10))
        for value in (70, 85, 92, 100):
            opacity_menu.add_radiobutton(label=f"{value}%", value=value,
                                         variable=self._alpha, command=self._change_alpha)
        self.menu.add_cascade(label="透明度", menu=opacity_menu)
        self.menu.add_separator()
        self.menu.add_command(label="結束", command=self.close)
        self._bind_compact(self.compact_frame)

    def _remember_position(self):
        self.root.update_idletasks()
        position = (self.root.winfo_x(), self.root.winfo_y())
        setattr(self.settings, f"{self.mode}_position", position)

    def _persist(self):
        self._save_error = not save_settings(self.settings, self.settings_path)
        self._update_footer()

    def set_mode(self, mode: str):
        """切換模式並保存、還原各自的位置。"""
        if self._closed or mode == self.mode:
            return
        if mode not in ("normal", "compact"):
            raise ValueError("未知的畫面模式")
        self._hide_tooltip()
        self._drag_origin = None
        self._remember_position()
        self.mode = self.settings.mode = mode
        self._apply_mode()
        self._persist()

    def _apply_mode(self):
        root = self.root
        root.withdraw()
        self.normal_frame.pack_forget()
        self.compact_frame.pack_forget()
        compact = self.mode == "compact"
        root.overrideredirect(compact)
        root.attributes("-topmost", compact or self._topmost.get())
        root.attributes("-alpha", self.settings.alpha if compact else 1.0)
        root.configure(bg=DARK if compact else BACKGROUND)
        root.resizable(not compact, not compact)
        root.minsize(1, 1)
        self._render()
        if compact:
            self.compact_frame.pack(fill="both", expand=True)
            root.update_idletasks()
            size = f"{self.compact_frame.winfo_reqwidth()}x{self.compact_frame.winfo_reqheight()}"
        else:
            self.normal_frame.pack(fill="both", expand=True)
            width = min(780, max(480, root.winfo_screenwidth() - 100))
            height = min(760, max(350, root.winfo_screenheight() - 140))
            root.minsize(min(600, width), 350)
            size = f"{width}x{height}"
        position = getattr(self.settings, f"{self.mode}_position")
        if position is None:
            position = (60, 60) if not compact else (100, 100)
        root.geometry(size + f"{position[0]:+d}{position[1]:+d}")
        root.deiconify()

    def _render(self):
        self._hide_tooltip()
        now = time.time()
        if self.mode == "normal":
            view = self.cards_canvas.yview()
            for child in self.cards_frame.winfo_children():
                child.destroy()
            if not self.statuses:
                tk.Label(self.cards_frame, text="正在等候用量資料…", bg=BACKGROUND,
                         fg=MUTED, pady=30).pack()
            for status in self.statuses:
                self._render_card(status, now)
            if view:
                self.cards_canvas.yview_moveto(view[0])
        else:
            for child in self.compact_frame.winfo_children():
                child.destroy()
            if not self.statuses:
                label = tk.Label(self.compact_frame, text=self._collection_error or "正在等候用量資料…",
                                 bg=DARK, fg=LIGHT)
                label.pack()
                self._bind_compact(label)
            for status in self.statuses:
                self._render_compact_row(status, now)
            self.root.update_idletasks()
            self.root.geometry(f"{self.compact_frame.winfo_reqwidth()}x{self.compact_frame.winfo_reqheight()}")
        self._update_footer()

    def _render_card(self, status: AgentStatus, now: float):
        card = tk.Frame(self.cards_frame, bg="white", padx=16, pady=12,
                        highlightbackground="#dce2e9", highlightthickness=1)
        card.pack(fill="x", padx=18, pady=(0, 10))
        title = tk.Frame(card, bg="white")
        title.pack(fill="x")
        tk.Label(title, text=status.display_name, bg="white", fg="#202c3b",
                 font=(FONT, 13, "bold")).pack(side="left")
        if status.plan:
            tk.Label(title, text=f"{status.plan.title()} 方案", bg="white", fg=MUTED).pack(side="right")
        details = tk.Label(card, text=token_text(status), anchor="w", justify="left",
                           bg="white", fg="#455267", wraplength=880)
        details.pack(fill="x", pady=(5, 9))
        card.bind("<Configure>", lambda e: details.configure(wraplength=max(100, e.width - 34)))
        for window in status.windows:
            line = tk.Frame(card, bg="white")
            line.pack(fill="x", pady=3)
            line.columnconfigure(1, weight=1)
            tk.Label(line, text=window.label, anchor="w", width=25, bg="white", fg="#35445a",
                     wraplength=240).grid(row=0, column=0, sticky="w", padx=(0, 12))
            bar = tk.Canvas(line, height=12, width=180, bg="#e7ebef", highlightthickness=0)
            bar.grid(row=0, column=1, sticky="ew", padx=(0, 10))
            color = quota_color(status, window, now)

            def draw(event, canvas=bar, pct=window.remaining_pct, fill=color):
                canvas.delete("all")
                canvas.create_rectangle(0, 0, event.width * pct / 100, 12, fill=fill, outline="")

            bar.bind("<Configure>", draw)
            tk.Label(line, text=f"剩 {window.remaining_pct:.0f}%", width=8, anchor="e",
                     bg="white", fg=color, font=(FONT, 10, "bold")).grid(row=0, column=2)
            tk.Label(line, text=reset_text(window, now), anchor="w", width=23,
                     bg="white", fg=MUTED).grid(row=0, column=3, padx=(8, 0))
        tk.Label(card, text=source_text(status), bg="white", fg=MUTED,
                 anchor="w").pack(fill="x", pady=(7, 0))
        for note in [*status.notes, *([status.error] if status.error else [])]:
            warning = tk.Label(card, text=f"⚠ {note}", bg="white", fg="#94611a",
                               anchor="w", justify="left", wraplength=850, font=(FONT, 9))
            warning.pack(fill="x", pady=(4, 0))
            warning.bind("<Configure>", lambda e, label=warning: label.configure(
                wraplength=max(100, e.width)))

    def _render_compact_row(self, status: AgentStatus, now: float):
        row = tk.Frame(self.compact_frame, bg=DARK)
        row.pack(fill="x", pady=3)
        name = {"claude": "Claude", "codex": "Codex", "agy": "Antigr"}.get(status.agent, status.display_name)
        parts = [(name, LIGHT)]
        for index, window in enumerate(status.windows[:2]):
            label = (window.label.replace("5 小時", "5h").replace("每週", "週")
                     .replace("Claude and GPT models", "Claude/GPT").replace(" Models", ""))
            parts.append((f"{' · ' if index else '  '}{label} {window.remaining_pct:.0f}%",
                          quota_color(status, window, now)))
        if not status.windows:
            parts.append(("  沒有額度資料", "#aaaaaa"))
        parts.append((f" · 今日 {format_tokens(status.tokens_today.total).replace(' ', '')}", LIGHT))
        if status.error:
            parts.append((" ⚠", "#d99a1e"))
        self._bind_compact(row, status)
        for text, color in parts:
            label = tk.Label(row, text=text, bg=DARK, fg=color, padx=0, pady=0,
                             font=(FONT, 10, "bold" if text == name else "normal"))
            label.pack(side="left")
            self._bind_compact(label, status)

    def _bind_compact(self, widget, status=None):
        widget.bind("<ButtonPress-1>", self._drag_start)
        widget.bind("<B1-Motion>", self._drag_move)
        widget.bind("<ButtonRelease-1>", self._drag_end)
        widget.bind("<Double-Button-1>", lambda e: self.set_mode("normal"))
        widget.bind("<Button-3>", self._popup_menu)
        if status is not None:
            widget.bind("<Enter>", lambda e, item=status: self._schedule_tooltip(item, e.x_root, e.y_root))
            widget.bind("<Leave>", self._tooltip_leave)

    def _drag_start(self, event):
        self._hide_tooltip()
        self._drag_origin = (event.x_root, event.y_root, self.root.winfo_x(), self.root.winfo_y())

    def _drag_move(self, event):
        if self._drag_origin is not None and self.mode == "compact":
            x, y, left, top = self._drag_origin
            self.root.geometry(f"{left + event.x_root - x:+d}{top + event.y_root - y:+d}")

    def _drag_end(self, event):
        if self._drag_origin is not None and self.mode == "compact":
            self._drag_origin = None
            self._remember_position()
            self._persist()

    def _popup_menu(self, event):
        self._hide_tooltip()
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    def _schedule_tooltip(self, status, x, y):
        if self._hover_agent == status.agent:
            return
        self._hide_tooltip()
        self._hover_agent = status.agent
        self._tooltip_after = self.root.after(1_000, lambda: self._show_tooltip(status, x, y))

    def _tooltip_leave(self, event):
        self.root.after_idle(self._check_tooltip_leave)

    def _check_tooltip_leave(self):
        if self._closed or self._hover_agent is None:
            return
        x, y = self.root.winfo_pointerxy()
        widget = self.root.winfo_containing(x, y)
        while widget is not None and widget.master is not self.compact_frame:
            widget = widget.master
        # 同一行的文字元件之間移動時，保留已累計的停留時間。
        rows = self.compact_frame.winfo_children()
        row_index = rows.index(widget) if widget in rows else -1
        if row_index < 0 or row_index >= len(self.statuses) or self.statuses[row_index].agent != self._hover_agent:
            self._hide_tooltip()

    def _show_tooltip(self, status, x, y):
        self._tooltip_after = None
        if self._closed or self.mode != "compact":
            return
        now = time.time()
        lines = [status.display_name + (f" · {status.plan.title()} 方案" if status.plan else ""), token_text(status)]
        lines.extend(f"{w.label}：剩 {w.remaining_pct:.0f}%  {reset_text(w, now)}" for w in status.windows)
        lines.append(source_text(status))
        lines.extend(f"⚠ {note}" for note in status.notes)
        if status.error:
            lines.append(f"⚠ {status.error}")
        if self._collection_error:
            lines.append(self._collection_error)
        tip = self._tooltip = tk.Toplevel(self.root)
        tip.withdraw()
        tip.overrideredirect(True)
        tip.attributes("-topmost", True)
        tk.Label(tip, text="\n".join(lines), justify="left", anchor="w", bg="#fffbe8",
                 fg="#242424", relief="solid", borderwidth=1, padx=12, pady=9,
                 wraplength=540, font=(FONT, 10)).pack()
        tip.update_idletasks()
        left = max(0, min(x + 12, tip.winfo_screenwidth() - tip.winfo_reqwidth() - 8))
        top = max(0, min(y + 22, tip.winfo_screenheight() - tip.winfo_reqheight() - 8))
        tip.geometry(f"+{left}+{top}")
        tip.deiconify()

    def _hide_tooltip(self):
        if self._tooltip_after is not None:
            self.root.after_cancel(self._tooltip_after)
            self._tooltip_after = None
        if self._tooltip is not None:
            self._tooltip.destroy()
            self._tooltip = None
        self._hover_agent = None

    def _mousewheel(self, event):
        if self.mode == "normal":
            self.cards_canvas.yview_scroll(-int(event.delta / 120), "units")

    def _change_topmost(self):
        self.settings.normal_topmost = self._topmost.get()
        if self.mode == "normal":
            self.root.attributes("-topmost", self.settings.normal_topmost)
        self._persist()

    def _change_alpha(self):
        self.settings.alpha = self._alpha.get() / 100
        if self.mode == "compact":
            self.root.attributes("-alpha", self.settings.alpha)
        self._persist()

    def refresh_now(self):
        """通知背景執行緒強制查詢；主執行緒不呼叫收集器。"""
        if not self._closed:
            self._footer_text.set("正在更新…")
            self._refresh_event.set()

    def _poll_results(self):
        self._poll_after = None
        if self._closed:
            return
        changed = False
        while True:
            try:
                statuses, timestamp, error = self._results.get_nowait()
            except queue.Empty:
                break
            self._collection_error = error
            if statuses is not None:
                self.statuses = statuses
                self._last_update = timestamp
            changed = True
        if changed:
            self._render()
        self._poll_after = self.root.after(500, self._poll_results)

    def _tick_countdowns(self):
        self._countdown_after = None
        if not self._closed:
            self._render()
            self._countdown_after = self.root.after(30_000, self._tick_countdowns)

    def _update_footer(self):
        text = ("最後更新 " + datetime.fromtimestamp(self._last_update).strftime("%H:%M:%S")
                if self._last_update is not None else "正在等候用量資料…")
        if self._collection_error:
            text += f" · {self._collection_error}"
        if self._save_error:
            text += " · 畫面設定無法儲存"
        self._footer_text.set(text)

    def close(self):
        """保存位置、取消排程並通知背景執行緒結束，不等待網路作業。"""
        if self._closed:
            return
        self._remember_position()
        self._persist()
        self._closed = True
        self._stop_event.set()
        self._refresh_event.set()
        self._hide_tooltip()
        for identifier in (self._poll_after, self._countdown_after):
            if identifier is not None:
                self.root.after_cancel(identifier)
        self.root.destroy()
