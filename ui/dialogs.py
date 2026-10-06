"""独立对话框：导出设置、增强组件管理。内容状态挂 app，窗口本身无状态。"""
import queue
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import theme


def open_export_settings(app):
    existing = getattr(app, '_export_win', None)
    if existing is not None and existing.winfo_exists():
        existing.lift()
        return
    top = tk.Toplevel(app.root)
    app._export_win = top
    top.title('SmartVoice · 导出设置')
    top.transient(app.root)
    top.configure(bg=theme.BG)
    for row, (label, var) in enumerate((('输出目录', app.export_dir), ('文件名', app.export_name),
                                       ('开头留白(ms)', app.leading_ms), ('结尾留白(ms)', app.trailing_ms))):
        ttk.Label(top, text=label).grid(row=row, column=0, padx=8, pady=6)
        ent = ttk.Entry(top, textvariable=var, width=48)
        ent.grid(row=row, column=1, padx=8, pady=6)
        app._bind_entry_context_menu(ent)

    def browse():
        path = filedialog.askdirectory(parent=top)
        if path:
            app.export_dir.set(path)
    ttk.Button(top, text='选择目录', command=browse).grid(row=0, column=2)
    ttk.Combobox(top, textvariable=app.export_format, values=['mp3', 'wav'], state='readonly').grid(row=4, column=1)
    ttk.Checkbutton(top, text='响度均衡（整条音频目标 -18 LUFS / 峰值 -1.5 dBTP）', variable=app.normalize_var).grid(row=5, columnspan=3, padx=8)
    ttk.Label(top, text='均衡会调整整体增益和动态范围，不改变文字、音高或时长；默认关闭。').grid(row=6, columnspan=3, padx=8, pady=8)

    def save():
        try:
            app._export_options()
            app._save_cfg()
            top.destroy()
        except (ValueError, tk.TclError) as e:
            messagebox.showerror('设置无效', str(e), parent=top)
    opened = app._export_options_safe()

    def dismiss():
        # 直接关窗：合法则保留改动，非法则恢复进入对话框时的可用值。
        try:
            app._export_options()
        except (ValueError, tk.TclError):
            app._apply_export_options(opened)
        top.destroy()
    top.protocol('WM_DELETE_WINDOW', dismiss)
    top._sv_dismiss = dismiss  # 主题切换走同一出口：非法值恢复进入时状态，不残留脏 Var。
    top.bind('<Escape>', lambda e: dismiss())
    ttk.Button(top, text='保存', command=save).grid(row=7, column=1, pady=8)
    try:
        top.winfo_children()[1].focus_set()
    except (tk.TclError, IndexError):
        pass


def open_component_manager(app):
    import components
    existing = getattr(app, '_component_win', None)
    if existing is not None and existing.winfo_exists():
        existing.lift()
        return
    top = tk.Toplevel(app.root)
    app._component_win = top
    top.title('SmartVoice · 增强组件')
    top.transient(app.root)
    top.geometry('650x300')
    top.resizable(False, False)
    top.configure(bg=theme.BG)
    ttk.Label(top, text='标准版可选安装；完整版已包含。安装包与组件必须为相同版本/架构。').pack(padx=12, pady=12)
    progress = ttk.Progressbar(top, maximum=100)
    progress.pack(side='bottom', fill='x', padx=12, pady=8)
    info = ttk.Label(top, text='准备就绪', wraplength=620)
    info.pack(side='bottom', fill='x', padx=12, pady=8)
    messages, cancel = queue.Queue(), threading.Event()
    active = [False]
    closing = [False]
    labels, buttons = {}, []

    def update_status():
        for name, label in labels.items():
            label.config(text='已包含/已安装' if components.available(name) else '未安装')

    def start(name, local=False):
        if active[0]:
            return
        path = filedialog.askopenfilename(parent=top, filetypes=[('SmartVoice组件', '*.zip')]) if local else None
        if local and not path:
            return
        if not local and not messagebox.askokcancel('下载增强组件',
                '将从 SmartVoice GitHub Releases 下载组件并校验SHA-256。G2PW包较大，是否继续？', parent=top):
            return
        active[0] = True
        cancel.clear()
        for button in buttons:
            button.state(['disabled'])

        def report(cur, total, phase):
            messages.put(('progress', (cur, total, phase)))

        def work():
            try:
                if local:
                    components.install_archive(name, path, report, cancel)
                else:
                    components.download(name, report, cancel)
                messages.put(('done', '组件已安装。若更新了已经加载的组件，请重启 SmartVoice。'))
            except Exception as e:
                messages.put(('done', str(e)))
        threading.Thread(target=work, daemon=True).start()
    for name, title in components.NAMES.items():
        row = ttk.Frame(top)
        row.pack(fill='x', padx=12, pady=6)
        ttk.Label(row, text=title, width=24).pack(side='left')
        labels[name] = ttk.Label(row, width=16)
        labels[name].pack(side='left')
        for caption, local in (('下载/安装', False), ('本地导入', True)):
            button = ttk.Button(row, text=caption, command=lambda n=name, l=local: start(n, l))
            button.pack(side='left', padx=3)
            buttons.append(button)
    update_status()

    def poll():
        if not top.winfo_exists():
            return
        try:
            for _ in range(200):
                kind, payload = messages.get_nowait()
                if kind == 'progress':
                    cur, total, phase = payload
                    progress['value'] = cur * 100 / max(1, total)
                    info.config(text=f'{phase}：{cur}/{total}')
                else:
                    active[0] = False
                    info.config(text=payload)
                    update_status()
                    for button in buttons:
                        button.state(['!disabled'])
                    if closing[0]:
                        top.destroy()
                        return
        except queue.Empty:
            pass
        if top.winfo_exists():
            top.after(100, poll)

    def close():
        if active[0]:
            cancel.set()
            closing[0] = True
            info.config(text='正在取消下载/解包，稍后自动关闭…')
        else:
            top.destroy()
    top.protocol('WM_DELETE_WINDOW', close)
    top._sv_close = close  # 主题切换走同一出口：下载中先取消不断孤儿线程。
    top.bind('<Escape>', lambda e: close())
    poll()
