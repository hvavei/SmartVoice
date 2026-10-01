"""转发服务(MultiTTS式): GET /voices 查人声, GET/POST /forward 合成语音."""
import json
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import appmeta

MAX_BODY = 1 << 20   # POST 请求体上限 1MB，防无上限读内存
MAX_TEXT = 100_000   # 单次合成文本上限


class _Handler(BaseHTTPRequestHandler):
    synth_fn = None   # (text, voice, rate) -> bytes
    voices_fn = None  # () -> [{name, id, lang}]
    server_version = f"SmartVoice/{appmeta.VERSION}"
    timeout = 30      # 连接/读写超时：慢速客户端与半开连接不会永久挂起线程

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            pass

    def _audio(self, data):
        try:
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            pass

    def _forward(self, text, voice, rate):
        if not isinstance(text, str) or not text.strip():
            return self._json({"error": "missing text"}, 400)
        if len(text) > MAX_TEXT:
            return self._json({"error": "text too long"}, 413)
        if (voice is not None and not isinstance(voice, str)) or not isinstance(rate, str):
            return self._json({"error": "voice and rate must be strings"}, 400)
        try:
            data = self.synth_fn(text, voice, rate)
        except Exception as e:
            return self._json({"error": str(e)[:300]}, 502)
        return self._audio(data)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path == "/voices":
            # voices_fn 可能触发联网拉人声：失败回 502 JSON，而不是断连无响应
            try:
                return self._json(self.voices_fn())
            except Exception as e:
                return self._json({"error": str(e)[:300]}, 502)
        if u.path == "/forward":
            return self._forward(q.get("text", [""])[0],
                                 q.get("voice", [""])[0] or None,
                                 q.get("rate", ["+0%"])[0])
        if u.path == "/":
            return self._json({"service": "SmartVoice forward",
                               "usage": "/forward?text=你好&voice=zh-CN-YunxiNeural&rate=+0%",
                               "voices": "/voices"})
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        if u.path != "/forward":
            return self._json({"error": "not found"}, 404)
        try:
            size = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            return self._json({"error": "bad content-length"}, 400)
        if size < 0 or size > MAX_BODY:
            return self._json({"error": "body too large"}, 413)
        try:
            body = json.loads(self.rfile.read(size) or b"{}")
        except Exception:
            return self._json({"error": "bad json"}, 400)
        if not isinstance(body, dict):
            return self._json({"error": "expected JSON object"}, 400)
        return self._forward(body.get("text", ""), body.get("voice"), body.get("rate", "+0%"))

    def log_message(self, fmt, *args):
        pass


def run_server(port, synth_fn, voices_fn):
    _Handler.synth_fn = staticmethod(synth_fn)
    _Handler.voices_fn = staticmethod(voices_fn)
    srv = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    try:
        srv.serve_forever()
    finally:
        srv.server_close()
