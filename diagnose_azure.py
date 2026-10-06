"""Azure 连通性/阶段耗时诊断；默认不发送付费合成，--live 才执行短句 A/B。

只输出白名单配置与耗时，不打印凭据、代理地址或响应错误正文。
"""
import argparse
import json
import os
from pathlib import Path
import socket
import ssl
import time
from urllib.parse import urlsplit

import requests
import engine
import storage


CONNECT_TIMEOUT = 8
HTTP_SHORT_TIMEOUT = (8, 15)
HTTP_LONG_TIMEOUT = (8, 30)
STT_TIMEOUT = (8, 35)


def cache_directory(config):
    return storage.CACHE_DIR if Path(config).resolve() == Path(engine.CONFIG_FILE).resolve() else Path(config).parent


def transcribe_samples(config, output):
    """识别诊断生成的短句，辅助定位服务端漏读；ASR 结果不是人工听音结论。"""
    import subprocess
    import docutils
    Path(output).mkdir(parents=True, exist_ok=True)
    _, key, region, _, _ = configuration(config)
    if not key:
        raise RuntimeError("诊断配置没有 Azure Key")
    rows = []
    for path in sorted(Path(output).glob("*.mp3")):
        try:
            conversion = subprocess.run([docutils._ffmpeg_path(), "-v", "error", "-i", str(path),
                                         "-ar", "16000", "-ac", "1", "-f", "wav", "pipe:1"],
                                        capture_output=True, check=True,
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.CalledProcessError) as e:
            rows.append({"file": path.name, "error": type(e).__name__, "ms": 0})
            continue
        t = time.perf_counter()
        try:
            with requests.post(f"https://{region}.stt.speech.microsoft.com/speech/recognition/conversation/cognitiveservices/v1",
                               params={"language": "zh-CN"},
                               headers={"Ocp-Apim-Subscription-Key": key,
                                        "Content-Type": "audio/wav; codecs=audio/pcm; samplerate=16000"},
                                data=conversion.stdout, timeout=STT_TIMEOUT) as r:
                row = {"file": path.name, "status": r.status_code}
                if r.status_code == 200:
                    try:
                        result = r.json()
                    except ValueError:
                        row["error"] = "non-json-response"
                    else:
                        if isinstance(result, dict):
                            row.update(recognition=result.get("RecognitionStatus"), text=result.get("DisplayText"))
                        else:
                            row["error"] = "unexpected-json-shape"
        except requests.RequestException as e:
            row = {"file": path.name, "error": type(e).__name__}
        row["ms"] = round((time.perf_counter()-t)*1000, 1)
        rows.append(row)
    print(json.dumps(rows, ensure_ascii=True, indent=2))
    Path(output, "transcripts.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


def configuration(path):
    raw_cfg = engine.load_json(path, {})
    cfg = (raw_cfg if Path(path).resolve() == Path(engine.CONFIG_FILE).resolve()
           else storage.unlock_settings(raw_cfg))
    azure = engine.kind_of(cfg.get("engine", "")) == "azure"
    profile = cfg if azure else cfg.get("engine_profiles", {}).get("azure", {})
    key = os.getenv("AZURE_SPEECH_KEY") or profile.get("key", "")
    region = engine.normalize_region(profile.get("region", engine.DEFAULT_REGION))
    endpoint = engine.fix_endpoint(profile.get("endpoint", ""), region)
    voices = engine.load_json(cache_directory(path) / f"voices_cache_{region}.json", engine.BUILTIN_VOICES)
    selected = cfg.get("person", "")
    voice = voices.get(selected, engine.BUILTIN_VOICES.get(selected, "zh-CN-XiaoxiaoNeural"))
    return cfg, key, region, endpoint, voice


def audit_voices(config, output=None):
    """官方列表与本地缓存逐 ID 对照；不根据名称后缀猜测 VoiceType。"""
    from collections import Counter
    _, key, region, _, _ = configuration(config)
    if not key:
        raise SystemExit("诊断配置没有 Azure Key")
    url = f"https://{region}.tts.speech.microsoft.com/cognitiveservices/voices/list"
    start = time.perf_counter()
    with requests.get(url, headers={"Ocp-Apim-Subscription-Key": key}, timeout=HTTP_LONG_TIMEOUT) as response:
        if response.status_code != 200:
            print(json.dumps({"endpoint": url, "status": response.status_code}))
            return
        try:
            voices = response.json()
        except ValueError:
            print(json.dumps({"endpoint": url, "status": 200, "error": "non-json-response"}))
            return
    if not isinstance(voices, list):
        print(json.dumps({"endpoint": url, "status": 200, "error": "unexpected-json-shape"}))
        return
    voices = [v for v in voices if isinstance(v, dict) and isinstance(v.get('ShortName'), str)]
    ids = {v['ShortName'] for v in voices}
    unusual = [{k: v.get(k) for k in ('ShortName', 'VoiceType', 'Status', 'Locale', 'SampleRateHertz')}
               for v in voices if not v['ShortName'].endswith('Neural')]
    caches = {}
    for name in (f"voices_cache_{region}.json", "voices_cache.json", "voices_cache_edge.json"):
        table = engine.load_json(cache_directory(config) / name, {})
        local = {v for v in table.values() if isinstance(v, str)} if isinstance(table, dict) else set()
        caches[name] = {"count": len(local), "not_in_official": sorted(local - ids),
                        "without_neural_suffix": sorted(v for v in local if not v.endswith('Neural'))}
    report = {"endpoint": url, "status": 200, "elapsed_ms": round((time.perf_counter()-start)*1000, 1),
              "official_count": len(voices), "voice_types": dict(Counter(v.get('VoiceType') for v in voices)),
              "official_without_neural_suffix": unusual, "local_comparison": caches}
    print(json.dumps(report, ensure_ascii=True, indent=2))
    if output:
        Path(output).mkdir(parents=True, exist_ok=True)
        Path(output, 'voice-origin.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')


def run(config, output, live=False, direct=False, voice_override=None, native_only=False):
    cfg, key, region, endpoint, voice = configuration(config)
    voice = voice_override or voice
    host = urlsplit(endpoint).hostname
    report = {"region": region, "host": host, "voice": voice, "route": "direct" if direct else "environment",
              "key_available": bool(key), "proxy_active": bool(requests.utils.get_environ_proxies(endpoint)),
              "parameters": {k: cfg.get(k) for k in ("rate_v", "pitch_v", "vol_v", "style", "degree", "role")}}
    t = time.perf_counter()
    try:
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        report["dns_ms"] = round((time.perf_counter() - t) * 1000, 1)
        report["address_count"] = len(addresses)
        t = time.perf_counter()
        with socket.create_connection((host, 443), timeout=CONNECT_TIMEOUT) as sock:
            report["tcp_ms"] = round((time.perf_counter() - t) * 1000, 1)
            t = time.perf_counter()
            with ssl.create_default_context().wrap_socket(sock, server_hostname=host):
                report["tls_ms"] = round((time.perf_counter() - t) * 1000, 1)
    except Exception as e:
        report["connection_error"] = type(e).__name__
    with requests.Session() as session:
        session.trust_env = not direct
        heads = []
        for _ in range(2):
            t = time.perf_counter()
            try:
                with session.head(endpoint, timeout=HTTP_SHORT_TIMEOUT, allow_redirects=False) as r:
                    heads.append({"http_status": r.status_code, "ms": round((time.perf_counter() - t) * 1000, 1)})
            except requests.RequestException as e:
                heads.append({"error": type(e).__name__, "ms": round((time.perf_counter() - t) * 1000, 1)})
        report["unauthenticated_head"] = heads
        if live and key:
            output = Path(output)
            output.mkdir(parents=True, exist_ok=True)
            text = "谁是我们的敌人？谁是我们的朋友？银行的工作人员重新核对了材料。"
            report["samples"] = []
            # 固定中性参数对比原生与自动注音，避免客户稿件/配置干扰。
            cases = (("native", False), ("native_warm", False)) if native_only else (("native", False), ("annotated", True), ("native_warm", False))
            for name, annotate in cases:
                t = time.perf_counter()
                ssml = engine._build_ssml(voice, text, "100%", "+0Hz", "+0%", annotate=annotate)
                sample = {"name": name, "format": engine.AZURE_OUTPUT_FORMAT,
                          "ssml_ms": round((time.perf_counter() - t) * 1000, 1),
                          "phoneme_count": ssml.count("<phoneme")}
                t = time.perf_counter()
                try:
                    with session.post(endpoint, data=ssml.encode("utf-8"), headers={
                            "Ocp-Apim-Subscription-Key": key, "Content-Type": "application/ssml+xml",
                            "X-Microsoft-OutputFormat": engine.AZURE_OUTPUT_FORMAT},
                            timeout=HTTP_LONG_TIMEOUT, stream=True) as r:
                        sample["headers_ms"] = round((time.perf_counter() - t) * 1000, 1)
                        sample["http_status"] = r.status_code
                        chunks = []
                        if r.status_code == 200 and "audio" in r.headers.get("Content-Type", ""):
                            for chunk in r.iter_content(4096):
                                if chunk:
                                    if not chunks:
                                        sample["first_audio_ms"] = round((time.perf_counter() - t) * 1000, 1)
                                    chunks.append(chunk)
                            data = b"".join(chunks)
                            sample["bytes"] = len(data)
                            (output / f"{name}.mp3").write_bytes(data)
                            (output / f"{name}.ssml").write_text(ssml, encoding="utf-8")
                except requests.RequestException as e:
                    sample["error"] = type(e).__name__
                sample["request_ms"] = round((time.perf_counter() - t) * 1000, 1)
                report["samples"].append(sample)
    print(json.dumps(report, ensure_ascii=True, indent=2))
    if output:
        Path(output).mkdir(parents=True, exist_ok=True)
        Path(output, "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=engine.CONFIG_FILE)
    parser.add_argument("--output")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--direct", action="store_true")
    parser.add_argument("--voice")
    parser.add_argument("--native-only", action="store_true")
    parser.add_argument("--transcribe", action="store_true")
    parser.add_argument("--voices", action="store_true")
    parser.add_argument("--refresh-cache", action="store_true", help="从官方列表更新指定配置目录的 Azure 人声及类型缓存")
    args = parser.parse_args(argv)
    if args.live and not args.output:
        parser.error("--live requires --output")
    if sum([bool(args.voices), bool(args.transcribe), bool(args.refresh_cache)]) > 1:
        parser.error('--voices/--transcribe/--refresh-cache 只能指定其一')
    if (args.voice or args.native_only) and not args.live:
        parser.error('--voice/--native-only 需要与 --live 联用')
    if args.refresh_cache:
        _, key, region, endpoint, _ = configuration(args.config)
        if not key:
            parser.error("诊断配置没有 Azure Key")
        engine.APP_DIR = str(cache_directory(args.config))
        table = engine.refresh_voices_azure(key, region, endpoint)
        print(json.dumps({'region': region, 'official_voices_saved': len(table)}, ensure_ascii=True))
    elif args.voices:
        audit_voices(args.config, args.output)
    elif args.transcribe:
        if not args.output:
            parser.error("--transcribe requires --output")
        transcribe_samples(args.config, args.output)
    else:
        run(args.config, args.output, args.live, args.direct, args.voice, args.native_only)


if __name__ == "__main__":
    main()
