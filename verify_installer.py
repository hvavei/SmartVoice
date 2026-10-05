"""在专用临时目录验证安装、覆盖升级保留数据、离线依赖检查和卸载保留数据。"""
import argparse
import json
from pathlib import Path
import subprocess
import tempfile
import os


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('installer', type=Path)
    parser.add_argument('--parent', type=Path, required=True)
    parser.add_argument('--components', type=Path, help='验证标准版安装本地组件后的完整功能')
    args = parser.parse_args()
    if not args.parent.is_dir():
        parser.error('parent must exist')
    if not args.installer.is_file():
        parser.error('installer not found')
    if args.components and not (args.components / 'components.json').is_file():
        parser.error('components.json not found')
    # 保留日志/验证结果以便审查，只卸载本脚本创建的测试安装。
    work = Path(tempfile.mkdtemp(prefix='installer-check-', dir=args.parent))
    app = work / 'app space 测试'
    user = work / 'user data 测试'
    env = dict(os.environ, SMARTVOICE_DATA_DIR=str(user))
    command = [str(args.installer.resolve()), '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART',
               '/NOICONS', '/TASKS=', f'/DIR={app}', f'/LOG={work / "install.log"}']
    subprocess.run(command, check=True, timeout=300)
    try:
        assert (app / 'SmartVoice.exe').is_file()
        assert (app / 'unins000.exe').is_file()
        assert not (app / 'config.json').exists(), 'Installer must not ship developer config'
        assert not list(app.rglob('voices_cache*.json')), 'Installer must not ship personal caches'
        assert not list(app.rglob('output')), 'Installer must not ship output audio'
        report = work / 'installation-check.json'
        subprocess.run([str(app / 'SmartVoice.exe'), '--installation-check', str(report)],
                       check=True, timeout=120, env=env)
        checks = json.loads(report.read_text(encoding='utf-8'))
        assert checks['ok'], checks
        if args.components:
            metadata = json.loads((args.components / 'components.json').read_text(encoding='utf-8'))
            # OCR先装先验，防止G2PW组件的公共依赖掩盖OCR包缺文件。
            from urllib.parse import urlsplit, unquote
            from pathlib import PurePosixPath
            for name in sorted(metadata['components'], key=lambda name: name != 'ocr'):
                entry = metadata['components'][name]
                archive = args.components / unquote(PurePosixPath(urlsplit(entry['url']).path).name)
                if not archive.is_file():
                    parser.error(f'component archive not found: {archive.name}')
                subprocess.run([str(app / 'SmartVoice.exe'), '--install-component', name, str(archive)],
                               check=True, timeout=300, env=env)
                subprocess.run([str(app / 'SmartVoice.exe'), '--installation-check', str(report)],
                               check=True, timeout=120, env=env)
                component_checks = json.loads(report.read_text(encoding='utf-8'))
                expected = {'ocr': 'pdf-and-ocr-inference', 'g2pw': 'g2pw-model-inference'}[name]
                assert component_checks['ok'] and expected in component_checks['checks'], component_checks
            subprocess.run([str(app / 'SmartVoice.exe'), '--installation-check', str(report)],
                           check=True, timeout=120, env=env)
            checks = json.loads(report.read_text(encoding='utf-8'))
            assert checks['ok'] and 'g2pw-model-inference' in checks['checks'] and 'pdf-and-ocr-inference' in checks['checks'], checks
        # 合成测试数据，不接触真实配置。
        config = b'{"installation_test":true}'
        user.mkdir(exist_ok=True)
        (user / 'settings.json').write_bytes(config)
        (user / 'exports').mkdir()
        (user / 'exports' / 'keep.txt').write_text('user data', encoding='utf-8')
        subprocess.run(command, check=True, timeout=300)
        assert (user / 'settings.json').read_bytes() == config
        assert (user / 'exports' / 'keep.txt').read_text(encoding='utf-8') == 'user data'
    finally:
        # 卸载失败不许掩盖 try 块里的真正断言错误；半装状态可能没有卸载器
        unins = app / 'unins000.exe'
        if unins.is_file():
            subprocess.run([str(unins), '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART'],
                           check=False, timeout=120)
            import time
            for _ in range(100):
                if not (app / 'SmartVoice.exe').exists() and not (app / '_internal').exists():
                    break
                time.sleep(0.1)
    assert (user / 'settings.json').read_bytes() == config
    assert (user / 'exports' / 'keep.txt').is_file()
    assert not (app / 'SmartVoice.exe').exists()
    assert not (app / '_internal').exists(), 'uninstall left _internal'
    print(json.dumps({'ok': True, 'workdir': str(work), 'checks': checks['checks'],
                      'upgrade_preserves_data': True, 'uninstall_preserves_data': True}, ensure_ascii=True, indent=2))


if __name__ == '__main__':
    main()
