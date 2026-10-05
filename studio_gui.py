"""SmartVoice 主界面：多引擎、项目编辑、可恢复配音任务。"""

BTN_W = 16  # 标准功能按钮宽度(字符数)
# SLOT_AUD_W/MAX_DUB/FILTERS/gender_of 随面板搬入 ui 包，此处经由 import 重导出以兼容旧引用。
import os
import re
import hashlib
import subprocess
import sys
import threading
import time
from pathlib import Path
import tempfile
from collections import OrderedDict
from concurrent.futures import wait
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import engine
import appmeta
import storage
import theme
from config_store import ConfigStore
from playback import MciPlayer, format_clock
from task_manager import DaemonPool, TaskManager
from ui.serverbar import build_serverbar
from ui.toolbar import build_toolbar
from ui.configbar import build_configbar, ZOOMS
from ui.stylepanel import build_stylepanel
from ui.editor import build_editor
from ui.voicepanel import FILTERS, build_voicepanel
from ui.dubpanel import MAX_DUB, SLOT_AUD_W, build_dubpanel, gender_of
import voice_tasks as workflow
from product_ui import ProductUI, cfg_bool, cfg_int
from theme import (ACCENT, ACCENT_D, BORDER, FEEDBACK, FG, GREEN, GREY,
                   BG, HEADING, HOVER, MUTED, OUTLINE, PANEL, SEL,
                   TOGGLE_IDLE, TOGGLE_OFF, TOGGLE_ON, TRACK, TROUGH)

OUT_DIR = str(storage.EXPORT_DIR)
AUDITION_TEXT = "谁是我们的敌人？谁是我们的朋友？"

# 两套主题(暖白·初/雾蓝) — 颜色统一定义在 theme.py，切换时同步模块并重建界面
# ZOOMS 随配置栏搬入 ui.configbar，此处经由 import 重导出以兼容旧引用。


def safe_name(s):
    return re.sub(r"[^A-Za-z0-9_\-]+", "_", s)[:60]


def loc_of(short):
    p = short.split("-")
    return "-".join(p[:2]) if len(p) >= 2 else short


def want_voice(filter_name, short):
    loc = loc_of(short)
    return {"全部": True, "大陆中文zh-CN": loc == "zh-CN",
            "港中文zh-HK": loc == "zh-HK", "台中文zh-TW": loc == "zh-TW",
            "粤语": loc in ("zh-HK",) or loc.startswith("yue"),
            "英文en": loc.startswith("en"), "日语ja": loc.startswith("ja"),
            "韩语ko": loc.startswith("ko"),
            "其他语种": not loc.startswith(("zh", "yue", "en", "ja", "ko"))}.get(filter_name, True)


