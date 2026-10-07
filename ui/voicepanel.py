"""人声列表面板：筛选栏 + 人声表格（单击选中、双击试听，代号列固定不可拖拽）。"""
from tkinter import ttk

FILTERS = ["全部", "大陆中文zh-CN", "港中文zh-HK", "台中文zh-TW",
           "粤语", "英文en", "日语ja", "韩语ko", "其他语种"]


def build_voicepanel(app, voice_box, IX, IY):
    bar = ttk.Frame(voice_box)
    bar.pack(fill="x", padx=IX, pady=IY)
    app.b_refresh = app._mkbtn(bar, "刷新", app.on_refresh, width=6)
    app.b_refresh.grid(row=0, column=0, padx=2)
    app.fb = ttk.Combobox(bar, textvariable=app.filter_var, values=FILTERS, state="readonly", width=12)
    app.fb.grid(row=0, column=1, padx=2, sticky="ew")
    app.fb.bind("<<ComboboxSelected>>", lambda e: app.rebuild_list())
    app.lab_gender = ttk.Label(bar, text="性别:")
    app.lab_gender.grid(row=0, column=2, padx=2)
    app.gb = ttk.Combobox(bar, textvariable=app.gender_var, values=["全部", "男", "女"], state="readonly", width=6)
    app.gb.grid(row=0, column=3, padx=2, sticky="ew")
    bar.columnconfigure(1, weight=2)
    bar.columnconfigure(3, weight=1)
    app.gb.bind("<<ComboboxSelected>>", lambda e: app.rebuild_list())

    wrap = ttk.Frame(voice_box)
    wrap.pack(fill="both", expand=True, padx=IX, pady=IY)
    app.tree = ttk.Treeview(wrap, columns=("gender", "name", "vid"), show="headings", selectmode="browse", height=9)
    # 显式重置并强制居中
    app.tree.heading("gender", text="性别", anchor="center")
    app.tree.heading("name", text="名称", anchor="center")
    app.tree.heading("vid", text="代号", anchor="center")

    for column, width in (("gender", 46), ("name", 120), ("vid", 160)):
        app.tree.column(column, width=width, minwidth=1, anchor="center", stretch=False)
    app.voice_scrollbar = ttk.Scrollbar(wrap, orient="vertical", command=app.tree.yview)
    app.tree.configure(yscrollcommand=app.voice_scrollbar.set)
    # 滚动条独占不收缩的一列，表格只使用剩余宽度。
    wrap.columnconfigure(0, weight=1)
    wrap.columnconfigure(1, weight=0)
    wrap.rowconfigure(0, weight=1)
    app.tree.grid(row=0, column=0, sticky="nsew")
    app.voice_scrollbar.grid(row=0, column=1, sticky="ns")
    app.tree.bind("<Configure>", app._resize_voice_columns)
    app.tree.bind("<<TreeviewSelect>>", app._on_tree_select)
    app.tree.bind("<Double-1>", app._on_tree_audition)
    app.tree.bind("<Return>", app._on_tree_audition)  # 键盘用户：方向键选中 + 回车试听。
    app.tree.bind('<Button-1>', app._block_column_resize, add='+')
    app.tree.bind('<B1-Motion>', app._block_column_resize, add='+')
    app.tree.bind('<Motion>', app._block_column_resize, add='+')
