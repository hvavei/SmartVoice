import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import engine
import storage
import voice_tasks as workflow


class StorageTests(unittest.TestCase):
    def test_dpapi_settings_round_trip_and_forget(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(storage, 'DATA_DIR', Path(tmp)):
            cfg = {'engine': 'Azure(填Key)', 'remember_key': True, 'key': 'secret-123',
                   'engine_profiles': {'azure': {'key': 'secret-123', 'region': 'eastus'}}}
            protected = storage.protect_settings(cfg)
            self.assertNotIn('secret-123', json.dumps(protected))
            self.assertNotIn('secret-123', (Path(tmp)/'credentials.json').read_text(encoding='utf-8'))
            self.assertEqual(storage.unlock_settings(protected)['key'], 'secret-123')
            cfg['remember_key'] = False
            self.assertEqual(storage.unlock_settings(storage.protect_settings(cfg))['key'], '')

    def test_unremembered_key_clears_vault(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(storage, 'DATA_DIR', Path(tmp)):
            cfg = {'engine': 'Azure(填Key)', 'remember_key': True, 'key': 's1',
                   'engine_profiles': {'azure': {'key': 's1', 'region': 'eastus'}}}
            storage.protect_settings(cfg)
            self.assertIn('azure', storage.read_json(Path(tmp)/'credentials.json'))
            cfg2 = {'engine': 'Azure(填Key)', 'remember_key': False, 'key': '',
                    'engine_profiles': {'azure': {'key': '', 'region': 'eastus',
                                                 'credential_ref': 'azure'}}}
            out = storage.protect_settings(cfg2)
            self.assertEqual(storage.read_json(Path(tmp)/'credentials.json'), {})
            self.assertNotIn('credential_ref', out['engine_profiles']['azure'])
            self.assertEqual(storage.unlock_settings(out)['key'], '')

    def test_unique_export_never_overwrites_existing(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = storage.unique_export(tmp, '成品', 'wav', b'first')
            second = storage.unique_export(tmp, '成品', 'wav', b'second')
            self.assertNotEqual(first, second)
            self.assertEqual(Path(first).read_bytes(), b'first')
            self.assertEqual(Path(second).read_bytes(), b'second')
            self.assertEqual(len(list(Path(tmp).iterdir())), 2)

    def test_unique_export_on_filesystem_without_hardlinks(self):
        import os
        if os.name != 'nt':
            self.skipTest('Windows atomic rename fallback')
        with tempfile.TemporaryDirectory() as tmp, patch('storage.os.link', side_effect=OSError('unsupported')):
            path = storage.unique_export(tmp, '成品', 'mp3', b'one')
            path2 = storage.unique_export(tmp, '成品', 'mp3', b'two')
            self.assertNotEqual(path, path2)
            self.assertEqual(Path(path).read_bytes(), b'one')
            self.assertEqual(Path(path2).read_bytes(), b'two')

    def test_long_path_helper(self):
        import os
        if os.name != 'nt':
            self.skipTest('Windows long paths')
        short = r'C:\SmartVoice\out.mp3'
        self.assertEqual(storage.long_path(short), short)  # 未超限原样返回
        self.assertEqual(storage.long_path('relative\\out.mp3'), 'relative\\out.mp3')
        long_abs = 'C:\\' + '深目录\\' * 80 + 'out.mp3'
        converted = storage.long_path(long_abs)
        self.assertTrue(converted.startswith('\\\\?\\'))
        self.assertNotIn('/', converted)
        already = '\\\\?\\C:\\x.mp3'
        self.assertEqual(storage.long_path(already), already)
        unc = '\\\\srv\\share\\' + 'd' * 240 + '.mp3'
        self.assertTrue(storage.long_path(unc).startswith('\\\\?\\UNC\\'))

    def test_long_path_roundtrip(self):
        import os
        if os.name != 'nt':
            self.skipTest('Windows long paths')
        with tempfile.TemporaryDirectory() as tmp:
            deep = Path(tmp)
            while len(str(deep)) < 280:
                deep = deep / 'subdir-deep-name'
            Path(storage.long_path(deep)).mkdir(parents=True, exist_ok=True)
            cfg_path = deep / 'settings.json'
            storage.atomic_json(cfg_path, {'v': 1})
            self.assertEqual(storage.read_json(cfg_path), {'v': 1})
            out = storage.unique_export(deep, '成品', 'wav', b'data')
            self.assertFalse(out.startswith('\\\\?\\'))  # 返回短形态：explorer 可用
            self.assertEqual(Path(out).read_bytes(), b'data')

    def test_fingerprint_reuse_and_parameter_invalidation(self):
        snap = {'engine': 'Azure(填Key)', 'key': 'secret', 'rate': '100%'}
        key = workflow.fingerprint('原文', 'voice', snap)
        self.assertEqual(key, workflow.fingerprint('原文', 'voice', dict(snap, person='显示名称')))
        for changed in (dict(snap, rate='120%'), dict(snap, key='other')):
            self.assertNotEqual(key, workflow.fingerprint('原文', 'voice', changed))
        self.assertNotIn('secret', key)
        a = dict(snap, ep='http://localhost:8000/v1?deployment=a')
        b = dict(snap, ep='http://localhost:8001/v1?deployment=a')
        c = dict(snap, ep='http://localhost:8000/v1?deployment=b')
        self.assertEqual(len({workflow.fingerprint('原文', 'voice', s) for s in (a, b, c)}), 3)
        self.assertEqual(workflow.safe_url('http://user:pass@localhost:8000/v1?token=secret'), 'http://localhost:8000/v1')

    def test_project_restores_text_bindings_audio_without_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = workflow.SegmentCache(Path(tmp)/'cache')
            key = workflow.fingerprint('原稿', 'voice', {})
            cache.save(key, b'audio')
            project = {'text': '原稿', 'slots': [{'name': '旁白', 'voice': 'voice', 'on': True}],
                       'parameters': {'rate': 100}, 'segments': [key], 'exports': []}
            path = Path(tmp)/'test.smartvoice'
            workflow.save_project(path, project, cache)
            target = workflow.SegmentCache(Path(tmp)/'restored')
            loaded = workflow.load_project(path, target)
            self.assertEqual(loaded['text'], '原稿')
            self.assertEqual(loaded['slots'], project['slots'])
            self.assertEqual(target.load(key), b'audio')
            (target.root/f'{key}.audio').write_bytes(b'corrupted')
            self.assertIsNone(target.load(key))

    def test_diagnostics_whitelist_redacts_secrets_and_original(self):
        text = '客户私密文稿'
        d = workflow.Diagnostics({'engine': 'Azure', 'key': 'secret-123', 'voice': 'v'}, text, 1)
        d.event('request', request_id='secret-123', http_status=200, text=text, key='secret-123')
        raw = json.dumps(d.snapshot(), ensure_ascii=False)
        self.assertNotIn(text, raw)
        self.assertNotIn('secret-123', raw)
        self.assertIn('200', raw)

    def test_cancel_closes_response_and_interrupts_retry(self):
        token = workflow.Cancellation()
        closed = threading.Event()
        response = Mock()
        response.close.side_effect = closed.set
        token.register(response)
        token.cancel()
        self.assertTrue(closed.wait(1))
        with workflow.network_context(token), self.assertRaises(workflow.Cancelled):
            workflow.retry_wait(10)

    def test_network_context_captures_http_and_trace_without_credentials(self):
        token = workflow.Cancellation()
        response = Mock(status_code=200, headers={'X-Microsoft-RequestId': 'trace-001'})
        response.iter_content.return_value = [b'audio']
        session = Mock()
        session.post.return_value = response
        d = workflow.Diagnostics({'engine': 'Azure', 'key': 'private'}, 'private text', 1)
        with workflow.network_context(token, d, 1):
            r = workflow.Session(session).post('https://example.com', headers={'Key': 'private'}, data='private text')
            self.assertEqual(b''.join(r.iter_content()), b'audio')
            self.assertIn(response, token.responses)
            r.close()
        self.assertFalse(token.responses)
        row = d.snapshot()['events'][0]
        self.assertEqual(row['http_status'], 200)
        self.assertEqual(row['request_id'], 'trace-001')
        self.assertNotIn('private', json.dumps(d.snapshot()))

    def test_capabilities_not_inferred_from_neural_suffix(self):
        records = {'zh-CN-OfficialVoice': {'VoiceType': 'Neural', 'StyleList': ['calm'], 'RolePlayList': ['Girl']},
                   'zh-CN-Lan:MAI-Voice-2-Flash': {'VoiceType': 'Neural'}}
        with patch.object(engine, 'azure_voice_metadata', return_value=records):
            cap = engine.voice_capabilities('zh-CN-OfficialVoice')
            self.assertTrue(cap['phoneme'])
            self.assertEqual(cap['styles'], ['calm'])
            self.assertFalse(engine.voice_capabilities('zh-CN-Lan:MAI-Voice-2-Flash')['pitch'])
            snap = {'engine': 'Azure(填Key)', 'style': 'calm', 'role': 'Girl'}
            engine.validate_voice_parameters('zh-CN-OfficialVoice', snap)
            with self.assertRaises(ValueError):
                engine.validate_voice_parameters('zh-CN-Lan:MAI-Voice-2-Flash', snap)


class ProductGuiTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        from studio_gui import App
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [patch.object(engine, 'load_json', return_value={}), patch.object(engine, 'save_json'),
                        patch('studio_gui.messagebox.askokcancel', return_value=True),
                        patch('studio_gui.messagebox.askyesnocancel', return_value=False),
                        patch('studio_gui.messagebox.showerror'), patch('studio_gui.messagebox.showinfo'),
                        patch.object(storage, 'LOG_DIR', Path(self.tmp.name)/'logs')]
        for p in self.patches:
            p.start()
        self.root = tk.Tk()
        self.a = App(self.root)
        self.a._segment_cache = workflow.SegmentCache(Path(self.tmp.name)/'cache')
        self.a.export_dir.set(self.tmp.name)
        self.a.key_var.set('test-secret')
        self.root.update()

    def tearDown(self):
        self.a.on_close()
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_format_undo_redo_preserves_role_bindings(self):
        a = self.a
        raw = '小明说。“大家好。”'
        a._set_real_text(raw)
        a.text.edit_reset()
        a._dub['slots'][0]['name'].set('解说员')
        before = a._dub_cfg_snapshot().copy()
        a.on_format_dialogue_lines()
        # 叙述绑定第一个启用槽位；引号对话原样保留（默认人声），不发明 [对话] 标签
        expected = '解说员:小明说。\n“大家好。”'
        self.assertEqual(a._get_real_text(), expected)
        self.assertEqual(a._dub_cfg_snapshot(), before)
        a.text.edit_undo()
        self.assertEqual(a._get_real_text(), raw)
        a.text.edit_redo()
        self.assertEqual(a._get_real_text(), expected)

    def test_format_keeps_unbound_dialogue_on_default_voice(self):
        a = self.a
        a._set_real_text('“走吧。”')
        a.on_format_dialogue_lines()
        self.assertEqual(a._get_real_text(), '“走吧。”')
        self.assertIn('无需调整', str(a.status.cget('text')))

    def test_main_text_wraps_words_to_box_width(self):
        self.assertEqual(str(self.a.text.cget('wrap')), 'word')

    def test_editor_info_binds_only_slot_roles(self):
        a = self.a
        a._dub['slots'][0]['name'].set('旁白')
        a._set_real_text('旁白:你好\n他说：走吧\n[未知角色]台词')
        a.text.mark_set('insert', '1.0')
        a._refresh_editor_info()
        info = str(a.editor_role.cget('text'))
        self.assertIn('当前角色：旁白', info)
        self.assertIn('未知角色', info)   # 显式括号角色未绑定要报警
        self.assertNotIn('他说', info)    # 普通冒号行不当角色、不报警
        a.text.mark_set('insert', '2.0')
        a._refresh_editor_info()
        self.assertIn('当前角色：默认人声', str(a.editor_role.cget('text')))
        self.assertEqual([t for t in a.text.tag_names('2.0') if t.startswith('speaker_')], [])
        self.assertEqual([t for t in a.text.tag_names('1.0') if t.startswith('speaker_')], ['speaker_0'])

    def test_editor_window_has_no_scrollbar_but_wheel_still_scrolls(self):
        a = self.a
        a.open_editor()
        import tkinter.ttk as ttk
        from product_ui import PeerText
        peer = next(w for w in a._editor_window.winfo_children() if isinstance(w, PeerText))
        self.assertEqual([w for w in a._editor_window.winfo_children() if isinstance(w, ttk.Scrollbar)], [])
        self.assertTrue(str(peer.bind_class('Text', '<MouseWheel>')).strip())
        self.root.update()
        peer.delete('1.0', 'end')
        peer.insert('1.0', '\n'.join(f'第{i}行' for i in range(500)))
        self.root.update()
        before = peer.yview()
        peer.event_generate('<MouseWheel>', delta=-240, x=peer.winfo_rootx() + 5, y=peer.winfo_rooty() + 5)
        self.root.update()
        self.assertNotEqual(peer.yview(), before)

    def test_peer_editor_shares_content_and_undo(self):
        a = self.a
        a._set_real_text('原稿')
        a.text.edit_reset()
        a.open_editor()
        from product_ui import PeerText
        peer = next(w for w in a._editor_window.winfo_children() if isinstance(w, PeerText))
        peer.insert('end', '新增')
        self.assertEqual(a._get_real_text(), '原稿新增')
        peer.edit_undo()
        self.assertEqual(a._get_real_text(), '原稿')

    def test_editor_window_uses_warm_theme(self):
        from studio_gui import BG, FG, PANEL
        a = self.a
        a.open_editor()
        from product_ui import PeerText
        peer = next(w for w in a._editor_window.winfo_children() if isinstance(w, PeerText))
        self.assertEqual(str(a._editor_window.cget('bg')), BG)
        self.assertEqual(str(peer.cget('bg')), PANEL)
        self.assertEqual(str(peer.cget('fg')), FG)

    def test_failed_segment_retries_only_missing_audio(self):
        a = self.a
        seq = a._seq
        snap = a.snapshot()
        jobs = [(snap['voice'], '角色', '第一段'), (snap['voice'], '角色', '第二段')]
        seen = []
        def synth(text, *args, **kwargs):
            seen.append(text)
            if text == '第二段':
                time.sleep(.3)      # 第一段必须先完成落盘，才谈得上“只重试缺失段”
                raise RuntimeError('network failed')
            return b'first'
        with patch.object(a, 'synth_net', side_effect=synth):
            with self.assertRaises(RuntimeError):
                a._synth_jobs(seq, jobs, snap)
        with patch.object(a, 'synth_net', return_value=b'second') as synth:
            audio = a._synth_jobs(seq, jobs, snap)
        self.assertEqual(audio, [b'first', b'second'])
        self.assertEqual(synth.call_count, 1)
        self.assertEqual(synth.call_args.args[0], '第二段')

    def test_active_task_repeated_clicks_do_not_enqueue(self):
        a = self.a
        a._active_task = True
        with patch.object(a, '_bg') as bg:
            a.on_single()
            a.on_multi()
            a.on_audition()
            bg.assert_not_called()

    def test_synth_button_is_synthesize_cancel_toggle(self):
        a = self.a
        self.assertEqual(str(a.b_single.cget('text')), '合成/取消')
        a._active_task = True
        with patch.object(a, 'on_stop') as stop, patch.object(a, '_bg') as bg:
            a.on_single()
        stop.assert_called_once()          # 运行中再点按钮 = 取消
        bg.assert_not_called()
        a._active_task = False

    def test_stop_keeps_seq_cancels_token_and_marks_stopping(self):
        a = self.a
        a._active_task = True
        seq, token = a._seq, a._task_token
        a.on_stop()
        self.assertEqual(a._seq, seq)                       # 不作废序号：已完成片段照常输出
        self.assertTrue(token.cancelled)                    # 在途请求收到中止信号
        self.assertTrue(a._stopping)
        self.assertIn('已完成的片段', str(a.status.cget('text')))
        a._active_task = False

    def test_cancelled_synth_keeps_completed_segments_serial(self):
        a = self.a
        snap = {'engine': 'Edge免费(免Key)'}                 # 非 Azure 走串行路径
        jobs = [('voice', 'role', str(i)) for i in range(3)]
        def synth(text, *args, **kwargs):
            if text == '1':
                raise workflow.Cancelled('任务已取消；已完成片段已保留')
            return text.encode()
        with patch.object(a, 'synth_net', side_effect=synth) as net:
            result = a._synth_jobs(a._seq, jobs, snap)
        self.assertEqual(result, [b'0', None, None])        # 已合成的保留，未完成的留空
        self.assertEqual(net.call_count, 2)                 # 取消后不再提交新段

    def test_cancelled_parallel_synth_harvests_finished_segment(self):
        a = self.a
        snap = {'engine': 'Azure(填Key)'}
        jobs = [('voice', 'role', '0'), ('voice', 'role', '1')]
        def synth(text, *args, **kwargs):
            if text == '1':
                time.sleep(.3)      # 给第0段留足合成+落盘时间，收割窗口才有意义
                raise workflow.Cancelled('任务已取消；已完成片段已保留')
            return b'0'
        with patch.object(a, 'synth_net', side_effect=synth):
            result = a._synth_jobs(a._seq, jobs, snap)
        self.assertEqual(result, [b'0', None])

    def test_stop_mid_task_exports_completed_parts(self):
        import wave
        a = self.a
        a._dub_all(False)
        a._dub['slots'][0]['name'].set('甲')
        a._dub['slots'][1]['name'].set('乙')
        a._dub['slots'][0]['on'].set(True)
        a._dub['slots'][1]['on'].set(True)
        a._set_real_text('[甲]你好\n[乙]大家好')
        buffer = io.BytesIO()
        with wave.open(buffer, 'wb') as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(24000)
            wav.writeframes(b'\0\0' * 2400)
        pcm = buffer.getvalue()
        second = threading.Event()
        release = threading.Event()
        calls = []
        def synth(text, *args, **kwargs):
            calls.append(text)
            if len(calls) == 1:
                return pcm
            second.set()
            release.wait(timeout=5)
            raise workflow.Cancelled('任务已取消；已完成片段已保留')
        with patch.object(a, 'synth_net', side_effect=synth), \
                patch.object(a.player, 'play_file'), patch.object(a, '_watch_play'):
            a.on_multi()
            self.assertTrue(second.wait(timeout=3))
            a.on_stop()                                      # 合成/取消按钮语义：不作废序号
            time.sleep(.3)                                   # 已完成段先落盘，收割窗口才不漏
            release.set()
            deadline = time.monotonic() + 8
            while a._active_task and time.monotonic() < deadline:
                self.root.update()
                time.sleep(.02)
        self.assertFalse(a._active_task)
        self.assertTrue(a._last_export and Path(a._last_export).is_file())
        self.assertGreater(Path(a._last_export).stat().st_size, 100)
        status = str(a.status.cget('text'))
        self.assertTrue(status.startswith('已停止 · 已输出完成片段'), status)

    def test_menubar_has_four_menus_and_task_entries(self):
        a = self.a
        import tkinter as tk
        menubar = a.root.nametowidget(a.root.cget('menu'))
        labels = [menubar.entrycget(i, 'label') for i in range(menubar.index('end') + 1)]
        self.assertEqual(labels, ['选项', '组件', '主题', '关于'])
        project = menubar.nametowidget(menubar.entrycget(0, 'menu'))
        entries = [project.entrycget(i, 'label') for i in range(project.index('end') + 1)
                   if project.type(i) == 'command']
        self.assertNotIn('重试未完成片段', entries)
        self.assertNotIn('清除试听缓存', entries)
        self.assertIn('导出设置…', entries)
        self.assertIn(str(a._font_bold()[1]), str(project.cget('font')))  # 菜单字号=功能区标题
        comp = menubar.nametowidget(menubar.entrycget(1, 'menu'))
        self.assertEqual(comp.index('end'), 0)
        self.assertEqual(comp.type(0), 'command')
        themes = menubar.nametowidget(menubar.entrycget(2, 'menu'))
        theme_labels = [themes.entrycget(i, 'label') for i in range(themes.index('end') + 1)]
        self.assertEqual(theme_labels, ['暖白·初', '雾蓝'])
        self.assertIn(str(a._font_bold()[1]), str(themes.cget('font')))

    def test_output_button_points_to_synthesized_file(self):
        import os
        import subprocess
        import tempfile
        a = self.a
        tmp = Path(tempfile.mkdtemp())
        made = tmp / '配音.mp3'
        made.write_bytes(b'x')
        a._last_export = str(made)
        calls = []
        orig_popen, orig_start = subprocess.Popen, os.startfile
        subprocess.Popen = lambda args, **kw: calls.append(list(args))
        os.startfile = lambda p: calls.append(['startfile', p])
        try:
            a.open_out()                       # 有成品：资源管理器选中该文件
            self.assertEqual(calls[0][:2], ['explorer', '/select,'])
            self.assertTrue(Path(calls[0][2]).is_file())
            calls.clear()
            a._last_export = str(tmp / 'missing.mp3')
            a.export_dir.set(str(tmp))         # 无成品：回退打开输出目录
            a.open_out()
        finally:
            subprocess.Popen, os.startfile = orig_popen, orig_start
        self.assertEqual(calls, [['startfile', str(tmp)]])

    def test_bad_export_settings_never_break_save_payload_or_dialog(self):
        import tkinter as tk
        from product_ui import cfg_bool, cfg_int
        a = self.a
        self.assertEqual(cfg_int('abc', 350, 0, 5000), 350)
        self.assertEqual(cfg_int(None, 350, 0, 5000), 350)
        self.assertEqual(cfg_int(99999, 350, 0, 5000), 5000)
        self.assertEqual(cfg_int(-5, 350, 0, 5000), 0)
        self.assertFalse(cfg_bool('false'))
        self.assertTrue(cfg_bool('TRUE'))
        a.export_dir.set('')                    # 非法：输出目录为空
        a.leading_ms.set(99999)                 # 非法：越界留白
        with self.assertRaises(ValueError):
            a._export_options()                 # 严格版供导出设置/合成入口明确报错
        safe = a._export_options_safe()         # 安全版永不抛
        self.assertEqual(safe['leading_ms'], 5000)
        self.assertEqual(safe['format'], 'mp3')
        a._save_cfg()                           # 配置暂存不再被导出设置卡死
        self.assertEqual(a._cfg_pending['export']['leading_ms'], 5000)
        payload = a._project_payload()          # 项目保存/比对同样不抛
        self.assertEqual(payload['export']['leading_ms'], 5000)
        a._apply_export_options({'leading_ms': 99999, 'format': 'ogg'})
        self.assertEqual(a.leading_ms.get(), 5000)
        self.assertEqual(a.export_format.get(), 'mp3')
        a.export_dir.set(self.tmp.name)         # 以可用状态进入对话框
        a.export_settings()                     # 非法编辑关窗即丢弃
        top = next(w for w in self.root.winfo_children()
                   if isinstance(w, tk.Toplevel) and '导出设置' in w.title())
        a.export_dir.set('')
        cmd = self.root.tk.call('wm', 'protocol', top._w, 'WM_DELETE_WINDOW')
        self.root.tk.call(cmd)
        self.assertFalse(top.winfo_exists())
        self.assertEqual(a.export_dir.get(), self.tmp.name)  # 恢复进入时的可用值

    def test_open_project_heals_invalid_export_directory(self):
        a = self.a
        a._set_real_text('原文A')
        payload = a._project_payload()
        payload['text'] = '项目里的原文'
        payload['export'] = dict(payload['export'], directory='')
        path = str(Path(self.tmp.name) / 'p.smartvoice')
        workflow.save_project(path, payload, a._segment_cache)
        with patch('product_ui.filedialog.askopenfilename', return_value=path), \
                patch.object(a, '_fail') as fail, \
                patch.object(a, '_confirm_discard', return_value=True):
            a.open_project()
        fail.assert_not_called()
        self.assertEqual(a._get_real_text(), '项目里的原文')   # 完整应用，无半开状态
        self.assertEqual(a._project_path, path)
        self.assertIsNotNone(a._project_baseline)
        self.assertFalse(a._project_changed())
        self.assertEqual(a.export_dir.get(), str(storage.EXPORT_DIR))  # 空目录回落默认

    def test_editor_window_uses_context_menu_instead_of_toolbar(self):
        a = self.a
        import tkinter.ttk as ttk
        from product_ui import PeerText
        a.open_editor()
        peer = next(w for w in a._editor_window.winfo_children() if isinstance(w, PeerText))
        self.assertTrue(str(peer.bind('<Button-3>')).strip())          # 编辑操作收敛到右键
        self.assertEqual([w for w in a._editor_window.winfo_children() if isinstance(w, ttk.Button)], [])
        self.assertTrue(str(a.text.bind('<Button-3>')).strip())        # 主文本右键仍在

    def test_dynamic_description_color_and_black_text(self):
        from theme import FEEDBACK, FG
        a = self.a
        self.assertEqual(FG, '#000000')                    # 功能区文字纯黑
        self.assertEqual(str(a.prog_lab.cget('foreground')), FEEDBACK)
        self.assertEqual(str(a.editor_role.cget('foreground')), FEEDBACK)
        self.assertEqual(str(a.play_time.cget('foreground')), FEEDBACK)
        import tkinter.ttk as ttk
        self.assertEqual(ttk.Style(a.root).lookup('Status.TLabel', 'foreground'), FEEDBACK)

    def test_section_titles_render_shadow_pair(self):
        a = self.a
        import tkinter.ttk as ttk
        def labels_of(frame):
            lw = frame.nametowidget(str(frame.cget('labelwidget')))
            # 标题必须真实可见：place 不参与请求尺寸会塌缩成1px（Tk 实测），grid 叠放才正确。
            self.assertTrue(lw.winfo_ismapped(), frame)
            self.assertGreater(lw.winfo_height(), 8, frame)
            return [w for w in lw.winfo_children() if isinstance(w, ttk.Label)]
        for frame in (a.f1, a.f2, a.f6, a.voice_box):
            self.assertEqual(len(labels_of(frame)), 2)     # 阴影字 + 主字
        title_row = a.f4.nametowidget(str(a.f4.cget('labelwidget')))
        shadow_box = title_row.winfo_children()[0]
        self.assertTrue(shadow_box.winfo_ismapped())
        self.assertGreater(shadow_box.winfo_height(), 8)
        self.assertEqual(len([w for w in shadow_box.winfo_children() if isinstance(w, ttk.Label)]), 2)
        dub_box = a.dub_title.master
        self.assertTrue(dub_box.winfo_ismapped())
        self.assertGreater(dub_box.winfo_height(), 8)
        self.assertEqual(len([w for w in dub_box.winfo_children()
                              if isinstance(w, ttk.Label)]), 2)

    def test_multivoice_validation_happens_before_network(self):
        a = self.a
        snap = a.snapshot()
        snap['style'] = 'calm'
        records = {'zh-CN-XiaoxiaoNeural': {'VoiceType': 'Neural', 'StyleList': ['calm']},
                   'zh-CN-YunxiNeural': {'VoiceType': 'Neural', 'StyleList': []}}
        jobs = [('zh-CN-XiaoxiaoNeural', '甲', '你好'), ('zh-CN-YunxiNeural', '乙', '大家好')]
        with patch.object(engine, 'azure_voice_metadata', return_value=records):
            with self.assertRaisesRegex(ValueError, r'角色 \[乙\]'):
                a._begin_task(jobs, snap, '原稿')
        self.assertFalse(a._active_task)

    def test_user_project_payload_has_no_key_endpoint_or_token(self):
        a = self.a
        a._set_real_text('项目原稿')
        a.ep_var.set('https://example.com?token=secret-token')
        raw = json.dumps(a._project_payload(), ensure_ascii=False)
        self.assertNotIn('test-secret', raw)
        self.assertNotIn('secret-token', raw)
        self.assertIn('项目原稿', raw)

    def test_exit_can_be_cancelled_while_task_active(self):
        a = self.a
        a._active_task = True
        with patch('studio_gui.messagebox.askokcancel', return_value=False):
            a.on_close()
        self.assertTrue(self.root.winfo_exists())
        self.assertTrue(a._active_task)

    def test_project_gui_roundtrip_restores_parameters_and_roles(self):
        a = self.a
        a._set_real_text('[小明]项目原稿')
        a._dub['slots'][0]['name'].set('小明')
        a.rate_var.set(80)
        a.export_format.set('wav')
        path = str(Path(self.tmp.name)/'project.smartvoice')
        with patch('product_ui.filedialog.asksaveasfilename', return_value=path):
            self.assertTrue(a.save_project())
        a._set_real_text('被改动的内容')
        a.rate_var.set(120)
        with patch('product_ui.filedialog.askopenfilename', return_value=path):
            a.open_project()
        self.assertEqual(a._get_real_text(), '[小明]项目原稿')
        self.assertEqual(a.rate_var.get(), 80)
        self.assertEqual(a._dub['slots'][0]['name'].get(), '小明')
        self.assertEqual(a.export_format.get(), 'wav')
        self.assertFalse(a._project_changed())

    def test_pause_and_resume_commands(self):
        player = self.a.player
        with patch.object(player, '_cmd', side_effect=['playing', '', 'paused', '']) as cmd:
            player.toggle_pause()
            player.toggle_pause()
        self.assertEqual([c.args[0] for c in cmd.call_args_list],
                         ['status ttsstudio mode', 'pause ttsstudio', 'status ttsstudio mode', 'resume ttsstudio'])

    def test_wav_export_uses_pcm_and_unique_path(self):
        a = self.a
        a.export_format.set('wav')
        snap = a.snapshot()
        a._begin_task([(snap['voice'], '角色', '原稿')], snap, '原稿')
        with patch('docutils.prepare_playback_audio', return_value=(b'mp3', b'RIFF-pcm')), \
                patch.object(a, '_play_with_flash'):
            done = a._prepare_playback(a._seq, b'source', 'ignored.mp3', '测试')
            done()
        self.assertTrue(a._last_export.endswith('.wav'))
        self.assertEqual(Path(a._last_export).read_bytes(), b'RIFF-pcm')
        self.assertFalse(a._active_task)

    def test_full_background_task_exports_and_completes_diagnostics(self):
        import wave
        a = self.a
        a._dub_all(False)
        a._set_real_text('验收原稿')
        buffer = io.BytesIO()
        with wave.open(buffer, 'wb') as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(24000)
            wav.writeframes(b'\0\0' * 2400)
        with patch.object(a, 'synth_net', return_value=buffer.getvalue()), \
                patch.object(a.player, 'play_file'), patch.object(a, '_watch_play'):
            a.on_single()
            deadline = time.monotonic() + 8
            while a._active_task and time.monotonic() < deadline:
                self.root.update()
                time.sleep(.02)
            self.assertFalse(a._active_task)
            self.assertTrue(Path(a._last_export).is_file())
            self.assertGreater(Path(a._last_export).stat().st_size, 100)
            self.assertEqual(len(a._segment_keys), 1)
            self.assertEqual(len(a._export_history), 1)
            self.assertTrue(Path(a._last_pcm).is_file())
            self.assertEqual(a._prog_ui['value'], 100)

    def test_text_toolbar_matches_peer_function_button_style(self):
        a = self.a
        self.assertEqual(str(a.b_import.cget('style')), 'TButton')
        self.assertEqual(str(a.b_single.cget('style')), 'TButton')
        self.assertEqual(str(a.b_play.cget('style')), 'TButton')
        self.assertEqual(str(a.b_stop.cget('style')), 'TButton')
        self.assertEqual(str(a.b_open.cget('style')), 'TButton')
        # 刷新、角色分配按钮同属默认 TButton，文本工具栏按钮与其共享样式。
        self.assertEqual(str(a.b_refresh.cget('style')), 'TButton')
        toolbar = [a.b_import, a.b_single, a.b_play, a.b_stop, a.b_open]
        self.assertTrue(all(button.winfo_manager() == 'grid' for button in toolbar))

    def test_window_move_configure_skips_layout_reflow(self):
        from types import SimpleNamespace
        a = self.a
        a._last_w = 640
        with patch.object(a.f5, 'configure') as configure, patch.object(a.root, 'after') as after:
            a._on_root_resize(SimpleNamespace(widget=a.root, width=640, height=700))
        configure.assert_not_called()
        after.assert_not_called()

    def test_window_resize_debounces_layout_reflow(self):
        from types import SimpleNamespace
        a = self.a
        a._last_w = 640
        with patch.object(a.f5, 'configure') as configure, patch.object(a.root, 'after', return_value='timer') as after:
            a._on_root_resize(SimpleNamespace(widget=a.root, width=760, height=700))
        configure.assert_called_once()
        after.assert_called_once()


class AudioOptionsTests(unittest.TestCase):
    def test_loudness_option_and_configurable_padding(self):
        import wave
        import numpy as np
        import docutils
        rate = 24000
        samples = (np.sin(np.arange(rate*3)*2*np.pi*440/rate)*300).astype('<i2')
        buffer = io.BytesIO()
        with wave.open(buffer, 'wb') as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(rate)
            wav.writeframes(samples.tobytes())
        _, pcm = docutils.prepare_playback_audio(buffer.getvalue(), 100, 200, normalize=True)
        with wave.open(io.BytesIO(pcm), 'rb') as wav:
            self.assertEqual(wav.getframerate(), rate)
            frames = np.frombuffer(wav.readframes(wav.getnframes()), dtype='<i2')
        self.assertLess(abs(len(frames) - int(rate*3.3)), 100)
        self.assertTrue(np.all(frames[:2400] == 0))
        self.assertGreater(np.max(np.abs(frames)), 300)


class BrandingTests(unittest.TestCase):
    ASSETS = Path(__file__).resolve().parent / 'assets'

    def test_icon_covers_all_sizes(self):
        from PIL import Image
        with Image.open(self.ASSETS / 'smartvoice.ico') as icon:
            self.assertEqual(sorted(icon.info['sizes']),
                             [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])

    def test_png_and_wizard_images(self):
        from PIL import Image
        with Image.open(self.ASSETS / 'smartvoice.png') as png:
            self.assertEqual(png.size, (512, 512))
        with Image.open(self.ASSETS / 'wizard-image.bmp') as wizard:
            self.assertEqual(wizard.size, (164, 314))
        with Image.open(self.ASSETS / 'wizard-small.bmp') as small:
            self.assertEqual(small.size, (55, 58))

    def test_installer_uses_wizard_images(self):
        script = (Path(__file__).resolve().parent / 'installer.iss').read_text(encoding='utf-8')
        self.assertIn('WizardImageFile=assets\\wizard-image.bmp', script)
        self.assertIn('WizardSmallImageFile=assets\\wizard-small.bmp', script)


if __name__ == '__main__':
    unittest.main()
