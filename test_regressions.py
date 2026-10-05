"""离线回归：发音标记、原文保真、配置隔离及后台任务生命周期。"""
import json
import io
import sys
import tempfile
import wave
from pathlib import Path
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import Mock, patch

import engine

MODELS_READY = (Path(__file__).resolve().parent / 'models' / 'g2pw' / 'g2pw.onnx').is_file()
requires_models = unittest.skipUnless(MODELS_READY, 'python prepare_models.py 资产未下载，跳过真实推理')


class EngineTests(unittest.TestCase):
    def test_official_voice_metadata_preserved_without_suffix_filter(self):
        rows = [{'ShortName': 'zh-CN-Lan:MAI-Voice-2-Flash', 'LocalName': 'Lan', 'Gender': 'Female',
                 'VoiceType': 'Neural', 'Status': 'Preview', 'Locale': 'zh-CN'},
                {'ShortName': 'zh-CN-Lan:MAI-Voice-2', 'LocalName': 'Lan', 'Gender': 'Female',
                 'VoiceType': 'NeuralHD', 'Status': 'Preview', 'Locale': 'zh-CN'}]
        response = Mock(status_code=200)
        response.json.return_value = rows
        session = Mock()
        session.get.return_value = response
        with tempfile.TemporaryDirectory() as tmp, patch.object(engine, 'APP_DIR', tmp), \
                patch.object(engine, '_sess', return_value=session):
            engine.azure_voice_metadata.cache_clear()
            table = engine.refresh_voices_azure('fake', 'eastasia', '')
            self.assertEqual(set(table.values()), {row['ShortName'] for row in rows})
            self.assertEqual(engine.get_voices('eastasia'), table)
            metadata = engine.azure_voice_metadata('eastasia')
            self.assertEqual(metadata[rows[0]['ShortName']]['VoiceType'], 'Neural')
            self.assertEqual(metadata[rows[1]['ShortName']]['VoiceType'], 'NeuralHD')
            self.assertEqual(engine.get_voices('eastus'), engine.BUILTIN_VOICES)
            response.close.assert_called_once()
        engine.azure_voice_metadata.cache_clear()

    def test_invalid_voice_response_does_not_destroy_cache(self):
        response = Mock(status_code=200)
        response.json.return_value = {'error': 'not a list'}
        session = Mock()
        session.get.return_value = response
        with patch.object(engine, '_sess', return_value=session), patch.object(engine, 'save_json') as save:
            with self.assertRaisesRegex(RuntimeError, '结构无效'):
                engine.refresh_voices_azure('fake', 'eastasia', '')
            save.assert_not_called()
        response.close.assert_called_once()

    def test_network_and_models_not_imported_at_gui_startup(self):
        import subprocess
        import sys
        result = subprocess.run([sys.executable, '-B', '-c',
            "import studio_gui,sys; assert not any(m in sys.modules for m in ('requests','numpy','onnxruntime','tokenizers','cv2'))"],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_azure_chunking_preserves_text_and_role_order(self):
        text = ('第一句内容。第二句是另一个完整的句子！\n' * 100) + '末尾不丢字'
        parts = list(engine.split_synthesis_text(text))
        self.assertEqual(''.join(parts), text)
        self.assertTrue(all(len(p) <= 600 for p in parts))
        jobs = engine.plan_azure_jobs([('voice1', '旁白', text), ('voice2', '角色', '下一位')])
        self.assertEqual(''.join(t for v, role, t in jobs if role == '旁白'), text)
        self.assertEqual(jobs[-1], ('voice2', '角色', '下一位'))
        self.assertEqual(''.join(engine.split_synthesis_text('甲'*1300)), '甲'*1300)

    def test_azure_rate_uses_relative_percent_without_changing_pitch_volume(self):
        for gui_rate, ssml_rate in (('100%', '+0%'), ('80%', '-20%'),
                                    ('120%', '+20%'), ('+20%', '+20%'), ('-10%', '-10%')):
            with self.subTest(rate=gui_rate):
                root = ET.fromstring(engine._build_ssml('en-US-JennyNeural', 'test', gui_rate, '+2Hz', '-10%'))
                prosody = root.find('.//{*}prosody')
                expected = {'pitch': '+2Hz', 'volume': '-10%'}
                if ssml_rate != '+0%':
                    expected['rate'] = ssml_rate
                self.assertEqual(prosody.attrib, expected)
                self.assertEqual(engine._signed_pct(gui_rate), ssml_rate)

    def test_merge_preserves_role_voice_text_and_length_boundaries(self):
        jobs = [('v1', '旁白', '第一句。'), ('v1', '旁白', '第二句。'),
                ('v1', '小明', '角色不同'), ('v2', '小明', '人声不同'),
                ('v1', '旁白', '回到旁白')]
        merged = engine.merge_dub_jobs(jobs)
        self.assertEqual(merged, [('v1', '旁白', '第一句。\n第二句。')] + jobs[2:])
        self.assertEqual('\n'.join(j[2] for j in jobs), '\n'.join(j[2] for j in merged))
        self.assertEqual(engine.merge_dub_jobs(jobs, max_chars=5), jobs)

    def test_pronunciation_preserves_text_and_neutral_tone(self):
        text = '银行重新办理，大腹便便。爸爸妈妈看看，重重叠叠。a&b<c>'
        with patch('polyphone.annotate_sapi', return_value=(text, {0: 'yin 2', 1: 'hang 2'})), \
                patch('components.available', return_value=True):
            root = ET.fromstring(engine._build_ssml('zh-CN-YunxiNeural', text, '100%', '+0Hz', '+0%'))
        self.assertEqual(''.join(root.itertext()), text)
        phones = root.findall('.//{*}phoneme')
        self.assertEqual([p.get('ph') for p in phones], ['yin 2 hang 2'])
        self.assertTrue(all(not p.findall('.//{*}phoneme') for p in phones))
        self.assertNotIn('<break', ET.tostring(root, encoding='unicode'))
        with patch('polyphone.annotate_sapi', return_value=('便宜', {0: 'pian 2', 1: 'yi 5'})), \
                patch('components.available', return_value=True):
            self.assertIn("ph='pian 2 yi 5'", engine.g2p_phoneme_annotator('便宜'))

    def test_audition_bypasses_g2pw_and_retains_style_and_punctuation(self):
        text = '谁是我们的敌人？谁是我们的朋友？'
        with patch('polyphone.annotate_sapi') as annotate, \
                patch('components.available', return_value=True):
            ssml = engine._build_ssml('zh-CN-XiaoxiaoNeural', text, '100%', '+0Hz', '+0%',
                                      style='friendly', styledegree='100%', annotate=False)
            annotate.assert_not_called()
        self.assertEqual(''.join(ET.fromstring(ssml).itertext()), text)
        self.assertIn("style='friendly'", ssml)
        self.assertNotIn('silence', ssml)
        self.assertNotIn('phoneme', ssml)

    def test_mai_uses_minimal_original_text_without_loading_model(self):
        text = '谁是我们的敌人？谁是我们的朋友？银行重新办理。a&b<c>'
        with patch('polyphone.annotate_sapi') as annotate, \
                patch('components.available', return_value=True):
            ssml = engine._build_ssml('zh-CN-Lan:MAI-Voice-2-Flash', text, '100%', '+0Hz', '+0%')
            annotate.assert_not_called()
        root = ET.fromstring(ssml)
        self.assertEqual(root.tag, '{http://www.w3.org/2001/10/synthesis}speak')
        self.assertEqual(''.join(root.itertext()), text)
        self.assertNotIn('<prosody', ssml)
        self.assertNotIn('<phoneme', ssml)

    def test_annotation_cannot_drop_or_replace_original_characters(self):
        text = '银行重新办理。'
        cases = [(text[1:], {0: 'hang 2'}),
                 (text, {-1: 'yin 2', 999: 'hang 2', 1: 'hang 2', 2: None})]
        for output in cases:
            with patch('polyphone.annotate_sapi', return_value=output), \
                    patch('components.available', return_value=True):
                ssml = engine._build_ssml('zh-CN-XiaoxiaoNeural', text, '100%', '+0Hz', '+0%')
            root = ET.fromstring(ssml)
            self.assertEqual(''.join(root.itertext()), text)
            for node in root.findall('.//{*}phoneme'):
                self.assertEqual(len(node.text), len(node.get('ph').split()) // 2)

    def test_truncated_response_is_rejected(self):
        import requests
        r = Mock(headers={'Content-Length': '100'})
        r.iter_content.return_value = [b'partial']
        with self.assertRaises(requests.exceptions.ChunkedEncodingError):
            engine._read_response_bytes(r)

    def test_no_mandarin_phonemes_for_other_locales(self):
        with patch('polyphone.annotate_sapi') as annotate, \
                patch('components.available', return_value=True):
            self.assertNotIn('<phoneme', engine._build_ssml('zh-HK-HiuMaanNeural', '银行', '100%', '+0Hz', '+0%'))
            annotate.assert_not_called()

    def test_config_failure_preserves_previous_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, 'config.json')
            engine.save_json(path, {'saved': True})
            with self.assertRaises(TypeError):
                engine.save_json(path, {'invalid': object()})
            self.assertEqual(engine.load_json(path, {}), {'saved': True})
            self.assertEqual(list(Path(tmp).iterdir()), [path])

    def test_azure_retries_interrupted_stream_and_closes_responses(self):
        import requests
        broken = Mock(status_code=200, headers={'Content-Type': 'audio/mpeg'})
        good = Mock(status_code=200, headers={'Content-Type': 'audio/mpeg'})
        def interrupted(**kwargs):
            yield b'partial'
            raise requests.exceptions.ChunkedEncodingError('connection lost')
        broken.iter_content.side_effect = interrupted
        good.iter_content.return_value = [b'complete']
        session = Mock()
        session.post.side_effect = [broken, good]
        with patch.object(engine, '_sess', return_value=session), patch('time.sleep'):
            data = engine.synth_azure('test', 'en-US-JennyNeural', 'fake', '', attempts=2)
        self.assertEqual(data, b'complete')
        broken.close.assert_called_once()
        good.close.assert_called_once()
        html = Mock(status_code=200, headers={'Content-Type': 'text/html'})
        session.post.side_effect = None
        session.post.return_value = html
        with patch.object(engine, '_sess', return_value=session), self.assertRaises(RuntimeError):
            engine.synth_azure('test', 'en-US-JennyNeural', 'fake', '', attempts=1)
        html.close.assert_called_once()

    def test_stream_progress_and_stages_for_http_engines(self):
        import base64
        for kind in ('azure', 'openai', 'volc'):
            with self.subTest(kind=kind):
                audio = b'ID3' + b'audio' * 2000
                body = json.dumps({'data': base64.b64encode(audio).decode('ascii')}).encode() if kind == 'volc' else audio
                response = Mock(status_code=200, headers={
                    'Content-Type': 'application/json' if kind == 'volc' else 'audio/mpeg',
                    'Content-Length': str(len(body))})
                response.iter_content.return_value = [body[:4096], body[4096:]]
                session = Mock()
                session.post.return_value = response
                progress, stages = [], []
                def report(cur, total):
                    progress.append((cur, total))
                with patch.object(engine, '_sess', return_value=session):
                    if kind == 'azure':
                        result = engine.synth_azure('test', 'en-US-JennyNeural', 'fake', '',
                                                    on_progress=report, on_stage=stages.append)
                    elif kind == 'openai':
                        result = engine.synth_openai('test', 'alloy', 'fake',
                                                     on_progress=report, on_stage=stages.append)
                    else:
                        result = engine.synth_volc('test', 'BV001_streaming', 'fake',
                                                   on_progress=report, on_stage=stages.append)
                self.assertEqual(result, audio)
                self.assertEqual(progress[0], (0, len(body)))
                self.assertEqual(progress[1], (4096, len(body)))
                self.assertEqual(progress[-1], (len(body), len(body)))
                self.assertTrue(session.post.call_args.kwargs['stream'])
                self.assertIn('接收响应' if kind == 'volc' else '接收音频', stages)
                response.close.assert_called_once()

    def test_unknown_or_compressed_length_does_not_invent_percent(self):
        for headers in ({}, {'Content-Length': 'invalid'},
                        {'Content-Length': '10', 'Content-Encoding': 'gzip'}):
            response = Mock(headers=headers)
            response.iter_content.return_value = [b'1234', b'5678']
            progress = Mock()
            self.assertEqual(engine._read_response_bytes(response, progress), b'12345678')
            self.assertEqual([c.args for c in progress.call_args_list], [(0, 0), (4, 0), (8, 0)])

    def test_edge_progress_has_bytes_without_total(self):
        progress, stages = [], []
        async def chunks():
            yield {'type': 'audio', 'data': b'ID3'}
            yield {'type': 'WordBoundary', 'text': 'test'}
            yield {'type': 'audio', 'data': b'audio'}
        comm = Mock()
        comm.stream.side_effect = chunks
        with patch('edge_tts.Communicate', return_value=comm):
            result = engine.synth_edge('test', 'voice', on_progress=lambda c, t: progress.append((c, t)),
                                       on_stage=stages.append)
        self.assertEqual(result, b'ID3audio')
        self.assertEqual(progress, [(3, 0), (8, 0)])
        self.assertIn('接收音频', stages)

    def test_formatting_is_idempotent(self):
        for text in ['她说。"你好"', '“爸爸！”小明说。', '「你好」', '“未闭合', '[小明]你好']:
            result = engine.format_dialogue_lines(text)
            self.assertEqual(engine.format_dialogue_lines(result), result)

    def test_format_binds_narration_to_slot_and_keeps_dialogue_verbatim(self):
        self.assertEqual(engine.format_dialogue_lines('小明说。“大家好。”', ['解说员']),
                         '解说员:小明说。\n“大家好。”')
        # 无启用槽位：只分行不加前缀
        self.assertEqual(engine.format_dialogue_lines('小明说。“大家好。”', []),
                         '小明说。\n“大家好。”')
        # 已知角色冒号行原样保留；普通冒号行/时间行不当角色
        self.assertEqual(engine.format_dialogue_lines('小明:你好', ['小明']), '小明:你好')
        self.assertEqual(engine.format_dialogue_lines('他说：走吧', ['旁白']), '旁白:他说：走吧')
        self.assertEqual(engine.format_dialogue_lines('12:30 出发', ['旁白']), '旁白:12:30 出发')

    def test_parse_dub_script_is_membership_first(self):
        segs = engine.parse_dub_script('他说：你好\n9:30\n2:台词\n[未知]保留', ['旁白', '小明'])
        self.assertEqual(segs, [(None, '他说：你好'), (None, '9:30'), (1, '台词'), (None, '[未知]保留')])
        # 旁白前缀行即使正文含时间也绑定；无槽位时数字前缀不误绑
        self.assertEqual(engine.parse_dub_script('旁白:12:30出发', ['旁白']), [(0, '12:30出发')])
        self.assertEqual(engine.parse_dub_script('1:台词', []), [(None, '1:台词')])

    def test_unknown_labels_preserve_text(self):
        text = 'https://example.com\n12:30\n提示:测试'
        self.assertEqual([x[1] for x in engine.parse_dub_script(text, ['旁白'])], text.splitlines())

    def test_openai_model_and_bad_response(self):
        response = Mock(status_code=200, headers={'Content-Type': 'audio/mpeg'}, content=b'ID3test')
        response.iter_content.return_value = [b'ID3test']
        session = Mock()
        session.post.return_value = response
        with patch.object(engine, '_sess', return_value=session):
            engine.synth_openai('test', 'alloy', 'fake', model='custom-model')
            body = json.loads(session.post.call_args.kwargs['data'])
            self.assertEqual(body['model'], 'custom-model')
            response.headers = {'Content-Type': 'text/html'}
            with self.assertRaises(RuntimeError):
                engine.synth_openai('test', 'alloy', 'fake', attempts=1)

    def test_volc_uses_capped_stream_without_progress(self):
        import base64
        payload = json.dumps({"data": base64.b64encode(b"ID3v").decode()}).encode()
        response = Mock(status_code=200, headers={'Content-Type': 'application/json'})
        response.iter_content.return_value = [payload]
        session = Mock()
        session.post.return_value = response
        with patch.object(engine, '_sess', return_value=session):
            out = engine.synth_volc('test', 'BV001_streaming', 'tok', 'app1', attempts=1)
        self.assertEqual(out, b'ID3v')
        self.assertTrue(session.post.call_args.kwargs['stream'])

    def test_edge_stream_concatenates_audio_chunks(self):
        import edge_tts

        class GoodComm:
            def __init__(self, *args, **kwargs):
                pass

            async def stream(self):
                yield {"type": "WordsBoundary", "data": {}}
                yield {"type": "audio", "data": b"ID3"}
                yield {"type": "audio", "data": b"xx"}

        with patch("edge_tts.Communicate", GoodComm):
            out = engine.synth_edge("hi", "zh-CN-YunxiNeural", attempts=1)
        self.assertEqual(out, b"ID3xx")

    def test_edge_stalled_stream_times_out_per_chunk(self):
        import asyncio
        import edge_tts

        class StalledComm:
            def __init__(self, *args, **kwargs):
                pass

            async def stream(self):
                yield {"type": "audio", "data": b"ID3"}
                await asyncio.sleep(3600)

        with patch("edge_tts.Communicate", StalledComm), \
                patch.object(engine, "EDGE_CHUNK_TIMEOUT", 0.05):
            with self.assertRaisesRegex(RuntimeError, "TimeoutError"):
                engine.synth_edge("你好", "zh-CN-YunxiNeural", attempts=1)


class DocumentReflowTests(unittest.TestCase):
    def test_soft_wrapped_lines_merge_into_paragraphs(self):
        import documents
        src = '第一段第一行还没写完\n第二行继续软换行\n\n第二段一句话。'
        self.assertEqual(documents.reflow_text(src),
                         '第一段第一行还没写完第二行继续软换行\n\n第二段一句话。')
        self.assertEqual(documents.reflow_text('end of line\ncontinues here.'),
                         'end of line continues here.')
        # 句末标点收尾的行各自成段，不吞并
        self.assertEqual(documents.reflow_text('一句。\n二句。'), '一句。\n二句。')

    def test_markers_and_indent_left_alone(self):
        import documents
        # 缩进块原样保留
        self.assertEqual(documents.reflow_text('  诗行\n    缩进\n正文'),
                         '  诗行\n    缩进\n正文')
        # 标记行另起段、段内软换行照常合并
        self.assertEqual(documents.reflow_text('[旁白]台词一\n台词二'), '[旁白]台词一台词二')
        self.assertEqual(documents.reflow_text('名字:前缀\n下一行'), '名字:前缀下一行')
        self.assertEqual(documents.reflow_text('- 列表项\n继续'), '- 列表项继续')

    def test_import_reflows_plain_text_but_keeps_structured_files(self):
        import documents
        with tempfile.TemporaryDirectory() as tmp:
            txt = Path(tmp) / 'a.txt'
            txt.write_text('很长的一行被源文件\n宽度截断了。\n\n第二段。', encoding='utf-8')
            self.assertEqual(documents.read_document(txt),
                             '很长的一行被源文件宽度截断了。\n\n第二段。')
            js = Path(tmp) / 'a.json'
            raw = '{\n  "k": "v",\n  "k2": "v2"\n}\n'
            js.write_text(raw, encoding='utf-8')
            self.assertEqual(documents.read_document(js), raw)
            lrc = Path(tmp) / 'a.lrc'
            lrc.write_text('[00:01.00]歌词一\n[00:02.00]歌词二', encoding='utf-8')
            self.assertEqual(documents.read_document(lrc), '歌词一\n歌词二')

    def test_srt_time_stripped_and_block_lines_merged(self):
        import documents
        with tempfile.TemporaryDirectory() as tmp:
            srt = Path(tmp) / 'a.srt'
            srt.write_text('1\n00:00:01,000 --> 00:00:02,000\n字幕第一行\n软换行\n\n'
                           '2\n00:00:03,000 --> 00:00:04,000\n第二段', encoding='utf-8')
            self.assertEqual(documents.read_document(srt), '字幕第一行软换行\n\n第二段')


class DocumentTests(unittest.TestCase):
    def test_playback_padding_preserves_all_segments_and_duration(self):
        import docutils
        import numpy as np
        rate = 24000
        tone = (np.sin(np.arange(rate // 5) * 2 * np.pi * 220 / rate) * 4000).astype('<i2')
        buf = io.BytesIO()
        with wave.open(buf, 'wb') as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(rate)
            wav.writeframes(tone.tobytes())
        # 21段覆盖长文分组处理分支；首尾各只加一次静音。
        for count in (1, 2, 21):
            with self.subTest(count=count):
                mp3, pcm = docutils.prepare_playback_audio([buf.getvalue()] * count)
                self.assertGreater(len(mp3), 100)
                with wave.open(io.BytesIO(pcm), 'rb') as wav:
                    self.assertEqual(wav.getframerate(), rate)
                    self.assertEqual(wav.getnchannels(), 1)
                    samples = np.frombuffer(wav.readframes(wav.getnframes()), dtype='<i2')
                head, tail = int(rate * .350), int(rate * .450)
                self.assertEqual(len(samples), head + len(tone) * count + tail)
                self.assertTrue(np.all(samples[:head] == 0))
                self.assertTrue(np.all(samples[-tail:] == 0))
                self.assertLessEqual(np.max(np.abs(samples[head:-tail].astype(int) - np.tile(tone, count))), 1)

    def test_mixed_pdf_ocr_keeps_page_order(self):
        import docutils
        pages = [Mock(), Mock(), Mock()]
        for page, text in zip(pages, ('第一页', '', '第三页')):
            page.extract_text.return_value = text
        with patch('pypdf.PdfReader', return_value=Mock(pages=pages)), \
                patch.object(docutils, '_ocr_pages', return_value={1: '第二页'}):
            self.assertEqual(docutils.extract_pdf_text('unused', ocr=True), '第一页\n\n第二页\n\n第三页')


class PolyphoneTests(unittest.TestCase):
    @requires_models
    def test_repeated_contexts_are_inferred_once_per_window(self):
        import polyphone as p
        p._lazy_init()
        text = '银行重新办理业务，工作人员认真解释。' * 10
        p._window_cache.clear()
        sizes = []
        def predict(inputs):
            sizes.append(len(inputs['input_ids']))
            return [None] * sizes[-1]
        try:
            with patch.object(p, '_predict', side_effect=predict):
                p._disambiguate(text)
                first_count = sum(sizes)
                p._disambiguate(text)
            raw_queries = sum(p._s2t.get(c, c) in p._char_ids for c in text)
            self.assertLess(first_count, raw_queries)
            self.assertEqual(sum(sizes), first_count)
        finally:
            p._window_cache.clear()

    @requires_models
    def test_repeated_text_reuses_inference_without_sharing_mutable_result(self):
        import polyphone
        polyphone._cached_disambiguate.cache_clear()
        with patch.object(polyphone, '_disambiguate', return_value=['hang2']) as infer:
            first = polyphone.disambiguate('行')
            first[0] = 'changed'
            self.assertEqual(polyphone.disambiguate('行'), ['hang2'])
            infer.assert_called_once()
        polyphone._cached_disambiguate.cache_clear()

    def test_latin_text_never_loads_model(self):
        import polyphone
        with patch.object(polyphone, '_lazy_init') as load:
            self.assertEqual(polyphone.annotate_sapi('hello!'), ('hello!', {}))
            load.assert_not_called()
    def test_invalid_annotations_are_omitted(self):
        import polyphone
        with patch.object(polyphone, 'disambiguate', return_value=['yin2', None, 'ㄧㄤ2', 'yi5']), \
                patch.object(polyphone, '_s2t', {}), patch.object(polyphone, '_char_ids', {'银': 0, '阳': 1, '宜': 2}):
            self.assertEqual(polyphone.annotate_sapi('银。阳宜'), ('银。阳宜', {0: 'yin 2', 3: 'yi 5'}))

    def test_monophonic_characters_are_left_to_azure(self):
        import polyphone
        text = '银行。你好'
        with patch.object(polyphone, 'disambiguate', return_value=['yin2', 'hang2', None, 'ni3', 'hao3']), \
                patch.object(polyphone, '_s2t', {'银': '銀'}), patch.object(polyphone, '_char_ids', {'行': 0}), \
                patch('components.available', return_value=True):
            result = engine.g2p_phoneme_annotator(text)
        self.assertEqual(result, "银<phoneme alphabet='sapi' ph='hang 2'>行</phoneme>。你好")

    def test_failed_initialization_can_retry(self):
        import polyphone
        with patch.object(polyphone, '_ready', False), \
                patch.object(polyphone, '_load_model', side_effect=[RuntimeError('failed'), None]) as load:
            with self.assertRaises(RuntimeError):
                polyphone._lazy_init()
            self.assertFalse(polyphone._ready)
            polyphone._lazy_init()
            polyphone._lazy_init()
            self.assertEqual(load.call_count, 2)

    @requires_models
    def test_overlapping_windows_share_tokenization(self):
        import polyphone as p
        p._lazy_init()
        p._tokenize_cache.clear()
        window = '银行重新办理业务'
        with patch.object(p, '_tokenize_and_map', wraps=p._tokenize_and_map) as tok:
            first = p._tokenize_cached(window)
            second = p._tokenize_cached(window)
            self.assertEqual(tok.call_count, 1)
            self.assertIs(first, second)
        p._tokenize_cache.clear()
        first = p.annotate_sapi('银行重新办理银行业务。')[1]
        self.assertGreater(len(p._tokenize_cache), 0)
        self.assertIn(1, first)
        p._tokenize_cache.clear()
        p._window_cache.clear()
        self.assertEqual(p.annotate_sapi('银行重新办理银行业务。')[1], first)

    @requires_models
    def test_model_real_inference_and_unicode_offsets(self):
        import polyphone
        text, phones = polyphone.annotate_sapi('银行')
        self.assertEqual(phones, {1: 'hang 2'})
        text = 'İ银行。'
        result, phones = polyphone.annotate_sapi(text)
        self.assertEqual(result, text)
        self.assertNotIn(1, phones)
        self.assertEqual(phones[2], 'hang 2')
        self.assertTrue(all(0 <= i < len(text) and phone for i, phone in phones.items()))


class GuiTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        from studio_gui import App
        self.save = patch.object(engine, 'save_json')
        self.load = patch.object(engine, 'load_json', return_value={})
        self.save.start()
        self.load.start()
        self.root = tk.Tk()
        self.app = App(self.root)
        import voice_tasks as workflow
        self.cache_tmp = tempfile.TemporaryDirectory()
        self.app._segment_cache = workflow.SegmentCache(self.cache_tmp.name)
        self.dialogs = [patch('studio_gui.messagebox.askyesnocancel', return_value=False),
                        patch('studio_gui.messagebox.askokcancel', return_value=True),
                        patch('studio_gui.messagebox.showerror'), patch('studio_gui.messagebox.showinfo')]
        for p in self.dialogs:
            p.start()
        self.root.update()

    def tearDown(self):
        self.app.on_close()
        self.cache_tmp.cleanup()
        for p in self.dialogs:
            p.stop()
        self.load.stop()
        self.save.stop()

    def test_engine_credentials_do_not_cross(self):
        a = self.app
        a.key_var.set('azure-only')
        a.engine_var.set(engine.ENGINE_CHOICES[2])
        a.on_engine_switch()
        self.assertEqual(a.key_var.get(), '')
        a.region_var.set('My-Model_V1')
        a._save_cfg()
        self.assertEqual(a.region_var.get(), 'My-Model_V1')
        a.engine_var.set(engine.ENGINE_CHOICES[0])
        a.on_engine_switch()
        self.assertEqual(a.key_var.get(), 'azure-only')

    def test_config_entries_have_right_click_menu(self):
        a = self.app
        for entry in (a.key_entry, a.region_entry, a.ep_entry, a.port_entry):
            self.assertTrue(str(entry.bind("<Button-3>")).strip())
            self.assertIsNotNone(getattr(entry, "_sv_entry_menu", None))

    def test_typing_key_schedules_save_and_hints_when_unremembered(self):
        a = self.app
        a.remember_var.set(False)
        a._key_hint_shown = False
        a.key_var.set("typed-secret")
        self.assertIsNotNone(a._cfg_pending)
        self.assertIn("记住Key", a.status.cget("text"))

    def test_config_save_kicks_poll_only_on_main_thread(self):
        # 工作线程调 root.after 会卡死 _bg_polling，合成完成事件永不到达。
        import threading
        from studio_gui import App
        a = self.app
        main_thread = threading.current_thread()
        kick_threads = []
        orig = App._bg_kick

        def spy(spied_self):
            kick_threads.append(threading.current_thread())
            return orig(spied_self)

        a.key_var.set("kick-check")
        with patch.object(App, "_bg_kick", autospec=True, side_effect=spy):
            a._save_cfg()
            a._flush_cfg()
            fut = a._cfg_pool.submit(lambda: None)
            fut.result(timeout=30)
            self.root.update()
        self.assertTrue(kick_threads)
        self.assertTrue(all(t is main_thread for t in kick_threads))

    def test_remembered_key_reaches_persist_payload(self):
        a = self.app
        a.remember_var.set(True)
        a.key_var.set("persist-secret")
        a._save_cfg()
        pending = a._cfg_pending
        self.assertTrue(pending["remember_key"])
        self.assertEqual(pending["key"], "persist-secret")
        self.assertEqual(pending["engine_profiles"]["azure"]["key"], "persist-secret")
        a.remember_var.set(False)
        a.key_var.set("temp-secret")
        a._save_cfg()
        pending = a._cfg_pending
        self.assertEqual(pending["key"], "")
        self.assertEqual(pending["engine_profiles"]["azure"]["key"], "")

    def test_theme_palettes_are_valid_and_readable(self):
        import theme

        def lum(hex_color):
            r, g, b = (int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5))
            f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
            return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)

        def ratio(a, b):
            la, lb = sorted((lum(a), lum(b)), reverse=True)
            return (la + 0.05) / (lb + 0.05)

        keys = set(theme.THEMES["warm"])
        self.assertEqual(set(theme.THEME_ORDER), set(theme.THEMES))
        self.assertEqual(set(theme.THEME_LABELS), set(theme.THEMES))
        for name, pal in theme.THEMES.items():
            self.assertEqual(set(pal), keys, name)
            for k, v in pal.items():
                vals = v if k == "ROLE" else [v]
                if k == "ROLE":
                    self.assertEqual(len(vals), 6, name)
                for c in vals:
                    self.assertRegex(c, r"^#[0-9a-f]{6}$", (name, k))
            self.assertGreaterEqual(ratio(pal["FG"], pal["PANEL"]), 4.5, (name, "正文"))
            self.assertGreaterEqual(ratio(pal["FEEDBACK"], pal["BG"]), 4.5, (name, "描述"))
        self.assertEqual(theme.THEMES["warm"]["FG"], "#000000")
        self.assertEqual(theme.THEMES["mist"]["FG"], "#000000")

    def test_theme_switch_rebuilds_and_persists(self):
        import theme
        a = self.app
        try:
            with patch.object(engine, "save_json") as mock_save:
                a.switch_theme("mist")
                self.root.update()
                self.assertEqual(theme.ACTIVE, "mist")
                import tkinter.ttk as ttk
                self.assertEqual(ttk.Style(self.root).lookup("Status.TLabel", "foreground"),
                                 theme.THEMES["mist"]["FEEDBACK"])
                self.assertEqual(str(a.text.cget("bg")), theme.THEMES["mist"]["PANEL"])
                self.assertEqual(str(a.text.cget("fg")), theme.THEMES["mist"]["FG"])
                self.assertIn("雾蓝", a.status.cget("text"))
                a._flush_cfg()
                fut = a._cfg_pool.submit(lambda: None)
                fut.result(timeout=30)
                cfg = mock_save.call_args[0][1]
                self.assertEqual(cfg["theme"], "mist")
        finally:
            a.switch_theme("warm")
            self.root.update()
        self.assertEqual(theme.ACTIVE, "warm")
        from theme import FG
        self.assertEqual(FG, "#000000")

    def test_theme_switch_preserves_manuscript(self):
        a = self.app
        try:
            a._set_real_text('稿件甲')
            a.switch_theme("mist")
            self.root.update()
            self.assertEqual(a._get_real_text(), '稿件甲')
        finally:
            a.switch_theme("warm")
            self.root.update()

    def test_parallel_synthesis_is_bounded_and_output_stays_in_order(self):
        import threading
        import time
        a = self.app
        snap = {'engine': engine.ENGINE_CHOICES[0]}
        jobs = [('voice', 'role', str(i)) for i in range(4)]
        barrier = threading.Barrier(2)
        lock = threading.Lock()
        active, peak = [0], [0]
        def synth(text, *args, **kwargs):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            if text in ('0', '1'):
                barrier.wait(timeout=2)
            if text == '0':
                time.sleep(.06)
            kwargs['on_stage']('接收音频')
            kwargs['on_progress'](50, 100)
            with lock:
                active[0] -= 1
            return text.encode()
        a._prog_begin('multi', 4, a._seq, '准备合成')
        with patch.object(a, 'synth_net', side_effect=synth):
            result = a._synth_jobs(a._seq, jobs, snap)
        self.assertEqual(result, [b'0', b'1', b'2', b'3'])
        self.assertEqual(peak[0], 2)
        a._bg_poll()
        self.assertEqual(a._prog['done'], 4)
        self.assertEqual(a._prog_ui['value'], 4)

    def test_parallel_progress_aggregates_out_of_order_and_does_not_reset(self):
        a = self.app
        a._prog_begin('multi', 2, a._seq, '准备合成')
        a._bgq.put(('progress', a._seq, (20, 100, 2)))
        a._bgq.put(('progress', a._seq, (50, 100, 1)))
        a._bg_poll()
        self.assertAlmostEqual(a._prog_ui['value'], .7)
        a._bgq.put(('segment', a._seq, 2))
        a._bg_poll()
        self.assertEqual(a._prog_ui['value'], 1.5)
        a._bgq.put(('phase', a._seq, ('等待重试', 1)))
        a._bgq.put(('progress', a._seq, (10, 100, 1)))
        a._bg_poll()
        self.assertEqual(a._prog_ui['value'], 1.5)
        progress, stage = a._progress_callbacks(a._seq)
        a.on_stop()
        with self.assertRaises(engine.SynthesisCancelled):
            stage('旧任务')
        with self.assertRaises(engine.SynthesisCancelled):
            progress(1, 2)

    def test_config_changes_are_coalesced_and_written_off_main_thread(self):
        import threading
        a = self.app
        main_id = threading.get_ident()
        thread_ids = []
        with patch.object(engine, 'save_json', side_effect=lambda *args: thread_ids.append(threading.get_ident())) as save:
            for i in range(10):
                a._dub['slots'][0]['name'].set(f'角色{i}')
            save.assert_not_called()
            self.root.after(650, self.root.quit)
            self.root.mainloop()
            self.assertEqual(save.call_count, 1)
            # settings.json 是全量覆盖写：multidub 必须随每次保存回写，否则槽位配置被抹掉。
            multidub = save.call_args.args[1].get('multidub')
            self.assertIsInstance(multidub, list)
            self.assertEqual(len(multidub), 9)
            self.assertEqual(multidub[0]['name'], '角色9')  # 与面板最后一次编辑一致
            self.assertNotEqual(thread_ids[0], main_id)

    def test_import_parser_runs_in_worker_and_does_not_overwrite_new_edits(self):
        import threading
        a = self.app
        main_id, ids = threading.get_ident(), []
        release = threading.Event()
        def parse(path):
            ids.append(threading.get_ident())
            release.wait(timeout=2)
            return '导入原稿'
        with patch('studio_gui.filedialog.askopenfilename', return_value='test.pdf'), \
                patch.object(a, '_read_document', side_effect=parse):
            a.import_txt()
            a._set_real_text('用户正在编辑')
            release.set()
            self.root.after(300, self.root.quit)
            self.root.mainloop()
        self.assertEqual(a._get_real_text(), '用户正在编辑')
        self.assertEqual(len(ids), 1)
        self.assertNotEqual(ids[0], main_id)

    def test_stale_tasks_and_real_progress(self):
        a = self.app
        a._prog_begin('pulse', 0, a._seq, 'test')
        self.assertEqual(a._prog_ui['value'], 0)
        callback = Mock()
        a._bg_n = 1
        a._bgq.put(('done', a._seq - 1, callback))
        a._bg_poll()
        callback.assert_not_called()
        a._bgq.put(('progress', a._seq, (50, 100)))
        a._bg_poll()
        self.assertEqual(a._prog_ui['value'], .5)
        self.assertEqual(a._prog_ui['maximum'], 3)
        a._bgq.put(('segment', a._seq - 1, 99))
        a._bg_poll()
        self.assertEqual(a._prog['done'], 0)
        a._bgq.put(('segment', a._seq, 1))
        a._bg_poll()
        self.assertEqual(a._prog['done'], 1)
        a._prog_cancel()

    def test_unknown_progress_does_not_invent_percent_or_draw_blue_block(self):
        a = self.app
        a._prog_begin('pulse', 0, a._seq, '等待合成')
        start = a._prog_ui['value']
        self.root.after(200, self.root.quit)
        self.root.mainloop()
        self.assertEqual(a._prog_ui['value'], start)
        a._bgq.put(('phase', a._seq, ('接收音频', 1)))
        a._bgq.put(('progress', a._seq, (8192, 0)))
        a._bg_poll()
        self.assertEqual(a._prog_ui['mode'], 'determinate')
        self.assertEqual(a._prog_ui['value'], 0)
        self.assertIn('8.0 KB', a.prog_lab.cget('text'))
        self.assertNotIn('%', a.prog_lab.cget('text'))
        a._prog_cancel()
        self.assertIsNone(a._prog['after'])
        self.assertNotIn('animate', a._prog_ui)
        from studio_gui import ACCENT, GREEN
        fills = [a.prog.itemcget(item, 'fill') for item in a.prog.find_all()]
        self.assertNotIn(ACCENT, fills)
        self.assertIn(GREEN, fills)

    def test_multi_segment_progress_is_not_download_percentage(self):
        a = self.app
        a._prog_begin('multi', 4, a._seq, '准备合成')
        a._bgq.put(('segment', a._seq, 1))
        a._bgq.put(('phase', a._seq, ('接收音频', 2)))
        a._bgq.put(('progress', a._seq, (500, 1000)))
        a._bg_poll()
        self.assertEqual(a._prog_ui['value'], 1.5)
        self.assertEqual(a._prog_ui['maximum'], 6)
        self.assertIn('1/4段', a.prog_lab.cget('text'))
        self.assertIn('50%', a.prog_lab.cget('text'))
        # 重试清空当前段字节进度，不清空已完成段数。
        a._bgq.put(('phase', a._seq, ('等待重试', 2)))
        a._bg_poll()
        self.assertEqual(a._prog['received'], 0)
        self.assertEqual(a._prog['done'], 1)
        self.assertEqual(a._prog_ui['value'], 1.5)
        for index in (2, 3, 4):
            a._bgq.put(('segment', a._seq, index))
        a._bgq.put(('phase', a._seq, ('正在拼接音频', 0)))
        a._bg_poll()
        self.assertEqual(a._prog_ui['value'], 4)
        self.assertIn('正在拼接音频', a.prog_lab.cget('text'))
        self.assertNotEqual(a._prog_ui['value'], a._prog_ui['maximum'])
        a._bgq.put(('post_complete', a._seq, 1))
        a._bgq.put(('phase', a._seq, ('正在保存音频', 0)))
        a._bg_poll()
        self.assertEqual(a._prog_ui['value'], 5)

    def test_audio_saved_in_worker_before_progress_finishes(self):
        import threading
        a = self.app
        a._prog_begin('pulse', 0, a._seq, '准备合成')
        threads = []
        def save(data, path):
            threads.append(threading.get_ident())
            self.assertEqual(data, b'audio')
            return path
        with patch.object(a, '_save_audio', side_effect=save), \
                patch('docutils.prepare_playback_audio', return_value=(b'audio', b'pcm')), \
                patch.object(a.player, 'play_file') as play, \
                patch.object(a, '_watch_play'):
            a._bg(lambda: a._prepare_playback(a._seq, b'audio', 'test.mp3', '测试'))
            self.root.after(300, self.root.quit)
            self.root.mainloop()
            self.assertEqual(len(threads), 1)
            self.assertNotEqual(threads[0], threading.get_ident())
            play.assert_not_called()  # 正式合成与播放已拆分；试听仍自动播放。
            self.assertLessEqual(a._prog_ui['value'], a._prog_ui['maximum'])
            self.assertNotIn('animate', a._prog_ui)
            self.assertIsNone(a._prog['after'])

    def test_single_audition_and_multi_wire_progress_callbacks(self):
        a = self.app
        a.key_var.set('fake')
        a._set_real_text('第一段\n第二段')
        captured = []
        def synth(text, snap, voice_id=None, on_progress=None, on_stage=None):
            self.assertTrue(callable(on_progress))
            self.assertTrue(callable(on_stage))
            on_stage('接收音频')
            on_progress(4096, 8192)
            return b'audio'
        with patch.object(a, '_bg', side_effect=captured.append), \
                patch.object(a, 'synth_net', side_effect=synth) as net, \
                patch.object(a, '_prepare_playback'):
            a._dub_all(False)
            a.on_single()
            captured.pop()()
            a._complete_task('completed')
            a._audition_voice('测试', a.voices[a.selected])
            captured.pop()()
            a._complete_task('completed')
            a._dub['slots'][0]['on'].set(True)
            a.on_multi()
            captured.pop()()
            self.assertEqual(net.call_count, 2)  # 多人同参数的整段复用首次合成缓存。
            a._bg_poll()
            self.assertEqual(a._prog['done'], 1)
            self.assertIn('正在拼接音频', a.prog_lab.cget('text'))

    def test_multi_preserves_parameter_snapshot_and_reports_concat_failure(self):
        a = self.app
        a.key_var.set('fake')
        a.rate_var.set(80)
        a.pitch_var.set(2)
        a.vol_var.set(90)
        a._dub['slots'][0]['name'].set('旁白')
        a._dub['slots'][1]['name'].set('小明')
        a._dub['slots'][1]['on'].set(True)
        a._set_real_text('[旁白]第一句\n[小明]第二句')
        captured, snapshots = [], []
        def synth(text, snap, *args, **kwargs):
            snapshots.append(dict(snap))
            return b'audio'
        with patch.object(a, '_bg', side_effect=captured.append), \
                patch.object(a, 'synth_net', side_effect=synth), \
                patch('docutils.prepare_playback_audio', side_effect=RuntimeError('拼接失败')):
            a.on_multi()
            # 后台开始前改动滑块，已启动任务仍使用同一份快照。
            a.rate_var.set(120)
            a.pitch_var.set(-5)
            a.vol_var.set(130)
            with self.assertRaisesRegex(RuntimeError, '拼接失败'):
                captured.pop()()
        self.assertEqual(len(snapshots), 2)
        self.assertEqual({k:v for k,v in snapshots[0].items() if k != '_index'},
                         {k:v for k,v in snapshots[1].items() if k != '_index'})
        self.assertEqual((snapshots[0]['rate'], snapshots[0]['pitch'], snapshots[0]['vol']),
                         ('80%', '+2Hz', '-10%'))

    def test_selecting_voice_does_not_cut_off_playback(self):
        a = self.app
        display = next(d for d in a.voices if d != a.selected)
        seq = a._seq
        with patch.object(a.player, 'stop') as stop:
            a.tree.selection_set(a._iid[display])
            self.root.update()
            self.assertEqual(a.selected, display)
            self.assertEqual(a._seq, seq)
            stop.assert_not_called()

    def test_audition_cache_reuses_audio_and_invalidates_on_parameter_change(self):
        a = self.app
        a.key_var.set('fake')
        captured = []
        voice = a.voices[a.selected]
        with patch.object(a, '_bg', side_effect=captured.append), \
                patch.object(a, 'synth_net', return_value=b'raw') as net, \
                patch('docutils.prepare_playback_audio', return_value=(b'mp3', b'pcm')) as prepare, \
                patch.object(a, '_save_audio', return_value='test.mp3'):
            for _ in range(2):
                a._audition_voice('测试', voice)
                done = captured.pop()()
                done.discard()
                a._complete_task('completed')
            self.assertEqual(net.call_count, 1)
            self.assertEqual(prepare.call_count, 1)
            self.assertTrue(net.call_args.args[1]['audition'])
            a.rate_var.set(120)
            a._audition_voice('测试', voice)
            captured.pop()().discard()
            self.assertEqual(net.call_count, 2)
            a.busy = True
            a._audition_voice('测试', voice)
            self.assertEqual(captured, [])


    def test_mci_plays_pcm_from_start_to_full_length(self):
        from studio_gui import MciPlayer
        p = MciPlayer()
        def cmd(command):
            return '2000' if command.endswith(' length') else ''
        with patch.object(p, '_cmd', side_effect=cmd) as calls:
            p.play_file('test.wav')
        commands = [c.args[0] for c in calls.call_args_list]
        self.assertTrue(any('type waveaudio' in c for c in commands))
        self.assertIn('seek ttsstudio to start', commands)
        self.assertIn('play ttsstudio from 0', commands)
        self.assertFalse(any(c.startswith('play ') and ' to ' in c for c in commands))

    def test_early_driver_stop_is_not_reported_as_success(self):
        from studio_gui import MciPlayer
        p = MciPlayer()
        p.opened, p.length_ms, p.started = True, 3000, 0
        with patch('studio_gui.time.monotonic', return_value=1), \
                patch.object(p, '_cmd', side_effect=['stopped', '1000']):
            self.assertTrue(p.is_playing())
        with patch('studio_gui.time.monotonic', return_value=2), \
                patch.object(p, '_cmd', side_effect=['stopped', '1000']):
            with self.assertRaisesRegex(RuntimeError, '提前结束'):
                p.is_playing()
        with patch('studio_gui.time.monotonic', return_value=3), \
                patch.object(p, '_cmd', side_effect=['stopped', '3000']):
            self.assertTrue(p.is_playing())
        with patch('studio_gui.time.monotonic', return_value=3.4), \
                patch.object(p, '_cmd', side_effect=['stopped', '3000']):
            self.assertFalse(p.is_playing())

    def test_transient_stopped_driver_state_can_resume(self):
        from studio_gui import MciPlayer
        p = MciPlayer()
        p.opened, p.length_ms, p.started = True, 3000, 0
        with patch('studio_gui.time.monotonic', return_value=1), \
                patch.object(p, '_cmd', side_effect=['stopped', '1000', 'playing']):
            self.assertTrue(p.is_playing())
            self.assertTrue(p.is_playing())
            self.assertIsNone(p._stopped_since)

    def test_zoom_does_not_stop_playback_or_invalidate_synthesis(self):
        a = self.app
        for finished in (False, True):
            a._prog_begin('single', 1, a._seq, '正在播放' if finished else '等待合成')
            a._prog['finished'] = finished
            seq = a._seq
            with patch.object(a.player, 'stop') as stop:
                a.zoom_var.set(125 if finished else 100)
                a.on_zoom()
                self.root.update()
                stop.assert_not_called()
            self.assertEqual(a._seq, seq)
            self.assertTrue(a._prog['active'])
            self.assertTrue(a.prog.winfo_ismapped())

    def test_double_click_header_or_blank_does_not_restart_audio(self):
        a = self.app
        event = Mock(x=20, y=10)
        with patch.object(a, 'on_audition') as audition:
            for region in ('heading', 'separator', 'nothing'):
                with patch.object(a.tree, 'identify_region', return_value=region):
                    a._on_tree_audition(event)
            audition.assert_not_called()
            row = next(iter(a._iid.values()))
            with patch.object(a.tree, 'identify_region', return_value='cell'), \
                    patch.object(a.tree, 'identify_row', return_value=row):
                a._on_tree_audition(event)
            audition.assert_called_once()

    @unittest.skipUnless(__import__('sys').platform == 'win32', 'Windows MCI integration')
    def test_native_mci_silent_wav_reaches_end(self):
        import time
        from studio_gui import MciPlayer
        player = MciPlayer()
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp, 'silence.wav'))
            with wave.open(path, 'wb') as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(24000)
                wav.writeframes(b'\0\0' * 12000)
            try:
                player.play_file(path)
                self.assertEqual(player.length_ms, 500)
                deadline = time.monotonic() + 3
                while player.is_playing() and time.monotonic() < deadline:
                    time.sleep(.05)
                self.assertFalse(player.is_playing())
            finally:
                player.stop()

    def test_voice_alignment_survives_engine_switch(self):
        a = self.app
        a._set_real_text('原稿保持不变\n[旁白] 测试')
        original_text = a.text.get('1.0', 'end-1c')
        widgets = [a.text, a.tree, a.b_import, a.b_single, a.b_stop, a.b_open]
        widgets += [s['combo'] for s in a._dub['slots']]
        ids = [w.winfo_id() for w in widgets]
        for choice in engine.ENGINE_CHOICES:
            a.engine_var.set(choice)
            a.on_engine_switch()
            self.root.update()
            self.assertEqual(ids, [w.winfo_id() for w in widgets])
            self.assertEqual(original_text, a.text.get('1.0', 'end-1c'))
            self.assertTrue(a.tree.get_children())
            for column in ('gender', 'name', 'vid'):
                self.assertEqual(str(a.tree.column(column, 'anchor')), 'center')
                self.assertEqual(str(a.tree.heading(column, 'anchor')), 'center')
            self.assertEqual(len(a._dub['slots']), 9)
            for slot in a._dub['slots']:
                combo = slot['combo']
                self.assertEqual(str(combo.cget('justify')), 'left')
                self.assertEqual(str(combo.cget('state')), 'readonly')
                self.assertIn(combo.get(), combo.cget('values'))
                # 调用实际 postcommand，检查展开列表自身的对齐。
                combo.tk.call(combo.cget('postcommand'))
                popup = combo.tk.call('ttk::combobox::PopdownWindow', combo)
                if self.root.tk.call('package', 'vcompare', self.root.tk.call('info', 'patchlevel'), '9.0') >= 0:
                    self.assertEqual(str(combo.tk.call(f'{popup}.f.l', 'cget', '-justify')), 'left')

    def test_voice_scrollbar_visible_at_minimum_size(self):
        a = self.app
        for zoom in (80, 100, 125, 150):
            a.zoom_var.set(zoom)
            a.on_zoom()
            for size in ('980x900', '640x640', '800x700', '640x640'):
                with self.subTest(zoom=zoom, size=size):
                    self.root.geometry(size)
                    self.root.update()
                    # 等待流式底部按钮栏和窗口的防抖布局结束。
                    self.root.after(180, self.root.quit)
                    self.root.mainloop()
                    self.root.update()
                    sb = a.voice_scrollbar
                    self.assertTrue(sb.winfo_ismapped())
                    self.assertGreater(sb.winfo_height(), 30)
                    self.assertEqual(sb.winfo_width(), sb.winfo_reqwidth())
                    self.assertLessEqual(sb.winfo_rootx() + sb.winfo_width(),
                                         a.voice_box.winfo_rootx() + a.voice_box.winfo_width())
                    self.assertLessEqual(a.tree.winfo_rootx() + a.tree.winfo_width(), sb.winfo_rootx())
                    self.assertLessEqual(sum(a.tree.column(c, 'width') for c in ('gender', 'name', 'vid')),
                                         a.tree.winfo_width())
                    self.assertLessEqual(abs(a.voice_box.winfo_width() - a.dub_box.winfo_width()), 1)
                    for i in range(80):
                        a.tree.insert('', 'end', values=('女', f'测试{i}', 'voice'))
                    a.tree.yview_moveto(1)
                    self.root.update()
                    self.assertGreater(a.tree.yview()[0], 0)

    def test_zoom_rebuilds_one_ui_and_preserves_slots(self):
        a = self.app
        a._set_real_text('缩放保留原稿')
        a._dub['slots'][0]['name'].set('讲解员')
        count = len(self.root.winfo_children())
        for zoom in (125, 100):
            a.zoom_var.set(zoom)
            a.on_zoom()
            self.root.update()
            self.assertEqual(len(self.root.winfo_children()), count)
            self.assertEqual(a._get_real_text(), '缩放保留原稿')
            self.assertEqual(a._dub['slots'][0]['name'].get(), '讲解员')
            self.assertTrue(a.tree.get_children())

    def test_bg_poll_survives_handler_exception_and_keeps_chain(self):
        a = self.app
        seen = []
        def boom(payload):
            seen.append(payload)
            raise RuntimeError('handler炸了')
        with patch.object(a, '_finish_done', side_effect=boom):
            a._bgq.put(('done', a._seq, 1))
            a._bg_poll()                 # 单条事件异常不许外抛、不许断链
        self.assertEqual(seen, [1])
        a._bg_n = 1
        a._bgq.put(('config_done', 0, None))
        a._bg_poll()                     # 轮询链仍然活着
        self.assertEqual(a._bg_n, 0)

    def test_cache_write_failure_does_not_kill_segment_task(self):
        a = self.app
        snap = {'engine': engine.ENGINE_CHOICES[1]}          # Edge 走串行路径
        jobs = [('zh-CN-YunxiNeural', '旁白', '缓存写失败仍是成功合成')]
        a._prog_begin('multi', 1, a._seq, '准备合成')
        with patch.object(a._segment_cache, 'save', side_effect=OSError(28, '磁盘空间不足')), \
             patch.object(a, 'synth_net', return_value=b'AUDIO'):
            result = a._synth_jobs(a._seq, jobs, snap)       # 写缓存失败不许判死整单任务
        self.assertEqual(result, [b'AUDIO'])
        a._bg_poll()
        self.assertEqual(a._prog['done'], 1)

    def test_preview_segments_do_not_enter_project_cache_list(self):
        a = self.app
        key = 'e' * 64
        a._task_preview = True
        a._bgq.put(('cache_segment', a._seq, key))            # 局部试听的段
        a._bg_poll()
        self.assertNotIn(key, a._segment_keys)
        a._task_preview = False
        a._bgq.put(('cache_segment', a._seq, key))            # 正式合成的段
        a._bgq.put(('cache_segment', a._seq + 1, 'f' * 64))   # 旧任务的段
        a._bg_poll()
        self.assertIn(key, a._segment_keys)
        self.assertNotIn('f' * 64, a._segment_keys)

    def test_fail_does_not_cancel_active_task_progress(self):
        a = self.app
        a._prog_begin('multi', 5, a._seq, '准备合成')
        a._active_task = True
        a._fail('校验错误')                     # showerror 已被 setUp patch
        self.assertTrue(a._prog['active'])       # 活动任务的进度不被错误弹窗撤销
        a._active_task = False
        a._prog_cancel()
        a._fail('无任务时正常清进度')
        self.assertFalse(a._prog['active'])

    def test_close_and_save_survive_invalid_export_settings(self):
        a = self.app
        a._project_baseline = a._project_payload()
        a.export_dir.set('')             # 非法输出目录
        a.leading_ms.set(99999)          # 非法留白
        self.assertTrue(a._confirm_discard())   # 关窗询问链不再被导出设置卡死
        a._save_cfg()
        self.assertEqual(a._cfg_pending['export']['leading_ms'], 5000)
        self.assertEqual(a._cfg_pending['export']['directory'], '')


