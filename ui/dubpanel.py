"""多人配音面板：9 个角色槽位卡片（开关色块 + 名称输入 + 人声下拉 + 试听）。"""
import tkinter as tk
from tkinter import ttk

import theme

MAX_DUB = 9      # 最多9人配音
SLOT_AUD_W = 6   # 配音卡片内试听小按钮统一宽


def gender_of(display):
    return "女" if "女" in display else ("男" if "男" in display else "")


def build_dubpanel(app, dub_box, IX, IY):
    box = dub_box
    box.config(text="")
    title_frame = ttk.Frame(box)
    shadow_box, app.dub_title = app._title_widget(title_frame, "多人配音（最多9人）")
    shadow_box.pack(side="left")
    app.dub_format_hint = ttk.Label(title_frame, text=" 格式：角色：台词",
                                    font=app._font(), foreground=theme.FEEDBACK)
    app.dub_format_hint.pack(side="left")
    box.configure(labelwidget=title_frame, labelanchor="nw")
    head = ttk.Frame(box)
    head.pack(fill="x", padx=IX, pady=IY)

    # 筛选按钮：同人声面板，放在角色分配前面
    app.dub_filter_var = getattr(app, 'dub_filter_var', tk.StringVar(value="全部"))
    app.dub_fb = ttk.Combobox(head, textvariable=app.dub_filter_var,
                              values=["全部", "男", "女"], state="readonly", width=6)
    app.dub_fb.pack(side="left", padx=2)
    app.dub_fb.bind("<<ComboboxSelected>>", lambda e: app._refresh_dub_voices())
    app._mkbtn(head, "角色分配", app.on_format_dialogue_lines, width=8).pack(side="left", padx=2)
    app._mkbtn(head, "角色重置", app.reset_roles, width=8).pack(side="left", padx=2)
    scroll = ttk.Frame(box)
    scroll.pack(fill="both", expand=True, padx=IX, pady=(0, IY))
    app.dub_canvas = tk.Canvas(scroll, bg=theme.BG, highlightthickness=0, bd=0)
    dub_sb = ttk.Scrollbar(scroll, orient="vertical", command=app.dub_canvas.yview)
    app.dub_canvas.configure(yscrollcommand=dub_sb.set)
    dub_sb.pack(side="right", fill="y")
    app.dub_canvas.pack(side="left", fill="both", expand=True)
    app.dub_body = ttk.Frame(app.dub_canvas)
    app._dub_window = app.dub_canvas.create_window((0, 0), window=app.dub_body, anchor="nw")
    app.dub_body.bind("<Configure>", lambda e: app.dub_canvas.configure(
        scrollregion=app.dub_canvas.bbox("all")))
    app.dub_canvas.bind("<Configure>", lambda e: app.dub_canvas.itemconfigure(
        app._dub_window, width=e.width))

    def _wheel(e):
        try:
            app.dub_canvas.yview_scroll(int(-1 * (e.delta / 120)), 'units')
        except tk.TclError:
            pass
        return 'break'
    # 卡片内的 Combobox/按钮保留原生滚轮行为，只在画布/卡片空白处滚动。
    app.dub_canvas.bind('<MouseWheel>', _wheel)
    app.dub_body.bind('<MouseWheel>', _wheel)
    app._dub["slots"] = []
    # 多人配音下拉人声：只展示 性别 + 代号 (如: 女 zh-CN-XiaoxiaoNeural)
    slot_items = []
    for disp, vid in app.voices.items():
        g = gender_of(disp) or "中"
        slot_items.append(f"{g} {vid}")

    for i in range(MAX_DUB):
        cfg = app.dub_cfg[i]
        card = ttk.Frame(app.dub_body)
        on_v = tk.BooleanVar(value=cfg["on"])
        name_v = tk.StringVar(value=cfg["name"])
        # 人声显示区是 Combobox，与左侧可编辑的角色名称 Entry 独立。
        cb_voice = ttk.Combobox(card, values=slot_items, width=15,
                                justify="left", state="readonly")
        cb_voice.configure(postcommand=lambda cb=cb_voice: app._center_voice_dropdown(cb))

        # 回显匹配
        cur_saved = cfg.get("voice", "")
        matched = next((item for item in slot_items if cur_saved and (item == cur_saved or item.split()[-1] == app.voices.get(cur_saved, cur_saved))), None)
        if matched:
            cb_voice.set(matched)
        elif cur_saved:
            cb_voice.set(cur_saved)
        elif slot_items:
            cur_vid = app.voices.get(app.selected, "")
            def_matched = next((item for item in slot_items if item.endswith(cur_vid)), slot_items[0])
            cb_voice.set(def_matched)

        # 精巧正方形色块，带细灰色描边
        box_sz = max(14, round(16 * app._zx()))
        color_cv = tk.Canvas(card, width=box_sz, height=box_sz, bg=theme.BG, highlightthickness=0, bd=0)
        color_cv.grid(row=0, column=0, padx=(2, 4))

        def _update_color(var=on_v, cv=color_cv, sz=box_sz):
            try:
                cv.delete("all")
                fill_col = theme.GREEN if var.get() else theme.TOGGLE_OFF
                cv.create_rectangle(1, 1, sz - 1, sz - 1, fill=fill_col, outline=theme.OUTLINE, width=1)
            except tk.TclError:
                pass
        _update_color()
        color_cv.bind("<Button-1>", lambda e, v=on_v, c=_update_color: (v.set(not v.get()), c(), app._save_cfg()))

        # 角色名称输入框宽度调整为 8，文字居中；失焦或清空时自动恢复默认名称
        ent = ttk.Entry(card, textvariable=name_v, width=8, justify="center")
        ent.grid(row=0, column=1, padx=2)
        app._bind_entry_context_menu(ent)

        def _on_name_focus_out(e, idx=i, var=name_v):
            if not var.get().strip():
                default_name = "旁白" if idx == 0 else f"角色{idx + 1}"
                var.set(default_name)
                app._save_cfg()
        ent.bind("<FocusOut>", _on_name_focus_out)

        cb_voice.grid(row=0, column=2, sticky='ew', padx=2)
        card.columnconfigure(2, weight=1)
        app._mkbtn(card, "试听", lambda k=i: app.on_slot_audition(k), width=SLOT_AUD_W).grid(
            row=0, column=3, padx=2)
        cb_voice.bind('<Configure>', lambda e: e.widget.xview_moveto(0))
        name_v.trace_add("write", lambda *a: app._save_cfg())
        cb_voice.bind("<<ComboboxSelected>>", lambda e: app._save_cfg())
        app._dub["slots"].append({"on": on_v, "name": name_v, "combo": cb_voice, "update_color": _update_color})
        card.bind('<MouseWheel>', _wheel)
    # 槽位紧凑纵向排列，右侧固定宽度且不会因窗口变窄被挤掉。
    for i, child in enumerate(app.dub_body.winfo_children()):
        child.grid(row=i, column=0, sticky="ew", pady=1)
    app.dub_body.columnconfigure(0, weight=1)
    app._dub["box"] = box
