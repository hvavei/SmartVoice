"""版本绑定的可选组件：HTTPS下载/离线导入、SHA256校验、安全解包和原子激活。"""
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
import zipfile
from urllib.parse import urlsplit

import storage

ROOT = Path(__file__).resolve().parent
COMPONENT_DIR = storage.DATA_DIR / 'components'
ABI = f'cp{sys.version_info.major}{sys.version_info.minor}-win-amd64'
NAMES = {'g2pw': 'G2PW 多音字增强', 'ocr': '扫描 PDF OCR'}
UNPACKED_SLACK = 65536  # 允许 zip 元数据与实际展开的字节误差
_lock = threading.RLock()
_dll_handles = []
_activated = set()


class MissingComponent(RuntimeError):
    pass


def catalog():
    path = ROOT / 'components.json'
    try:
        data = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict) or not isinstance(data.get('components'), dict):
        return {'schema': 1, 'components': {}}
    return data


_REQUIRED_ENTRY_KEYS = ('abi', 'sha256', 'bytes', 'unpacked_bytes', 'url')


def _entry(name):
    if name not in NAMES:
        raise MissingComponent(f'未知组件: {name}')
    entry = catalog().get('components', {}).get(name)
    if (not isinstance(entry, dict) or entry.get('abi') != ABI
            or any(k not in entry for k in _REQUIRED_ENTRY_KEYS)):
        raise MissingComponent(f'{NAMES[name]} 组件清单缺失或与当前Python架构不兼容')
    return entry


def bundled(name):
    if name == 'g2pw':
        return (ROOT / 'models/g2pw/g2pw.onnx').is_file()
    return (ROOT / 'rapidocr_onnxruntime/models').is_dir() if getattr(sys, 'frozen', False) else importlib.util.find_spec('rapidocr_onnxruntime') is not None


def installed_root(name):
    if bundled(name):
        return ROOT
    entry = _entry(name)
    pointer = storage.read_json(COMPONENT_DIR / f'{name}.json')
    if not isinstance(pointer, dict):
        raise MissingComponent(f'尚未安装{NAMES[name]}；请到“组件 → 管理组件”下载或导入组件包')
    if pointer.get('sha256') == entry['sha256'] and pointer.get('folder') == f"{name}-{entry['sha256'][:16]}":
        path = COMPONENT_DIR / pointer['folder']
        if (path / 'component.json').is_file():
            return path
    raise MissingComponent(f'尚未安装{NAMES[name]}；请到“组件 → 管理组件”下载或导入组件包')


def available(name):
    try:
        installed_root(name)
        return True
    except (MissingComponent, OSError, ValueError, KeyError):
        return False


def activate(name):
    with _lock:
        root = installed_root(name)
        if root == ROOT or name in _activated:
            return root
        # 包含与本程序ABI一致的完整可选模块；优先于冻结包中的纯Python模块查找。
        sys.path.insert(0, str(root))
        handles = []
        try:
            if os.name == 'nt':
                for directory in {p.parent for p in root.rglob('*.dll')}:
                    handles.append(os.add_dll_directory(str(directory)))
            importlib.invalidate_caches()
        except Exception:
            # 激活失败回滚：不留半套 sys.path / DLL 句柄状态，下次调用可重试
            try:
                sys.path.remove(str(root))
            except ValueError:
                pass
            for h in handles:
                try:
                    h.close()
                except OSError:
                    pass
            raise
        _dll_handles.extend(handles)
        _activated.add(name)
        # 回收同名旧版本组件目录（升级后的残留；例如 G2PW 约 700MB）
        if root != ROOT:
            for d in COMPONENT_DIR.glob(f'{name}-*'):
                if d.is_dir() and d.resolve() != root.resolve() and str(d) not in sys.path:
                    shutil.rmtree(d, ignore_errors=True)
        return root


def _cleanup_stale_tmp():
    """清理强杀/断电残留的临时目录（仅处理超过1小时的，避免误删在用目录）。"""
    try:
        now = time.time()
        for prefix in ('unpack-', 'download-', 'repair-'):
            for d in COMPONENT_DIR.glob(prefix + '*'):
                try:
                    if d.is_dir() and now - d.stat().st_mtime > 3600:
                        shutil.rmtree(d, ignore_errors=True)
                except OSError:
                    pass
    except OSError:
        pass