class StabilityTests(unittest.TestCase):
    def test_segment_cache_capacity_prunes_oldest_pairs(self):
        import voice_tasks as workflow
        with tempfile.TemporaryDirectory() as tmp:
            cache = workflow.SegmentCache(tmp)
            cache.MAX_BYTES = 300
            k1, k2 = 'a' * 64, 'b' * 64
            cache.save(k1, b'x' * 100)
            cache._prune_at = 0
            cache.save(k2, b'y' * 100)
            self.assertIsNone(cache.load(k1))          # 最旧片段成对淘汰
            self.assertEqual(cache.load(k2), b'y' * 100)

    def test_forward_server_caps_body_and_text_with_timeout(self):
        import http.client
        import socket
        import threading
        import json as jsonlib
        import forward_server
        forward_server._Handler.synth_fn = staticmethod(lambda t, v, r: b'AUDIO')
        forward_server._Handler.voices_fn = staticmethod(lambda: [{'name': 'n', 'id': 'i'}])
        self.assertEqual(forward_server._Handler.timeout, 30)
        srv = forward_server.ThreadingHTTPServer(('127.0.0.1', 0), forward_server._Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        port = srv.server_address[1]
        try:
            conn = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
            conn.request('POST', '/forward', body=jsonlib.dumps({'text': '你好'}),
                         headers={'Content-Type': 'application/json'})
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.read(), b'AUDIO')
            conn.close()
            conn = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
            conn.request('POST', '/forward',
                         body=jsonlib.dumps({'text': '字' * (forward_server.MAX_TEXT + 1)}),
                         headers={'Content-Type': 'application/json'})
            resp = conn.getresponse()
            self.assertEqual(resp.status, 413)         # 超长文本
            resp.read()
            conn.close()
            with socket.create_connection(('127.0.0.1', port), 5) as sock:
                sock.sendall((f'POST /forward HTTP/1.1\r\nHost: t\r\n'
                              f'Content-Length: {forward_server.MAX_BODY + 1}\r\n\r\n').encode())
                head = sock.recv(4096)
            self.assertIn(b'413', head)                # 声明超大请求体：读头即拒
            conn = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
            conn.request('GET', '/voices')
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
            resp.read()
            conn.close()
        finally:
            srv.shutdown()
            srv.server_close()


