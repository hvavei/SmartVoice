"""入口: 默认开GUI; --server 只跑转发; --smoke-test 离线自检(打包验证用).

另支持 --version、--installation-check REPORT_JSON、--install-component NAME ZIP、--port N。
"""
import argparse
import appmeta

from appmeta import VERSION


def installation_check(report_path):
    """安装后离线验收，可在无控制台 EXE 中输出 JSON 结果；不读取 Key 或联网。"""
    import io
    import json
    from pathlib import Path
    import wave
    # 自检音频规格：24000Hz/16bit 单声道 0.2 秒静音；播放链路期望 21600 帧。
    _CHECK_SR, _CHECK_FRAMES, _CHECK_PLAY_FRAMES, _CHECK_MP3_MIN = 24000, 2400, 21600, 100
    checks = []
    try:
        import components
        if components.available('g2pw'):
            import polyphone
            _, phones = polyphone.annotate_sapi('银行')
            assert phones.get(1) == 'hang 2'
            checks.append('g2pw-model-inference')
        else:
            import engine
            assert engine.g2p_phoneme_annotator('银行') == '银行'
            checks.append('g2pw-optional-native-fallback')
        import docutils
        buf = io.BytesIO()
        with wave.open(buf, 'wb') as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(_CHECK_SR)
            wav.writeframes(b'\0\0' * _CHECK_FRAMES)
        mp3, pcm = docutils.prepare_playback_audio(buf.getvalue())
        assert len(mp3) > _CHECK_MP3_MIN
        with wave.open(io.BytesIO(pcm), 'rb') as wav:
            assert wav.getnframes() == _CHECK_PLAY_FRAMES
        checks.append('ffmpeg-export-and-playback')
        import pypdf
        import pypdfium2
        if components.available('ocr'):
            ocr = docutils.create_ocr()
            import numpy as np
            ocr(np.full((64, 200, 3), 255, dtype=np.uint8))
            checks.append('pdf-and-ocr-inference')
        else:
            checks.append('pdf-text-available-ocr-optional')
        import edge_tts
        import requests
        checks.append('network-adapters-import')
        import storage
        import voice_tasks as workflow
        import tempfile
        assert storage.dpapi(storage.dpapi(b'self-test'), decrypt=True) == b'self-test'
        with tempfile.TemporaryDirectory() as tmp:
            cache = workflow.SegmentCache(Path(tmp)/'cache')
            key = workflow.fingerprint('test', 'voice', {})
            cache.save(key, mp3)
            project = Path(tmp)/'self-test.smartvoice'
            workflow.save_project(project, {'text': 'test', 'slots': [], 'segments': [key]}, cache)
            assert workflow.load_project(project, cache)['text'] == 'test'
        checks.append('dpapi-project-segment-cache')
        import theme
        assert set(theme.THEMES) == set(theme.THEME_ORDER) == set(theme.THEME_LABELS)
        assert theme.THEMES['warm']['FG'] == '#000000'
        import product_ui
        import studio_gui
        assert callable(studio_gui.App.switch_theme)
        checks.append('theme-and-menu-present')
        result = {'ok': True, 'checks': checks}
    except Exception as e:
        import re
        msg = re.sub(r'[A-Za-z]:\\[^"\s]*', '<path>', str(e))
        result = {'ok': False, 'checks': checks, 'error': f'{type(e).__name__}: {msg[:300]}'}
    try:
        Path(report_path).parent.mkdir(parents=True, exist_ok=True)
        Path(report_path).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    except OSError as e:
        raise SystemExit(f'无法写入报告 {report_path}: {e}')
    if not result['ok']:
        raise SystemExit(1)


