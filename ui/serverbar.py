"""转发服务栏：端口输入 + 启动/停止按钮 + 状态标签。

约定：控件状态一律挂在 app（ctx）上；颜色读 theme 模块属性（构建时求值，换肤无需同步）。
"""
import tkinter as tk
from tkinter import ttk

import theme


def build_serverbar(app, f6):
    # 端口输入由 _reflow 统一布点，此处只创建控件。
    ttk.Label(f6, text="端口:")
    app.port_entry = ttk.Entry(f6, textvariable=app.port_var, width=8, justify="center")
    app._bind_entry_context_menu(app.port_entry)
    app.server_btn = app._mkbtn(f6, "停止转发" if app.server else "启动转发", app.toggle_server)
    app.server_lab = ttk.Label(f6, text="运行中" if app.server else "未启动",
                               foreground=theme.FEEDBACK)
    # 网页跨域调用开关：默认关；打开后任意网站可调本机服务（烧Key配额），重启服务生效。
    app._toggle(f6, app.cors_var, "网页调用", app._save_cfg)
    app._flowbar(f6)
