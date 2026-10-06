"""下载官方G2pW推理资产；只用于构建，不在应用启动时联网。"""
from pathlib import Path
import hashlib
import json
import shutil
import tempfile
import zipfile

import requests


REQUIRED = ('g2pw.onnx', 'POLYPHONIC_CHARS.txt', 'MONOPHONIC_CHARS.txt', 'config.py', 'version',
            'bopomofo_to_pinyin_wo_tune_dict.json', 'bert-base-chinese_s2t_dict.txt',
            'char_bopomofo_dict.json', 'LICENSE-G2PW.txt', 'vocab.txt')
ZIP_MEMBERS = ('g2pw.onnx', 'POLYPHONIC_CHARS.txt', 'MONOPHONIC_CHARS.txt', 'config.py', 'version')


def check_asset(target, name):
    """单个资产检查：返回错误信息，无错返回 None（供自愈删除与严格校验共用）。"""
    path = Path(target) / name
    if not path.is_file() or path.stat().st_size == 0:
        return f'G2PW 资产缺失: {name}'
    if name == 'g2pw.onnx' and path.stat().st_size <= 10 * 1024**2:
        return '模型文件异常过小，疑似错误页，请删除 g2pw.onnx 后重跑'
    if name in ('bopomofo_to_pinyin_wo_tune_dict.json', 'char_bopomofo_dict.json'):
        try:
            json.loads(path.read_text(encoding='utf-8'))
        except ValueError:
            return f'G2PW 资产不是合法 JSON（{name}），请删除后重跑'
    if name == 'vocab.txt':
        try:
            first = path.read_text(encoding='utf-8').splitlines()[0]
        except (ValueError, IndexError):
            return '词表异常，请删除 vocab.txt 后重跑'
        if '[PAD]' not in first:
            return '词表异常（首行无 [PAD]），请删除 vocab.txt 后重跑'
    return None


def validate_assets(target):
    """校验已落盘资产：缺失/空文件、异常模型、坏 JSON、坏词表一律明确报错。"""
    for name in REQUIRED:
        error = check_asset(Path(target), name)
        if error:
            raise RuntimeError(error)


def prepare():
    target = Path(__file__).parent / 'models' / 'g2pw'
    target.mkdir(parents=True, exist_ok=True)
    try:
        previous = json.loads((target / 'manifest.json').read_text(encoding='utf-8'))
        sources = {name: info['source'] for name, info in
                   previous.get('manifest', previous).items()
                   if isinstance(info, dict) and info.get('source')}
    except (OSError, ValueError):
        sources = {}

    def download(url, path, cap):
        # 先写 .part 再原子替换：中断只留临时文件，正式文件永远是完整下载；
        # 限流防投毒/无限流写满磁盘，失败清理 .part。
        part = path.with_name(path.name + '.part')
        received = 0
        try:
            with requests.get(url, stream=True, timeout=(15, 120)) as r:
                r.raise_for_status()
                with part.open('wb') as out:
                    for block in r.iter_content(1024 * 1024):
                        if not block:
                            continue
                        received += len(block)
                        if received > cap:
                            raise ValueError('资产超出预期大小，中止下载')
                        out.write(block)
            # replace 同样可能被杀软锁住失败：纳入 try，同样清理 .part 不留残留。
            part.replace(path)
        except Exception:
            try:
                part.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        sources[path.name] = url

    if any(check_asset(target, n) is not None for n in ZIP_MEMBERS):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / 'model.zip'
            download('https://storage.googleapis.com/esun-ai/g2pW/G2PWModel-v2-onnx.zip', archive,
                     800 * 1024**2)
            with zipfile.ZipFile(archive) as z:
                for name in z.namelist():
                    base = Path(name).name
                    if base in ('g2pw.onnx', 'POLYPHONIC_CHARS.txt', 'MONOPHONIC_CHARS.txt', 'config.py', 'version'):
                        part = target / (base + '.part')
                        with z.open(name) as src, part.open('wb') as dst:
                            shutil.copyfileobj(src, dst)
                        part.replace(target / base)
    repo = 'https://raw.githubusercontent.com/GitYCC/g2pW/master/'
    for name in REQUIRED:
        # 自愈：坏文件先删，后续按缺失重下，不再死循环报同一错。
        if name not in ZIP_MEMBERS and check_asset(target, name) is not None:
            try:
                (target / name).unlink(missing_ok=True)
            except OSError:
                pass
    for name in ('bopomofo_to_pinyin_wo_tune_dict.json', 'bert-base-chinese_s2t_dict.txt',
                 'char_bopomofo_dict.json'):
        if (target / name).is_file() and (target / name).stat().st_size > 0:
            continue  # 已有非空文件不再重下，保证幂等
        download(repo + 'g2pw/' + name, target / name, 50 * 1024**2)
    if not (target / 'LICENSE-G2PW.txt').is_file() or (target / 'LICENSE-G2PW.txt').stat().st_size == 0:
        download(repo + 'LICENCE', target / 'LICENSE-G2PW.txt', 50 * 1024**2)
    vocab = target / 'vocab.txt'
    if not vocab.is_file() or vocab.stat().st_size == 0:
        download('https://huggingface.co/google-bert/bert-base-chinese/resolve/main/vocab.txt', vocab,
                 5 * 1024**2)
    validate_assets(target)
    manifest = {}
    # 只收录 REQUIRED 资产：杂散文件（__pycache__/DS_Store/残留 .part）不许冒充资产进清单。
    for name in REQUIRED:
        path = target / name
        with path.open('rb') as f:
            digest = hashlib.file_digest(f, 'sha256').hexdigest()
        manifest[name] = {'sha256': digest, 'bytes': path.stat().st_size,
                          'source': sources.get(name, 'G2PWModel-v2-onnx.zip')}
    manifest_path = target / 'manifest.json'
    manifest_tmp = manifest_path.with_name(manifest_path.name + '.part')
    manifest_tmp.write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    manifest_tmp.replace(manifest_path)
    print('G2PW assets ready:', target)


if __name__ == '__main__':
    prepare()
