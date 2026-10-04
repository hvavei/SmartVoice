"""底部文本工具条：导入/合成/播放/暂停/输出目录五个等权按钮。"""


def build_toolbar(app, f5):
    buttons = (
        ('b_import', '导入文本', app.import_txt),
        ('b_single', '合成/取消', app.on_single),
        ('b_play', '播放/停止', app.toggle_playback),
        ('b_stop', '暂停/继续', app.toggle_pause),
        ('b_open', '输出目录', app.open_out),
    )
    for column, (attr, text, command) in enumerate(buttons):
        button = app._mkbtn(f5, text, command, width=8)
        # 与“刷新”“角色分配”“角色重置”统一使用默认 TButton。
        # 保留等宽 grid 布局，但不再使用 Tool.TButton 的紧凑色彩/高度。
        button.configure(style='TButton')
        button.grid(row=0, column=column, padx=2, pady=1, sticky='ew')
        setattr(app, attr, button)
        f5.columnconfigure(column, weight=1, uniform='text_tools')
    f5.rowconfigure(0, weight=1)
