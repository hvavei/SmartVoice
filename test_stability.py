"""资源释放、组件修复与长文本刷新回归；无需联网或真实模型。"""
import hashlib
import json
from pathlib import Path
import tempfile
import tkinter as tk
import unittest
from unittest.mock import Mock, PropertyMock, patch
import zipfile

import components
import documents
import docutils
import engine
import storage


class ResourceTests(unittest.TestCase):
    def test_dependency_publish_failure_restores_complete_old_directory(self):
        import build_release
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            internal = root / '_internal'
            internal.mkdir()
            (internal / 'original').write_bytes(b'working')
            def broken_copy(source, target):
                target.mkdir()
                (target / 'partial').write_bytes(b'incomplete')
                raise OSError('disk full')
            with patch('build_release.shutil.copytree', side_effect=broken_copy):
                with self.assertRaises(OSError):
                    build_release.replace_internal(root / 'new', internal)
            self.assertEqual((internal / 'original').read_bytes(), b'working')
            self.assertFalse((internal / 'partial').exists())
            self.assertFalse((root / '_internal.old').exists())

    def test_word_mapping_preserves_mixed_unicode_and_whitespace_offsets(self):
        import polyphone
        text = '  abc12银\tİ行😀 xyz\n'
        words, mapping, spans = polyphone._wordize_and_map(text)
        self.assertEqual(words, ['abc12', '银', '\t', 'İ', '行', '😀', 'xyz', '\n'])
        self.assertEqual(len(mapping), len(text))
        for i, (start, end) in enumerate(spans):
            self.assertEqual(text[start:end], words[i])
            self.assertEqual(mapping[start:end], [i] * (end - start))
        self.assertEqual([i for i, value in enumerate(mapping) if value is None],
                         [i for i, char in enumerate(text) if char == ' '])

    def test_failed_export_flush_removes_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch('storage.os.fsync', side_effect=OSError('disk full')):
                with self.assertRaises(OSError):
                    storage.unique_export(directory, 'voice', 'mp3', b'audio')
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_openai_without_progress_still_caps_and_closes_response(self):
        response = Mock(status_code=200, headers={'Content-Type': 'audio/mpeg'})
        response.iter_content.return_value = [b'0123456789']
        session = Mock()
        session.post.return_value = response
        with patch.object(engine, '_sess', return_value=session), patch.object(engine, 'MAX_RESPONSE_BYTES', 4):
            with self.assertRaises(RuntimeError):
                engine.synth_openai('sample', 'alloy', 'fake', attempts=1)
        self.assertTrue(session.post.call_args.kwargs['stream'])
        response.close.assert_called_once()

    def test_pdf_reader_closes_on_broken_page_tree(self):
        reader = Mock()
        type(reader).pages = PropertyMock(side_effect=ValueError('broken page tree'))
        with patch('pypdf.PdfReader', return_value=reader):
            with self.assertRaises(ValueError):
                docutils.extract_pdf_text('unused.pdf')
        reader.close.assert_called_once()

    def test_ocr_failure_is_reported_and_page_closed(self):
        page = Mock()
        page.render.side_effect = RuntimeError('render failed')
        pdf = Mock()
        pdf.__getitem__ = Mock(return_value=page)
        context = Mock()
        context.__enter__ = Mock(return_value=pdf)
        context.__exit__ = Mock(return_value=False)
        with patch('docutils.create_ocr'), patch('pypdfium2.PdfDocument', return_value=context):
            with self.assertRaisesRegex(RuntimeError, '第 3 页'):
                docutils._ocr_pages('unused.pdf', [2])
        page.close.assert_called_once()
        context.__exit__.assert_called_once()

    def test_reflow_punctuation_without_breaking_hyphenation(self):
        self.assertEqual(documents.reflow_text('Hello,\nworld'), 'Hello, world')
        self.assertEqual(documents.reflow_text('semi-\nautomatic'), 'semi-automatic')
        self.assertEqual(documents.reflow_text('(\nword'), '(word')


class ComponentRepairTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.archive = self.root / 'component.zip'
        with zipfile.ZipFile(self.archive, 'w') as archive:
            archive.writestr('component.json', json.dumps({'name': 'ocr', 'abi': components.ABI}))
            archive.writestr('payload.txt', b'complete')
        with zipfile.ZipFile(self.archive) as archive:
            unpacked = sum(item.file_size for item in archive.infolist())
        self.entry = {'sha256': hashlib.sha256(self.archive.read_bytes()).hexdigest(),
                      'bytes': self.archive.stat().st_size, 'unpacked_bytes': unpacked}
        for name, value in [('COMPONENT_DIR', self.root / 'components'), ('_activated', set())]:
            patcher = patch.object(components, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(components, '_entry', return_value=self.entry)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.final = components.install_archive('ocr', self.archive)

    def test_reimport_repairs_corruption_and_removes_stale_files(self):
        (self.final / 'payload.txt').write_bytes(b'broken')
        (self.final / 'obsolete.txt').write_text('old', encoding='utf-8')
        components.install_archive('ocr', self.archive)
        self.assertEqual((self.final / 'payload.txt').read_bytes(), b'complete')
        self.assertFalse((self.final / 'obsolete.txt').exists())

    def test_pointer_write_failure_rolls_back_previous_component(self):
        (self.final / 'payload.txt').write_bytes(b'previous')
        pointer = (components.COMPONENT_DIR / 'ocr.json').read_bytes()
        with patch('components.storage.atomic_json', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                components.install_archive('ocr', self.archive)
        self.assertEqual((self.final / 'payload.txt').read_bytes(), b'previous')
        self.assertEqual((components.COMPONENT_DIR / 'ocr.json').read_bytes(), pointer)
        self.assertEqual(len(list(components.COMPONENT_DIR.iterdir())), 2)

    def test_loaded_component_cannot_be_destructively_replaced(self):
        components._activated.add('ocr')
        with self.assertRaisesRegex(RuntimeError, '重启'):
            components.install_archive('ocr', self.archive)
        self.assertEqual((self.final / 'payload.txt').read_bytes(), b'complete')


class EditorRefreshTests(unittest.TestCase):
    def setUp(self):
        from studio_gui import App
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.app = App.__new__(App)
        self.app.root = self.root
        self.app.text = tk.Text(self.root)
        self.app.editor_info = tk.Frame(self.root)
        self.app.editor_role = tk.Label(self.root)
        self.app._dub = {'slots': [{'name': tk.StringVar(self.root, '旁白'),
                                    'on': tk.BooleanVar(self.root, True)}]}

    def test_large_text_keeps_highlighting_with_batched_updates(self):
        app = self.app
        app.text.insert('1.0', ('[旁白]' + '文' * 24 + '\n') * 3000)
        with patch.object(app.text, 'tag_add', wraps=app.text.tag_add) as add:
            app._refresh_editor_info()
            self.assertLess(add.call_count, 20)
            self.assertEqual(len(app.text.tag_ranges('speaker_0')), 6000)
            add.reset_mock()
            app.text.mark_set('insert', '2000.5')
            app._refresh_editor_info()
            add.assert_not_called()
        app._dub['slots'][0]['on'].set(False)
        app._refresh_editor_info()
        self.assertEqual(app.text.tag_ranges('speaker_0'), ())
        self.assertIn('未绑定', app.editor_role.cget('text'))

    def test_typing_inside_narration_skips_tag_updates(self):
        app = self.app
        app.text.insert('1.0', '[旁白]你好\n这是旁白。\n[旁白]再见\n')
        app._refresh_editor_info()
        before = app.text.tag_ranges('speaker_0')
        self.assertEqual(len(before), 4)
        with patch.object(app.text, 'tag_add', wraps=app.text.tag_add) as add, \
                patch.object(app.text, 'tag_remove', wraps=app.text.tag_remove) as remove:
            app.text.insert('2.3', 'X')  # 旁白行内打字：标注语义不变
            app._refresh_editor_info()
            add.assert_not_called()
            remove.assert_not_called()
        self.assertEqual(app.text.tag_ranges('speaker_0'), before)

    def test_role_bracket_edit_retags_only_damage_region(self):
        app = self.app
        app.text.insert('1.0', '[旁白]一\n[旁白]二\n[旁白]三\n')
        app._refresh_editor_info()
        with patch.object(app.text, 'tag_add', wraps=app.text.tag_add) as add:
            app.text.delete('2.1', '2.2')  # [旁白] -> [白]：未绑定
            app._refresh_editor_info()
            self.assertEqual(add.call_count, 1)  # 只加 speaker_missing 一次
        self.assertEqual(tuple(map(str, app.text.tag_ranges('speaker_0'))), ('1.0', '1.4', '3.0', '3.4'))
        self.assertEqual(len(app.text.tag_ranges('speaker_missing')), 2)

    def test_progress_redraw_survives_widget_recreation(self):
        app = self.app
        app._prog_ui = {'maximum': 100, 'value': 50}
        app.prog = tk.Canvas(self.root, width=200, height=10)
        app._prog_draw()
        with patch.object(app.prog, 'coords', wraps=app.prog.coords) as coords:
            app._prog_draw()
            coords.assert_not_called()
        app.prog.destroy()
        app.prog = tk.Canvas(self.root, width=200, height=10)
        app._prog_draw()
        self.assertEqual(app.prog.itemcget('fill', 'state'), 'normal')
        self.assertGreater(app.prog.coords('fill')[2], 1)


if __name__ == '__main__':
    unittest.main()