class App(ProductUI):
    def __init__(self, root):
        self.root = root
        self.player = MciPlayer()
        self._playback_dir = tempfile.TemporaryDirectory(prefix="SmartVoice-play-")
        self._playing_pcm = None
        self._audition_cache = OrderedDict()
        self._audition_cache_lock = threading.Lock()
        self._synth_pool = DaemonPool(max_workers=2, thread_name_prefix="tts")
        self._config_store = ConfigStore(
            save_fn=lambda cfg: engine.save_json(engine.CONFIG_FILE, cfg),
            schedule_fn=lambda ms, cb: self.root.after(ms, cb),
            cancel_fn=self.root.after_cancel,
            # 只入队（线程安全）；唤醒轮询必须在主线程做，见 _flush_cfg。
            report_fn=lambda tag, err: self._bgq.put((tag, None, err)),
        )
        self.server = None
        self.busy = False
        self._last_w = 0
        cfg = engine.load_json(engine.CONFIG_FILE, {})
        self._init_product(cfg)
        saved_engine = cfg.get("engine", "Azure(填Key)")
        self.engine_var = tk.StringVar(
            value=saved_engine if saved_engine in engine.ENGINE_CHOICES else "Azure(填Key)")
        self.key_var = tk.StringVar(value=(os.getenv("AZURE_SPEECH_KEY", cfg.get("key", ""))
                                          if engine.kind_of(saved_engine) == 'azure' else cfg.get('key', '')))
        self.show_var = tk.BooleanVar(value=False)
        self.region_var = tk.StringVar(value=cfg.get("region", engine.DEFAULT_REGION))
        self.ep_var = tk.StringVar(value=cfg.get("endpoint", engine.default_tts_endpoint(self.region_var.get())))
        self.remember_var = tk.BooleanVar(value=cfg_bool(cfg.get("remember_key", False)))
        self._engine_profiles = cfg.get("engine_profiles", {})
        if not isinstance(self._engine_profiles, dict):
            self._engine_profiles = {}
        self._field_kind = engine.kind_of(self.engine_var.get())
        # v2.0参数统一: 入口即钳位+合法性校验, 杜绝滑块位置与标签/实际合成值不对齐
        self.rate_var = tk.IntVar(value=cfg_int(cfg.get("rate_v") or 100, 100, 50, 200))
        self.vol_var = tk.IntVar(value=cfg_int(cfg.get("vol_v") or 100, 100, 50, 150))
        self.pitch_var = tk.IntVar(value=cfg_int(cfg.get("pitch_v") or 0, 0, -12, 12))
        self.style_var = tk.StringVar(value=cfg.get("style", "默认") if cfg.get("style", "默认") in engine.STYLES else "默认")
        self.deg_var = tk.StringVar(value=cfg.get("degree", "100%") if cfg.get("degree", "100%") in engine.DEGREES else "100%")
        self.role_var = tk.StringVar(value=cfg.get("role", "默认") if cfg.get("role", "默认") in engine.ROLES else "默认")
        self.filter_var = tk.StringVar(value="全部")
        self.gender_var = tk.StringVar(value="全部")
        self.port_var = tk.StringVar(value=str(cfg.get("port", "8774")))
        self.zoom_var = tk.IntVar(value=cfg_int(cfg.get("zoom", 100), 100, 80, 150))
        self.text_content = ""
        self.voices = {}
        self.selected = cfg.get("person", "")
        self._iid = {}
        self._flowbars = []
        self._resize_after = None
        self._rebuilding = False
        # 9人配音槽位配置(持久化): [{name, voice(display名), on}]
        self.dub_cfg = self._valid_dub_cfg(cfg.get("multidub"))
        self._prog = {"active": False, "mode": None, "total": 0,
                      "done": 0, "seq": 0, "after": None}
        self._tasks = TaskManager(
            schedule_fn=lambda ms, cb: self.root.after(ms, cb),
            cancel_fn=self.root.after_cancel,
        )
        self._tasks.handle = self._handle_bg_event
        self._tasks.render = self._prog_render
        self._tasks.is_active = lambda: self._prog.get("active")
        self._dub = {}  # 重建时重填的配音控件引用
        self._load_engine_voices()
        self._theme_name = theme.apply(cfg.get("theme", theme.DEFAULT_THEME),
                                       globals(), sys.modules.get("product_ui"))
        self._theme_var = tk.StringVar(value=self._theme_name)
        root.title(f"{appmeta.NAME} {appmeta.VERSION}")
        icon = Path(__file__).resolve().parent / 'assets' / 'smartvoice.ico'
        if icon.is_file():
            root.iconbitmap(default=str(icon))
        root.geometry("980x900")
        root.minsize(640, 640)
        root.configure(bg=BG)
        self._build_ui()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        # 窗口缩放/拉伸时重算所有流式按钮栏间距, 防遮挡
        root.bind("<Configure>", self._on_root_resize)

    def on_close(self):
        try:
            if not self.root.winfo_exists():
                return
        except tk.TclError:
            return
        if self._active_task and not messagebox.askokcancel('任务尚未完成', '退出会取消当前任务；已完成片段会保留，下次可复用。是否退出？'):
            return
        if not self._confirm_discard():
            return
        self._preempt()
        self._prog_cancel()
        self._save_cfg()      # 捕获退出前的最后修改（如刚输入的Key/端口）
        pending = self._flush_cfg()
        if pending is not None:
            wait([pending], timeout=10)  # 落盘最多等10秒；超时则后台线程继续写，不钉死退出
        # wait=False：池线程均为 daemon，收尾工作（诊断落盘等）允许 orphan，不阻塞 destroy。
        self._config_store.shutdown(wait=False)
        self._synth_pool.shutdown(wait=False, cancel_futures=True)
        for timer in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            try:
                self.root.after_cancel(timer)
            except tk.TclError:
                pass  # 快照后已触发的定时器无需再取消
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        self.root.destroy()
        # 播放/写盘线程可能短暂占用临时文件：有限重试后忽略残留。
        for attempt in range(3):
            try:
                self._playback_dir.cleanup()
                break
            except OSError:
                if attempt == 2:
                    break
                time.sleep(0.3)

    @staticmethod
    def _valid_dub_cfg(raw):
        """配音槽位合法化: 恒9槽, 名非空, on为bool."""
        out = []
        raw = raw if isinstance(raw, list) else []
        for i in range(MAX_DUB):
            d = raw[i] if i < len(raw) and isinstance(raw[i], dict) else {}
            default_name = "旁白" if i == 0 else f"角色{i + 1}"
            name = str(d.get("name", default_name)).strip() or default_name
            out.append({"name": name[:12], "voice": str(d.get("voice", "")),
                        "on": bool(d.get("on", i == 0))})
        return out

    # ---- 主题与字体 ----
    def _font(self, delta=0):
        return ("微软雅黑", max(8, round((10 + delta) * self.zoom_var.get() / 100)))

    def _font_bold(self, delta=0):
        return ("微软雅黑", max(8, round((10 + delta) * self.zoom_var.get() / 100)), "bold")

    def _menu_font(self):
        """顶栏/右键菜单字号与功能区标题一致，随缩放同步。"""
        return ("微软雅黑", self._font_bold()[1])

    def _title_widget(self, parent, text, color=None):
        """功能区标题：主字下方1px柔和阴影；同格 grid 叠放（place 不参与请求尺寸，会塌缩成1px不可见）。"""
        box = ttk.Frame(parent)
        shadow = ttk.Label(box, text=text, font=self._font_bold(), foreground=GREY)
        top = ttk.Label(box, text=text, font=self._font_bold(), foreground=color or ACCENT)
        shadow.grid(row=0, column=0, sticky="nw", pady=(1, 0))   # 下移1px，露出底部阴影
        top.grid(row=0, column=0, sticky="nw")                    # 后建者在上层盖住阴影主体
        return box, top

    def _refont_menus(self):
        """缩放后同步顶栏与右键菜单字号（菜单不是普通控件，update() 不会处理）。"""
        font = self._menu_font()
        def walk(menu):
            try:
                menu.configure(font=font)
                end = menu.index('end')
                for i in range((end + 1) if end is not None else 0):
                    if menu.type(i) == 'cascade':
                        sub = menu.entrycget(i, 'menu')
                        if sub:
                            walk(menu.nametowidget(sub))
            except tk.TclError:
                pass
        for child in self.root.winfo_children():
            if isinstance(child, tk.Menu):
                walk(child)

    def _theme(self):
        s = ttk.Style()
        try:
            s.theme_use("clam")
        except Exception:
            pass
        self.root.option_add("*Font", self._font())
        s.configure("TFrame", background=BG)
        s.configure("TLabel", background=BG, foreground=FG)
        s.configure("TLabelframe", background=BG, foreground=ACCENT, borderwidth=1, bordercolor=BORDER)
        s.configure("TLabelframe.Label", background=BG, foreground=ACCENT, font=self._font_bold())
        s.configure("TButton", background=PANEL, foreground=FG, borderwidth=1,
                    bordercolor=BORDER, padding=6)
        s.map("TButton", background=[("active", HOVER)], bordercolor=[("active", ACCENT)])
        s.configure("Tool.TButton", background=PANEL, foreground=FG,
                    borderwidth=1, bordercolor=BORDER, padding=6)
        s.map("Tool.TButton", background=[("active", HOVER)],
              bordercolor=[("active", ACCENT)])
        s.configure("TCombobox", fieldbackground=PANEL, background=PANEL,
                    foreground=FG, arrowcolor=ACCENT,
                    lightcolor=BORDER, darkcolor=TROUGH, bordercolor=BORDER)
        s.configure("TEntry", fieldbackground=PANEL, foreground=FG, justify="center")
        s.configure("Vertical.TScrollbar", background=TROUGH, troughcolor=BG,
                    borderwidth=0, arrowcolor=ACCENT)
        s.configure("Status.TLabel", background=BG, foreground=FEEDBACK)
        rh = max(24, round(26 * self.zoom_var.get() / 100))
        s.configure("Treeview", background=PANEL, foreground=FG,
                    fieldbackground=PANEL, font=self._font(), rowheight=rh, borderwidth=0)
        s.configure("Treeview.Heading", background=HEADING, foreground=FG, font=self._font())
        s.map("Treeview", background=[("selected", SEL)], foreground=[("selected", FG)])

    def _load_engine_voices(self):
        kind = engine.kind_of(self.engine_var.get())
        if kind == "edge":
            self.voices = engine.get_edge_voices()
        elif kind == "openai":
            self.voices = engine.get_openai_voices()
        elif kind == "volc":
            self.voices = engine.get_volc_voices()
        else:
            self.voices = engine.get_voices(engine.normalize_region(self.region_var.get()))
        if self.selected not in self.voices:
            self.selected = next(iter(self.voices), "")

    # ---- 缩放自适应: 所有间距按zoom等比, 窗口<Configure>防抖重算 ----
    def _zx(self):
        try:
            return max(0.8, min(1.5, int(self.zoom_var.get()) / 100.0))
        except Exception:
            return 1.0

    def _pads(self):
        z = self._zx()
        px = max(6, round(10 * z))    # 外层横向
        py = max(3, round(6 * z))     # 外层纵向
        ix = max(3, round(4 * z))     # 内层/按钮栏横向
        iy = max(1, round(2 * z))     # 内层/按钮栏纵向
        return px, py, ix, iy

    def _mkbtn(self, parent, text, cmd, width=BTN_W):
        # 所有操作按钮共用一套样式；长按钮使用标准宽度，紧凑操作只收窄宽度。
        return ttk.Button(parent, text=text,
                          width=max(4, round(width * self._zx())), command=cmd,
                          style="TButton")

    def _on_root_resize(self, event):
        # 只响应顶层窗口自身尺寸变化, 子控件变化直接忽略防抖动死循环
        if event.widget is not self.root:
            return
        w = getattr(event, "width", 0)
        last_w = getattr(self, "_last_w", 0)
        # Windows 移动窗口时也可能发送 Configure；宽度未变时完全跳过布局计算。
        if last_w and abs(w - last_w) < 8:
            return
        self._last_w = w
        if hasattr(self, 'f5'):
            try:
                if w > 100:
                    self.f5.configure(width=max(1, w - 2 * self._pads()[0]))
            except tk.TclError:
                pass
        if self._resize_after:
            try:
                self.root.after_cancel(self._resize_after)
            except Exception:
                pass
        self._resize_after = self.root.after(120, self._reflow_all)

    def _reflow_all(self):
        self._resize_after = None
        for bar in list(getattr(self, "_flowbars", [])):
            try:
                if bar.winfo_exists():
                    _, _, ix, iy = self._pads()
                    self._reflow(bar, ix, iy)
            except Exception:
                pass

    # ---- 界面构建：启动/缩放使用；引擎切换只更新已有控件 ----
    # v1.6顺序: 引擎f1 -> 调节f2 -> 文本f4 -> 人声f3(expand) -> 底部f5/f6/status
    def _build_ui(self):
        # 缩放重建前清理旧控件；引擎切换不调用此方法。独立子窗口(编辑/导出/组件)不重建。
        for widget in self.root.winfo_children():
            if not isinstance(widget, tk.Toplevel):
                widget.destroy()
        self._dub = {}
        self._theme()
        # 放大字体时同步保留列表区的最小可用高度。
        self.root.minsize(640, max(700, round(700 * self._zx())))
        self._flowbars = []
        root = self.root
        PX, PY, IX, IY = self._pads()
        
        # 1. 引擎配置区 (Container)
        self.f1 = ttk.LabelFrame(root)
        self.f1.configure(labelwidget=self._title_widget(self.f1, "朗读引擎")[0])
        self._setup_f1(self.f1, IX, IY)
        
        # 2. 调节/风格区
        self.f2 = ttk.LabelFrame(root)
        self.f2.configure(labelwidget=self._title_widget(self.f2, "调节/风格")[0])
        self._setup_f2(self.f2, IX, IY)

        # 3. 底部功能区 (预先占位)
        self.status = ttk.Label(root, text="就绪", style="Status.TLabel")
        self.status.pack(side="bottom", fill="x", padx=PX + 2, pady=IY)
        
        self.f6 = ttk.LabelFrame(root)
        self.f6.configure(labelwidget=self._title_widget(self.f6, "转发服务(供浏览器插件等调用)")[0])
        self.f6.pack(side="bottom", fill="x", padx=PX, pady=PY)
        self._setup_f6(self.f6)

        self.f5 = ttk.Frame(root)
        self._setup_f5(self.f5)
        self.f5.pack_propagate(False)
        self.f5.configure(height=max(30, round(34 * self._zx())))
        self.f5.pack(side="bottom", fill="x", padx=PX, pady=IY)
        self.root.after(100, self._fit_text_toolbar)
        self.root.after(400, self._fit_text_toolbar)

        self.f1.pack(fill="x", padx=PX, pady=(PY, 0))
        self.f2.pack(fill="x", padx=PX, pady=PY)
        self.f4 = ttk.LabelFrame(root, text="")
        self.f4.pack(side="bottom", fill="x", padx=PX, pady=PY)
        self._setup_f4(self.f4, IX, IY)
        self.f3 = ttk.Frame(root)
        self.f3.pack(fill="both", expand=True, padx=PX, pady=(0, 0))
        self.voice_box = ttk.LabelFrame(self.f3)
        self.voice_box.configure(labelwidget=self._title_widget(self.voice_box, "人声(单击选中, 双击试听)")[0])
        self.dub_box = ttk.LabelFrame(self.f3, text="多人配音（最多9人）")
        self.voice_box.grid(row=0, column=0, sticky="nsew", padx=(0, 3))
        self.dub_box.grid(row=0, column=1, sticky="nsew", padx=(3, 0))
        self.f3.columnconfigure(0, weight=1, uniform="mid_panel")
        self.f3.columnconfigure(1, weight=1, uniform="mid_panel")
        self.f3.rowconfigure(0, weight=1)
        self._setup_f3_content(self.voice_box, self.dub_box, IX, IY)
        self.rebuild_list()
        self._build_product_menu()
        self._schedule_editor_info()
        # 配置输入即暂存(_save_cfg 内去抖)；Key 另带未记住提示。变量常驻，trace 只加一次。
        if not getattr(self, "_cfg_traced", False):
            self.key_var.trace_add("write", self._on_key_edited)
            for var in (self.region_var, self.ep_var, self.port_var):
                var.trace_add("write", lambda *a: self._save_cfg())
            self._cfg_traced = True

    def _fit_text_toolbar(self):
        try:
            width = self.root.winfo_width()
            if width > 100:
                self.f5.configure(width=max(1, width - 2 * self._pads()[0]))
        except tk.TclError:
            pass

    def _setup_f1(self, f1, IX, IY):
        build_configbar(self, f1, IX, IY)

    def _setup_f2(self, f2, IX, IY):
        build_stylepanel(self, f2, IX, IY)

    def _setup_f6(self, f6):
        build_serverbar(self, f6)

    def _setup_f5(self, f5):
        build_toolbar(self, f5)

    def _setup_f4(self, f4, IX, IY):
        build_editor(self, f4, IX, IY, AUDITION_TEXT)
    def _setup_f3_content(self, voice_box, dub_box, IX, IY):
        build_voicepanel(self, voice_box, IX, IY)
        build_dubpanel(self, dub_box, IX, IY)

    def _resize_voice_columns(self, event):
        # 按实际视口分配列宽，避免固定 minwidth 总和超过窄窗宽度。
        available = max(3, event.width - 4)
        gender = max(1, round(available * 46 / 326))
        name = max(1, round(available * 120 / 326))
        for column, width in (("gender", gender), ("name", name),
                              ("vid", max(1, available - gender - name))):
            self.tree.column(column, width=width)

    def _block_column_resize(self, event):
        if self.tree.identify_region(event.x, event.y) == 'separator':
            return 'break'

    def _open_editor_double_click(self, event=None):
        self.open_editor()
        return 'break'  # 阻止 Text 类默认双击选词和继续分发。


    # ---- 开关色块 ----
    def _toggle(self, parent, var, label, on_change):
        # 色块在左文字在右, 凸起边框增强立体感; 文字定宽左对齐, 两行开关右缘对齐
        f = ttk.Frame(parent)
        b = tk.Button(f, width=4, relief="raised", bd=2, font=self._font(-1),
                      command=lambda: self._flip(var, b, on_change))
        b.pack(side="left", padx=(0, 3))
        ttk.Label(f, text=label, width=8, anchor="w").pack(side="left")
        self._paint_toggle(b, var.get())
        return f

    def _flip(self, var, btn, on_change):
        var.set(not var.get())
        self._paint_toggle(btn, var.get())
        if on_change:
            on_change()

    @staticmethod
    def _paint_toggle(btn, on):
        btn.config(text="开" if on else "关", relief="raised", bd=2,
                   bg=GREEN if on else GREY, fg="white",
                   activebackground=TOGGLE_ON if on else TOGGLE_IDLE,
                   activeforeground="white",
                   highlightbackground=GREEN if on else GREY)

    def _setup_placeholder(self, placeholder):
        """提示是覆盖标签，不写进原稿和撤销栈。"""
        self._placeholder_label = tk.Label(self.text, text='输入或导入原稿 · 双击打开大窗口编辑',
                                            bg=PANEL, fg=MUTED, font=self._font())
        self._placeholder_label.bind('<Button-1>', lambda e: (self._placeholder_label.place_forget(), self.text.focus_set()))
        self._placeholder_label.bind('<Double-Button-1>', self._open_editor_double_click)
        self.text.bind('<FocusIn>', lambda e: self._placeholder_label.place_forget())
        self.text.bind('<FocusOut>', lambda e: self._schedule_editor_info())
        if self.text_content:
            self.text.insert("1.0", self.text_content)
        else:
            self._placeholder_label.place(x=12, y=8)
        self.text.edit_reset()

    def _get_real_text(self):
        """读取真实文本内容（占位只是覆盖 Label，文本框本身为空，无需标志位）"""
        return self.text.get("1.0", "end-1c")

    def _set_real_text(self, text):
        """外部填入真实文本时退出占位状态"""
        self.text.config(fg=FG)
        self.text.edit_separator()
        self.text.configure(autoseparators=False)
        self.text.delete("1.0", "end")
        self.text.insert("1.0", text)
        self.text.edit_separator()
        self.text.configure(autoseparators=True)
        self._schedule_editor_info()

    def _bind_text_context_menu(self, widget=None, editor=False):
        """文本右键菜单(撤销/剪切/复制/粘贴/删除/全选/试听)；主窗口另带独立编辑入口。"""
        widget = widget or self.text
        old = getattr(widget, "_sv_ctx_menu", None)
        if old is not None:
            try:
                old.destroy()
            except tk.TclError:
                pass
        # 菜单挂在 widget 名下: 控件销毁(独立编辑窗口关闭)时随之一并销毁, 不泄漏
        menu = tk.Menu(widget, tearoff=0, font=self._menu_font(),
                       bg=PANEL, fg=FG, activebackground=SEL, activeforeground=FG)
        widget._sv_ctx_menu = menu

        def undo():
            widget.focus_set()
            widget.edit_undo()
            self._schedule_editor_info()

        def redo():
            widget.focus_set()
            widget.edit_redo()
            self._schedule_editor_info()

        def paste():
            widget.event_generate("<<Paste>>")

        def delete():
            if widget.tag_ranges("sel"):
                widget.delete("sel.first", "sel.last")

        menu.add_command(label="撤销 (Ctrl+Z)", command=undo)
        menu.add_command(label="重做 (Ctrl+Y)", command=redo)
        menu.add_separator()
        menu.add_command(label="剪切 (Ctrl+X)", command=lambda: widget.event_generate("<<Cut>>"))
        menu.add_command(label="复制 (Ctrl+C)", command=lambda: widget.event_generate("<<Copy>>"))
        menu.add_command(label="粘贴 (Ctrl+V)", command=paste)
        menu.add_command(label="删除", command=delete)
        menu.add_command(label="全选 (Ctrl+A)", command=lambda: widget.tag_add("sel", "1.0", "end"))
        menu.add_separator()
        menu.add_command(label="试听选中", command=lambda: self.preview_text(True, widget))
        menu.add_command(label="试听当前段", command=lambda: self.preview_text(False, widget))
        if not editor:
            menu.add_command(label="独立大窗口编辑", command=self.open_editor)

        def _popup(e):
            try:
                menu.tk_popup(e.x_root, e.y_root)
            finally:
                try:
                    menu.grab_release()
                except tk.TclError:
                    pass

        widget.bind("<Button-3>", _popup)

    def _bind_entry_context_menu(self, entry):
        """单行输入框右键菜单(剪切/复制/粘贴/删除/全选)；Key/地址类 Entry 原生无右键，需显式绑定。"""
        old = getattr(entry, "_sv_entry_menu", None)
        if old is not None:
            try:
                old.destroy()
            except tk.TclError:
                pass
        menu = tk.Menu(entry, tearoff=0, font=self._menu_font(),
                       bg=PANEL, fg=FG, activebackground=SEL, activeforeground=FG)
        entry._sv_entry_menu = menu

        def delete():
            try:
                if entry.selection_present():
                    entry.delete("sel.first", "sel.last")
            except tk.TclError:
                pass

        def select_all():
            try:
                entry.focus_set()
                entry.select_range(0, "end")
            except tk.TclError:
                pass

        menu.add_command(label="剪切 (Ctrl+X)", command=lambda: entry.event_generate("<<Cut>>"))
        menu.add_command(label="复制 (Ctrl+C)", command=lambda: entry.event_generate("<<Copy>>"))
        menu.add_command(label="粘贴 (Ctrl+V)", command=lambda: entry.event_generate("<<Paste>>"))
        menu.add_command(label="删除", command=delete)
        menu.add_command(label="全选", command=select_all)

        def _popup(e):
            try:
                menu.tk_popup(e.x_root, e.y_root)
            finally:
                try:
                    menu.grab_release()
                except tk.TclError:
                    pass

        entry.bind("<Button-3>", _popup)

    def _on_key_edited(self, *a):
        """Key 输入即暂存(去抖在 _save_cfg 内)；未勾选记住时给一次性状态提示。"""
        self._save_cfg()
        try:
            if (self.key_var.get().strip() and not self.remember_var.get()
                    and not getattr(self, "_key_hint_shown", False)):
                self._key_hint_shown = True
                self.status.config(text="Key 仅本次有效，打开「记住Key」才会加密保存")
        except tk.TclError:
            pass

    def on_format_dialogue_lines(self):
        """点击『角色分配』按钮：按段落/引号机械分行；叙述绑定第一个启用角色，引号对话保留默认人声"""
        txt = self._get_real_text()
        if not txt:
            return self._fail("文本框内无内容，请先输入或导入小说文本")

        names = self._enabled_role_names()
        formatted = engine.format_dialogue_lines(txt, names)
        if not formatted:
            return self._fail("没有可排版的内容")
        if formatted == txt:
            role_msg = '文本已是规范分行，无需调整'
            self._show_info_in_prog_zone(role_msg)
            self.status.config(text=role_msg)
            return

        self._set_real_text(formatted)
        if names:
            role_msg = f'已分行（Ctrl+Z撤销）· 叙述→{names[0]} · 引号对话用默认人声，可用[角色名]指定'
        else:
            role_msg = '已分行（Ctrl+Z撤销）· 全部用默认人声，可用[角色名]指定角色'

        self._show_info_in_prog_zone(role_msg)
        self.status.config(text=role_msg)

    def _show_info_in_prog_zone(self, msg, duration=3500):
        """在进度条位置短暂展示动态操作描述，使用动态描述色(FEEDBACK)，与合成/播放进度互不干扰"""
        if self._prog.get("active"):
            return
        try:
            if getattr(self, '_info_after', None) is not None:
                self.root.after_cancel(self._info_after)  # 连发提示不许提前清掉后一条
                self._info_after = None
            self.prog.pack_forget()
            self.prog_lab.config(text=msg, foreground=FEEDBACK)
            self.prog_lab.pack(side="left")
            self._info_after = self.root.after(duration, self._clear_info_in_prog_zone)
        except tk.TclError:
            pass

    def _clear_info_in_prog_zone(self):
        self._info_after = None
        if self._prog.get("active"):
            return
        try:
            self.prog_lab.config(text="")
            self.prog_lab.pack_forget()
        except tk.TclError:
            pass

    def _apply_show(self):
        try:
            self.key_entry.config(show="" if self.show_var.get() else "*")
        except tk.TclError:
            pass

    def _slider(self, parent, row, c0, name, var, lo, hi, lab, default):
        ttk.Label(parent, text=name + ":").grid(row=row, column=c0, padx=4, pady=0)
        # 语速50～200%保留原范围，物理轨道分两段映射，100%严格对应正中点。
        control_var = tk.DoubleVar(value=self._rate_position(var.get())) if name == '语速' else var
        def changed(value):
            if name == '语速':
                var.set(self._rate_value(float(value)))
            self._show_rvp()
        slider = tk.Scale(parent, from_=0 if name == '语速' else lo, to=100 if name == '语速' else hi,
                  orient="horizontal", variable=control_var,
                 showvalue=False, bg=BG, fg=FG, troughcolor=TROUGH,
                 activebackground=ACCENT, highlightthickness=0, bd=0,
                 font=self._font(-1),
                 command=changed)
        if name == '语速':
            if getattr(self, '_rate_trace', None):
                var.trace_remove('write', self._rate_trace)
            def sync(*args):
                position = self._rate_position(var.get())
                if abs(control_var.get()-position) > .01:
                    control_var.set(position)
            self._rate_trace = var.trace_add('write', sync)
        slider.grid(row=row, column=c0 + 1, padx=4, pady=0, sticky="ew")
        if not hasattr(self, '_sliders'):
            self._sliders = {}
        self._sliders[name] = slider
        parent.columnconfigure(c0 + 1, weight=1)
        # 固定标签宽度以保证三个数值列和复位按钮完美对齐
        lab.config(width=5)
        lab.grid(row=row, column=c0 + 2, padx=5, pady=0, sticky="e")
        ttk.Button(parent, text="↺", width=3, style="Tool.TButton",
                   command=lambda: self._reset_slider(var, default)).grid(
                         row=row, column=c0 + 3, padx=(0, 4), pady=0, sticky="w")

    @staticmethod
    def _rate_position(value):
        value = min(200, max(50, float(value)))
        return value - 50 if value <= 100 else 50 + (value - 100) / 2

    @staticmethod
    def _rate_value(position):
        return round(50 + position if position <= 50 else 100 + (position-50)*2)

    def _reset_slider(self, var, default):
        var.set(default)
        self._show_rvp()
        self._save_cfg()

    def _rvp(self):
        # 单一真相源: 三滑块钳位后统一派生, 合成与标签永不分叉
        rate = min(200, max(50, int(self.rate_var.get())))
        vol = min(150, max(50, int(self.vol_var.get())))
        pitch = min(12, max(-12, int(self.pitch_var.get())))
        return (f"{rate}%", f"{vol - 100:+d}%", f"{pitch:+d}Hz")

    def _show_rvp(self):
        try:
            rate, vol, pitch = self._rvp()
            self.rate_lab.config(text=rate)
            self.vol_lab.config(text=vol)
            self.pitch_lab.config(text=pitch)
        except Exception:
            pass

    def _refresh_style_state(self):
        try:
            azure = engine.kind_of(self.engine_var.get()) == "azure"
            caps = engine.voice_capabilities(self.voices.get(self.selected, ''), self.region_var.get()) if azure else {}
            styles, roles = ['默认'] + caps.get('styles', []), ['默认'] + caps.get('roles', [])
            self.style_combo.config(values=styles, state='readonly' if azure else 'disabled')
            self.role_combo.config(values=roles, state='readonly' if azure else 'disabled')
            self.deg_combo.config(state='readonly' if azure and self.style_var.get() != '默认' else 'disabled')
            for label, field in (('语速', 'rate'), ('音量', 'volume'), ('音调', 'pitch')):
                supported = caps.get(field, False) if azure else (engine.kind_of(self.engine_var.get()) != 'openai' or field == 'rate')
                self._sliders[label].config(state='normal' if supported else 'disabled')
            self._refresh_engine_fields()
        except (tk.TclError, AttributeError):
            pass

    def _refresh_engine_fields(self):
        try:
            kind = engine.kind_of(self.engine_var.get())
            if kind == "edge":
                self.lab_key.config(text="Key:")
                self.lab_region.config(text="Region:")
                self.lab_ep.config(text="终结点:")
                self.key_entry.config(state="disabled")
                self.region_entry.config(state="disabled")
                self.ep_entry.config(state="disabled")
            elif kind == "openai":
                self.lab_key.config(text="Key:")
                self.lab_region.config(text="模型:")
                self.lab_ep.config(text="地址:")
                self.key_entry.config(state="normal")
                self.region_entry.config(state="normal")
                self.ep_entry.config(state="normal")
                if not (self.ep_var.get() or "").strip() or "tts.speech.microsoft" in self.ep_var.get():
                    self.ep_var.set(engine.DEFAULT_OPENAI_SPEECH)
                if (self.region_var.get() or "") in ("", engine.DEFAULT_REGION, "eastasia", "eastus"):
                    self.region_var.set(engine.OPENAI_MODEL_DEFAULT)
            elif kind == "volc":
                self.lab_key.config(text="Token:")
                self.lab_region.config(text="AppID:")
                self.lab_ep.config(text="地址:")
                self.key_entry.config(state="normal")
                self.region_entry.config(state="normal")
                self.ep_entry.config(state="normal")
                if not (self.ep_var.get() or "").strip() or "tts.speech.microsoft" in self.ep_var.get():
                    self.ep_var.set(engine.DEFAULT_VOLC_TTS)
            else:
                self.lab_key.config(text="Key:")
                self.lab_region.config(text="Region:")
                self.lab_ep.config(text="终结点:")
                self.key_entry.config(state="normal")
                self.region_entry.config(state="normal")
                self.ep_entry.config(state="normal")
                if "openai.com" in (self.ep_var.get() or "") or "openspeech.bytedance" in (self.ep_var.get() or ""):
                    self.ep_var.set(engine.default_tts_endpoint(engine.DEFAULT_REGION))
                    self.region_var.set(engine.DEFAULT_REGION)
        except (tk.TclError, AttributeError):
            pass

    def _on_style_change(self):
        self._refresh_style_state()
        self._save_cfg()

    def on_zoom(self):
        # 原地更新字体，保留原稿、撤销栈和独立编辑窗口。
        self._theme()
        self.root.minsize(640, max(700, round(700*self._zx())))
        def update(widget):
            try:
                if 'font' in widget.keys():
                    widget.configure(font=self._font_bold() if 'bold' in str(widget.cget('font')) else self._font())
                for child in widget.winfo_children():
                    update(child)
            except tk.TclError:
                pass
        for child in self.root.winfo_children():
            if not isinstance(child, tk.Menu):
                update(child)
        self._refont_menus()
        self._save_cfg()
        try:
            # 几何尺寸构建时按 zoom 求值，缩放后同步跟上（字体已在 update 中处理）。
            self.prog.configure(width=max(120, round(150 * self._zx())),
                                height=max(9, round(11 * self._zx())))
            self.text.configure(padx=max(8, round(10 * self._zx())),
                                pady=max(4, round(6 * self._zx())))
        except tk.TclError:
            pass
        self._reflow_all()

    def switch_theme(self, name):
        """切换主题：同步调色板到业务模块、关闭已开子窗口、重建界面并持久化。"""
        if self._active_task:
            return messagebox.showinfo('任务运行中', '请先取消或等待任务完成再切换主题')
        key = theme.apply(name, globals(), sys.modules.get("product_ui"))
        self._theme_name = key
        self._theme_var.set(key)
        try:
            self.text_content = self.text.get('1.0', 'end-1c')
        except (tk.TclError, AttributeError):
            pass
        for attr in ("_editor_window", "_export_win", "_component_win"):
            win = getattr(self, attr, None)
            try:
                if win is not None and win.winfo_exists():
                    win.destroy()
            except tk.TclError:
                pass
            setattr(self, attr, None)
        self._build_ui()
        self._save_cfg()
        self.status.config(text=f"已切换主题：{theme.THEME_LABELS[key]}")

    def on_engine_switch(self):
        if self._active_task:
            self.engine_var.set(self._retry_plan[1]['engine'] if self._retry_plan else self.engine_var.get())
            return messagebox.showinfo('任务运行中', '请先取消或等待任务完成再切换引擎')
        # 切引擎仅更新凭证区与人声列表，不重建GUI，消除按钮闪烁
        self._preempt()
        self._prog_cancel()
        
        # 状态持久化
        self._engine_profiles[self._field_kind] = {
            "key": self.key_var.get(), "region": self.region_var.get(),
            "endpoint": self.ep_var.get()}
        self._field_kind = engine.kind_of(self.engine_var.get())
        
        # 加载配置
        defaults = {
            "azure": {"region": engine.DEFAULT_REGION, "endpoint": engine.default_tts_endpoint(engine.DEFAULT_REGION)},
            "openai": {"region": engine.OPENAI_MODEL_DEFAULT, "endpoint": engine.DEFAULT_OPENAI_SPEECH},
            "volc": {"region": "", "endpoint": engine.DEFAULT_VOLC_TTS},
            "edge": {"region": "", "endpoint": ""},
        }
        profile = self._engine_profiles.get(self._field_kind, defaults[self._field_kind])
        self.key_var.set(profile.get("key", ""))
        self.region_var.set(profile.get("region", ""))
        self.ep_var.set(profile.get("endpoint", ""))
        
        # 仅刷新数据与输入区状态
        self._load_engine_voices()
        self._refresh_engine_fields()
        self._refresh_style_state()
        self.rebuild_list()
        self._refresh_dub_voices()
        self._save_cfg()
        self.status.config(text=f"已切到{self.engine_var.get()}, 共{len(self.voices)}个人声")

    # ---- 主线程快照 + 工作线程只跑网络, 回主线程更新界面 ----
    def snapshot(self):
        eng = self.engine_var.get()
        voice = self.voices.get(self.selected, "")
        if not voice:
            raise RuntimeError("请先在列表点选一个人声")
        rate, vol, pitch = self._rvp()
        snap = {"engine": eng, "voice": voice, "person": self.selected,
                "rate": rate, "vol": vol, "pitch": pitch,
                "style": self.style_var.get(), "degree": self.deg_var.get(),
                "role": self.role_var.get()}
        kind = engine.kind_of(eng)
        if kind == "azure":
            key = self.key_var.get().strip()
            if not key:
                raise RuntimeError("Azure引擎请先填Key")
            rg = engine.normalize_region(self.region_var.get())
            self.region_var.set(rg)
            snap["region"] = rg
            snap["key"] = key
            snap["ep"] = engine.fix_endpoint(self.ep_var.get(), rg)
            self.ep_var.set(snap["ep"])
        elif kind == "openai":
            key = self.key_var.get().strip()
            if not key:
                raise RuntimeError("OpenAI兼容引擎请先填Key")
            snap["key"] = key
            snap["ep"] = self.ep_var.get().strip() or engine.DEFAULT_OPENAI_SPEECH
            snap["region"] = self.region_var.get().strip() or engine.OPENAI_MODEL_DEFAULT
        elif kind == "volc":
            key = self.key_var.get().strip()
            if not key:
                raise RuntimeError("火山引擎请先填Token")
            snap["key"] = key
            snap["region"] = self.region_var.get().strip()
            snap["ep"] = self.ep_var.get().strip() or engine.DEFAULT_VOLC_TTS
        self._save_cfg()
        return snap

    # 配置三件套代理到 ConfigStore：测试与旧调用方读写不变，状态只存一份。
    @property
    def _cfg_pool(self):
        return self._config_store.pool

    @property
    def _cfg_pending(self):
        return self._config_store.pending

    @_cfg_pending.setter
    def _cfg_pending(self, value):
        self._config_store.pending = value

    @property
    def _cfg_after(self):
        return self._config_store._after

    @_cfg_after.setter
    def _cfg_after(self, value):
        self._config_store._after = value

    def _save_cfg(self):
        try:
            try:
                rg = (engine.normalize_region(self.region_var.get())
                      if engine.kind_of(self.engine_var.get()) == "azure"
                      else self.region_var.get().strip())
                if self.region_var.get() != rg:
                    self.region_var.set(rg)
            except Exception:
                rg = self.region_var.get().strip()
            self._engine_profiles[self._field_kind] = {
                "key": self.key_var.get(), "region": rg, "endpoint": self.ep_var.get().strip()}
            profiles = {k: dict(v, key=v.get("key", "") if self.remember_var.get() else "")
                        for k, v in self._engine_profiles.items()}
            pending = {
                "engine_profiles": profiles,
                "engine": self.engine_var.get(), "region": rg,
                "endpoint": self.ep_var.get().strip(), "person": self.selected,
                "rate_v": cfg_int(self.rate_var.get(), 100, 50, 200),
                "vol_v": cfg_int(self.vol_var.get(), 100, 50, 150),
                "pitch_v": cfg_int(self.pitch_var.get(), 0, -12, 12), "style": self.style_var.get(),
                "degree": self.deg_var.get(), "role": self.role_var.get(),
                "port": self.port_var.get().strip(), "zoom": cfg_int(self.zoom_var.get(), 100, 80, 150),
                "remember_key": bool(self.remember_var.get()),
                "theme": self._theme_name,
                "key": self.key_var.get() if self.remember_var.get() else "",
                "export": self._export_options_safe(),
                "multidub": [dict(s) for s in self._dub_cfg_snapshot()]}
        except (ValueError, tk.TclError):
            # 控件状态暂时不可读(如销毁中)：跳过本次暂存，不影响后续保存。
            return
        # 到点回调走 _flush_cfg（计数+唤醒），不能直调 store.flush，否则轮询提前退出。
        self._config_store.schedule(pending, self._flush_cfg)

    def _flush_cfg(self):
        if self._config_store.pending is None:
            return None
        self._bg_n += 1
        future = self._config_store.flush()
        self._bg_kick()
        return future

    def _dub_cfg_snapshot(self):
        # 配音面板持久化快照(面板未建时回退内存值)
        try:
            self._sync_dub_cfg()
        except Exception:
            pass
        return self.dub_cfg

    def synth_net(self, text, snap, voice_id=None, on_progress=None, on_stage=None):
        with workflow.network_context(snap.get('_cancel') or workflow.Cancellation(), snap.get('_diag'), snap.get('_index', 0)):
            return self._synth_net(text, snap, voice_id, on_progress, on_stage)

    def _synth_net(self, text, snap, voice_id=None, on_progress=None, on_stage=None):
        vid = voice_id or snap["voice"]
        kind = engine.kind_of(snap["engine"])
        if kind == "azure":
            return engine.synth_azure(text, vid, snap["key"], snap["ep"],
                                      snap["rate"], snap["pitch"], snap["vol"],
                                      snap["style"], snap["degree"], snap["role"],
                                      region=snap.get("region", ""),
                                      on_progress=on_progress, on_stage=on_stage,
                                      annotate=not snap.get("audition", False))
        if kind == "openai":
            return engine.synth_openai(text, vid, snap["key"], snap.get("ep", ""),
                                       snap["rate"], model=snap.get("region", engine.OPENAI_MODEL_DEFAULT),
                                       on_progress=on_progress, on_stage=on_stage)
        if kind == "volc":
            return engine.synth_volc(text, vid, snap["key"], snap.get("region", ""),
                                     snap.get("ep", ""), snap["rate"],
                                      snap["pitch"], snap["vol"],
                                      on_progress=on_progress, on_stage=on_stage)
        return engine.synth_edge(text, vid, snap["rate"], snap["pitch"], snap["vol"],
                                 on_progress=on_progress, on_stage=on_stage)

    def _save_audio(self, data, path):
        path = Path(path)
        return storage.unique_export(path.parent, path.stem, path.suffix.lstrip('.'), data)

    # ---- 绿色任务进度：每段接收/完成 + 音频整理 + 落盘，不用时间猜测云端进度 ----
    def _prog_hide(self):
        try:
            self.prog.pack_forget()
            self.prog_lab.pack_forget()
        except tk.TclError:
            pass

    def _prog_draw(self, value=None):
        ui = getattr(self, "_prog_ui", None)
        if not ui:
            return
        try:
            if not self.prog.winfo_exists():
                return
            w = self.prog.winfo_width()
            h = self.prog.winfo_height()
            if w <= 1:
                w = int(self.prog.cget("width"))
            if h <= 1:
                h = int(self.prog.cget("height"))
            if not self.prog.find_withtag("track"):
                self._last_fill_r = None
                self.prog.create_rectangle(0, 0, 0, 0, outline=BORDER, fill=TRACK, tags="track")
                self.prog.create_rectangle(0, 0, 0, 0, outline="", fill=GREEN, tags="fill")
            mx = max(1, ui.get("maximum", 100))
            v = ui["value"] if value is None else value
            ratio = min(1.0, max(0.0, float(v) / float(mx)))
            fill_r = (w - 1) if ratio >= 0.999 else int(1 + (w - 2) * ratio) if ratio > 0 else 0
            geometry = (self.prog, fill_r, w, h)
            if getattr(self, '_last_fill_r', None) == geometry:
                return
            self.prog.coords("track", 0, 0, w - 1, h - 1)
            if ratio > 0:
                # 满格时严格填满到 w - 1，无任何右侧缝隙与空白
                self.prog.coords("fill", 1, 1, fill_r, h - 1)
                self.prog.itemconfigure("fill", state="normal")
            else:
                self.prog.itemconfigure("fill", state="hidden")
            self._last_fill_r = geometry
        except tk.TclError:
            pass

    def _prog_cancel(self):
        self._prog["active"] = False
        try:
            after = self._prog.get("after")
            if after:
                self.root.after_cancel(after)
        except Exception:
            pass
        self._prog["after"] = None
        try:
            self._prog_ui.update({"mode": "determinate", "value": 0})
            self.prog_lab.config(text="")
            self._prog_hide()
        except tk.TclError:
            pass

    def _prog_begin(self, mode, total, seq, label):
        self._prog_cancel()
        total = max(1, total)
        self._prog.update({"active": True, "mode": mode, "total": total,
                           "done": 0, "seq": seq, "stage": label,
                           "index": 1, "received": 0, "bytes_total": 0,
                           "started": time.monotonic(), "finished": False,
                           "segments": {}, "completed": set(), "post_done": 0,
                           "highwater": 0.0})
        try:
            self.prog.pack(side="left", padx=(0, 6))
            self.prog_lab.pack(side="left")
            self._prog_ui.update(mode="determinate", value=0, maximum=total + 2)
        except tk.TclError:
            pass
        self._prog_poll()

    def _progress_callbacks(self, seq, index=1, token=None):
        """工作线程只发布消息，限流字节更新以避免快速流撑满 GUI 队列。"""
        last = [0.0]
        diagnostics = self._diag
        token = token if token is not None else self._task_token

        def cancelled():
            # 序号作废或任务被点停止时，中止在途请求（不 bump 序号，已完成段照常输出）。
            if seq != self._seq or token.cancelled:
                raise engine.SynthesisCancelled("合成已取消")

        def progress(received, total):
            cancelled()
            now = time.monotonic()
            if received == 0 or (total > 0 and received >= total) or now - last[0] >= 0.05:
                last[0] = now
                self._bgq.put(("progress", seq, (received, total, index)))

        def stage(label):
            cancelled()
            last[0] = 0.0
            if diagnostics:
                now = time.monotonic()
                diagnostics.event(label, index, duration_ms=round((now - stage.started)*1000, 1))
                stage.started = now
            self._bgq.put(("phase", seq, (label, index)))
        stage.started = time.monotonic()
        return progress, stage

    def _prog_render(self):
        p = self._prog
        if p.get("finished"):
            return
        multi = p["mode"] == "multi"
        received, total_bytes = p["received"], p["bytes_total"]
        value = p["done"] + sum(s.get("fraction", 0) for i, s in p["segments"].items()
                                if i not in p["completed"]) + p["post_done"]
        p["highwater"] = max(p["highwater"], value)
        self._prog_ui.update(mode="determinate", value=p["highwater"], maximum=p["total"] + 2)
        text = p["stage"]
        if received or total_bytes:
            if total_bytes > 0:
                text += f" {min(100, received * 100 // total_bytes)}%"
            text += f" · {received / 1024:.1f} KB"
        if multi:
            text = f"已完成 {p['done']}/{p['total']}段 · " + text
        elif not total_bytes and p["index"] > 0 and not p["done"]:
            text += " · 等待云端进度"
        text += f" · {int(time.monotonic() - p['started'])}秒"
        self.prog_lab.config(text=text)
        self._prog_draw()

    def _prog_poll(self):
        self._prog["after"] = None
        if not self._prog.get("active") or self._prog["seq"] != self._seq:
            return
        try:
            if not self.prog.winfo_exists():
                return
            self._prog_render()
            self._prog["after"] = self.root.after(250, self._prog_poll)
        except tk.TclError:
            pass

    def _prepare_playback(self, seq, data, out, label, cache_key=None, prepared=None):
        """后台统一拼接/首尾缓冲/解码，MP3 导出与 PCM 播放保持同一内容。"""
        if seq != self._seq:
            return None
        # 所有网络段完成，绿色填充保留；整理/保存各占一个真实完成步骤。
        self._bgq.put(("synth_complete", seq, None))
        options = dict(getattr(self, '_task_export', {'directory': OUT_DIR, 'name': '配音', 'format': 'mp3',
                                                     'leading_ms': 350, 'trailing_ms': 450, 'normalize': False}))
        if prepared is None:
            import docutils
            self._bgq.put(("phase", seq, ("整理音频与首尾缓冲", 0)))
            started = time.monotonic()
            prepared = docutils.prepare_playback_audio(data, options['leading_ms'], options['trailing_ms'],
                                                       normalize=options['normalize'])
            if self._diag:
                self._diag.event('audio_processing', duration_ms=round((time.monotonic()-started)*1000, 1))
        if seq != self._seq:
            return None
        mp3, pcm = prepared
        self._bgq.put(("post_complete", seq, 1))
        if cache_key is not None and len(mp3) + len(pcm) <= 12 * 1024 * 1024:
            with self._audition_cache_lock:
                self._audition_cache[cache_key] = prepared
                self._audition_cache.move_to_end(cache_key)
                while (len(self._audition_cache) > 16 or
                       sum(len(a) + len(b) for a, b in self._audition_cache.values()) > 12 * 1024 * 1024):
                    self._audition_cache.popitem(last=False)
        self._bgq.put(("phase", seq, ("正在保存音频", 0)))
        # 不复用播放文件名，旧任务无法覆盖当前正在播放的文件。
        pcm_path = os.path.join(self._playback_dir.name, f"{seq}-{time.time_ns()}.wav")
        with open(pcm_path, "wb") as f:
            f.write(pcm)
            f.flush()
        try:
            if seq != self._seq:
                self._discard_pcm(pcm_path)
                return None
            preview = getattr(self, '_task_preview', False)
            directory = str(storage.CACHE_DIR / 'auditions') if preview else options['directory']
            name = os.path.splitext(os.path.basename(out))[0] if preview else options['name']
            if preview:
                # 试听文件按名覆盖：同名旧文件先删，避免 cache\auditions 只增不减。
                pattern = re.compile(re.escape(name) + r'(?:-\d+)?\.' + re.escape(options['format']))
                from glob import escape
                for old in Path(directory).glob(escape(name) + '*'):
                    if old.is_file() and pattern.fullmatch(old.name):
                        try:
                            old.unlink()
                        except OSError:
                            pass
            out = os.path.join(directory, name + '.' + options['format'])
            started = time.monotonic()
            path = self._save_audio(mp3 if options['format'] == 'mp3' else pcm, out)
            if self._diag:
                self._diag.event('save_audio', duration_ms=round((time.monotonic()-started)*1000, 1))
        except Exception:
            self._discard_pcm(pcm_path)
            raise
        def done():
            partial = self._stopping  # 停止过：输出的是已完成的部分片段
            if not preview:
                self._last_export = path
                self._export_history.append({'path': path, 'created': time.time(), 'format': options['format']})
            if self._last_pcm and self._last_pcm != pcm_path:
                self._discard_pcm(self._last_pcm)
            self._last_pcm = pcm_path
            self._complete_task('completed')
            if preview:
                self._play_with_flash(seq, path, label, pcm_path)
            else:
                self._mark_audio_ready()
                head = '已停止 · 已输出完成片段：' if partial else '合成完成：'
                self.status.config(text=f'{head}{path} · 点击“播放/停止”试听')
        done.discard = lambda: self._discard_pcm(pcm_path)
        return done

    def _discard_pcm(self, path):
        # 播放中的文件先停再删：Windows 下 MCI 句柄未关会删除失败。
        if getattr(self, '_playing_pcm', None) == path:
            self._playing_pcm = None
            try:
                if self._play_watch is not None:
                    self.root.after_cancel(self._play_watch)
                    self._play_watch = None
            except tk.TclError:
                pass
            self.player.stop()
        try:
            os.remove(path)
        except OSError:
            pass

    def _play_with_flash(self, seq, path, label, pcm_path):
        if seq != self._seq:
            self._discard_pcm(pcm_path)
            return
        # 文件保存完成后才结束任务进度，播放状态不冒充播放进度。
        try:
            after = self._prog.get("after")
            if after:
                self.root.after_cancel(after)
                self._prog["after"] = None
        except Exception:
            pass
        try:
            self._prog_ui["mode"] = "determinate"
            self._prog["finished"] = True
            self._prog_ui["value"] = 100
            self._prog_ui["maximum"] = 100
            self._prog_draw(100)
            self.prog_lab.config(text="合成完成 · 正在播放")
        except Exception:
            pass
        try:
            self._playing_pcm = pcm_path
            self.player.play_file(pcm_path)
        except Exception as e:
            # 文件已删: 同步清掉指向它的引用, 避免"播放/停止"再点到不存在的文件
            if getattr(self, '_last_pcm', None) == pcm_path:
                self._last_pcm = None
            self._discard_pcm(pcm_path)
            raise RuntimeError(f"文件已存好但内置播放失败: {e}")
        self.status.config(text=f"正在播放 [{label}]: {path}")
        self._watch_play(seq)

    def _mark_audio_ready(self):
        timer = self._prog.get('after')
        if timer:
            self.root.after_cancel(timer)
        self._prog.update(after=None, finished=True)
        self._prog_ui.update(mode='determinate', value=100, maximum=100)
        self._prog_draw(100)
        self.prog_lab.config(text='合成完成 · 待播放')
        if self._last_pcm:
            try:
                import wave
                with wave.open(self._last_pcm, 'rb') as wav:
                    seconds = round(wav.getnframes()/wav.getframerate())
                self.play_time.config(text=f'00:00 / {seconds//60:02d}:{seconds%60:02d}')
            except (OSError, wave.Error):
                self.play_time.config(text='已生成 / 待播放')

    def toggle_playback(self):
        if self.player.opened:
            self._stop_playback()
            self.status.config(text='播放已停止，音频设备已释放')
        elif self._last_pcm:
            self.replay()
        else:
            messagebox.showinfo('播放', '请先合成音频')

    def _watch_play(self, seq):
        self._play_watch = None
        if seq != self._seq:
            return
        try:
            self.play_time.config(text=f'{format_clock(self.player.position_ms())} / {format_clock(self.player.length_ms())}')
            if self.player.is_playing():
                self._play_watch = self.root.after(200, lambda: self._watch_play(seq))
                return
        except Exception as e:
            self._stop_playback()
            return self._fail(str(e))
        self._stop_playback()
        self._prog_cancel()
        try:
            self.status.config(text="就绪")
        except tk.TclError:
            pass

    # 后台泵四件套代理到 TaskManager：测试与旧调用方读写不变，状态只存一份。
    @property
    def _bgq(self):
        return self._tasks.queue

    @property
    def _bg_n(self):
        return self._tasks.inflight

    @_bg_n.setter
    def _bg_n(self, value):
        self._tasks.inflight = value

    @property
    def _bg_polling(self):
        return self._tasks._polling

    @_bg_polling.setter
    def _bg_polling(self, value):
        self._tasks._polling = value

    @property
    def _seq(self):
        return self._tasks.seq

    @_seq.setter
    def _seq(self, value):
        self._tasks.seq = value

    def _bg(self, work):
        # v2.0线程安全: 工作线程只往队列放结果, 主线程轮询回放, 永不跨线程碰Tk
        # (旧版root.after跨线程在任务瞬间完成时会"main thread is not in main loop"崩溃)
        self.busy = True
        seq = self._tasks.seq
        self._tasks.submit(lambda: self._bg_run(work, seq))

    def _bg_kick(self):
        self._tasks.kick()

    def _bg_poll(self):
        self._tasks.poll()

    def _handle_bg_event(self, kind, seq, payload):
        if kind == 'cache_segment':
            # 局部试听/预览的段不进项目缓存清单, 否则污染项目保存
            if (seq == self._seq and not getattr(self, '_task_preview', False)
                    and payload not in self._segment_keys):
                self._segment_keys.append(payload)
            return False
        if kind == "config_done":
            self._bg_n = max(0, self._bg_n - 1)
            if payload:
                msg = f"配置保存失败: {payload}"
                if self._active_task:
                    # 任务运行中不覆盖任务状态, 追加提示
                    msg = f"{self.status.cget('text')} · {msg}"
                self.status.config(text=msg)
            return False
        if kind in ("segment", "progress", "phase", "synth_complete", "post_complete"):
            if seq == self._seq and self._prog.get("active") and seq == self._prog["seq"]:
                if kind == "segment":
                    self._prog["completed"].add(payload)
                    self._prog["done"] = len(self._prog["completed"])
                elif kind == "synth_complete":
                    self._prog["completed"] = set(range(1, self._prog["total"] + 1))
                    self._prog["done"] = self._prog["total"]
                elif kind == "post_complete":
                    self._prog["post_done"] = payload
                elif kind == "phase":
                    label, index = payload
                    self._prog.update(stage=label, index=index, received=0, bytes_total=0)
                else:
                    cur, total = payload[:2]
                    index = payload[2] if len(payload) > 2 else self._prog["index"]
                    segment = self._prog["segments"].setdefault(index, {})
                    if total > 0:
                        segment["fraction"] = max(segment.get("fraction", 0), min(.99, cur / total))
                    self._prog.update(received=cur, bytes_total=total, index=index)
                    self._prog["stage"] = f"接收第{index}段" if self._prog["mode"] == "multi" else "接收音频"
                return True
            return False
        self._bg_n = max(0, self._bg_n - 1)
        if seq != self._seq:
            if kind == "done":
                if hasattr(payload, "discard"):
                    payload.discard()
                if not self._active_task:
                    self.status.config(text="旧任务结果已因新操作丢弃")
            return False
        if kind == "done":
            self._finish_done(payload)
        else:
            self._finish_fail(payload)
        return False

    def _bg_run(self, work, seq):
        try:
            done = work()
        except Exception as e:
            if seq == self._seq and self._diag and self._active_task and not isinstance(e, workflow.Cancelled):
                self._diag.event('failed', error_type=type(e).__name__)
            # 取消异常带类型入队，收尾时不弹失败框。
            self._bgq.put(("fail", seq, e if isinstance(e, workflow.Cancelled) else str(e)[:400]))
            return
        self._bgq.put(("done", seq, done))

    def _finish_done(self, done):
        self.busy = False
        try:
            if callable(done):
                done()
        except Exception as e:
            if self._active_task:
                self._task_token.cancel()
                self._complete_task('failed')
            self._fail(str(e)[:400])

    def _finish_fail(self, err):
        self.busy = False
        if self._active_task:
            self._task_token.cancel()
            self._complete_task('cancelled' if isinstance(err, workflow.Cancelled) else 'failed')
        if isinstance(err, workflow.Cancelled):
            self._prog_cancel()
            self.status.config(text="已取消")
            return
        self._fail(str(err))

    def _fail(self, msg):
        # 活动任务的进度条不因校验错误/弹窗被撤销; 仅无任务时才清进度。
        if not self._active_task:
            self._prog_cancel()
        try:
            messagebox.showerror("失败", msg)
            self.status.config(text=f"失败: {msg[:100]}")
        except tk.TclError:
            pass

    # ---- 按钮(主线程): v1.6试听互斥, 先stop再快照 ----
    def _preempt(self):
        # 切换即停: 停掉正在播的 + 作废上一任务序号(后台回来的旧结果直接丢弃)
        # 仅显式新任务、停止、缩放和切引擎中断播放，浏览列表不中断。
        self._stop_playback()
        if self._active_task:
            self._complete_task('cancelled')
        self._task_token.cancel()
        self._task_token = workflow.Cancellation()
        self._audition_pending_key = None
        self._tasks.preempt()
        self.busy = False
        return self._seq

    def _stop_playback(self):
        if self._play_watch:
            try:
                self.root.after_cancel(self._play_watch)
            except tk.TclError:
                pass
            finally:
                self._play_watch = None
        self.player.stop()
        if self._playing_pcm and self._playing_pcm != self._last_pcm:
            self._discard_pcm(self._playing_pcm)
        self._playing_pcm = None

    def _begin_task(self, jobs, snap, text, preview=False):
        if self._active_task:
            raise RuntimeError('已有任务正在运行，请等待完成或先取消')
        for voice, label, body in jobs:
            try:
                engine.validate_voice_parameters(voice, snap)
            except ValueError as e:
                raise ValueError(f'角色 [{label}]：{e}') from e
        self._task_export = self._export_options()
        self._task_preview = preview
        try:
            import components
            if components.available('g2pw'):
                # 注音可用性参与指纹：装上组件后旧（未注音）缓存不得继续命中；
                # 无组件时不加键，保持与历史缓存一致，不做全量失效。
                snap['annotate'] = True
        except Exception:
            pass
        if not preview:
            for voice, _, body in jobs:
                key = workflow.fingerprint(body, voice, snap)
                if key not in self._segment_keys:
                    self._segment_keys.append(key)
        self._diag = workflow.Diagnostics(snap, text, len(jobs))
        self._diag.data['voice_ids'] = sorted({job[0] for job in jobs})
        self._active_task = True
        self._stopping = False
        self.b_play.state(['disabled'])
        snap['_cancel'], snap['_diag'] = self._task_token, self._diag
        self._retry_plan = (list(jobs), dict(snap), text, preview)

    def _complete_task(self, outcome):
        self._active_task = False
        self._stopping = False
        self.b_play.state(['!disabled'])
        if self._diag:
            self._cfg_pool.submit(self._diag.finish, outcome)

    def _cancelled_done(self):
        """取消后没有任何已完成片段：直接收尾，不弹失败框。"""
        def done():
            self._prog_cancel()
            self._complete_task('cancelled')
            self.status.config(text='已取消：没有可输出的已完成片段')
        return done

    def preview_text(self, selection, widget=None):
        if self._active_task:
            return messagebox.showinfo('任务运行中', '请先完成或取消当前任务')
        widget = widget or self._editor_target()
        try:
            text = widget.get('sel.first', 'sel.last') if selection else widget.get('insert linestart', 'insert lineend')
        except tk.TclError:
            return messagebox.showinfo('局部试听', '请先选中文字')
        if not text.strip():
            return
        seq = self._preempt()
        try:
            snap = self.snapshot()
            names, vmap = self._dub_snapshot()
            if selection:
                first_line = widget.get('sel.first linestart', 'sel.first lineend')
                match = re.match(r'^\s*(?:\[([^\]]+)\]|([^:：\s]{1,12})[:：])', first_line)
                if match and not re.match(r'^\s*(?:\[|[^:：\s]{1,12}[:：])', text):
                    name = (match.group(1) or match.group(2)).strip()
                    if name in names:
                        text = f'[{name}]' + text
            segs = engine.parse_dub_script(text, names)
            jobs = [(snap['voice'] if i is None else vmap[names[i]],
                     snap['person'] if i is None else names[i], body) for i, body in segs]
            if engine.kind_of(snap['engine']) == 'azure':
                jobs = engine.plan_azure_jobs(jobs)
            self._begin_task(jobs, snap, text, True)
        except Exception as e:
            return self._fail(str(e))
        self._prog_begin('multi' if len(jobs) > 1 else 'single', len(jobs), seq, '局部试听')
        def work():
            parts = [p for p in self._synth_jobs(seq, jobs, snap) if p]
            if not parts:
                return self._cancelled_done()
            return self._prepare_playback(seq, parts, os.path.join(OUT_DIR, '局部试听.mp3'), '局部试听')
        self._bg(work)

    def on_single(self):
        if self._active_task:
            # 按钮化身“合成/取消”：运行中再点即停止提交，已合成片段照常拼接输出。
            if self._stopping:
                return self.status.config(text="正在输出已完成的片段，稍候完成…")
            return self.on_stop()
        # 如果启用了至少一个多人配音槽位，则执行多人合成
        if any(s["on"].get() for s in getattr(self, "_dub", {}).get("slots", []) if s.get("on")):
            return self.on_multi()
        
        my = self._preempt()
        self._prog_cancel()
        try:
            snap = self.snapshot()
            txt = self._get_real_text()
            if not txt:
                raise RuntimeError("文本为空")
            jobs = [(snap["voice"], snap["person"], txt)]
            if engine.kind_of(snap["engine"]) == "azure":
                jobs = engine.plan_azure_jobs(jobs)
            self._begin_task(jobs, snap, txt)
        except Exception as e:
            return self._fail(str(e))
        self._prog_begin("multi" if len(jobs) > 1 else "single", len(jobs), my, "准备合成")
        self.status.config(text="正在合成...")

        def work():
            if my != self._seq:
                return lambda: self.status.config(text="已切换, 旧任务丢弃")

            parts = [p for p in self._synth_jobs(my, jobs, snap) if p]
            if not parts:
                return self._cancelled_done()
            out = os.path.join(OUT_DIR, "合成导出.mp3")
            return self._prepare_playback(my, parts, out, snap["person"])
        self._bg(work)

    def _synth_jobs(self, seq, jobs, snap):
        """Azure 最多两路在途请求；输出按原文排序，回调按段编号聚合。

        点停止（取消）后不再提交新段，未完成段返回 None，已完成段照常拼接输出。
        实现见 synth_jobs.run_jobs，本方法只组装无 Tk 的调用上下文。
        """
        from synth_jobs import JobContext, run_jobs
        ctx = JobContext(
            is_current=lambda s: s == self._seq,
            emit=lambda kind, s, payload: self._bgq.put((kind, s, payload)),
            cache=self._segment_cache,
            callbacks=lambda s, i, t: self._progress_callbacks(s, i, t),
            synth=lambda text, seg, voice, **kw: self.synth_net(text, seg, voice, **kw),
            pool=self._synth_pool,
            token=lambda: self._task_token,
        )
        return run_jobs(ctx, seq, jobs, snap)

    def _audition_voice(self, display, voice_id):
        if self._active_task:
            return
        try:
            snap = self.snapshot()
        except Exception as e:
            return self._fail(str(e))
        snap["audition"] = True  # 固定短句无须加载多音字模型，保持 Azure 原生问句韵律。
        # 试听缓存键脱敏：Key/端点只存哈希（与指纹的 credential_scope 同理），内存不留明文。
        redacted = dict(snap)
        if redacted.get('key'):
            redacted['key'] = hashlib.sha256(str(redacted['key']).encode()).hexdigest()
        if redacted.get('ep'):
            redacted['ep'] = hashlib.sha256(str(redacted['ep']).encode()).hexdigest()
        cache_key = (AUDITION_TEXT, voice_id,
                     tuple(sorted((k, v) for k, v in redacted.items() if k not in ("person", "voice"))),
                     tuple(sorted(self._export_options_safe().items())))
        if self.busy and getattr(self, "_audition_pending_key", None) == cache_key:
            return  # 同一试听请求尚未完成，连续双击不重复请求/重置进度。
        my = self._preempt()
        self._audition_pending_key = cache_key
        self._prog_cancel()
        try:
            self._begin_task([(voice_id, display, AUDITION_TEXT)], snap, AUDITION_TEXT, True)
        except Exception as e:
            return self._fail(str(e))
        self._prog_begin("single", 1, my, "正在合成")
        self.status.config(text=f"正在合成试听 [{display}]...")

        def work():
            if my != self._seq:
                return lambda: self.status.config(text="已切换, 旧任务丢弃")
            out = os.path.join(OUT_DIR, f"试听-{safe_name(voice_id)}.mp3")
            with self._audition_cache_lock:
                cached = self._audition_cache.get(cache_key)
                if cached is not None:
                    self._audition_cache.move_to_end(cache_key)
            if cached is not None:
                self._bgq.put(("phase", my, ("复用试听音频", 0)))
                return self._prepare_playback(my, None, out, display, prepared=cached)
            progress, stage = self._progress_callbacks(my)
            data = self.synth_net(AUDITION_TEXT, snap, voice_id,
                                  on_progress=progress, on_stage=stage)
            return self._prepare_playback(my, data, out, display, cache_key=cache_key)
        self._bg(work)

    def on_audition(self):
        # 单击只选中，不中断播放；双击明确发起新试听。
        display = self.selected
        voice_id = self.voices.get(display, "")
        if not voice_id:
            return self._fail("请先在列表点选一个人声")
        # 试听使用固定文案：谁是我们的敌人？谁是我们的朋友？
        self._audition_voice(display, voice_id)

    def _on_tree_audition(self, event):
        # 双击表头、列分隔线或空白处不能重启当前试听。
        if self.tree.identify_region(event.x, event.y) not in ("cell", "tree"):
            return
        row = self.tree.identify_row(event.y)
        if not row:
            return
        display = next((name for name, iid in self._iid.items() if iid == row), None)
        if display is not None:
            self.selected = display
            self.on_audition()
        return "break"

    # ---- 多人配音(独立面板, 最多9人): 文本按 `角色名:台词` / `[角色名]台词` 分派 ----
    @staticmethod
    def _center_voice_dropdown(combo):
        """选中显示区和弹出列表分别对齐；Tk 9 支持 Listbox.justify。"""
        popup = combo.tk.call("ttk::combobox::PopdownWindow", combo)
        listbox = f"{popup}.f.l"
        options = combo.tk.call(listbox, "configure")
        if any(str(option[0]) == "-justify" for option in options):
            combo.tk.call(listbox, "configure", "-justify", "left")

    def reset_roles(self):
        if self._active_task:
            return
        default_voice = self.voices.get(self.selected, '')
        display = f'{gender_of(self.selected) or "中"} {default_voice}' if default_voice else ''
        for i, slot in enumerate(self._dub.get('slots', [])):
            slot['name'].set('旁白' if i == 0 else f'角色{i + 1}')
            slot['on'].set(i == 0)
            slot['combo'].set(display)
            slot['update_color']()
        self._save_cfg()
        self._schedule_editor_info()
        self.status.config(text='角色已重置：旁白启用，其余关闭，人声恢复为当前选中人声')

    def _dub_all(self, on):
        # 测试专用：批量开/关全部槽位（顶栏/面板无此入口）。
        for s in self._dub.get("slots", []):
            try:
                s["on"].set(on)
                if "update_color" in s:
                    s["update_color"]()
            except tk.TclError:
                pass
        self._save_cfg()

    def _dub_snapshot(self):
        """读配音面板 -> ([names], {name: voice_id}); 启用槽位未绑定人声直接报错."""
        names, vmap = [], {}
        for s in self._dub.get("slots", []):
            try:
                if not s["on"].get():
                    continue
                name = (s["name"].get().strip() or f"角色{len(names) + 1}")[:12]
            except tk.TclError:
                continue
            if name in vmap:
                raise ValueError(f'角色名称重复：{name}')
            raw_val = s["combo"].get().strip()
            # 从 "女 zh-CN-XiaoxiaoNeural" 或旧显示名中反解有效 voice_id
            vid = ""
            if " " in raw_val:
                candidate_vid = raw_val.split()[-1]
                if candidate_vid in self.voices.values():
                    vid = candidate_vid
            if not vid:
                vid = self.voices.get(raw_val, "")
            if not vid:
                raise ValueError(f'角色 [{name}] 未绑定有效人声，请在槽位中重选')
            names.append(name)
            vmap[name] = vid
        return names, vmap

    def _enabled_role_names(self):
        """仅取启用槽位名（不校验人声），供『角色分配』等只需名字的场合使用。"""
        names = []
        for s in self._dub.get("slots", []):
            try:
                if s["on"].get():
                    name = (s["name"].get().strip() or f"角色{len(names) + 1}")[:12]
                    names.append(name)
            except tk.TclError:
                continue
        return names

    def _sync_dub_cfg(self):
        for i, s in enumerate(self._dub.get("slots", [])):
            try:
                # 与 _valid_dub_cfg 的 name[:12] 截断保持一致, 防保存后回读被改名
                name = s["name"].get().strip() or f"角色{i + 1}"
                self.dub_cfg[i] = {"name": name[:12],
                                   "voice": s["combo"].get(), "on": bool(s["on"].get())}
            except tk.TclError:
                pass

    def _refresh_dub_voices(self):
        # 人声表变化后刷新各槽下拉, 格式只显示: 性别 + 代号 (如: 女 zh-CN-XiaoxiaoNeural)
        slot_items = []
        for disp, vid in self.voices.items():
            g = gender_of(disp) or "中"
            slot_items.append(f"{g} {vid}")
        
        for s in self._dub.get("slots", []):
            try:
                cb = s["combo"]
                cur = cb.get()
                cb.config(values=slot_items)
                if not slot_items:
                    cb.set("")
                if cur not in slot_items and slot_items:
                    # 匹配当前选择的人声代号
                    cur_vid = self.voices.get(self.selected, "")
                    matched = next((item for item in slot_items if item.endswith(cur_vid)), slot_items[0])
                    cb.set(matched)
            except tk.TclError:
                pass

    def on_slot_audition(self, k):
        try:
            s = self._dub["slots"][k]
            raw_val = s["combo"].get().strip()
            vid = raw_val.split()[-1] if " " in raw_val else self.voices.get(raw_val, "")
        except (tk.TclError, IndexError):
            return self._fail("配音槽位不可用")
        if not vid or vid not in self.voices.values():
            return self._fail("该槽位人声已不在列表, 请重选")
        self._audition_voice(raw_val, vid)

    def on_multi(self):
        if self._active_task:
            return
        my = self._preempt()
        self._prog_cancel()
        try:
            snap = self.snapshot()
            txt = self._get_real_text()
            if not txt:
                raise RuntimeError("文本为空")
            names, vmap = self._dub_snapshot()
            if not names:
                raise RuntimeError("请先在多人配音区勾选至少1人")
            for line in txt.splitlines():
                match = re.match(r'^\s*\[([^\]]+)\]', line)
                if match and match.group(1).strip() not in names:
                    raise ValueError(f'未绑定或未启用的角色：{match.group(1)}')
            segs = engine.parse_dub_script(txt, names)
            if not segs:
                raise RuntimeError("文本为空")
            jobs = []  # (voice_id, label, text)
            for idx, body in segs:
                if idx is None:
                    jobs.append((snap["voice"], f"默认:{snap['person']}", body))
                else:
                    nm = names[idx]
                    jobs.append((vmap[nm], nm, body))
            if engine.kind_of(snap["engine"]) == "azure":
                jobs = engine.plan_azure_jobs(jobs)
            self._begin_task(jobs, snap, txt)
        except Exception as e:
            return self._fail(str(e))
        total = len(jobs)
        self._prog_begin("multi", total, my, "正在合成")
        self.status.config(text=f"正在合成多人配音(共{total}段)...")

        def work():
            parts = [p for p in self._synth_jobs(my, jobs, snap) if p]
            if my != self._seq:
                return None
            if not parts:
                return self._cancelled_done()
            self._bgq.put(("phase", my, ("正在拼接音频", 0)))
            import datetime
            stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            out = os.path.join(OUT_DIR, f"多人配音-{stamp}.mp3")
            return self._prepare_playback(my, parts, out, f"多人配音{len(parts)}段")
        self._bg(work)

    def on_stop(self):
        # 不作废任务序号：停止提交新的网络请求，已完成的片段照常拼接输出。
        self._stop_playback()
        self._audition_pending_key = None
        self._task_token.cancel()
        if self._active_task:
            self._stopping = True
            self.status.config(text="已停止提交，正在输出已完成的片段…")
        else:
            self._prog_cancel()
            self.status.config(text="已停止")

    def on_refresh(self):
        if self._active_task:
            return
        kind = engine.kind_of(self.engine_var.get())
        if kind == "edge":
            self.status.config(text="拉取Edge全量人物中...")
            region, ep, key = "", "", ""
        elif kind == "openai":
            self.voices = engine.list_openai_voices()
            if self.selected not in self.voices:
                self.selected = next(iter(self.voices), "")
            self.rebuild_list()
            self._refresh_dub_voices()
            self.status.config(text=f"OpenAI兼容内置{len(self.voices)}个人声")
            return
        elif kind == "volc":
            self.voices = engine.list_volc_voices()
            if self.selected not in self.voices:
                self.selected = next(iter(self.voices), "")
            self.rebuild_list()
            self._refresh_dub_voices()
            self.status.config(text=f"火山引擎内置{len(self.voices)}个人声")
            return
        else:
            key = self.key_var.get().strip()
            if not key:
                return self._fail("请先填Key再刷新")
            region, ep = self.region_var.get(), self.ep_var.get()
            self.status.config(text="拉取全量人物中(超时会自动重试)...")
        is_edge = (kind == "edge")
        eng_at_start = self.engine_var.get()

        def work():
            if is_edge:
                voices = engine.list_edge_voices()
            else:
                voices = engine.refresh_voices_azure(key, region, ep)

            def done():
                # 切引擎后回来的旧列表直接丢弃, 防覆盖新引擎人声
                if self.engine_var.get() != eng_at_start:
                    return
                self.voices = voices
                if self.selected not in self.voices:
                    self.selected = next(iter(self.voices), "")
                self.rebuild_list()
                self._refresh_dub_voices()
                self._refresh_style_state()
                self.status.config(text=f"已拉取{len(self.voices)}个人物")
            return done
        self._bg(work)

    def import_txt(self):
        if self._active_task:
            return
        types = [
            ("支持的所有文档", "*.txt;*.md;*.markdown;*.srt;*.lrc;*.json;*.csv;*.docx;*.pdf"),
            ("文本文档", "*.txt;*.md;*.markdown"),
            ("字幕歌词", "*.srt;*.lrc"),
            ("Word 文档", "*.docx"),
            ("PDF 文档", "*.pdf"),
            ("数据文件", "*.json;*.csv"),
            ("所有文件", "*.*")
        ]
        path = filedialog.askopenfilename(filetypes=types)
        if not path:
            return
        original = self._get_real_text()
        self.status.config(text=f"正在导入: {os.path.basename(path)}")
        def work():
            content = self._read_document(path)
            def done():
                if self._get_real_text() != original:
                    self.status.config(text="导入期间原稿已被编辑，未覆盖；请重新导入")
                    return
                self._set_real_text(content)
                self.status.config(text=f"已导入: {os.path.basename(path)} ({len(content)} 字)")
            return done
        self._bg(work)

    @staticmethod
    def _read_document(path):
        """纯 IO/解析函数，PDF OCR 也在后台运行，不接触 Tk 控件。"""
        import documents
        return documents.read_document(path)

    def open_out(self):
        # 指向合成文件：有成品时在资源管理器中选中它，否则打开输出目录。
        last = getattr(self, '_last_export', None)
        if last and Path(last).is_file():
            try:
                subprocess.Popen(['explorer', '/select,', str(Path(last).resolve())])
                return
            except OSError:
                pass
        path = self.export_dir.get()
        try:
            os.makedirs(path, exist_ok=True)
            os.startfile(path)
        except (OSError, ValueError) as e:
            self._fail(f"打开输出目录失败: {e}")

    def _voice_status(self, display):
        text = f"已选: {display}"
        if engine.kind_of(self.engine_var.get()) == "azure":
            metadata = engine.azure_voice_metadata(self.region_var.get()).get(self.voices.get(display, ""), {})
            if isinstance(metadata, dict) and metadata.get('VoiceType'):
                text += f" · Azure接口类型: {metadata['VoiceType']}"
                if metadata.get('Status'):
                    text += f" · {metadata['Status']}"
        return text

    def _on_tree_select(self, e):
        # 重建列表时的程序化选中不触发停播, 只有用户真点选才互斥停旧音
        if getattr(self, "_rebuilding", False):
            return
        sel = self.tree.selection()
        if not sel:
            return
        for display, iid in self._iid.items():
            if iid == sel[0]:
                if display == self.selected:
                    return
                self.selected = display
                self._save_cfg()
                self._refresh_style_state()
                self.status.config(text=self._voice_status(display))
                return

    def _filtered_items(self):
        g = self.gender_var.get()
        return [(k, v) for k, v in self.voices.items()
                if want_voice(self.filter_var.get(), v)
                and (g == "全部" or gender_of(k) == g)]

    def rebuild_list(self):
        self._rebuilding = True
        try:
            try:
                self.tree.delete(*self.tree.get_children())
            except tk.TclError:
                return
            self._iid = {}
            vals = self._filtered_items()
            if not vals:
                self.status.config(text="无匹配人声")
            sel_iid = None
            for display, vid in vals:
                iid = self.tree.insert("", "end", values=(gender_of(display), display.split()[0], vid))
                self._iid[display] = iid
                if display == self.selected:
                    sel_iid = iid
            if sel_iid:
                try:
                    self.tree.selection_set(sel_iid)
                    self.tree.see(sel_iid)
                except tk.TclError:
                    pass
        finally:
            self._rebuilding = False

    def _flowbar(self, bar, padx=None, pady=None):
        # v1.6: 按钮栏随窗口宽度自动换行 + 缩放自适应间距, 窄窗也不挤掉元素
        # padx/pady不写死, 每次用当前zoom重算, 防150%时遮挡
        if bar not in getattr(self, "_flowbars", []):
            self._flowbars.append(bar)
        bar.bind("<Configure>", lambda e: self._reflow_debounced(bar))
        self.root.after(50, lambda: self._reflow_current(bar))

    def _reflow_debounced(self, bar):
        # 子栏Configure抖动多, 只在宽度真正变化时重排；宽度记在栏自身，随控件销毁，不用 id() 键。
        try:
            W = bar.winfo_width()
        except tk.TclError:
            return
        if abs(W - getattr(bar, "_flow_w", 0)) < 6:
            return
        bar._flow_w = W
        _, _, ix, iy = self._pads()
        self._reflow(bar, ix, iy)

    def _reflow_current(self, bar):
        try:
            _, _, ix, iy = self._pads()
            self._reflow(bar, ix, iy)
        except Exception:
            pass

    def _reflow(self, bar, padx=4, pady=2):
        if getattr(self, "_reflowing", False):
            return
        try:
            W = bar.winfo_width()
        except tk.TclError:
            return
        if W <= 1 or bar.winfo_manager() == "":
            return
        self._reflowing = True
        try:
            kids = list(bar.winfo_children())
            try:
                labelwidget = str(bar.cget('labelwidget'))
            except tk.TclError:
                labelwidget = ''
            # 先清掉旧grid列配置, 否则列数变少时残留uniform会挤压
            try:
                ncols = max(len(kids), 8)
                for i in range(ncols):
                    bar.grid_columnconfigure(i, weight=0, uniform="")
            except tk.TclError:
                pass
            r, c, x = 0, 0, 0
            for w in kids:
                if labelwidget and str(w) == labelwidget:
                    continue  # 区标题占位框不参与重排，grid 会顶掉 labelwidget
                try:
                    ww = w.winfo_reqwidth() + padx * 2
                except tk.TclError:
                    continue
                if x + ww > W and c > 0:
                    r += 1
                    c, x = 0, 0
                try:
                    w.grid(row=r, column=c, padx=padx, pady=pady, sticky="w")
                except tk.TclError:
                    continue
                x += ww
                c += 1
        finally:
            self._reflowing = False

    def toggle_server(self):
        import forward_server
        if self._active_task:
            return self.status.config(text="任务运行中，稍后再切换转发服务")
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
            self.server_btn.config(text="启动转发")
            self.server_lab.config(text="未启动")
            return
        try:
            snap = self.snapshot()
            port = int(self.port_var.get().strip())
            if not 1 <= port <= 65535:
                raise ValueError("端口号必须在 1～65535 之间")
        except Exception as e:
            return self._fail(str(e))

        voices = dict(self.voices)
        valid = set(voices.values()) | {snap["voice"]}

        def synth_fn(text, voice, rate):
            vid = voice or snap["voice"]
            if vid not in valid:
                vid = snap["voice"]
            return self.synth_net(text, dict(snap, rate=rate or snap["rate"]), vid)

        def voices_fn():
            return [{"name": k, "id": v} for k, v in voices.items()]

        forward_server._Handler.synth_fn = staticmethod(synth_fn)
        forward_server._Handler.voices_fn = staticmethod(voices_fn)
        try:
            # 仅绑定回环地址：无鉴权的合成接口不应暴露给局域网，浏览器插件等本机调用不受影响。
            self.server = forward_server.BoundedThreadingHTTPServer(("127.0.0.1", port),
                                                                    forward_server._Handler)
        except Exception as e:
            return self._fail(f"端口{port}启动失败: {e}")
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.server_btn.config(text="停止转发")
        self.server_lab.config(text=f"运行中 http://127.0.0.1:{port}/forward?text=你好")
        self.status.config(text="转发服务已启动(使用启动时的调节值)")
