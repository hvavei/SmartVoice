"""转发服务(MultiTTS式): GET /voices 查人声, GET/POST /forward 合成语音."""
import json
import threading
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import appmeta

MAX_BODY = 1 << 20   # POST 请求体上限 1MB，防无上限读内存
MAX_TEXT = 100_000   # 单次合成文本上限
MAX_WORKERS = 8      # 并发合成上限，超出排队
MAX_QUEUED = 16      # 排队上限，再多直接 503（防 Slowloris 耗尽线程）


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
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            pass

    def _audio(self, data):
        try:
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Connection", "close")
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
                                 q.get("rate", ["+0%"])[0] or "+0%")
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
        return self._forward(body.get("text", ""), body.get("voice") or None,
                               body.get("rate", "+0%") or "+0%")

    def log_message(self, fmt, *args):
        pass


def run_server(port, synth_fn, voices_fn, max_workers=MAX_WORKERS, queue_size=MAX_QUEUED):
    _Handler.synth_fn = staticmethod(synth_fn)
    _Handler.voices_fn = staticmethod(voices_fn)
    srv = BoundedThreadingHTTPServer(("127.0.0.1", port), _Handler,
                                     max_workers=max_workers, queue_size=queue_size)
    try:
        srv.serve_forever()
    finally:
        srv.server_close()


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """有界线程池替代每连接一线程：占满后直接 503 + 关连接，不再无上限建线程。"""
    daemon_threads = True

    def __init__(self, *args, max_workers=MAX_WORKERS, queue_size=MAX_QUEUED, **kwargs):
        super().__init__(*args, **kwargs)
        self._pool = ThreadPoolExecutor(max_workers=max_workers,
                                        thread_name_prefix="forward")
        self._slots = threading.Semaphore(max_workers + queue_size)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            _reject_overloaded(request)
            return
        try:
            self._pool.submit(self._process_one, request, client_address)
        except Exception:
            self._slots.release()
            raise

    def _process_one(self, request, client_address):
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            try:
                self.shutdown_request(request)
            finally:
                self._slots.release()

    def server_close(self):
        super().server_close()
        self._pool.shutdown(wait=False, cancel_futures=True)


def _reject_overloaded(request):
    """排空请求头(+已声明 body)再回 503：残留未读数据会让 Windows 发 RST 吞掉状态码。"""
    try:
        request.settimeout(2)
        data = b""
        while b"\r\n\r\n" not in data and len(data) < 65536:
            chunk = request.recv(4096)
            if not chunk:
                break
            data += chunk
        head, _, rest = data.partition(b"\r\n\r\n")
        length = 0
        for line in head.split(b"\r\n")[1:]:
            if line.lower().startswith(b"content-length:"):
                try:
                    length = max(0, int(line.split(b":", 1)[1].strip()))
                except ValueError:
                    length = 0
                break
        length = min(length, MAX_BODY)
        while len(rest) < length:
            chunk = request.recv(min(65536, length - len(rest)))
            if not chunk:
                break
            rest += chunk
    except OSError:
        pass
    finally:
        try:
            request.settimeout(None)
        except OSError:
            pass
    try:
        request.sendall(b"HTTP/1.1 503 Service Unavailable\r\n"
                        b"Connection: close\r\nContent-Length: 0\r\n\r\n")
    except OSError:
        pass
    try:
        request.close()
    except OSError:
        pass
