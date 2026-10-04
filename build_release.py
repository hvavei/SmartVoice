"""构建单文件安装包 + 免重复解包的运行目录；从干净构建目录打包，不包含用户配置。"""
import argparse
import hashlib
import io
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import zipfile
import os

ROOT = Path(__file__).resolve().parent


def sign_file(path, args):
    if not (args.pfx or args.certificate_thumbprint):
        return False
    tool = args.signtool or shutil.which('signtool.exe')
    if not tool:
        raise RuntimeError('Signing requested but signtool.exe is unavailable')
    command = [str(tool), 'sign', '/fd', 'SHA256', '/tr', args.timestamp, '/td', 'SHA256']
    if args.pfx:
        password = os.getenv(args.password_env)
        if not password:
            raise RuntimeError('Certificate password environment variable is not set')
        command += ['/f', str(args.pfx), '/p', password]
    else:
        command += ['/sha1', args.certificate_thumbprint]
    result = subprocess.run(command + [str(path)], capture_output=True)
    if result.returncode:
        raise RuntimeError('Code signing failed (credential-bearing command suppressed)')
    subprocess.run([str(tool), 'verify', '/pa', str(path)], check=True)
    return True


def fetch_compiler(work):
    import requests
    version = '6.7.3'
    target = work / ('inno-' + version)
    compiler = target / 'tools' / 'ISCC.exe'
    if compiler.is_file() and (target / '.ok').is_file():
        return compiler
    if target.exists():
        # 无完成标记视为上次中断的残留：整目录重来，防止半套编译器混入构建
        shutil.rmtree(target, ignore_errors=True)
    url = f'https://api.nuget.org/v3-flatcontainer/tools.innosetup/{version}/tools.innosetup.{version}.nupkg'
    with requests.get(url, timeout=(10, 90)) as response:
        response.raise_for_status()
        package = response.content
    digest = hashlib.sha256(package).hexdigest()
    if digest != 'f780898e402ff80612cc8d9fcb8c6e02932bd1cb4c900ffdaa31f9341cfb49f4':
        raise RuntimeError('Compiler package checksum mismatch')
    print('Compiler package SHA256:', digest, flush=True)
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(package)) as archive:
        for item in archive.infolist():
            if not (target / item.filename).resolve().is_relative_to(target.resolve()):
                raise RuntimeError('Invalid compiler archive path')
        archive.extractall(target)
    matches = list(target.rglob('ISCC.exe'))
    if len(matches) != 1:
        raise RuntimeError('Compiler archive must contain one ISCC.exe')
    (target / '.ok').write_text('ok', encoding='utf-8')
    return matches[0]


def fetch_signtool(work):
    import requests
    version = '10.0.28000.2705'
    target = work / ('signtool-' + version)
    tool = target / 'signtool.exe'
    if tool.is_file() and (target / '.ok').is_file():
        return tool
    if target.exists():
        # 无完成标记视为上次中断的残留（可能只有半个 exe）：重下重解
        shutil.rmtree(target, ignore_errors=True)
    url = f'https://api.nuget.org/v3-flatcontainer/microsoft.windows.sdk.buildtools/{version}/microsoft.windows.sdk.buildtools.{version}.nupkg'
    with requests.get(url, timeout=(10, 300)) as response:
        response.raise_for_status()
        package = response.content
    digest = hashlib.sha256(package).hexdigest()
    if digest != '8bfdfb6ca2633f531cf80b5fa22512ba61a394d7988f0970db83baadc67929ed':
        raise RuntimeError('Signing tool package checksum mismatch')
    print('Signing tool package SHA256:', digest, flush=True)
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(package)) as archive:
        matches = [item for item in archive.namelist()
                   if item.startswith('bin/') and item.endswith('/x64/signtool.exe')]
        if len(matches) != 1:
            raise RuntimeError('Signing tool archive must contain one x64/signtool.exe')
        part = target / 'signtool.exe.part'
        part.write_bytes(archive.read(matches[0]))
        part.replace(tool)
    (target / '.ok').write_text('ok', encoding='utf-8')
    return tool


