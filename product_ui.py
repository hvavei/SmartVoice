"""SmartVoice 的项目、编辑、诊断与导出交互；核心合成保持在 App/engine。"""
import json
import os
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import webbrowser
import zipfile
import re

import appmeta
import engine
import storage
import voice_tasks as workflow
from theme import ACCENT, BG, BORDER, FG, PANEL


def cfg_int(value, default, lo=None, hi=None):
    """配置数值入口统一转换：坏值回退默认并钳位，杜绝启动/保存崩溃。"""
    try:
        v = int(value)
    except (TypeError, ValueError, tk.TclError):
        return default
    if lo is not None and v < lo:
        v = lo
    if hi is not None and v > hi:
        v = hi
    return v


def cfg_bool(value):
    if isinstance(value, str):
        return value.strip().lower() in ('1', 'true', 'yes', 'on')
    return bool(value)


class PeerText(tk.Text):
    def __init__(self, parent, source):
        self._setup(parent, {})
        self._tclCommands = []
        source.tk.call(source._w, 'peer', 'create', self._w, '-undo', True, '-wrap', 'word',
                       '-font', source.cget('font'), '-padx', 10, '-pady', 8)


class ProductUI:
    def _init_product(self, cfg):
        self._active_task = False
        self._stopping = False
        self._task_token = workflow.Cancellation()
        self._diag = None
        self._retry_plan = None
        self._segment_cache = workflow.SegmentCache()
        self._segment_keys = []
        self._export_history = []
        self._project_path = None
        self._project_baseline = None
        self._last_export = None
        self._last_pcm = None
        self._editor_after = None
        self._editor_window = None
        self._play_watch = None
        export = cfg.get('export', {})
        if not isinstance(export, dict):
            export = {}
        self.export_dir = tk.StringVar(value=str(export.get('directory', str(storage.EXPORT_DIR))))
        self.export_name = tk.StringVar(value=str(export.get('name', '配音')))
        fmt = str(export.get('format', 'mp3')).lower()
        self.export_format = tk.StringVar(value=fmt if fmt in ('mp3', 'wav') else 'mp3')
        self.leading_ms = tk.IntVar(value=cfg_int(export.get('leading_ms', 350), 350, 0, 5000))
        self.trailing_ms = tk.IntVar(value=cfg_int(export.get('trailing_ms', 450), 450, 0, 5000))
        self.normalize_var = tk.BooleanVar(value=cfg_bool(export.get('normalize', False)))

    def _build_product_menu(self):
        # 顶栏只保留三个入口：编辑类操作走文本右键，停止由“合成/取消”承担。
        mfont = self._menu_font()
        bar = tk.Menu(self.root, font=mfont)

        def submenu():
            return tk.Menu(bar, tearoff=False, font=mfont)

        project = submenu()
        for label, command in [('新建项目', self.new_project), ('打开项目…', self.open_project),
                               ('保存项目', self.save_project), ('项目另存为…', lambda: self.save_project(True))]:
            project.add_command(label=label, command=command)
        project.add_separator()
        project.add_command(label='导出设置…', command=self.export_settings)
        project.add_command(label='打开刚生成的文件', command=self.open_last_export)
        bar.add_cascade(label='选项', menu=project)

        comp = submenu()
        comp.add_command(label='管理组件（G2PW / 扫描PDF OCR）', command=self.component_manager)
        bar.add_cascade(label='组件', menu=comp)

        about = submenu()
        for label, command in [('版本说明', lambda: messagebox.showinfo(appmeta.NAME, appmeta.RELEASE_NOTES)),
                               ('检查更新', self.check_updates),
                               ('反馈问题（GitHub）', lambda: webbrowser.open(appmeta.FEEDBACK_URL)),
                               (None, None),
                               ('复制诊断信息', self.copy_diagnostics), ('导出问题报告…', self.export_diagnostics),
                               ('打开用户数据目录', self.open_data_dir)]:
            if label is None:
                about.add_separator()
            else:
                about.add_command(label=label, command=command)
        bar.add_cascade(label='关于', menu=about)

        self.root.configure(menu=bar)
        self.root.bind('<Control-s>', lambda e: (self.save_project(), 'break')[-1])

    def _editor_target(self):
        try:
            widget = self.root.focus_get()
            if isinstance(widget, tk.Text) and widget.winfo_exists():
                return widget
            last = getattr(self, '_active_editor', None)
            return last if last is not None and last.winfo_exists() else self.text
        except (tk.TclError, KeyError):
            return self.text

    def _undo(self, redo=False):
        try:
            widget = self._editor_target()
            widget.edit_redo() if redo else widget.edit_undo()
        except tk.TclError:
            pass
        self._schedule_editor_info()
        return 'break'

    def _bind_editor(self, widget):
        widget.bind('<FocusIn>', lambda e: setattr(self, '_active_editor', widget), add='+')
        widget.bind('<<Modified>>', self._editor_modified)
        widget.bind('<KeyRelease>', lambda e: self._schedule_editor_info())
        widget.bind('<ButtonRelease-1>', lambda e: self._schedule_editor_info())
        widget.bind('<Control-z>', lambda e: self._undo(False))
        widget.bind('<Control-y>', lambda e: self._undo(True))
        widget.bind('<Control-Shift-Z>', lambda e: self._undo(True))
        widget.bind('<Control-a>', lambda e: (widget.tag_add('sel', '1.0', 'end-1c'), 'break')[-1])

    def _editor_modified(self, event):
        if event.widget.edit_modified():
            event.widget.edit_modified(False)
            self._schedule_editor_info()

    def _schedule_editor_info(self):
        if self._editor_after:
            self.root.after_cancel(self._editor_after)
        self._editor_after = self.root.after(180, self._refresh_editor_info)

    def _refresh_editor_info(self):
        self._editor_after = None
        try:
            self._update_editor_info()
        except tk.TclError:
            pass  # 定时回调到达时，编辑窗口或主界面可能已经销毁。

    def _update_editor_info(self):
        if not hasattr(self, 'editor_info'):
            return
        raw = self.text.get('1.0', 'end-1c')
        slots = self._dub.get('slots', [])
        names = {s['name'].get().strip()[:12]: i for i, s in enumerate(slots) if s['on'].get()}
        colors = ['#e5f1ff', '#edf6ff', '#dcecff', '#eef7ff', '#e8f3ff', '#d9eaff']
        signature = (self.text, raw, tuple(names.items()))
        if signature != getattr(self, '_editor_highlight_signature', None):
            for tag in self.text.tag_names():
                if tag.startswith('speaker_'):
                    self.text.tag_remove(tag, '1.0', 'end')
            missing, ranges = set(), {}
            paragraphs = 0
            for n, line in enumerate(raw.split('\n'), 1):
                paragraphs += bool(line.strip())
                m = re.match(r'^\s*(?:\[([^\]]+)\]|([^:：\s]{1,12})[:：])', line)
                if not m:
                    continue
                name = (m.group(1) or m.group(2)).strip()
                if m.group(1) is None and name not in names:
                    continue
                tag = f'speaker_{names[name]}' if name in names else 'speaker_missing'
                if name not in names:
                    missing.add(name)
                ranges.setdefault(tag, []).extend((f'{n}.0', f'{n}.{m.end()}'))
            for tag, indices in ranges.items():
                if tag == 'speaker_missing':
                    self.text.tag_configure(tag, underline=True, foreground='#a34100')
                else:
                    self.text.tag_configure(tag, background=colors[int(tag[8:]) % len(colors)])
                for start in range(0, len(indices), 512):
                    self.text.tag_add(tag, *indices[start:start + 512])
            self.text.tag_raise('sel')
            self._editor_highlight_signature = signature
            self._editor_highlight_stats = (paragraphs, missing)
        paragraphs, missing = self._editor_highlight_stats
        line = self._editor_target().get('insert linestart', 'insert lineend')
        m = re.match(r'^\s*(?:\[([^\]]+)\]|([^:：\s]{1,12})[:：])', line)
        role = '默认人声'
        if m and (m.group(1) or (m.group(2) or '').strip() in names):
            role = (m.group(1) or m.group(2)).strip()
        self.editor_role.config(text=f'{len(raw)}字 · {paragraphs}段 · 当前角色：{role}'
                                + (f' · 未绑定：{",".join(sorted(missing))[:45]}' if missing else ''))
        if hasattr(self, '_placeholder_label'):
            self._placeholder_label.place_forget() if raw else self._placeholder_label.place(x=12, y=8)

    def open_editor(self):
        if self._editor_window and self._editor_window.winfo_exists():
            self._editor_window.lift()
            return
        top = tk.Toplevel(self.root)
        top.title('SmartVoice · 文本编辑')
        top.geometry('900x650')
        top.configure(bg=BG)
        self._editor_window = top
        peer = PeerText(top, self.text)
        peer.configure(bg=PANEL, fg=FG, insertbackground=FG, relief='flat', borderwidth=0,
                       highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACCENT)
        # 不放滚动条与工具栏：右键菜单 + 快捷键覆盖全部编辑/试听操作，编辑区占满整窗。
        peer.pack(fill='both', expand=True)
        self._bind_editor(peer)
        self._bind_text_context_menu(peer, editor=True)
        peer.focus_set()

    def _project_payload(self):
        self._sync_dub_cfg()
        params = {}
        for k, var in self._parameter_vars().items():
            try:
                params[k] = var.get()
            except tk.TclError:
                # 个别参数控件值损坏时跳过该键, 保存/比对不被单个坏值卡死
                continue
        return {'text': self.text.get('1.0', 'end-1c'), 'slots': [dict(s) for s in self.dub_cfg],
                'engine': self.engine_var.get(), 'person': self.selected,
                'engine_settings': {'region': self.region_var.get(), 'endpoint': workflow.safe_url(self.ep_var.get())},
                'parameters': params, 'export': self._export_options_safe(), 'segments': list(self._segment_keys),
                'exports': list(self._export_history)}

    def _parameter_vars(self):
        return {'rate': self.rate_var, 'volume': self.vol_var, 'pitch': self.pitch_var,
                'style': self.style_var, 'degree': self.deg_var, 'role': self.role_var}

    def _project_changed(self):
        if self._project_baseline is None:
            return bool(self.text.get('1.0', 'end-1c').strip())
        return self._project_payload() != self._project_baseline

    def _confirm_discard(self):
        try:
            changed = self._project_changed()
        except Exception:
            # 无法判定时按“有修改”处理: 关窗/切换前总给用户保存机会, 窗口绝不会关不掉。
            changed = True
        if not changed:
            return True
        answer = messagebox.askyesnocancel('未保存项目', '项目有修改，是否先保存？')
        if answer is None:
            return False
        return self.save_project() if answer else True

    def save_project(self, save_as=False):
        path = self._project_path
        if save_as or not path:
            storage.PROJECT_DIR.mkdir(parents=True, exist_ok=True)
            path = filedialog.asksaveasfilename(title='保存 SmartVoice 项目', initialdir=str(storage.PROJECT_DIR),
                                               defaultextension='.smartvoice', filetypes=[('SmartVoice项目', '*.smartvoice')])
        if not path:
            return False
        try:
            payload = self._project_payload()
            missing = workflow.save_project(path, payload, self._segment_cache)
            self._project_path, self._project_baseline = path, payload
            if missing:
                self.status.config(text=f'项目已保存（{missing} 段合成缓存缺失，合成后自动补齐）：{path}')
            else:
                self.status.config(text='项目已保存：' + str(path))
            return True
        except Exception as e:
            self._fail(str(e))
            return False

    def open_project(self):
        if self._active_task:
            return self._fail('请先取消或等待当前任务完成，再打开项目')
        if not self._confirm_discard():
            return
        path = filedialog.askopenfilename(filetypes=[('SmartVoice项目', '*.smartvoice')])
        if not path:
            return
        try:
            project = workflow.load_project(path, self._segment_cache)
            # 界面一行未动前先矫正项目导出设置：非法值回落默认，杜绝半应用状态。
            export = project.get('export', {})
            if not isinstance(export, dict):
                export = {}
            if not str(export.get('directory', '')).strip():
                export = dict(export, directory=str(storage.EXPORT_DIR))
            project['export'] = export
            label = project.get('engine')
            if label not in engine.ENGINE_CHOICES:
                raise ValueError('项目引擎不受支持')
            self.engine_var.set(label)
            self.on_engine_switch()
            service = project.get('engine_settings', {})
            self.region_var.set(service.get('region', self.region_var.get()))
            self.ep_var.set(service.get('endpoint', self.ep_var.get()))
            self._load_engine_voices()
            self.selected = project.get('person', '')
            if self.selected not in self.voices:
                self.selected = next(iter(self.voices), '')
            for k, var in self._parameter_vars().items():
                if k in project.get('parameters', {}):
                    var.set(project['parameters'][k])
            self.dub_cfg = self._valid_dub_cfg(project['slots'])
            self.text_content = project['text']
            self._segment_keys = project.get('segments', [])
            self._export_history = project.get('exports', [])
            self._last_export = self._export_history[-1].get('path') if self._export_history else None
            if self._last_pcm:
                self._discard_pcm(self._last_pcm)
                self._last_pcm = None
            self._apply_export_options(project.get('export', {}))
            # PeerText 与 self.text 绑定：_build_ui 会销毁并重建 self.text，
            # 重建前先关编辑窗，防独立编辑窗持有失效 peer。
            if self._editor_window is not None and self._editor_window.winfo_exists():
                self._editor_window.destroy()
            self._editor_window = None
            self._build_ui()
            self._project_path = path
            self._project_baseline = self._project_payload()
            self._retry_plan = None
            self._schedule_editor_info()
            self.status.config(text='已打开项目；命中指纹的片段会直接复用')
        except Exception as e:
            self._fail(str(e))

    def new_project(self):
        if self._active_task or not self._confirm_discard():
            return
        self._set_real_text('')
        self.text.edit_reset()
        self._project_path = None
        self._segment_keys, self._export_history = [], []
        self._last_export = None
        if self._last_pcm:
            self._discard_pcm(self._last_pcm)
            self._last_pcm = None
        self._retry_plan = None
        self._project_baseline = self._project_payload()

    def _export_options(self):
        options = {'directory': self.export_dir.get(), 'name': self.export_name.get(),
                   'format': self.export_format.get().lower(), 'leading_ms': int(self.leading_ms.get()),
                   'trailing_ms': int(self.trailing_ms.get()), 'normalize': bool(self.normalize_var.get())}
        if options['format'] not in ('mp3', 'wav') or not options['directory']:
            raise ValueError('请设置有效输出目录和 MP3/WAV 格式')
        if not all(0 <= options[k] <= 5000 for k in ('leading_ms', 'trailing_ms')):
            raise ValueError('首尾留白范围为0～5000毫秒')
        return options

    def _export_options_safe(self):
        """永不抛异常的导出参数快照：坏值钳位/回退，供保存配置与项目比对使用。"""
        def sget(var, default):
            try:
                v = var.get()
            except tk.TclError:
                return default
            return default if v is None else str(v)

        def iget(var, default):
            try:
                raw = var.get()
            except tk.TclError:
                raw = default
            return cfg_int(raw, default, 0, 5000)

        fmt = sget(self.export_format, 'mp3').lower()
        try:
            norm = cfg_bool(self.normalize_var.get())
        except tk.TclError:
            norm = False
        return {'directory': sget(self.export_dir, ''), 'name': sget(self.export_name, '配音'),
                'format': fmt if fmt in ('mp3', 'wav') else 'mp3',
                'leading_ms': iget(self.leading_ms, 350), 'trailing_ms': iget(self.trailing_ms, 450),
                'normalize': norm}

    def _apply_export_options(self, options):
        if not isinstance(options, dict):
            return
        fmt = str(options.get('format', 'mp3')).lower()
        values = {'directory': str(options.get('directory', '')), 'name': str(options.get('name', '配音')),
                  'format': fmt if fmt in ('mp3', 'wav') else 'mp3',
                  'leading_ms': cfg_int(options.get('leading_ms', 350), 350, 0, 5000),
                  'trailing_ms': cfg_int(options.get('trailing_ms', 450), 450, 0, 5000),
                  'normalize': cfg_bool(options.get('normalize', False))}
        for k, var in (('directory', self.export_dir), ('name', self.export_name),
                       ('format', self.export_format), ('leading_ms', self.leading_ms),
                       ('trailing_ms', self.trailing_ms), ('normalize', self.normalize_var)):
            if k in options:
                var.set(values[k])

    def export_settings(self):
        existing = getattr(self, '_export_win', None)
        if existing is not None and existing.winfo_exists():
            existing.lift()
            return
        top = tk.Toplevel(self.root)
        self._export_win = top
        top.title('SmartVoice · 导出设置')
        top.configure(bg=BG)
        for row, (label, var) in enumerate((('输出目录', self.export_dir), ('文件名', self.export_name),
                                           ('开头留白(ms)', self.leading_ms), ('结尾留白(ms)', self.trailing_ms))):
            ttk.Label(top, text=label).grid(row=row, column=0, padx=8, pady=6)
            ent = ttk.Entry(top, textvariable=var, width=48)
            ent.grid(row=row, column=1, padx=8, pady=6)
            self._bind_entry_context_menu(ent)
        def browse():
            path = filedialog.askdirectory(parent=top)
            if path:
                self.export_dir.set(path)
        ttk.Button(top, text='选择目录', command=browse).grid(row=0, column=2)
        ttk.Combobox(top, textvariable=self.export_format, values=['mp3', 'wav'], state='readonly').grid(row=4, column=1)
        ttk.Checkbutton(top, text='响度均衡（整条音频目标 -18 LUFS / 峰值 -1.5 dBTP）', variable=self.normalize_var).grid(row=5, columnspan=3, padx=8)
        ttk.Label(top, text='均衡会调整整体增益和动态范围，不改变文字、音高或时长；默认关闭。').grid(row=6, columnspan=3, padx=8, pady=8)
        def save():
            try:
                self._export_options()
                self._save_cfg()
                top.destroy()
            except (ValueError, tk.TclError) as e:
                messagebox.showerror('设置无效', str(e), parent=top)
        opened = self._export_options_safe()

        def dismiss():
            # 直接关窗：合法则保留改动，非法则恢复进入对话框时的可用值。
            try:
                self._export_options()
            except (ValueError, tk.TclError):
                self._apply_export_options(opened)
            top.destroy()
        top.protocol('WM_DELETE_WINDOW', dismiss)
        ttk.Button(top, text='保存', command=save).grid(row=7, column=1, pady=8)

    def open_last_export(self):
        if self._last_export and Path(self._last_export).is_file():
            try:
                os.startfile(self._last_export)
            except OSError as e:
                messagebox.showerror('打开音频', f'无法打开文件：{e}')
        else:
            messagebox.showinfo('打开音频', '当前会话还没有生成音频')

    def toggle_pause(self):
        if self.player.opened:
            self.player.toggle_pause()

    def replay(self):
        if self._active_task:
            return
        if not self._last_pcm or not Path(self._last_pcm).is_file():
            return messagebox.showinfo('播放', '没有可播放的音频，请先合成')
        self._preempt()
        self._playing_pcm = self._last_pcm
        try:
            self.player.play_file(self._last_pcm)
        except Exception as e:
            self._playing_pcm = None
            return messagebox.showerror('播放失败', str(e))
        self._watch_play(self._seq)

    def component_manager(self):
        import components
        import queue
        import threading
        existing = getattr(self, '_component_win', None)
        if existing is not None and existing.winfo_exists():
            existing.lift()
            return
        top = tk.Toplevel(self.root)
        self._component_win = top
        top.title('SmartVoice · 增强组件')
        top.geometry('650x300')
        top.resizable(False, False)
        top.configure(bg=BG)
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
        poll()

    def copy_diagnostics(self):
        data = self._diag.snapshot() if self._diag else {'software': appmeta.NAME, 'version': appmeta.VERSION, 'state': '尚无合成任务'}
        self.root.clipboard_clear()
        self.root.clipboard_append(json.dumps(data, ensure_ascii=False, indent=2))
        self.status.config(text='已复制脱敏诊断（不含Key、Token、原文或音频）')

    def export_diagnostics(self):
        path = filedialog.asksaveasfilename(defaultextension='.zip', filetypes=[('问题报告', '*.zip')])
        if not path:
            return
        include_text = messagebox.askyesno('可选附件', '是否将当前原文加入报告？默认脱敏诊断无需原文。', default='no')
        include_audio = messagebox.askyesno('可选附件', '是否将最后导出的音频加入报告？', default='no') if self._last_export else False
        import io
        memory = io.BytesIO()
        with zipfile.ZipFile(memory, 'w', compression=zipfile.ZIP_DEFLATED) as z:
            data = self._diag.snapshot() if self._diag else {'software': appmeta.NAME, 'version': appmeta.VERSION}
            z.writestr('diagnostics.json', json.dumps(data, ensure_ascii=False, indent=2))
            if include_text:
                z.writestr('text.txt', self.text.get('1.0', 'end-1c'))
            if include_audio and Path(self._last_export).is_file():
                z.write(self._last_export, 'audio' + Path(self._last_export).suffix)
        storage.atomic_bytes(path, memory.getvalue())
        self.status.config(text='问题报告已导出；不会自动上传')

    def open_data_dir(self):
        storage.DATA_DIR.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(storage.DATA_DIR)
        except OSError as e:
            messagebox.showerror('打开数据目录', f'无法打开：{e}')

    def check_updates(self):
        if self._active_task:
            return messagebox.showinfo('任务运行中', '请在任务完成后检查更新')
        def work():
            import requests
            with requests.get(appmeta.UPDATE_API, headers={'Accept': 'application/vnd.github+json'}, timeout=(5, 15)) as r:
                if r.status_code == 404:
                    return lambda: messagebox.showinfo('检查更新', '仓库尚未发布公开版本，或仓库当前不可公开访问。')
                r.raise_for_status()
                release = r.json()
            tag = str(release.get('tag_name', '')).lstrip('v')
            def version(s):
                if not re.fullmatch(r'\d+\.\d+\.\d+', s):
                    return ()
                return tuple(map(int, s.split('.')))
            newer = version(tag) > version(appmeta.VERSION)
            def done():
                if newer and messagebox.askyesno('发现新版本', f'当前 {appmeta.VERSION}，最新 {tag}，打开官方下载页？'):
                    webbrowser.open(appmeta.RELEASES_URL)
                elif not newer:
                    messagebox.showinfo('检查更新', f'当前版本 {appmeta.VERSION}，未发现更高的稳定版本。')
            return done
        self._bg(work)