def install_archive(name, archive_path, progress=None, cancelled=None):
    entry = _entry(name)
    path = Path(archive_path)
    if path.stat().st_size != entry['bytes']:
        raise ValueError('组件大小与当前版本清单不符')
    with path.open('rb') as f:
        if hashlib.file_digest(f, 'sha256').hexdigest() != entry['sha256']:
            raise ValueError('组件 SHA-256 校验失败，未安装')
    COMPONENT_DIR.mkdir(parents=True, exist_ok=True)
    with _lock, tempfile.TemporaryDirectory(prefix='unpack-', dir=COMPONENT_DIR) as tmp:
        # 清理在锁内执行：不与另一安装的解包/替换临界区并发
        _cleanup_stale_tmp()
        staging = Path(tmp)
        with zipfile.ZipFile(path) as z:
            infos = z.infolist()
            if sum(i.file_size for i in infos) > entry['unpacked_bytes'] + UNPACKED_SLACK:
                raise ValueError('组件展开大小异常')
            names = set()
            for i in infos:
                if i.filename in names or '\\' in i.filename or ':' in i.filename or (i.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError('组件路径/链接无效')
                names.add(i.filename)
                if not (staging / i.filename).resolve().is_relative_to(staging.resolve()):
                    raise ValueError('组件包含越界路径')
            try:
                manifest = json.loads(z.read('component.json'))
            except KeyError:
                raise ValueError('组件标识不匹配')
            if manifest.get('name') != name or manifest.get('abi') != ABI:
                raise ValueError('组件标识不匹配')
            for index, i in enumerate(infos):
                if cancelled and cancelled.is_set():
                    raise RuntimeError('组件安装已取消')
                z.extract(i, staging)
                if progress:
                    progress(index + 1, len(infos), '解包')
        folder = f"{name}-{entry['sha256'][:16]}"
        final = COMPONENT_DIR / folder
        if final.exists() and (name in _activated or str(final) in sys.path):
            raise RuntimeError('组件正在使用，请重启软件后再导入修复')
        # 先保留旧目录；解包替换或指针提交失败时恢复上一份可用组件。
        with tempfile.TemporaryDirectory(prefix='repair-', dir=COMPONENT_DIR) as backup_dir:
            backup = Path(backup_dir) / 'previous'
            if final.exists():
                os.replace(final, backup)
            try:
                os.replace(staging, final)
                staging.mkdir()
                storage.atomic_json(COMPONENT_DIR / f'{name}.json',
                                    {'sha256': entry['sha256'], 'folder': folder})
            except Exception:
                if final.exists():
                    shutil.rmtree(final)
                if backup.exists():
                    os.replace(backup, final)
                raise
    return final


def download(name, progress=None, cancelled=None):
    import requests
    entry = _entry(name)
    url = entry['url']
    if urlsplit(url).scheme != 'https' or urlsplit(url).hostname != 'github.com':
        raise ValueError('不受支持的组件下载来源')
    COMPONENT_DIR.mkdir(parents=True, exist_ok=True)
    with _lock:
        _cleanup_stale_tmp()
    with tempfile.TemporaryDirectory(prefix='download-', dir=COMPONENT_DIR) as tmp:
        archive = Path(tmp) / 'component.zip'
        with requests.get(url, stream=True, timeout=(10, 30)) as response:
            if response.status_code == 404:
                raise RuntimeError('该版本组件尚未上传到 GitHub Releases；请先发布组件包或选择本地导入')
            response.raise_for_status()
            if urlsplit(response.url).scheme != 'https':
                raise ValueError('组件下载跳转到非HTTPS地址')
            received = 0
            with archive.open('wb') as f:
                for block in response.iter_content(256 * 1024):
                    if cancelled and cancelled.is_set():
                        raise RuntimeError('组件下载已取消')
                    received += len(block)
                    if received > entry['bytes']:
                        raise ValueError('组件响应超出预期大小')
                    f.write(block)
                    if progress:
                        progress(received, entry['bytes'], '下载')
        if cancelled and cancelled.is_set():
            raise RuntimeError('组件下载已取消')
        return install_archive(name, archive, progress, cancelled)