def smoke_test():
    import json
    import threading
    import time
    import urllib.request
    import engine
    import forward_server
    ok = []
    # 1. 终结点自动换算(eastus通用地址->合成地址, 回归用)
    ep = engine.fix_endpoint("https://eastus.api.cognitive.microsoft.com/", "eastus")
    assert ep == "https://eastus.tts.speech.microsoft.com/cognitiveservices/v1", ep
    ok.append("endpoint-fix")
    # 1b. eastasia默认+机房不一致强制纠偏(截图401的根因: Region与终结点host打架)
    assert engine.DEFAULT_REGION == "eastasia", engine.DEFAULT_REGION
    assert engine.normalize_region(" EastAsia ") == "eastasia"
    assert engine.normalize_region("") == "eastasia"
    assert engine.region_of_endpoint("https://eastasia.tts.speech.microsoft.com/cognitiveservices/v1") == "eastasia"
    ep2 = engine.fix_endpoint("https://eastus.tts.speech.microsoft.com/cognitiveservices/v1", "eastasia")
    assert ep2 == "https://eastasia.tts.speech.microsoft.com/cognitiveservices/v1", ep2
    ep3 = engine.fix_endpoint("", "eastasia")
    assert ep3 == "https://eastasia.tts.speech.microsoft.com/cognitiveservices/v1", ep3
    ok.append("eastasia-fix")
    # 2. 语种归类
    assert engine.lang_of("ja-JP-NanamiNeural") == "ja-JP"
    assert engine.lang_of("zh-CN-Xiaochen:DragonHDLatestNeural") == "zh-CN"
    ok.append("lang-filter")
    # 3. 重试逻辑(前2次超时, 第3次成功; 走全局Session)
    import requests

    class R:
        status_code = 200
        headers = {"Content-Type": "audio/mpeg", "Content-Length": "5000"}
        content = b"x" * 5000
        def iter_content(self, chunk_size=4096):
            yield self.content
        def close(self):
            pass

    class FakeSess:
        def __init__(self, fn):
            self.fn = fn
        def post(self, url, headers=None, data=None, timeout=None, **kw):
            return self.fn(url, headers=headers, data=data, timeout=timeout, **kw)
        def get(self, url, headers=None, timeout=None, **kw):
            return self.fn(url, headers=headers, timeout=timeout, **kw)

    n = {"c": 0}
    orig_sess = engine._SESS

    def fake(url, headers=None, data=None, timeout=None, **kw):
        n["c"] += 1
        if n["c"] < 3:
            raise requests.exceptions.ReadTimeout("simulated")
        return R()

    engine._SESS = FakeSess(fake)
    try:
        data = engine.synth_azure("test", "zh-CN-YunxiNeural", "k",
                                  "https://eastasia.tts.speech.microsoft.com/cognitiveservices/v1",
                                  region="eastasia")
    finally:
        engine._SESS = orig_sess
    assert len(data) == 5000 and n["c"] == 3, (len(data), n["c"])
    ok.append("retry")
    # 3a2. 401空包必须给机房提示且不重试(截图里zh-HK试听失败就是它)
    class R401:
        status_code = 401
        headers = {"Content-Type": "application/json"}
        content = b""
        text = ""
        def close(self):
            pass

    def fake401(url, headers=None, data=None, timeout=None, **kw):
        return R401()

    engine._SESS = FakeSess(fake401)
    try:
        engine.synth_azure("test", "zh-HK-HiuGaaiNeural", "k",
                           "https://eastasia.tts.speech.microsoft.com/cognitiveservices/v1",
                           region="eastasia", attempts=3)
        raise AssertionError("401 should raise")
    except RuntimeError as e:
        msg = str(e)
        assert "401" in msg and "eastasia" in msg, msg
    finally:
        engine._SESS = orig_sess
    ok.append("auth-hint")
    # 3b. SSML含语速/音量/音调/风格/转义
    seen = {}

    def fake2(url, headers=None, data=None, timeout=None, **kw):
        seen["ssml"] = data.decode("utf-8")
        seen["auth"] = (headers or {}).get("Ocp-Apim-Subscription-Key")
        return R()

    engine._SESS = FakeSess(fake2)
    try:
        engine.synth_azure("a&b<c>", "zh-CN-XiaoxiaoNeural", "k",
                           "https://eastasia.tts.speech.microsoft.com/cognitiveservices/v1",
                           rate="80%", pitch="+2Hz", volume="-10%",
                           style="cheerful", styledegree="150%", role="默认",
                           attempts=1, region="eastasia")
    finally:
        engine._SESS = orig_sess
    s = seen["ssml"]
    for needle in ["rate='-20%'", "pitch='+2Hz'", "volume='-10%'",
                   "style='cheerful'", "styledegree='1.5'", "a&amp;b&lt;c&gt;",
                   "xmlns:mstts"]:
        assert needle in s, needle
    assert 'mstts:silence' not in s  # 首尾缓冲在本地整条音频上添加。
    ok.append("ssml-style")
    # 3c. Edge百分比归一化(界面100%->+0%)
    assert engine._signed_pct("100%") == "+0%", engine._signed_pct("100%")
    assert engine._signed_pct("120%") == "+20%"
    assert engine._signed_pct("+15%") == "+15%"
    ok.append("edge-normalize")
    # 3d. 引擎归类
    assert engine.kind_of("Azure(填Key)") == "azure"
    assert engine.kind_of("Edge免费(免Key)") == "edge"
    assert engine.kind_of("OpenAI兼容(填Key)") == "openai"
    assert engine.kind_of("火山引擎(填Token)") == "volc"
    assert engine.kind_of("旧引擎配置") == "azure"
    assert engine.kind_of("") == "azure"
    assert "OpenAI兼容(填Key)" in engine.ENGINE_CHOICES
    assert "火山引擎(填Token)" in engine.ENGINE_CHOICES
    ok.append("kind")
    assert engine.openai_speech_url("") == engine.DEFAULT_OPENAI_SPEECH
    assert engine.openai_speech_url("https://api.openai.com/v1") == engine.DEFAULT_OPENAI_SPEECH
    assert abs(engine.openai_rate_to_speed("100%") - 1.0) < 1e-6
    assert abs(engine.openai_rate_to_speed("120%") - 1.2) < 1e-6
    seen_oa = {}

    def fake_oa(url, headers=None, data=None, timeout=None, **kw):
        seen_oa["url"] = url
        seen_oa["auth"] = (headers or {}).get("Authorization")
        seen_oa["body"] = json.loads(data.decode("utf-8"))
        class RO:
            status_code = 200
            headers = {"Content-Type": "audio/mpeg"}
            content = b"ID3" + b"o" * 100
            def iter_content(self, chunk_size=4096):
                yield self.content
            def close(self):
                pass
        return RO()

    engine._SESS = FakeSess(fake_oa)
    try:
        out = engine.synth_openai("你好", "alloy", "sk-test", attempts=1)
    finally:
        engine._SESS = orig_sess
    assert out.startswith(b"ID3")
    assert seen_oa["auth"] == "Bearer sk-test"
    assert seen_oa["body"]["voice"] == "alloy"
    ok.append("openai-synth")
    seen_vc = {}

    def fake_vc(url, headers=None, data=None, timeout=None, **kw):
        seen_vc["url"] = url
        seen_vc["auth"] = (headers or {}).get("Authorization")
        class RV:
            status_code = 200
            headers = {"Content-Type": "application/json"}
            def iter_content(self, chunk_size=4096):
                import base64
                yield json.dumps({"data": base64.b64encode(b"ID3volc").decode("ascii")}).encode()
            def close(self):
                pass
        return RV()

    engine._SESS = FakeSess(fake_vc)
    try:
        outv = engine.synth_volc("你好", "BV001_streaming", "tok", "app1", attempts=1)
    finally:
        engine._SESS = orig_sess
    assert outv == b"ID3volc"
    assert seen_vc["auth"].startswith("Bearer;")
    ok.append("volc-synth")
    # 3f. 配音脚本解析
    segs = engine.parse_dub_script(
        "旁白:大家好\n[小伙] 你好呀\n3:我是三号\n无标记行\n", ["旁白", "小伙", "姑娘"])
    assert segs == [(0, "大家好"), (1, "你好呀"), (2, "我是三号"), (None, "无标记行")], segs
    assert engine.parse_dub_script("   \n", ["旁白"]) == []
    ok.append("dub-parse")
    # 4. 转发服务本机实测
    orig_synth = forward_server._Handler.__dict__['synth_fn']
    orig_voices = forward_server._Handler.__dict__['voices_fn']
    srv = None
    try:
        forward_server._Handler.synth_fn = staticmethod(lambda t, v, r: b"ID3fakeaudio")
        forward_server._Handler.voices_fn = staticmethod(lambda: [{"name": "t", "id": "x", "lang": "zh-CN"}])
        from http.server import ThreadingHTTPServer
        srv = ThreadingHTTPServer(("127.0.0.1", 0), forward_server._Handler)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        time.sleep(0.5)
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/voices", timeout=10) as r:
            assert r.status == 200 and "zh-CN" in r.read().decode("utf-8")
        ok.append("forward-voices")
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/forward?text=hi", timeout=10) as r:
            assert r.status == 200 and r.read() == b"ID3fakeaudio"
        ok.append("forward-audio")
    finally:
        if srv is not None:
            srv.shutdown()
            srv.server_close()
        forward_server._Handler.synth_fn = orig_synth
        forward_server._Handler.voices_fn = orig_voices
    print("SMOKE PASS: " + ",".join(ok), flush=True)


