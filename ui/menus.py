"""顶栏菜单：选项 / 组件 / 主题 / 关于；编辑类操作走文本右键。"""
import tkinter as tk
from tkinter import messagebox
import webbrowser

import appmeta
import theme
from theme import FG, PANEL, SEL


def build_product_menu(app):
    mfont = app._menu_font()
    bar = tk.Menu(app.root, font=mfont)
    menu_colors = dict(bg=PANEL, fg=FG, activebackground=SEL, activeforeground=FG)

    def submenu(parent=None):
        return tk.Menu(parent or bar, tearoff=False, font=mfont, **menu_colors)

    project = submenu()
    for label, command in [('新建项目', app.new_project), ('打开项目…', app.open_project),
                           ('保存项目', app.save_project), ('项目另存为…', lambda: app.save_project(True))]:
        project.add_command(label=label, command=command)
    project.add_separator()
    project.add_command(label='导出设置…', command=app.export_settings)
    project.add_command(label='打开刚生成的文件', command=app.open_last_export)
    bar.add_cascade(label='选项', menu=project)

    comp = submenu()
    comp.add_command(label='管理组件（G2PW / 扫描PDF OCR）', command=app.component_manager)
    bar.add_cascade(label='组件', menu=comp)

    theme_menu = submenu()
    for _key in theme.THEME_ORDER:
        theme_menu.add_radiobutton(label=theme.THEME_LABELS[_key], variable=app._theme_var,
                                   value=_key, command=lambda k=_key: app.switch_theme(k))
    bar.add_cascade(label='主题', menu=theme_menu)

    about = submenu()
    for label, command in [('版本说明', lambda: messagebox.showinfo(appmeta.NAME, appmeta.RELEASE_NOTES)),
                           ('检查更新', app.check_updates),
                           ('反馈问题（GitHub）', lambda: webbrowser.open(appmeta.FEEDBACK_URL)),
                           (None, None),
                           ('复制诊断信息', app.copy_diagnostics), ('导出问题报告…', app.export_diagnostics),
                           ('打开用户数据目录', app.open_data_dir)]:
        if label is None:
            about.add_separator()
        else:
            about.add_command(label=label, command=command)
    bar.add_cascade(label='关于', menu=about)

    app.root.configure(menu=bar)
    app.root.bind('<Control-s>', lambda e: (app.save_project(), 'break')[-1])