SELF_SIGNED_PS1 = r'''param([string]$ExportPath)
$ErrorActionPreference = 'Stop'
$cn = 'SmartVoice (Self-Signed)'
$eku = '1.3.6.1.5.5.7.3.3'
$cert = Get-ChildItem Cert:\CurrentUser\My |
    Where-Object { $_.Subject -eq "CN=$cn" -and $_.HasPrivateKey -and $_.NotAfter -gt (Get-Date).AddDays(30) -and (($_.EnhancedKeyUsageList | ForEach-Object ObjectId) -contains $eku) } |
    Sort-Object NotAfter -Descending | Select-Object -First 1
if (-not $cert) {
    $cert = New-SelfSignedCertificate -Type CodeSigningCert -Subject "CN=$cn" `
        -CertStoreLocation Cert:\CurrentUser\My -NotAfter (Get-Date).AddYears(5) `
        -KeyAlgorithm RSA -KeyLength 2048 -HashAlgorithm SHA256
}
if (-not (Get-ChildItem Cert:\CurrentUser\Root | Where-Object { $_.Thumbprint -eq $cert.Thumbprint })) {
    $cer = Join-Path $env:TEMP 'smartvoice-self-signed.cer'
    Export-Certificate -Cert $cert -FilePath $cer -Force | Out-Null
    Import-Certificate -FilePath $cer -CertStoreLocation Cert:\CurrentUser\Root | Out-Null
    Remove-Item $cer -Force
}
if ($ExportPath) {
    Export-Certificate -Cert $cert -FilePath $ExportPath -Force | Out-Null
}
Write-Output "THUMB=$($cert.Thumbprint)"
'''


def ensure_self_signed(work, export_path):
    """创建/复用本机自签代码签名证书并装入当前用户根信任库（替代已废弃的 makecert）。"""
    script = work / 'ensure-self-signed.ps1'
    script.write_text(SELF_SIGNED_PS1, encoding='utf-8')
    result = subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                             '-File', str(script), '-ExportPath', str(export_path)],
                            capture_output=True, text=True, encoding='utf-8', errors='replace')
    if result.returncode:
        raise RuntimeError('Self-signed certificate setup failed: '
                           + (result.stderr or result.stdout or '')[-400:])
    lines = [line.strip() for line in (result.stdout or '').splitlines()
             if line.strip().startswith('THUMB=')]
    thumb = lines[-1][len('THUMB='):] if lines else ''
    if len(thumb) != 40 or any(ch not in '0123456789ABCDEF' for ch in thumb):
        raise RuntimeError('Self-signed certificate setup returned no thumbprint')
    if not export_path.is_file():
        raise RuntimeError('Self-signed certificate export failed')
    return thumb


