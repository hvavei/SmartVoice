"""从完整冻结目录生成标准版和独立组件，纯Python源码随组件补齐动态加载。"""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import zipfile

import appmeta

COMMON = ('numpy', 'numpy.libs', 'onnxruntime', 'tokenizers')
OCR = ('cv2', 'rapidocr_onnxruntime', 'shapely', 'shapely.libs', 'pyclipper', 'yaml')
ABI = f'cp{sys.version_info.major}{sys.version_info.minor}-win-amd64'


def optional(name):
    lower = name.lower()
    if lower == 'models':
        return 'g2pw'
    if any(lower == n or lower.startswith(n + '-') for n in COMMON):
        return 'common'
    if any(lower == n.lower() or lower.startswith(n.lower() + '-') for n in OCR):
        return 'ocr'
    return None


def copy_sources(package, destination):
    spec = importlib.util.find_spec(package)
    if not spec or not spec.submodule_search_locations:
        raise RuntimeError(f'组件依赖包缺失: {package}')
    root = Path(next(iter(spec.submodule_search_locations)))
    for path in root.rglob('*'):
        relative = path.relative_to(root)
        if any(p in ('tests', 'test', '__pycache__', '.git') for p in relative.parts):
            continue
        if path.is_file() and path.suffix.lower() in ('.py', '.json', '.yaml', '.txt', '.pem', '.onnx'):
            target = destination / package / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)


def package(full, work, release):
    internal = full / '_internal'
    # OCR不使用视频读写；PIL仅需JPEG/PNG，移除无关视频DLL，保留音频FFmpeg。
    for path in internal.glob('cv2/opencv_videoio_ffmpeg*.dll'):
        path.unlink()
    for path in internal.glob('numpy/_core/_multiarray_tests*.pyd'):
        path.unlink()
    standard = work / 'standard' / 'SmartVoice'
    if standard.exists():
        shutil.rmtree(standard)
    standard.mkdir(parents=True)
    for path in full.iterdir():
        if path.is_file():
            shutil.copy2(path, standard / path.name)
    std_internal = standard / '_internal'
    std_internal.mkdir()
    for path in internal.iterdir():
        if optional(path.name) is None:
            if path.is_dir():
                shutil.copytree(path, std_internal/path.name)
            else:
                shutil.copy2(path, std_internal/path.name)
    result = {'schema': 1, 'app_version': appmeta.VERSION, 'components': {}}
    for name in ('g2pw', 'ocr'):
        payload = work / 'component-payloads' / name
        if payload.exists():
            shutil.rmtree(payload)
        payload.mkdir(parents=True)
        for path in internal.iterdir():
            if optional(path.name) in ('common', name):
                if path.is_dir():
                    shutil.copytree(path, payload/path.name)
                else:
                    shutil.copy2(path, payload/path.name)
        for module in ('numpy', 'onnxruntime', 'tokenizers') + (('cv2', 'rapidocr_onnxruntime', 'shapely', 'pyclipper', 'yaml') if name == 'ocr' else ()):
            copy_sources(module, payload)
        manifest = {'name': name, 'abi': ABI, 'version': appmeta.VERSION}
        (payload/'component.json').write_text(json.dumps(manifest), encoding='utf-8')
        filename = f'{name}-{appmeta.VERSION}-{ABI}.zip'
        archive_path = release / filename
        unpacked = 0
        with zipfile.ZipFile(archive_path, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=3) as z:
            for path in sorted(payload.rglob('*')):
                if path.is_file():
                    unpacked += path.stat().st_size
                    z.write(path, path.relative_to(payload).as_posix())
        with archive_path.open('rb') as f:
            digest = hashlib.file_digest(f, 'sha256').hexdigest()
        result['components'][name] = dict(manifest, sha256=digest, bytes=archive_path.stat().st_size,
            unpacked_bytes=unpacked, url=f'{appmeta.REPOSITORY}/releases/download/components-v{appmeta.VERSION}/{filename}')
    text = json.dumps(result, ensure_ascii=False, indent=2)
    for path in (release/'components.json', internal/'components.json', std_internal/'components.json'):
        path.write_text(text, encoding='utf-8')
    return standard
