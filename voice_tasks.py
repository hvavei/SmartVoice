"""SmartVoice 持久分段缓存、可取消网络上下文、项目/脱敏诊断。"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import threading
import time
import uuid
from urllib.parse import urlsplit, urlunsplit

import appmeta
import storage


class Cancelled(RuntimeError):
    pass


class Cancellation:
    def __init__(self):
        self.event = threading.Event()
        self.lock = threading.Lock()
        self.responses = set()

    def check(self):
        if self.event.is_set():
            raise Cancelled('任务已取消；已完成片段已保留')

    @property
    def cancelled(self):
        return self.event.is_set()

    def register(self, response):
        with self.lock:
            self.responses.add(response)
        if self.event.is_set():
            self.unregister(response)
            response.close()
            self.check()

    def unregister(self, response):
        with self.lock:
            self.responses.discard(response)

    def cancel(self):
        self.event.set()
        with self.lock:
            responses = list(self.responses)
            self.responses.clear()
        # Response.close 可能等待读取锁，不能堵塞 Tk。
        def close():
            for response in responses:
                try:
                    response.close()
                except Exception:
                    pass
        threading.Thread(target=close, daemon=True).start()


def safe_url(value):
    try:
        p = urlsplit(value)
        host = p.hostname or ''
        if ':' in host:
            host = '[' + host + ']'
        if p.port is not None:
            host += ':' + str(p.port)
        return urlunsplit((p.scheme, host, p.path, '', ''))
    except ValueError:
        return ''


class Diagnostics:
    def __init__(self, snap, text, segments):
        self.lock = threading.Lock()
        self.started = time.monotonic()
        self.data = {'software': appmeta.NAME, 'version': appmeta.VERSION, 'task_id': uuid.uuid4().hex,
                     'engine': snap.get('engine'), 'region_or_model': snap.get('region'),
                     'voice_id': snap.get('voice'), 'characters': len(text), 'segments': segments,
                     'events': [], 'outcome': 'running'}
        self.secrets = [str(snap[k]) for k in ('key', 'token') if snap.get(k)]

    def event(self, stage, index=0, **fields):
        # 严格白名单，不保留服务端原始正文、异常消息或请求文本。
        allowed = ('http_status', 'error_type', 'request_id', 'duration_ms', 'cached')
        row = {'stage': str(stage)[:100], 'segment': index,
               'elapsed_ms': round((time.monotonic()-self.started)*1000, 1)}
        row.update({k: v for k, v in fields.items() if k in allowed})
        with self.lock:
            self.data['events'].append(row)
            self.data['events'] = self.data['events'][-4000:]

    def snapshot(self):
        with self.lock:
            data = json.loads(json.dumps(self.data))
        def redact(value):
            if isinstance(value, str):
                for secret in self.secrets:
                    value = value.replace(secret, '[已脱敏]')
                return value
            if isinstance(value, dict):
                return {k: redact(v) for k, v in value.items()}
            if isinstance(value, list):
                return [redact(v) for v in value]
            return value
        return redact(data)

    def finish(self, outcome):
        with self.lock:
            self.data['outcome'] = outcome
            self.data['total_ms'] = round((time.monotonic()-self.started)*1000, 1)
        storage.atomic_json(storage.LOG_DIR / f"{self.data['task_id']}.json", self.snapshot())
        try:
            # 诊断日志只增不删会无限累积（含每次试听）；保留最近200份。
            logs = sorted(storage.LOG_DIR.glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True)
            for stale in logs[200:]:
                stale.unlink(missing_ok=True)
        except OSError:
            pass


_local = threading.local()


@contextmanager
def network_context(token, diagnostics=None, index=0):
    previous = getattr(_local, 'context', None)
    _local.context = (token, diagnostics, index)
    try:
        yield
    finally:
        _local.context = previous


def retry_wait(seconds):
    context = getattr(_local, 'context', None)
    if context:
        context[0].event.wait(seconds)
        context[0].check()
    else:
        time.sleep(seconds)


class Response:
    def __init__(self, response, token):
        self._response, self._token = response, token
        token.register(response)

    def __getattr__(self, name):
        return getattr(self._response, name)

    def iter_content(self, *args, **kwargs):
        for chunk in self._response.iter_content(*args, **kwargs):
            self._token.check()
            yield chunk

    def close(self):
        try:
            self._response.close()
        finally:
            self._token.unregister(self._response)


class Session:
    def __init__(self, session):
        self.session = session

    def request(self, method, *args, **kwargs):
        context = getattr(_local, 'context', None)
        if not context:
            return getattr(self.session, method)(*args, **kwargs)
        token, diagnostics, index = context
        token.check()
        start = time.monotonic()
        try:
            response = getattr(self.session, method)(*args, **kwargs)
        except BaseException as e:
            # BaseException（取消/中断）同样记诊断再原样抛出：旧 except Exception
            # 兜不住它们，之前是裸穿透、无 network_error 事件。
            if diagnostics:
                try:
                    diagnostics.event('network_error', index, error_type=type(e).__name__)
                except Exception:
                    pass
            raise
        except Exception as e:
            if diagnostics:
                diagnostics.event('network_error', index, error_type=type(e).__name__)
            token.check()
            raise
        if diagnostics:
            request_id = response.headers.get('X-Microsoft-RequestId', response.headers.get('x-request-id', ''))
            diagnostics.event('http_headers', index, http_status=response.status_code,
                              request_id=request_id[:150], duration_ms=round((time.monotonic()-start)*1000, 1))
        return Response(response, token)

    def post(self, *args, **kwargs):
        return self.request('post', *args, **kwargs)

    def get(self, *args, **kwargs):
        return self.request('get', *args, **kwargs)


def fingerprint(text, voice, snap):
    # 账户哈希隔离不同租户；完整端点参与指纹但不写入缓存元数据。
    params = {k: snap.get(k) for k in ('engine', 'region', 'ep', 'rate', 'pitch', 'vol', 'style', 'degree', 'role', 'audition')}
    params['ep'] = hashlib.sha256(str(params.get('ep') or '').encode()).hexdigest()
    params['credential_scope'] = hashlib.sha256(str(snap.get('key', '') or '').encode()).hexdigest()
    if snap.get('annotate'):
        # 未启用时兼容旧缓存，启用时通过新增字段区分。
        params['annotate'] = True
    payload = {'revision': 3, 'text': text, 'voice': voice, 'params': params}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class SegmentCache:
    MAX_BYTES = 2 * 1024**3  # 磁盘上限：超出后按最久未写入优先清理，缺失片段下次合成自动重建

    def __init__(self, root=None):
        self.root = Path(root or storage.CACHE_DIR / 'segments')
        self._prune_at = 0.0
        self._prune_lock = threading.Lock()

    def load(self, key):
        try:
            audio_path = self.root / f'{key}.audio'
            json_path = self.root / f'{key}.json'
            data = audio_path.read_bytes()
            meta = storage.read_json(json_path)
            if not isinstance(meta, dict):
                return None
            if not (data and meta.get('sha256') == hashlib.sha256(data).hexdigest()):
                return None
            # 命中即刷新 mtime：淘汰看“最久未用”而不是“最久写入”，热段不被误删。
            try:
                os.utime(audio_path, None)
                os.utime(json_path, None)
            except OSError:
                pass
            return data
        except OSError:
            return None

    def save(self, key, data):
        if not data:
            raise RuntimeError('空片段不能写入缓存')
        storage.atomic_bytes(self.root / f'{key}.audio', data)
        storage.atomic_json(self.root / f'{key}.json', {'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)})
        self._prune()

    def _prune(self):
        """容量控制：最多每分钟扫描一次，按 mtime 淘汰最旧片段；顺带回收孤儿 json/tmp。"""
        with self._prune_lock:
            now = time.time()
            if now - self._prune_at < 60:
                return
            self._prune_at = now
        try:
            pairs = []
            total = 0
            for audio in self.root.glob('*.audio'):
                try:
                    stat = audio.stat()
                    json_path = audio.with_suffix('.json')
                    json_size = json_path.stat().st_size if json_path.exists() else 0
                except OSError:
                    continue
                pairs.append((stat.st_mtime, audio, json_path, stat.st_size, json_size))
                total += stat.st_size + json_size
            if total > self.MAX_BYTES:
                pairs.sort()
                for _, audio, json_path, audio_size, json_size in pairs:
                    if total <= self.MAX_BYTES:
                        break
                    # 按文件分别扣减：某个 unlink 失败不得让 total 虚高导致过度淘汰。
                    try:
                        audio.unlink(missing_ok=True)
                        total -= audio_size
                    except OSError:
                        pass
                    try:
                        json_path.unlink(missing_ok=True)
                        total -= json_size
                    except OSError:
                        pass
            # 孤儿回收：.audio 缺失残留超过1小时的 .json、崩溃残留的 tmp 临时文件。
            for json_path in self.root.glob('*.json'):
                if json_path.with_suffix('.audio').exists():
                    continue
                try:
                    if now - json_path.stat().st_mtime > 3600:
                        json_path.unlink()
                except OSError:
                    pass
            # 镜像分支：.json 缺失残留超过1小时的 .audio（save 先写 audio 后写 json，崩溃夹中间）。
            for audio in self.root.glob('*.audio'):
                if audio.with_suffix('.json').exists():
                    continue
                try:
                    if now - audio.stat().st_mtime > 3600:
                        audio.unlink()
                except OSError:
                    pass
            for temp in self.root.glob('tmp*'):
                try:
                    if temp.is_file() and now - temp.stat().st_mtime > 3600:
                        temp.unlink()
                except OSError:
                    pass
        except OSError:
            pass


def _is_segment_key(key):
    """片段指纹：64位小写十六进制。"""
    return isinstance(key, str) and len(key) == 64 and all(c in '0123456789abcdef' for c in key)


def save_project(path, project, cache):
    import zipfile
    manifest = {k: project[k] for k in ('text', 'slots', 'engine', 'person', 'export', 'exports') if k in project}
    # 只写入合法指纹：否则保存出的项目会被 load_project 拒绝（自产不可读）。
    manifest['segments'] = [k for k in project.get('segments', []) if _is_segment_key(k)]
    manifest['parameters'] = {k: v for k, v in project.get('parameters', {}).items()
                              if k in ('rate', 'volume', 'pitch', 'style', 'degree', 'role')}
    service = project.get('engine_settings', {})
    manifest['engine_settings'] = {'region': service.get('region', ''), 'endpoint': safe_url(service.get('endpoint', ''))}
    manifest.update(format='SmartVoiceProject', schema=1)
    # 直接写临时文件（流式），避免 BytesIO 全内存双份拷贝；与 load_project 的1GB上限对齐。
    tmp = Path(storage.long_path(str(path)) + '.tmp')
    missing = 0
    written = 0
    try:
        with zipfile.ZipFile(tmp, 'w', compression=zipfile.ZIP_STORED) as z:
            z.writestr('project.json', json.dumps(manifest, ensure_ascii=False, indent=2))
            for key in set(manifest['segments']):
                audio = cache.load(key)
                if not audio:
                    missing += 1
                    continue
                written += len(audio)
                if written > 1024**3:
                    raise ValueError('项目音频超过1GB，无法保存；请清理片段缓存或拆分项目')
                z.writestr(f'segments/{key}.audio', audio)
        tmp.replace(storage.long_path(path))
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
    return missing


def load_project(path, cache):
    import zipfile
    with zipfile.ZipFile(storage.long_path(path)) as z:
        names = set(z.namelist())
        if 'project.json' not in names:
            raise ValueError('不是 SmartVoice 项目文件（缺少 project.json）')
        if sum(info.file_size for info in z.infolist()) > 1024**3:
            raise ValueError('项目过大（超过1GB）')
        if z.getinfo('project.json').file_size > 10 * 1024**2:
            raise ValueError('项目描述过大（超过10MB）')
        project = json.loads(z.read('project.json'))
        if project.get('format') != 'SmartVoiceProject' or project.get('schema') != 1:
            raise ValueError('不支持的项目格式')
        if not isinstance(project.get('text'), str) or not isinstance(project.get('slots'), list):
            raise ValueError('项目缺少原稿/角色')
        # 类型闸门前置：全部通过后才允许调用方改 UI，杜绝畸形项目造成半应用状态。
        if not isinstance(project.get('engine_settings'), dict):
            raise ValueError('项目引擎设置无效')
        service = project['engine_settings']
        if not isinstance(service.get('region', ''), str) or not isinstance(service.get('endpoint', ''), str):
            raise ValueError('项目引擎设置无效')
        params = project.get('parameters', {})
        if not isinstance(params, dict):
            raise ValueError('项目参数无效')
        for name in ('rate', 'volume', 'pitch'):
            if name in params and (not isinstance(params[name], int) or isinstance(params[name], bool)):
                raise ValueError('项目参数无效')
        for name in ('style', 'degree', 'role'):
            if name in params and not isinstance(params[name], str):
                raise ValueError('项目参数无效')
        if 'person' in project and not isinstance(project['person'], str):
            raise ValueError('项目人声无效')
        if 'exports' in project and (not isinstance(project['exports'], list)
                                     or any(not isinstance(e, dict) for e in project['exports'])):
            raise ValueError('项目导出记录无效')
        for key in project.get('segments', []):
            if not _is_segment_key(key):
                raise ValueError('片段指纹无效')
            member = f'segments/{key}.audio'
            if member in names:
                if z.getinfo(member).file_size > 10 * 1024**2:
                    raise ValueError('项目片段过大（超过10MB）')
                cache.save(key, z.read(member))
        return project
