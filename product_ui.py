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
from export_options import coerce_export_options, validate_export_options
from ui.dialogs import open_component_manager, open_export_settings
from ui.menus import build_product_menu
from theme import ACCENT, BG, BORDER, FG, PANEL, ROLE, SEL, WARN
import theme


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
                       '-maxundo', 5000, '-autoseparators', True,
                       '-font', source.cget('font'), '-padx', 10, '-pady', 8)


class ProductUI:
    def _clamp_wh(self, w, h, margin=80):
        """初始几何钳位到屏幕：高 DPI 下物理放大后底部工具条不被裁掉。
        不声明 DPI 感知（无多 DPI 测试机，不动渲染行为），只防裁剪。"""
        try:
            sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
            return max(480, min(w, sw - margin)), max(480, min(h, sh - margin))
        except tk.TclError:
            return w, h

    def _clamp_minh(self, h, margin=80):
        try:
            return max(480, min(h, self.root.winfo_screenheight() - margin))
        except tk.TclError:
            return h

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
        self.export_dir = tk.StringVar(value=str(export.get('directory', str(storage.default_export_dir()))))
        self.export_name = tk.StringVar(value=str(export.get('name', '配音')))
        fmt = str(export.get('format', 'mp3')).lower()
        self.export_format = tk.StringVar(value=fmt if fmt in ('mp3', 'wav') else 'mp3')
        self.leading_ms = tk.IntVar(value=cfg_int(export.get('leading_ms', 350), 350, 0, 5000))
        self.trailing_ms = tk.IntVar(value=cfg_int(export.get('trailing_ms', 450), 450, 0, 5000))
        self.normalize_var = tk.BooleanVar(value=cfg_bool(export.get('normalize', False)))

    def _build_product_menu(self):
        build_product_menu(self)

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

        def _clear_empty_selection(event=None):
            # 空文本拖选会给空白行挂 sel 标蓝：widget 绑定先于 Text 类绑定执行，
            # after_idle 延后到类绑定挂完 sel 之后再清除。
            try:
                if not widget.get('1.0', 'end-1c'):
                    widget.after_idle(_do_clear)
            except tk.TclError:
                pass

        def _do_clear():
            try:
                widget.tag_remove('sel', '1.0', 'end')
            except tk.TclError:
                pass
        widget.bind('<B1-Motion>', _clear_empty_selection, add='+')
        widget.bind('<ButtonRelease-1>', _clear_empty_selection, add='+')
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
            try:
                self.root.after_cancel(self._editor_after)
            except tk.TclError:
                pass
        self._editor_after = self.root.after(180, self._refresh_editor_info)

    def _refresh_editor_info(self):
        self._editor_after = None
        try:
            self._update_editor_info()
        except tk.TclError:
            pass  # 定时回调到达时，编辑窗口或主界面可能已经销毁。

    @staticmethod
    def _damage_range(old, new):
        """新旧序列 diff 出损伤区；返回 (start, end_old, end_new)，新区为新序列半区。
        既用于文本行定区，也用于标注比对。Tk 标签随编辑自动移位，区外天然正确。"""
        n = min(len(old), len(new))
        start = 0
        while start < n and old[start] == new[start]:
            start += 1
        end_old, end_new = len(old), len(new)
        while end_old > start and end_new > start and old[end_old - 1] == new[end_new - 1]:
            end_old -= 1
            end_new -= 1
        return start, end_old, end_new

    @staticmethod
    def _parse_speaker_lines(lines, names):
        """逐行求标注四元组 (tag|None, end|None, name|None, isempty)，供统计与打标签共用。"""
        infos = []
        for line in lines:
            isempty = not line.strip()
            m = re.match(r'^\s*(?:\[([^\]]+)\]|([^:：\s]{1,12})[:：])', line)
            if not m:
                infos.append((None, None, None, isempty))
                continue
            name = (m.group(1) or m.group(2)).strip()
            if m.group(1) is None and name not in names:
                infos.append((None, None, None, isempty))
                continue
            tag = f'speaker_{names[name]}' if name in names else 'speaker_missing'
            infos.append((tag, m.end(), name if tag == 'speaker_missing' else None, isempty))
        return infos

    def _update_editor_info(self):
        if not hasattr(self, 'editor_info'):
            return
        raw = self.text.get('1.0', 'end-1c')
        slots = self._dub.get('slots', [])
        names = {s['name'].get().strip()[:12]: i for i, s in enumerate(slots) if s['on'].get()}
        colors = ROLE
        signature = (self.text, raw, tuple(names.items()))
        if signature != getattr(self, '_editor_highlight_signature', None):
            new_lines = raw.split('\n')
            cached = getattr(self, '_editor_highlight_cache', None)
            if cached is None or cached[0] is not self.text or cached[1] != tuple(names.items()):
                infos = self._parse_speaker_lines(new_lines, names)
                merged, region = infos, (0, len(new_lines))
            else:
                start, end_old, end_new = self._damage_range(cached[2], new_lines)
                region_infos = self._parse_speaker_lines(new_lines[start:end_new], names)
                if region_infos == cached[3][start:end_old]:
                    # 损伤区标注语义无变化（如旁白行内打字）：复用旧标注，零 Tcl、零统计。
                    merged, region = cached[3], None
                    paragraphs, missing = cached[4]
                else:
                    merged = cached[3][:start] + region_infos + cached[3][end_old:]
                    region = (start, end_new)
            if region is not None:
                paragraphs = sum(1 for e in merged if e[3])
                missing = {e[2] for e in merged if e[0] == 'speaker_missing'}
                start, end_new = region
                if end_new > start:
                    for tag in self.text.tag_names():
                        if tag.startswith('speaker_'):
                            self.text.tag_remove(tag, f'{start + 1}.0', f'{end_new}.end')
                    ranges = {}
                    for n, info in enumerate(merged[start:end_new], start + 1):
                        if info[0] is None:
                            continue
                        tag, end_col = info[0], info[1]
                        ranges.setdefault(tag, []).extend((f'{n}.0', f'{n}.{end_col}'))
                    for tag, indices in ranges.items():
                        if tag == 'speaker_missing':
                            self.text.tag_configure(tag, underline=True, foreground=WARN)
                        else:
                            self.text.tag_configure(tag, background=colors[int(tag[8:]) % len(colors)])
                        for at in range(0, len(indices), 512):
                            self.text.tag_add(tag, *indices[at:at + 512])
                    self.text.tag_raise('sel')
            self._editor_highlight_signature = signature
            self._editor_highlight_stats = (paragraphs, missing)
            self._editor_highlight_cache = (self.text, tuple(names.items()), new_lines, merged,
                                            (paragraphs, missing))
        paragraphs, missing = self._editor_highlight_stats
        line = self._editor_target().get('insert linestart', 'insert lineend')
        m = re.match(r'^\s*(?:\[([^\]]+)\]|([^:：\s]{1,12})[:：])', line)
        role = '默认人声'
        if m and (m.group(1) or (m.group(2) or '').strip() in names):
            role = (m.group(1) or m.group(2)).strip()
        self.editor_role.config(text=f'{len(raw)}字 · {paragraphs}段 · 当前角色：{role}'
                                + (f' · 未绑定：{",".join(sorted(missing))[:45]}' if missing else ''))
        if hasattr(self, '_placeholder_label'):
            if raw:
                self._placeholder_label.place_forget()
            else:
                self._placeholder_label.place(x=0, y=0, relwidth=1, relheight=1)

    def open_editor(self):
        if self._editor_window and self._editor_window.winfo_exists():
            self._editor_window.lift()
            return
        top = tk.Toplevel(self.root)
        top.title('SmartVoice · 文本编辑')
        top.transient(self.root)
        w, h = self._clamp_wh(900, 650)
        top.geometry(f'{w}x{h}')
        top.configure(bg=BG)
        self._editor_window = top
        peer = PeerText(top, self.text)
        peer.configure(bg=PANEL, fg=FG, insertbackground=FG, relief='flat', borderwidth=0,
                       highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACCENT)
        # 不放滚动条与工具栏：右键菜单 + 快捷键覆盖全部编辑/试听操作，编辑区占满整窗。
        peer.pack(fill='both', expand=True)
        self._bind_editor(peer)
        self._bind_text_context_menu(peer, editor=True)
        top.protocol('WM_DELETE_WINDOW', top.destroy)
        peer.bind('<Escape>', lambda e: top.destroy())
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
                export = dict(export, directory=str(storage.default_export_dir()))
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
        return validate_export_options(self.export_dir.get(), self.export_name.get(),
                                       self.export_format.get(), self.leading_ms.get(),
                                       self.trailing_ms.get(), self.normalize_var.get())

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
        return coerce_export_options({'directory': sget(self.export_dir, ''),
                                      'name': sget(self.export_name, '配音'), 'format': fmt,
                                      'leading_ms': iget(self.leading_ms, 350),
                                      'trailing_ms': iget(self.trailing_ms, 450),
                                      'normalize': norm})

    def _apply_export_options(self, options):
        if not isinstance(options, dict):
            return
        values = coerce_export_options(options)
        for k, var in (('directory', self.export_dir), ('name', self.export_name),
                       ('format', self.export_format), ('leading_ms', self.leading_ms),
                       ('trailing_ms', self.trailing_ms), ('normalize', self.normalize_var)):
            if k in options:
                var.set(values[k])

    def export_settings(self):
        open_export_settings(self)

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
        else:
            self.status.config(text='当前没有播放，无需暂停')

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
        open_component_manager(self)

    def copy_diagnostics(self):
        data = self._diag.snapshot() if self._diag else {'software': appmeta.NAME, 'version': appmeta.VERSION, 'state': '尚无合成任务'}
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(json.dumps(data, ensure_ascii=False, indent=2))
        except tk.TclError:
            self.status.config(text='剪贴板被占用，复制失败')
            return
        self.status.config(text='已复制脱敏诊断（不含Key、Token、原文或音频）')

    def export_diagnostics(self):
        path = filedialog.asksaveasfilename(defaultextension='.zip', filetypes=[('问题报告', '*.zip')])
        if not path:
            return
        include_text = messagebox.askyesno('可选附件', '是否将当前原文加入报告？默认脱敏诊断无需原文。', default='no')
        include_audio = messagebox.askyesno('可选附件', '是否将最后导出的音频加入报告？', default='no') if self._last_export else False
        import io
        text = self.text.get('1.0', 'end-1c') if include_text else None
        audio = str(self._last_export) if include_audio and Path(self._last_export).is_file() else None
        diag = self._diag.snapshot() if self._diag else {'software': appmeta.NAME, 'version': appmeta.VERSION}
        def work():
            memory = io.BytesIO()
            with zipfile.ZipFile(memory, 'w', compression=zipfile.ZIP_DEFLATED) as z:
                z.writestr('diagnostics.json', json.dumps(diag, ensure_ascii=False, indent=2))
                if text is not None:
                    z.writestr('text.txt', text)
                if audio is not None:
                    z.write(audio, 'audio' + Path(audio).suffix)
            storage.atomic_bytes(path, memory.getvalue())
            return lambda: self.status.config(text='问题报告已导出；不会自动上传')
        self._bg(work)

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
