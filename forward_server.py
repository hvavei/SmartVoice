"""转发服务(MultiTTS式): GET /voices 查人声, GET/POST /forward 合成语音."""
import json
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import appmeta
from task_manager import DaemonPool

MAX_BODY = 1 << 20   # POST 请求体上限 1MB，防无上限读内存
MAX_TEXT = 100_000   # 单次合成文本上限
MAX_WORKERS = 8      # 并发合成上限，超出排队
MAX_QUEUED = 16      # 排队上限，再多直接 503（防 Slowloris 耗尽线程）
MAX_URL = 32 * 1024        # GET 整行上限：基类 64K 才拦 414，32K 以上直接 JSON 413
MAX_HEADERS = 64 * 1024     # 请求头总量上限，防超大 header 吃内存


class _Handler(BaseHTTPRequestHandler):
    synth_fn = None   # (text, voice, rate) -> bytes
    voices_fn = None  # () -> [{name, id, lang}]
    cors_enabled = False  # 网页跨域调用开关：默认关；打开后任意网站可调本机服务（烧Key配额）
    server_version = f"SmartVoice/{appmeta.VERSION}"
    timeout = 30      # 连接/读写超时：慢速客户端与半开连接不会永久挂起线程

    def _cors_headers(self):
        if self.cors_enabled:
            self.send_header("Access-Control-Allow-Origin", "*")

    def _json(self, obj, code=200):
        try:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError):
            # voices_fn 返回不可序列化对象：回 502 而不是断连无响应。
            code, body = 502, b'{"error":"unserializable"}'
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self._cors_headers()
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
            self._cors_headers()
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
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as e:
            # 工作线程内转 502：不断连，服务继续活；只报类型名，
            # 引擎错误串可能含内网地址，--cors 打开后不许被任意网页读走。
            return self._json({"error": type(e).__name__}, 502)
        return self._audio(data)

    def _headers_too_big(self):
        try:
            return sum(len(k) + len(v) for k, v in self.headers.items()) > MAX_HEADERS
        except Exception:
            return True

    def do_GET(self):
        if len(self.path) > MAX_URL or self._headers_too_big():
            return self._json({"error": "request too large"}, 413)
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path == "/voices":
            # voices_fn 可能触发联网拉人声：失败回 502 JSON，而不是断连无响应
            try:
                return self._json(self.voices_fn())
            except (KeyboardInterrupt, SystemExit):
                raise
            except BaseException as e:
                return self._json({"error": type(e).__name__}, 502)
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
        if self._headers_too_big():
            return self._json({"error": "request too large"}, 413)
        if self.headers.get("Transfer-Encoding"):
            # 不支持 chunked：无 Content-Length 时旧逻辑把真 body 当空报 400，
            # 残留数据靠关连接蒙混；语义错了就直说 501。
            return self._json({"error": "chunked unsupported"}, 501)
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

    def do_OPTIONS(self):
        if not self.cors_enabled:
            return self._json({"error": "not found"}, 404)
        try:
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            pass

    def log_message(self, fmt, *args):
        pass


def run_server(port, synth_fn, voices_fn, max_workers=MAX_WORKERS, queue_size=MAX_QUEUED,
               cors=False):
    _Handler.synth_fn = staticmethod(synth_fn)
    _Handler.voices_fn = staticmethod(voices_fn)
    _Handler.cors_enabled = bool(cors)
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
        # DaemonPool：退出时不被池里空闲长连接线程钉死（原生池线程非 daemon）。
        self._pool = DaemonPool(max_workers=max_workers,
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
    """排空请求头(+已声明 body)再回 503：残留未读数据会让 Windows 发 RST 吞掉状态码。
    整段排空设总时限：Slowloris 式 1 字节/2s 之前能堵住 acceptor 主线程数小时。"""
    deadline = time.monotonic() + 3
    try:
        request.settimeout(2)
        data = b""
        while b"\r\n\r\n" not in data and len(data) < 65536:
            if time.monotonic() > deadline:
                break
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
            if time.monotonic() > deadline:
                break
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