def main():
    ap = argparse.ArgumentParser(prog="SmartVoice")
    ap.add_argument("--version", action="store_true")
    ap.add_argument("--smoke-test", action="store_true")
    ap.add_argument("--installation-check", metavar="REPORT_JSON")
    ap.add_argument('--install-component', nargs=2, metavar=('NAME', 'ZIP'))
    ap.add_argument("--server", action="store_true")
    ap.add_argument("--port", type=int, default=None, help="端口号（默认读已保存配置，否则 8774）")
    args = ap.parse_args()
    modes = [bool(args.install_component), args.version, args.smoke_test,
             bool(args.installation_check), args.server]
    if sum(1 for m in modes if m) > 1:
        ap.error('--version/--smoke-test/--installation-check/--install-component/--server 只能指定其一')
    if args.install_component:
        import components
        name, archive = args.install_component
        if name not in components.NAMES:
            ap.error('Unknown component')
        try:
            components.install_archive(name, archive)
        except Exception as e:
            raise SystemExit(f'组件安装失败: {e}')
        return
    if args.version:
        print(VERSION)
        return
    if args.smoke_test:
        return smoke_test()
    if args.installation_check:
        return installation_check(args.installation_check)
    if args.server:
        import engine
        import forward_server
        cfg = engine.load_json(engine.CONFIG_FILE, {})
        raw_port = args.port if args.port is not None else cfg.get("port", appmeta.DEFAULT_PORT)
        try:
            port = int(str(raw_port).strip())
        except (TypeError, ValueError):
            ap.error('端口号必须在 1～65535 之间')
        if not 1 <= port <= 65535:
            ap.error('端口号必须在 1～65535 之间')
        kind = engine.kind_of(cfg.get("engine", "Azure(填Key)"))
        key = cfg.get("key", "")
        region = cfg.get("region", "")
        ep = cfg.get("endpoint", "")
        default_voice = {"openai": "alloy", "volc": "BV001_streaming"}.get(kind, "zh-CN-YunxiNeural")
        voice = default_voice
        if kind == "azure":
            region = engine.normalize_region(region)
            ep = engine.fix_endpoint(ep, region)
            if not key:
                raise SystemExit("Azure引擎 settings.json 里没有Key, 请先开GUI填一次")
            synth = (lambda t, v, r: engine.synth_azure(t, v or voice, key, ep, r or "+0%",
                                                        region=region))
            vlist = (lambda: [{"name": k, "id": v}
                              for k, v in engine.get_voices(region).items()])
        elif kind == "openai":
            if not key:
                raise SystemExit("OpenAI兼容引擎 settings.json 里没有Key")
            synth = (lambda t, v, r: engine.synth_openai(t, v or voice, key, ep, r or "+0%",
                                                       model=region or engine.OPENAI_MODEL_DEFAULT))
            vlist = (lambda: [{"name": k, "id": v}
                              for k, v in engine.get_openai_voices().items()])
        elif kind == "volc":
            if not key:
                raise SystemExit("火山引擎 settings.json 里没有Token")
            synth = (lambda t, v, r: engine.synth_volc(t, v or voice, key, region, ep, r or "+0%"))
            vlist = (lambda: [{"name": k, "id": v}
                              for k, v in engine.get_volc_voices().items()])
        else:
            synth = (lambda t, v, r: engine.synth_edge(t, v or voice, r or "+0%"))
            vlist = (lambda: [{"name": k, "id": v}
                              for k, v in engine.get_edge_voices().items()])
        # person(人声显示名) → 人声ID：GUI 存的是 person，旧 voice_id 键只作兼容回退
        person = str(cfg.get("person", "") or cfg.get("voice_id", "") or "")
        if person:
            try:
                table = {"azure": lambda: engine.get_voices(region),
                         "openai": engine.get_openai_voices,
                         "volc": engine.get_volc_voices}.get(kind, engine.get_edge_voices)()
                voice = table.get(person) or (person if person in table.values() else voice)
            except Exception:
                pass  # 解析失败回退默认人声，转发服务仍可用
        try:
            forward_server.run_server(port, synth, vlist)
        except OSError as e:
            raise SystemExit(f"端口 {port} 启动失败: {e}")
        return
    import tkinter as tk
    from studio_gui import App
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