class DeepRegressionTests(unittest.TestCase):
    """深扫修复的守护测试：组件激活顺序、项目类型闸门、编码启发、占用换号等。"""

    def test_ocr_component_activates_before_numpy_import(self):
        import inspect
        import docutils
        src = inspect.getsource(docutils._ocr_pages)
        self.assertLess(src.index('create_ocr()'), src.index('import numpy'),
                        '标准版 numpy 随 OCR 组件分发：必须先激活组件再导入 numpy')

    def test_save_project_returns_missing_count_and_leaves_no_tmp(self):
        import voice_tasks as workflow
        with tempfile.TemporaryDirectory() as td:
            cache = workflow.SegmentCache(Path(td) / 'cache')
            good = 'a' * 64
            cache.save(good, b'AUDIO')
            payload = {'text': 'hi', 'slots': [], 'engine': 'Edge免费(免Key)', 'person': 'p',
                       'export': {}, 'exports': [], 'parameters': {'rate': 100},
                       'segments': [good, 'not-a-fingerprint', 123],
                       'engine_settings': {'region': 'eastus', 'endpoint': 'https://x'}}
            path = Path(td) / 'p.smartvoice'
            self.assertEqual(workflow.save_project(path, payload, cache), 0)
            self.assertFalse(Path(str(path) + '.tmp').exists())   # 失败/成功都不留临时文件
            loaded = workflow.load_project(path, cache)
            self.assertEqual(loaded['text'], 'hi')
            payload['segments'] = ['b' * 64]                      # 缓存中缺失的段
            self.assertEqual(workflow.save_project(path, payload, cache), 1)

    def test_load_project_type_gates_reject_bad_manifests(self):
        import zipfile
        import voice_tasks as workflow
        with tempfile.TemporaryDirectory() as td:
            cache = workflow.SegmentCache(Path(td) / 'cache')

            def build(name, mutate):
                project = {'text': 'hi', 'slots': [], 'engine': 'Edge免费(免Key)', 'person': 'p',
                           'export': {}, 'exports': [], 'parameters': {},
                           'engine_settings': {'region': '', 'endpoint': ''},
                           'segments': [], 'format': 'SmartVoiceProject', 'schema': 1}
                mutate(project)
                path = Path(td) / name
                with zipfile.ZipFile(path, 'w') as z:
                    z.writestr('project.json', json.dumps(project, ensure_ascii=False))
                return path

            cases = [
                ('工程设置非对象', lambda p: p.__setitem__('engine_settings', ['x'])),
                ('region非字符串', lambda p: p['engine_settings'].__setitem__('region', 3)),
                ('rate非整数', lambda p: p['parameters'].__setitem__('rate', 'fast')),
                ('exports非列表', lambda p: p.__setitem__('exports', {'a': 1})),
            ]
            for i, (label, mutate) in enumerate(cases):
                path = build(f'bad{i}.smartvoice', mutate)
                with self.assertRaises(ValueError, msg=f'应拒绝: {label}'):
                    workflow.load_project(path, cache)

    def test_read_text_detects_bomless_utf16_and_normalizes_separators(self):
        import documents
        with tempfile.TemporaryDirectory() as td:
            le = Path(td) / 'le.txt'
            le.write_bytes('大家好，这是测试文本。Hello world 12345!'.encode('utf-16-le'))
            self.assertEqual(documents.read_text(le), '大家好，这是测试文本。Hello world 12345!')
            be = Path(td) / 'be.txt'
            be.write_bytes('Hello world from SmartVoice.'.encode('utf-16-be'))
            self.assertEqual(documents.read_text(be), 'Hello world from SmartVoice.')
            sep = Path(td) / 'sep.txt'
            sep.write_bytes('第一行 第二行\x0b第三行 第四行\x85'.encode('utf-8'))
            self.assertEqual(documents.read_text(sep), '第一行\n第二行\n第三行\n第四行\n')
            bom = Path(td) / 'bom.txt'
            bom.write_bytes('﻿带BOM的文本'.encode('utf-8'))
            self.assertEqual(documents.read_text(bom), '带BOM的文本')

    def test_engine_load_json_reads_utf8_bom(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'cfg.json'
            path.write_bytes('﻿{"region": "eastus"}'.encode('utf-8'))
            self.assertEqual(engine.load_json(path, {}), {'region': 'eastus'})

    def test_unique_export_skips_locked_target_and_numbers_next(self):
        import os
        import storage
        with tempfile.TemporaryDirectory() as td:
            real_rename = os.rename
            calls = {'rename': 0}

            def guarded_rename(src, dst):
                calls['rename'] += 1
                if calls['rename'] == 1:
                    raise PermissionError(13, '目标被播放器占用')
                return real_rename(src, dst)

            with patch('os.link', side_effect=PermissionError(13, '无硬链接')), \
                 patch('os.rename', side_effect=guarded_rename):
                path = storage.unique_export(td, '配音', 'mp3', b'data')
            self.assertEqual(Path(path).name, '配音-1.mp3')   # 占用换号而不是导出失败
            self.assertEqual(Path(path).read_bytes(), b'data')

    def test_forward_voices_failure_returns_502_json(self):
        import http.client
        import threading
        import forward_server

        def boom():
            raise RuntimeError('拉取人声失败')

        prev = forward_server._Handler.voices_fn
        forward_server._Handler.voices_fn = staticmethod(boom)
        srv = forward_server.ThreadingHTTPServer(('127.0.0.1', 0), forward_server._Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            conn = http.client.HTTPConnection('127.0.0.1', srv.server_address[1], timeout=5)
            conn.request('GET', '/voices')
            resp = conn.getresponse()
            self.assertEqual(resp.status, 502)                # 失败回结构化 502，而不是断连无响应
            body = json.loads(resp.read().decode('utf-8'))
            self.assertIn('拉取人声失败', body['error'])
            conn.close()
        finally:
            srv.shutdown()
            srv.server_close()
            forward_server._Handler.voices_fn = prev

    def test_response_bytes_cap_rejects_oversized_stream(self):
        import requests

        class FakeResponse:
            headers = {}

            def iter_content(self, chunk_size=4096):
                for _ in range(64):
                    yield b'x' * 1024

        with patch.object(engine, 'MAX_RESPONSE_BYTES', 8 * 1024):
            with self.assertRaises(requests.exceptions.ChunkedEncodingError):
                engine._read_response_bytes(FakeResponse())

    def test_read_text_skips_gb18030_for_binary_data_with_nul(self):
        import documents
        with tempfile.NamedTemporaryFile(suffix='.txt', delete=False) as f:
            f.write(b'Header\x00\x01\x02Binary\x00Data')
            tmp = Path(f.name)
        try:
            with self.assertRaises(ValueError):
                documents.read_text(tmp)
        finally:
            tmp.unlink(missing_ok=True)

    def test_forward_server_uses_connection_close_header(self):
        import http.client
        import threading
        import forward_server

        srv = forward_server.ThreadingHTTPServer(('127.0.0.1', 0), forward_server._Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            conn = http.client.HTTPConnection('127.0.0.1', srv.server_address[1], timeout=5)
            conn.request('GET', '/')
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers.get('Connection'), 'close')
            conn.close()
        finally:
            srv.shutdown()
            srv.server_close()

    def test_forward_post_empty_voice_rate_normalized(self):
        import http.client
        import threading
        import forward_server

        seen = {}

        def fake_synth(text, voice, rate):
            seen['voice'] = voice
            seen['rate'] = rate
            return b'ID3x'

        prev = forward_server._Handler.synth_fn
        forward_server._Handler.synth_fn = staticmethod(fake_synth)
        srv = forward_server.ThreadingHTTPServer(('127.0.0.1', 0), forward_server._Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            conn = http.client.HTTPConnection('127.0.0.1', srv.server_address[1], timeout=5)
            conn.request('POST', '/forward', body=json.dumps({"text": "hi", "voice": "", "rate": ""}),
                         headers={'Content-Type': 'application/json'})
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.read(), b'ID3x')
            self.assertIsNone(seen['voice'])
            self.assertEqual(seen['rate'], '+0%')
            conn.close()
        finally:
            srv.shutdown()
            srv.server_close()
            forward_server._Handler.synth_fn = prev

    def test_saturated_forward_server_rejects_with_503(self):
        import socket
        import threading
        import time
        import forward_server

        entered = threading.Event()
        release = threading.Event()

        def slow_synth(text, voice, rate):
            entered.set()
            release.wait(timeout=10)
            return b"ID3slow"

        prev = forward_server._Handler.synth_fn
        forward_server._Handler.synth_fn = staticmethod(slow_synth)
        srv = forward_server.BoundedThreadingHTTPServer(
            ('127.0.0.1', 0), forward_server._Handler, max_workers=1, queue_size=0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            holder = socket.create_connection(('127.0.0.1', srv.server_address[1]), timeout=5)
            holder.sendall(b"POST /forward HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                            b"Content-Length: 14\r\n\r\n{\"text\": \"hi\"}")
            self.assertTrue(entered.wait(timeout=5))
            probe = socket.create_connection(('127.0.0.1', srv.server_address[1]), timeout=5)
            try:
                probe.sendall(b"GET /forward?text=hi HTTP/1.1\r\nHost: x\r\n\r\n")
                resp = probe.makefile('rb').readline().decode('latin-1')
                self.assertIn('503', resp)
            finally:
                probe.close()
            release.set()
            deadline = time.monotonic() + 5
            while True:
                check = socket.create_connection(('127.0.0.1', srv.server_address[1]), timeout=5)
                try:
                    check.sendall(b"GET /forward?text=hi HTTP/1.1\r\nHost: x\r\n\r\n")
                    line = check.makefile('rb').readline().decode('latin-1')
                    if '200' in line:
                        break
                finally:
                    check.close()
                self.assertLess(time.monotonic(), deadline)
                time.sleep(.05)
            holder.close()
        finally:
            release.set()
            srv.shutdown()
            srv.server_close()
            forward_server._Handler.synth_fn = prev

    def test_migrate_commits_settings_before_cleaning_source(self):
        import storage
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            legacy = tmp / 'config.json'
            legacy.write_text(json.dumps({'engine': 'Azure(Key)', 'key': 'LEGACY-KEY',
                                          'region': 'eastus', 'endpoint': 'https://x/v1'}),
                              encoding='utf-8')
            settings = tmp / 'settings.json'
            with patch.object(storage, 'SETTINGS_FILE', settings), \
                    patch.object(storage, 'DATA_DIR', tmp), \
                    patch.object(storage, 'CACHE_DIR', tmp / 'cache'):
                storage.migrate_legacy(tmp)
                self.assertTrue(settings.is_file())
                cfg = storage.unlock_settings(storage.read_json(settings))
                self.assertEqual(cfg.get('key'), 'LEGACY-KEY')
                self.assertTrue(cfg.get('remember_key'))
                # 旧源明文已清；幂等：再次迁移直接返回，不动已提交配置。
                storage.migrate_legacy(tmp)
                self.assertEqual(storage.unlock_settings(storage.read_json(settings)).get('key'),
                                 'LEGACY-KEY')

    def test_protect_settings_preserves_vault_on_transient_failure(self):
        import storage
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / 'credentials.json').write_text(json.dumps({'azure': 'OLD-BLOB'}), encoding='utf-8')
            cfg = {'remember_key': True,
                   'engine_profiles': {'azure': {'key': '', 'credential_ref': 'azure'}}}
            with patch.object(storage, 'DATA_DIR', tmp):
                out = storage.protect_settings(cfg)
            vault = json.loads((tmp / 'credentials.json').read_text(encoding='utf-8'))
            self.assertEqual(vault, {'azure': 'OLD-BLOB'})
            self.assertEqual(out['engine_profiles']['azure'].get('credential_ref'), 'azure')

    def test_load_project_rejects_oversized_members(self):
        import voice_tasks as workflow
        import zipfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            for member, size in (('project.json', 11 * 1024**2),):
                archive = tmp / 'big.smartvoice'
                with zipfile.ZipFile(archive, 'w') as z:
                    z.writestr(member, 'x' * size)
                with self.assertRaisesRegex(ValueError, '过大'):
                    workflow.load_project(archive, workflow.SegmentCache(tmp / 'cache'))

    def test_prune_throttle_is_thread_safe(self):
        import threading
        import voice_tasks as workflow
        with tempfile.TemporaryDirectory() as tmp:
            cache = workflow.SegmentCache(Path(tmp) / 'cache')
            errors = []
            def storm():
                try:
                    for _ in range(20):
                        cache._prune()
                except Exception as e:  # noqa: BLE001
                    errors.append(e)
            threads = [threading.Thread(target=storm) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(errors, [])
            self.assertGreater(cache._prune_at, 0)

    def test_srt_inequality_and_known_tags(self):
        import documents
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'sample.srt'
            path.write_text('1\n00:00:01,000 --> 00:00:02,000\n3 < 5 > 2\n\n'
                            '2\n00:00:03,000 --> 00:00:04,000\n<b>加粗</b>正文\n',
                            encoding='utf-8')
            text = documents.read_document(path)
        self.assertIn('3 < 5 > 2', text)
        self.assertIn('加粗正文', text)
        self.assertNotIn('<b>', text)

    def test_lrc_extended_tags_stripped(self):
        import documents
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'sample.lrc'
            path.write_text('[LENGTH:03:45]\n[TI:标题]\n[01:02:03.00]歌词\n[00:04.00]第二句\n',
                            encoding='utf-8')
            lines = documents.read_document(path).split('\n')
        self.assertEqual(lines, ['', '', '歌词', '第二句', ''])

    def test_docx_strict_namespace_and_corrupt(self):
        import documents
        import zipfile
        strict = ('<w:document xmlns:w="http://purl.oclc.org/ooxml/wordprocessingml/main">'
                  '<w:body><w:p><w:r><w:t>严格版</w:t></w:r></w:p></w:body></w:document>')
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            good = tmp / 'strict.docx'
            with zipfile.ZipFile(good, 'w') as z:
                z.writestr('word/document.xml', strict)
            self.assertEqual(documents.read_document(good), '严格版')
            bad = tmp / 'bad.docx'
            bad.write_bytes(b'not a zip')
            with self.assertRaisesRegex(ValueError, '损坏'):
                documents.read_document(bad)
            empty = tmp / 'empty.docx'
            with zipfile.ZipFile(empty, 'w') as z:
                z.writestr('other.txt', 'x')
            with self.assertRaises(ValueError):
                documents.read_document(empty)

    def test_ocr_failure_keeps_text_pages(self):
        import components
        import docutils
        pages = [Mock(), Mock()]
        for page, text in zip(pages, ('第一页', '')):
            page.extract_text.return_value = text
        with patch('pypdf.PdfReader', return_value=Mock(pages=pages)), \
                patch.object(docutils, '_ocr_pages',
                             side_effect=components.MissingComponent('缺OCR')):
            self.assertEqual(docutils.extract_pdf_text('unused', ocr=True), '第一页\n\n')
        with patch('pypdf.PdfReader', return_value=Mock(pages=[pages[1]])), \
                patch.object(docutils, '_ocr_pages',
                             side_effect=components.MissingComponent('缺OCR')):
            with self.assertRaisesRegex(RuntimeError, 'OCR 不可用'):
                docutils.extract_pdf_text('unused', ocr=True)

    def test_cli_modes_are_mutually_exclusive(self):
        import main as entry
        with patch.object(sys, 'argv', ['SmartVoice', '--version', '--server']):
            with self.assertRaises(SystemExit) as cm:
                entry.main()
            self.assertEqual(cm.exception.code, 2)
        with patch.object(sys, 'argv', ['SmartVoice', '--version', '--port', '0']):
            self.assertIsNone(entry.main())  # 端口只约束 --server，不误伤其它模式

    def test_verify_rejects_missing_files(self):
        import verify_installer
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(sys, 'argv', ['verify_installer.py', 'nope.exe',
                                            '--parent', tmp]):
                with self.assertRaises(SystemExit) as cm:
                    verify_installer.main()
                self.assertEqual(cm.exception.code, 2)

    def test_installation_check_covers_theme(self):
        import main as entry
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / 'sub' / 'report.json'
            self.assertIsNone(entry.installation_check(str(report)))
            data = json.loads(report.read_text(encoding='utf-8'))
            self.assertTrue(data['ok'])
            self.assertIn('theme-and-menu-present', data['checks'])


class TaskManagerTests(unittest.TestCase):
    def _manager(self, calls):
        from task_manager import TaskManager
        return TaskManager(schedule_fn=lambda ms, cb: calls.append((ms, cb)) or "h",
                           cancel_fn=lambda h: calls.append(("cancel", h)))

    def test_submit_preempt_and_poll_routing(self):
        calls = []
        mgr = self._manager(calls)
        self.assertEqual(mgr.submit(lambda: None), 0)
        self.assertEqual(mgr.inflight, 1)
        self.assertEqual(len(calls), 1)  # 提交即唤醒一次
        self.assertEqual(mgr.preempt(), 1)
        seen, rendered = [], []
        mgr.handle = lambda k, s, p: seen.append((k, s, p)) or True
        mgr.render = lambda: rendered.append(1)
        mgr.is_active = lambda: True
        mgr.put("progress", 1, (50, 100))
        mgr.poll()
        self.assertEqual(seen, [("progress", 1, (50, 100))])
        self.assertEqual(rendered, [1])
        self.assertTrue(mgr._polling)  # 有在途，已续跑
        mgr.inflight = 0
        mgr.poll()
        self.assertFalse(mgr._polling)  # 空队列、无在途，停跑

    def test_poll_reschedules_while_inflight(self):
        calls = []
        mgr = self._manager(calls)
        mgr.handle = lambda k, s, p: False
        mgr.poll()  # 空队列、无在途：不再唤醒
        self.assertEqual(calls, [])
        mgr.inflight = 1
        mgr.poll()  # 有在途：续跑一次
        self.assertEqual(len(calls), 1)


class ExportPlaybackTests(unittest.TestCase):
    def test_format_clock(self):
        from playback import format_clock
        self.assertEqual(format_clock(0), "00:00")
        self.assertEqual(format_clock(59999), "00:59")
        self.assertEqual(format_clock(61000), "01:01")
        self.assertEqual(format_clock(-5), "00:00")
        self.assertEqual(format_clock(3599999), "59:59")

    def test_validate_export_options(self):
        from export_options import validate_export_options
        good = validate_export_options("D:/out", "配音", "MP3", 350, 450, False)
        self.assertEqual(good, {"directory": "D:/out", "name": "配音", "format": "mp3",
                                "leading_ms": 350, "trailing_ms": 450, "normalize": False})
        for bad in ({"fmt": "ogg"}, {"directory": ""}, {"leading_ms": 99999},
                    {"trailing_ms": -1}, {"leading_ms": "abc"}):
            args = dict(directory="D:/out", name="x", fmt="mp3",
                        leading_ms=350, trailing_ms=450, normalize=False)
            args.update(bad)
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    validate_export_options(**args)

    def test_coerce_export_options_never_raises(self):
        from export_options import coerce_export_options
        out = coerce_export_options({"leading_ms": 99999, "format": "ogg"})
        self.assertEqual((out["leading_ms"], out["format"], out["name"]), (5000, "mp3", "配音"))
        out = coerce_export_options({"directory": None, "name": None, "format": None,
                                     "leading_ms": None, "trailing_ms": "abc", "normalize": "yes"})
        self.assertEqual(out, {"directory": "", "name": "配音", "format": "mp3",
                               "leading_ms": 350, "trailing_ms": 450, "normalize": True})
        out = coerce_export_options("not-a-dict")
        self.assertEqual(out["format"], "mp3")


class PrepareTests(unittest.TestCase):
    @staticmethod
    def _write_fake_assets(target, corrupt=None):
        target.mkdir(parents=True, exist_ok=True)
        (target / 'g2pw.onnx').write_bytes(b'\0' * (11 * 1024**2))
        for name in ('POLYPHONIC_CHARS.txt', 'MONOPHONIC_CHARS.txt', 'config.py', 'version',
                     'bert-base-chinese_s2t_dict.txt', 'LICENSE-G2PW.txt'):
            (target / name).write_text('placeholder\n', encoding='utf-8')
        (target / 'bopomofo_to_pinyin_wo_tune_dict.json').write_text('{}', encoding='utf-8')
        (target / 'char_bopomofo_dict.json').write_text('{}', encoding='utf-8')
        (target / 'vocab.txt').write_text('[PAD]\n[UNK]\n', encoding='utf-8')
        if corrupt == 'missing-json':
            (target / 'char_bopomofo_dict.json').unlink()
        elif corrupt == 'bad-json':
            (target / 'char_bopomofo_dict.json').write_text('{oops', encoding='utf-8')
        elif corrupt == 'bad-vocab':
            (target / 'vocab.txt').write_text('hello\n', encoding='utf-8')
        elif corrupt == 'tiny-onnx':
            (target / 'g2pw.onnx').write_bytes(b'<html>error</html>')

    def test_validate_assets_accepts_complete_set(self):
        import prepare_models
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'models'
            self._write_fake_assets(target)
            self.assertIsNone(prepare_models.validate_assets(target))

    def test_validate_assets_rejects_each_failure(self):
        import prepare_models
        cases = [('missing-json', '缺失'), ('bad-json', '合法 JSON'),
                 ('bad-vocab', '[PAD]'), ('tiny-onnx', '异常过小')]
        for corrupt, needle in cases:
            with self.subTest(corrupt=corrupt):
                with tempfile.TemporaryDirectory() as tmp:
                    target = Path(tmp) / 'models'
                    self._write_fake_assets(target, corrupt=corrupt)
                    with self.assertRaisesRegex(RuntimeError, needle):
                        prepare_models.validate_assets(target)

    def test_prepare_skips_downloads_when_assets_valid(self):
        import prepare_models
        import requests
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'models' / 'g2pw'
            self._write_fake_assets(target)

            def no_network(*args, **kwargs):
                raise AssertionError('must not download')

            fake_module = str(Path(tmp) / 'prepare_models.py')
            with patch.object(prepare_models, '__file__', fake_module), \
                    patch.object(requests, 'get', side_effect=no_network):
                prepare_models.prepare()
            manifest = json.loads((target / 'manifest.json').read_text(encoding='utf-8'))
            self.assertEqual(set(manifest), set(prepare_models.REQUIRED))

    def test_branding_failure_keeps_previous_outputs(self):
        import os as _os
        import prepare_branding
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            src_old, src_new = tmp / 'old.png', tmp / 'new.png'
            Image.new('RGB', (64, 48), 'navy').save(src_old)
            Image.new('RGB', (64, 48), 'maroon').save(src_new)
            dest = tmp / 'assets'
            prepare_branding.prepare(src_old, dest)
            before = {p.name: p.read_bytes() for p in dest.iterdir()}
            self.assertEqual(set(before), {'smartvoice.png', 'smartvoice.ico',
                                           'wizard-image.bmp', 'wizard-small.bmp'})
            real_replace = _os.replace
            calls = []

            def flaky_replace(src, dst):
                calls.append(1)
                if len(calls) == 1:
                    raise OSError('killed mid-build')
                return real_replace(src, dst)

            with patch('os.replace', side_effect=flaky_replace):
                with self.assertRaises(OSError):
                    prepare_branding.prepare(src_new, dest)
            self.assertEqual({p.name: p.read_bytes() for p in dest.iterdir()}, before)
            prepare_branding.prepare(src_new, dest)
            for img in ('smartvoice.png', 'wizard-image.bmp', 'wizard-small.bmp'):
                with Image.open(dest / img) as picture:
                    picture.load()


class SynthJobsTests(unittest.TestCase):
    def _ctx(self, synth, pool=None, current=True, cache=None):
        import voice_tasks as workflow
        from synth_jobs import JobContext
        from concurrent.futures import ThreadPoolExecutor
        store = {}
        fake_cache = cache or type("C", (), {
            "load": lambda self, k: store.get(k),
            "save": lambda self, k, v: store.__setitem__(k, v),
        })()
        events = []
        return JobContext(
            is_current=lambda s: current if isinstance(current, bool) else current(),
            emit=lambda k, s, p: events.append((k, s, p)),
            cache=fake_cache,
            callbacks=lambda s, i, t: (lambda *a: None, lambda *a: None),
            synth=synth,
            pool=pool or ThreadPoolExecutor(max_workers=2),
            token=lambda: workflow.Cancellation(),
        ), events, store

    def test_serial_order_and_cache_reuse_without_tk(self):
        import engine
        from synth_jobs import run_jobs
        calls = []
        ctx, events, store = self._ctx(lambda text, seg, voice, **kw: calls.append(text) or text.encode())
        snap = {"engine": "Edge免费(免Key)"}
        jobs = [("v", "r", c) for c in "abc"]
        self.assertEqual(run_jobs(ctx, 7, jobs, snap), [b"a", b"b", b"c"])
        self.assertEqual(calls, ["a", "b", "c"])
        calls.clear()
        self.assertEqual(run_jobs(ctx, 7, jobs, snap), [b"a", b"b", b"c"])
        self.assertEqual(calls, [])  # 第二遍全命中缓存，不再合成
        kinds = [k for k, s, p in events]
        self.assertIn("cache_segment", kinds)
        self.assertIn("segment", kinds)

    def test_parallel_bounded_and_ordered(self):
        import threading
        import time
        import engine
        from synth_jobs import run_jobs
        from concurrent.futures import ThreadPoolExecutor
        barrier = threading.Barrier(2)
        lock = threading.Lock()
        active, peak = [0], [0]

        def synth(text, seg, voice, **kw):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            try:
                if text in ("0", "1"):
                    barrier.wait(timeout=5)
                if text == "0":
                    time.sleep(.06)
                return text.encode()
            finally:
                with lock:
                    active[0] -= 1

        pool = ThreadPoolExecutor(max_workers=2)
        try:
            ctx, events, store = self._ctx(synth, pool=pool)
            snap = {"engine": engine.ENGINE_CHOICES[0]}
            jobs = [("v", "r", str(i)) for i in range(4)]
            self.assertEqual(run_jobs(ctx, 3, jobs, snap), [b"0", b"1", b"2", b"3"])
            self.assertLessEqual(peak[0], 2)
        finally:
            pool.shutdown(wait=True)

    def test_cancel_keeps_finished_parts(self):
        import voice_tasks as workflow
        from synth_jobs import run_jobs

        def synth(text, seg, voice, **kw):
            if text == "bad":
                raise workflow.Cancelled("停")
            return text.encode()

        ctx, events, store = self._ctx(synth)
        snap = {"engine": "Edge免费(免Key)"}
        self.assertEqual(run_jobs(ctx, 1, [("v", "r", "ok"), ("v", "r", "bad"), ("v", "r", "later")],
                                    snap), [b"ok", None, None])

    def test_stale_seq_aborts_immediately(self):
        from synth_jobs import run_jobs
        calls = []
        ctx, events, store = self._ctx(lambda text, seg, voice, **kw: calls.append(text) or b"x",
                                       current=False)
        snap = {"engine": "Edge免费(免Key)"}
        self.assertEqual(run_jobs(ctx, 9, [("v", "r", "a"), ("v", "r", "b")], snap), [None, None])
        self.assertEqual(calls, [])

    def test_cache_write_failure_keeps_audio(self):
        from synth_jobs import run_jobs
        diag = Mock()

        class BadCache:
            def load(self, key):
                return None

            def save(self, key, data):
                raise OSError("磁盘满")

        ctx, events, store = self._ctx(lambda text, seg, voice, **kw: b"data", cache=BadCache())
        snap = {"engine": "Edge免费(免Key)", "_diag": diag}
        self.assertEqual(run_jobs(ctx, 5, [("v", "r", "x")], snap), [b"data"])
        diag.event.assert_called_with("cache_write_failed", error_type="OSError")


if __name__ == '__main__':
    unittest.main()
