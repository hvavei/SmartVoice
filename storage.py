"""用户数据、DPAPI 凭据及原子文件写入；与安装程序目录分离。"""
import base64
import copy
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import tempfile
import threading

DATA_DIR = Path(os.getenv('SMARTVOICE_DATA_DIR', Path(os.getenv('LOCALAPPDATA', Path.home())) / 'SmartVoice'))
SETTINGS_FILE = DATA_DIR / 'settings.json'
CACHE_DIR = DATA_DIR / 'cache'
PROJECT_DIR = DATA_DIR / 'projects'
EXPORT_DIR = DATA_DIR / 'exports'
LOG_DIR = DATA_DIR / 'logs'
_vault_lock = threading.Lock()


def atomic_bytes(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as f:
            temp = Path(f.name)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        # 清理失败不得掩盖 os.replace 的原始异常（杀软锁临时文件时）。
        if temp:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass


def read_json(path, default=None):
    try:
        # utf-8-sig：外部工具（记事本/VS Code）另存为带 BOM 的 UTF-8 也能读，无 BOM 时行为不变。
        return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return {} if default is None else default


def atomic_json(path, data):
    atomic_bytes(path, json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8'))


def engine_kind(label):
    """引擎归类: azure / edge / openai / volc；未知值回Azure。"""
    text = label or ''
    if text.startswith('Edge'):
        return 'edge'
    if text.startswith('OpenAI'):
        return 'openai'
    if text.startswith('火山'):
        return 'volc'
    return 'azure'


def dpapi(data, decrypt=False):
    if os.name != 'nt':
        raise RuntimeError('保存凭证需要 Windows DPAPI；未写入明文凭证')
    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_byte))]
    buffer = ctypes.create_string_buffer(data)
    src = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    dst = Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    fn = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                   ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    fn.restype = wintypes.BOOL
    if not fn(ctypes.byref(src), None, None, None, None, 1, ctypes.byref(dst)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(dst.data, dst.size)
    finally:
        free = ctypes.windll.kernel32.LocalFree
        free.argtypes = [ctypes.c_void_p]
        free.restype = ctypes.c_void_p
        free(dst.data)


def protect_settings(cfg):
    cfg = copy.deepcopy(cfg)
    profiles = cfg.setdefault('engine_profiles', {})
    with _vault_lock:
        vault = {}
        for kind, profile in profiles.items():
            secret = profile.pop('key', '')
            profile.pop('credential_ref', None)
            if cfg.get('remember_key') and secret:
                vault[kind] = base64.b64encode(dpapi(secret.encode('utf-8'))).decode('ascii')
                profile['credential_ref'] = kind
        cfg.pop('key', None)
        atomic_json(DATA_DIR / 'credentials.json', vault)
    return cfg


def unlock_settings(cfg):
    cfg = copy.deepcopy(cfg)
    vault = read_json(DATA_DIR / 'credentials.json')
    for kind, profile in cfg.get('engine_profiles', {}).items():
        ref = profile.get('credential_ref')
        if ref and ref in vault:
            try:
                profile['key'] = dpapi(base64.b64decode(vault[ref]), decrypt=True).decode('utf-8')
            except (OSError, ValueError, RuntimeError, TypeError, UnicodeDecodeError):
                # TypeError：credentials.json 被改写成非字符串值时不得穿透到 load_json 的宽兜底。
                profile['key'] = ''
    selected = cfg.get('engine', '')
    kind = engine_kind(selected)
    cfg['key'] = cfg.get('engine_profiles', {}).get(kind, {}).get('key', cfg.get('key', ''))
    return cfg


def migrate_legacy(program_dir):
    if SETTINGS_FILE.exists():
        return
    candidates = [Path(program_dir) / 'config.json',
                  Path(os.getenv('LOCALAPPDATA', Path.home())) / 'AzureTTSStudio' / 'config.json']
    for path in candidates:
        if not path.is_file():
            continue
        cfg = read_json(path)
        if not isinstance(cfg, dict) or not cfg:
            continue
        label = cfg.get('engine', '')
        kind = engine_kind(label)
        if not isinstance(cfg.get('engine_profiles'), dict):
            cfg['engine_profiles'] = {}
        profiles = cfg['engine_profiles']
        profiles.setdefault(kind, {k: cfg.get(k, '') for k in ('key', 'region', 'endpoint')})
        if not isinstance(cfg.get('export'), dict):
            cfg['export'] = {}
        # 旧输出只引用，不移动或删除用户作品。
        cfg['export']['directory'] = str(path.parent / 'output')
        # 旧配置有明文 Key 但没有 remember_key 时按保存处理，避免"先清旧、后发现没存"把 Key 销毁。
        has_secret = bool(cfg.get('key')) or any(
            isinstance(p, dict) and p.get('key') for p in profiles.values())
        if has_secret and not cfg.get('remember_key'):
            cfg['remember_key'] = True
        safe = protect_settings(cfg)
        # 清除旧明文Key与拷贝缓存均为尽力而为：旧目录只读时不阻断迁移。
        try:
            atomic_json(path, safe)
        except OSError:
            pass
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            for cache in path.parent.glob('voices_*.json'):
                try:
                    atomic_bytes(CACHE_DIR / cache.name, cache.read_bytes())
                except OSError:
                    pass
        except OSError:
            pass
        # 幂等闸门最后写=提交标记：中途失败下次启动可安全重试，不会留下"已迁移一半"状态。
        atomic_json(SETTINGS_FILE, safe)
        return


def unique_export(directory, name, extension, data):
    """同目录完整写入后以硬链接原子发布，存在同名文件时不覆盖。"""
    import re
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).strip(' .')[:100] or '配音'
    base = name.split('.')[0]
    if base.upper() in {'CON', 'PRN', 'AUX', 'NUL'} or re.fullmatch(r'(COM|LPT)[1-9]', base, re.I):
        name = '_' + name
    extension = extension.lower()
    if extension not in ('mp3', 'wav'):
        raise ValueError('仅支持 MP3/WAV')
    with tempfile.NamedTemporaryFile(dir=directory, delete=False) as f:
        tmp = Path(f.name)
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    try:
        for number in range(10000):
            target = directory / f'{name}{"-" + str(number) if number else ""}.{extension}'
            try:
                os.link(tmp, target)
                return str(target)
            except FileExistsError:
                continue
            except OSError:
                # Windows 上同目录 rename 不覆盖已存在目标，也支持无硬链接的盘符。
                if os.name != 'nt':
                    raise
                try:
                    os.rename(tmp, target)
                    return str(target)
                except FileExistsError:
                    continue
                except PermissionError:
                    # 目标被播放器/杀软占用（无硬链接盘符）：换下一个序号而不是导出失败。
                    continue
        raise RuntimeError('同名输出过多，请更改文件名')
    finally:
        tmp.unlink(missing_ok=True)
