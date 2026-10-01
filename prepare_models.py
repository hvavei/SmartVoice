"""下载官方G2pW推理资产；只用于构建，不在应用启动时联网。"""
from pathlib import Path
import hashlib
import json
import shutil
import tempfile
import zipfile

import requests


def prepare():
    target = Path(__file__).parent / 'models' / 'g2pw'
    target.mkdir(parents=True, exist_ok=True)
    sources = {}

    def download(url, path):
        # 先写 .part 再原子替换：中断只留临时文件，正式文件永远是完整下载
        part = path.with_name(path.name + '.part')
        with requests.get(url, stream=True, timeout=(15, 120)) as r:
            r.raise_for_status()
            with part.open('wb') as out:
                for block in r.iter_content(1024 * 1024):
                    out.write(block)
        part.replace(path)
        sources[path.name] = url

    if not (target / 'g2pw.onnx').exists():
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / 'model.zip'
            download('https://storage.googleapis.com/esun-ai/g2pW/G2PWModel-v2-onnx.zip', archive)
            with zipfile.ZipFile(archive) as z:
                for name in z.namelist():
                    base = Path(name).name
                    if base in ('g2pw.onnx', 'POLYPHONIC_CHARS.txt', 'MONOPHONIC_CHARS.txt', 'config.py', 'version'):
                        part = target / (base + '.part')
                        with z.open(name) as src, part.open('wb') as dst:
                            shutil.copyfileobj(src, dst)
                        part.replace(target / base)
    repo = 'https://raw.githubusercontent.com/GitYCC/g2pW/master/'
    for name in ('bopomofo_to_pinyin_wo_tune_dict.json', 'bert-base-chinese_s2t_dict.txt',
                 'char_bopomofo_dict.json'):
        download(repo + 'g2pw/' + name, target / name)
    download(repo + 'LICENCE', target / 'LICENSE-G2PW.txt')
    download('https://huggingface.co/google-bert/bert-base-chinese/resolve/main/vocab.txt', target / 'vocab.txt')
    required = ('g2pw.onnx', 'POLYPHONIC_CHARS.txt', 'MONOPHONIC_CHARS.txt', 'config.py', 'version',
                'bopomofo_to_pinyin_wo_tune_dict.json', 'bert-base-chinese_s2t_dict.txt',
                'char_bopomofo_dict.json', 'LICENSE-G2PW.txt', 'vocab.txt')
    missing = [n for n in required if not (target / n).is_file()]
    if missing:
        raise RuntimeError('G2PW 资产缺失: ' + ', '.join(missing))
    manifest = {}
    for path in target.iterdir():
        if path.is_file() and path.name != 'manifest.json' and not path.name.endswith('.part'):
            with path.open('rb') as f:
                digest = hashlib.file_digest(f, 'sha256').hexdigest()
            manifest[path.name] = {'sha256': digest, 'bytes': path.stat().st_size,
                                   'source': sources.get(path.name, 'G2PWModel-v2-onnx.zip')}
    (target / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    print('G2PW assets ready:', target)


if __name__ == '__main__':
    prepare()