def replace_internal(source, internal):
    """发布依赖目录失败时清除半份拷贝，再恢复旧目录。"""
    old = internal.with_name(internal.name + '.old')
    if not internal.exists() and old.exists():
        old.rename(internal)
    if old.exists():
        shutil.rmtree(old)
    if internal.exists():
        internal.rename(old)
    try:
        shutil.copytree(source, internal)
    except Exception:
        if internal.exists():
            shutil.rmtree(internal)
        if old.exists():
            old.rename(internal)
        raise
    if old.exists():
        shutil.rmtree(old, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workdir', type=Path, default=Path(tempfile.gettempdir()) / 'SmartVoice-release')
    parser.add_argument('--compiler', type=Path)
    parser.add_argument('--fetch-compiler', action='store_true')
    parser.add_argument('--skip-build', action='store_true')
    parser.add_argument('--signtool', type=Path)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--pfx', type=Path)
    group.add_argument('--certificate-thumbprint')
    group.add_argument('--self-signed', action='store_true',
                       help='create/reuse a local self-signed code-signing certificate (this machine only)')
    parser.add_argument('--password-env', default='SMARTVOICE_SIGN_PASSWORD')
    # 新版 SDK signtool 拒绝 https 时间戳 URL；RFC3161 时间戳标准走 http。
    parser.add_argument('--timestamp', default='http://timestamp.digicert.com')
    args = parser.parse_args()
    work = args.workdir.resolve()
    work.mkdir(parents=True, exist_ok=True)
    compiler = args.compiler or (fetch_compiler(work) if args.fetch_compiler else None)
    if compiler is None or not compiler.is_file():
        parser.error('Specify --compiler ISCC.exe or --fetch-compiler')
    stage = work / 'dist' / 'SmartVoice'
    if not args.skip_build:
        # 增量判断只比较资源路径而不比较资源内容，仅更新 assets/ 图标不会触发 EXE 重打标，
        # 会留下旧图标的可执行文件；完整构建始终从干净构建目录开始。
        # dist 也要清：PyInstaller --noconfirm 不删旧输出，已删除的旧文件会混入新安装包。
        shutil.rmtree(work / 'build', ignore_errors=True)
        shutil.rmtree(work / 'dist', ignore_errors=True)
        subprocess.run([sys.executable, '-B', '-m', 'PyInstaller', '--noconfirm',
                        '--workpath', str(work / 'build'), '--distpath', str(work / 'dist'),
                        str(ROOT / 'SmartVoice.spec')], cwd=ROOT, check=True)
    if not (stage / 'SmartVoice.exe').is_file() or not (stage / '_internal').is_dir():
        raise RuntimeError('Incomplete application build')
    # 明确允许列表：不能把 dist 中的 Key、音频、缓存打入安装包。
    shutil.copy2(ROOT / 'README.txt', stage / 'README.txt')
    shutil.copy2(ROOT / 'Remove-Legacy-SmartVoice.bat', stage / 'Remove-Legacy-SmartVoice.bat')
    release = ROOT / 'release'
    release.mkdir(exist_ok=True)
    if args.pfx or args.certificate_thumbprint or args.self_signed:
        tool = args.signtool or shutil.which('signtool.exe')
        if not tool:
            print('signtool.exe not found; downloading Microsoft SDK signing tools...', flush=True)
            tool = fetch_signtool(work)
        args.signtool = Path(tool)
    if args.self_signed:
        args.certificate_thumbprint = ensure_self_signed(work, release / 'SmartVoice-Signing-Root.cer')
        print('Self-signed certificate thumbprint:', args.certificate_thumbprint, flush=True)
    import package_components
    standard = package_components.package(stage, work, release)
    signed = False
    for edition, folder in (('Standard', standard), ('Full', stage)):
        signed = sign_file(folder / 'SmartVoice.exe', args)
        subprocess.run([str(compiler), f'/DSourceDir={folder}', f'/DOutputDir={release}', f'/DEdition={edition}',
                        str(ROOT / 'installer.iss')], cwd=ROOT, check=True)
        sign_file(release / f'SmartVoice-Setup-{edition}.exe', args)
    if args.self_signed:
        status = (f'SELF-SIGNED: CN=SmartVoice (Self-Signed) {args.certificate_thumbprint}; '
                  'verified on this machine only, install SmartVoice-Signing-Root.cer elsewhere')
    else:
        status = 'Authenticode signed and verified' if signed else 'UNSIGNED: signing certificate not supplied'
    (release / 'signature-status.txt').write_text(status, encoding='utf-8')
    # 本地预览目录同步为本次构建产物；只动程序文件，dist 里若有用户文件保持不动。
    # 先改名旧目录再放入新目录：任何一步中断，dist 都还能恢复出可用的上一版。
    dist = ROOT / 'dist'
    dist.mkdir(exist_ok=True)
    replace_internal(stage / '_internal', dist / '_internal')
    # exe/README 与 _internal 联动：先写 .new 再原子替换，中断不留"新依赖+旧程序"错配。
    for name in ('SmartVoice.exe', 'README.txt'):
        tmp = dist / (name + '.new')
        shutil.copy2(stage / name, tmp)
        os.replace(tmp, dist / name)
    print('Installers:', release / 'SmartVoice-Setup-Standard.exe', release / 'SmartVoice-Setup-Full.exe', flush=True)
    print('Application:', dist / 'SmartVoice.exe', flush=True)


if __name__ == '__main__':
    main()
