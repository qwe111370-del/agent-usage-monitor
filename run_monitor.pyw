"""啟動 Agent 用量監控；加 --demo 展示假資料。懸浮模式左鍵拖曳、雙擊還原、右鍵選單。"""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import os
import sys
import time
import tkinter as tk
from tkinter import messagebox
from types import SimpleNamespace


class ChineseHelpFormatter(argparse.HelpFormatter):
    """讓命令列說明也使用繁體中文。"""

    def add_usage(self, usage, actions, groups, prefix=None):
        super().add_usage(usage, actions, groups, prefix="用法：")


class ChineseArgumentParser(argparse.ArgumentParser):
    """參數有誤時只顯示簡短的中文操作提示。"""

    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(2, "參數錯誤，請使用 --help 查看操作方式。\n")


class DemoCollector:
    """只回傳假資料，不載入後端、不讀取個人資料、不連網。"""

    def __init__(self):
        self.updates = 0

    def collect(self, force_remote: bool = False):
        from monitor.demo_data import demo_statuses

        self.updates += 1
        statuses = demo_statuses(time.time())
        for status in statuses:
            status.tokens_today.output += self.updates * 1_200
        return statuses


def main() -> int:
    """先設定 DPI 與單一實例，再依模式建立資料收集器。"""
    parser = ChineseArgumentParser(description="Agent 用量監控", add_help=False,
                                   formatter_class=ChineseHelpFormatter)
    parser._optionals.title = "選項"
    parser.add_argument("-h", "--help", action="help", help="顯示操作說明並結束")
    parser.add_argument("--demo", action="store_true", help="使用虛構資料，不讀取個人資料或連網")
    args = parser.parse_args()
    mutex = None
    kernel = None
    if os.name == "nt":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
        kernel.CreateMutexW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle.restype = wintypes.BOOL
        mutex = kernel.CreateMutexW(None, False, "Local\\AgentUsageMonitor")
        already_running = ctypes.get_last_error() == 183
    else:
        already_running = False
    root = None
    try:
        root = tk.Tk()
        root.withdraw()
        if already_running:
            messagebox.showinfo("Agent 用量監控", "已經在執行", parent=root)
            return 0
        if kernel is not None and not mutex:
            messagebox.showerror("Agent 用量監控", "無法建立單一執行個體鎖定。", parent=root)
            return 1
        if args.demo:
            collector = DemoCollector()
            config = SimpleNamespace(local_interval_s=30)
        else:
            try:
                from monitor.collector import Collector
                from monitor.config import load_config

                config = load_config()
                collector = Collector(config)
            except Exception:
                messagebox.showerror("Agent 用量監控", "無法載入資料收集器或設定，請確認後端檔案已備妥。", parent=root)
                return 1
        from monitor.gui import MonitorApp

        app = MonitorApp(root, collector, config)
        root.mainloop()
        return 0
    finally:
        if root is not None:
            try:
                root.destroy()
            except tk.TclError:
                pass
        if mutex and kernel is not None:
            kernel.CloseHandle(mutex)


if __name__ == "__main__":
    raise SystemExit(main())
