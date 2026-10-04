"""调节风格面板：风格/强度/角色下拉 + 语速/音量/音调滑块。"""
from tkinter import ttk

import engine


def build_stylepanel(app, f2, IX, IY):
    for row, (label, attr, var, values) in enumerate((
            ("风格:", "style_combo", app.style_var, engine.STYLES),
            ("强度:", "deg_combo", app.deg_var, engine.DEGREES),
            ("角色:", "role_combo", app.role_var, engine.ROLES))):
        ttk.Label(f2, text=label).grid(row=row, column=0, padx=IX, sticky="e")
        combo = ttk.Combobox(f2, textvariable=var, values=values, state="readonly", width=18)
        combo.grid(row=row, column=1, padx=IX, sticky="ew")
        combo.bind("<<ComboboxSelected>>", lambda e: app._on_style_change())
        setattr(app, attr, combo)
    app.rate_lab = ttk.Label(f2, width=6, anchor="e")
    app.vol_lab = ttk.Label(f2, width=6, anchor="e")
    app.pitch_lab = ttk.Label(f2, width=6, anchor="e")
    app._slider(f2, 0, 2, "语速", app.rate_var, 50, 200, app.rate_lab, 100)
    app._slider(f2, 1, 2, "音量", app.vol_var, 50, 150, app.vol_lab, 100)
    app._slider(f2, 2, 2, "音调", app.pitch_var, -12, 12, app.pitch_lab, 0)
    for row in range(3):
        f2.grid_rowconfigure(row, minsize=max(30, round(34 * app._zx())))
    app._show_rvp()
    app._refresh_style_state()
