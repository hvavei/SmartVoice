"""文本编辑区：标题行 + 进度条 + 原稿 Text + 字数/角色/播放时间信息行。"""
import tkinter as tk
from tkinter import ttk

import theme


def build_editor(app, f4, IX, IY):
    title_row = ttk.Frame(f4)
    shadow, title = app._title_widget(title_row, "文本")
    shadow.pack(side="left")
    app.prog_wrap = ttk.Frame(title_row)
    app.prog_wrap.pack(side="left", padx=(8, 0))
    f4.configure(labelwidget=title_row)
    app.prog = tk.Canvas(app.prog_wrap, width=max(120, round(150 * app._zx())),
                         height=max(9, round(11 * app._zx())), bg=theme.PANEL,
                         highlightthickness=1, highlightbackground=theme.BORDER, bd=0)
    app.prog.bind("<Configure>", lambda e: app._prog_draw())
    app.prog_lab = ttk.Label(app.prog_wrap, text="", anchor="w", foreground=theme.FEEDBACK)
    app._prog_ui = {"mode": "determinate", "value": 0, "maximum": 100}
    app.text = tk.Text(f4, height=3, undo=True, autoseparators=True, maxundo=5000,
                       bg=theme.PANEL, fg=theme.FG, insertbackground=theme.FG,
                       font=app._font(), relief="flat", borderwidth=0, wrap="word",
                       highlightthickness=1, highlightbackground=theme.BORDER,
                       highlightcolor=theme.ACCENT, padx=max(8, round(10 * app._zx())),
                       pady=max(4, round(6 * app._zx())))
    app.text.pack(fill="x", padx=IX, pady=IY)
    app._setup_placeholder()
    app._bind_text_context_menu()
    app.editor_info = ttk.Frame(f4)
    app.editor_info.pack(fill='x', padx=IX)
    app.editor_role = ttk.Label(app.editor_info, text='0字 · 0段 · 当前角色：默认人声',
                                foreground=theme.FEEDBACK)
    app.editor_role.pack(side='left')
    app.play_time = ttk.Label(app.editor_info, text='00:00 / 00:00', foreground=theme.FEEDBACK)
    app.play_time.pack(side='left', padx=(12, 0))
    app._bind_editor(app.text)
    app.text.bind('<Double-Button-1>', app._open_editor_double_click)
