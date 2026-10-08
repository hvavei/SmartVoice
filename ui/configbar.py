"""朗读引擎配置栏：引擎下拉、Key/Region/终结点输入、显示与记住开关、界面缩放。"""
import tkinter as tk
from tkinter import ttk

import engine

ZOOMS = [80, 90, 100, 110, 125, 150]


def build_configbar(app, f1, IX, IY):
    ttk.Label(f1, text="引擎:").grid(row=0, column=0, padx=IX, pady=IY, sticky="e")
    app.engine_combo = ttk.Combobox(f1, textvariable=app.engine_var,
                                    values=engine.ENGINE_CHOICES, state="readonly", width=16)
    app.engine_combo.grid(row=0, column=1, padx=IX, pady=IY, sticky="ew")
    app.engine_combo.bind("<<ComboboxSelected>>", lambda e: app.on_engine_switch())
    app.lab_key = ttk.Label(f1, text="Key:", width=6, anchor="e")
    app.lab_key.grid(row=0, column=2, padx=IX, sticky="e")
    app.key_entry = ttk.Entry(f1, textvariable=app.key_var, width=20, justify="center")
    app.key_entry.grid(row=0, column=3, padx=2, sticky="ew")
    app._apply_show()
    app._toggle(f1, app.show_var, "显示", app._apply_show).grid(row=0, column=4, padx=IX, sticky="w")
    ttk.Frame(f1).grid(row=0, column=5, sticky="ew")
    zoom_box = ttk.Frame(f1)
    # 缩放区钉在右侧：与窗口右缘距离固定且留有余量（padx 右 IX+8），
    # 宽窗多余空间全部由 Key/终结点输入框列（col3 weight=1）吸收铺满同行。
    zoom_box.grid(row=0, column=6, columnspan=2, padx=(IX, IX + 8), sticky="e")
    app.lab_zoom = ttk.Label(zoom_box, text="界面缩放:")
    app.lab_zoom.pack(side="left", padx=(0, 4))
    app.zb = ttk.Combobox(zoom_box, textvariable=app.zoom_var, values=ZOOMS,
                          state="readonly", width=5, justify="center")
    app.zb.pack(side="left")
    app.zb.bind("<<ComboboxSelected>>", lambda e: app.on_zoom())
    app.lab_region = ttk.Label(f1, text="Region:", width=7, anchor="e")
    app.lab_region.grid(row=1, column=0, padx=IX, pady=IY, sticky="e")
    app.region_entry = ttk.Entry(f1, textvariable=app.region_var, width=16, justify="center")
    app.region_entry.grid(row=1, column=1, padx=IX, sticky="ew")
    app.lab_ep = ttk.Label(f1, text="终结点:", width=6, anchor="e")
    app.lab_ep.grid(row=1, column=2, padx=IX, sticky="e")
    app.ep_entry = ttk.Entry(f1, textvariable=app.ep_var, width=20, justify="center")
    app.ep_entry.grid(row=1, column=3, padx=2, sticky="ew")
    app._toggle(f1, app.remember_var, "记住Key", app._save_cfg).grid(row=1, column=4, padx=IX, sticky="w")
    # col3 weight=1：Key/终结点输入框吸收宽窗全部多余空间铺满同行，窄窗收缩到 minsize。
    f1.columnconfigure(3, weight=1, minsize=70)
    # col5 空档列：minsize 保证显示开关与界面缩放区始终有可见间距。
    f1.columnconfigure(5, minsize=12)
    for entry in (app.key_entry, app.region_entry, app.ep_entry):
        app._bind_entry_context_menu(entry)
    app._refresh_engine_fields()
