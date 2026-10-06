"""从用户提供的图片生成 SmartVoice 各用途品牌资源（构建时使用 Pillow）。

输出到 assets/：
- smartvoice.ico       窗口/任务栏/快捷方式/安装程序图标，16~256 共 7 档；
                       16/24/32 小尺寸自动锐化，保证任务栏与标题栏可辨识。
- smartvoice.png       512x512 应用与文档用图，全分辨率 LANCZOS 下采样。
- wizard-image.bmp     安装向导侧栏主图（Inno Setup，164x314）。
- wizard-small.bmp     安装向导标题栏小图（Inno Setup，55x58）。
"""
import argparse
import struct
from pathlib import Path
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)
SHARPEN_MAX = 32
WIZARD_SIZE = (164, 314)
WIZARD_SMALL_SIZE = (55, 58)
TITLE_FILL = (13, 79, 150)
SUBTITLE_FILL = (60, 110, 165)


def _load_square(source):
    with Image.open(source) as original:
        if min(original.size) < 256:
            raise ValueError('源图过小（短边不足256px），请提供≥512px的正方形图，否则图标模糊')
        image = ImageOps.exif_transpose(original).convert('RGB')
    side = min(image.size)
    if side != image.size[0] or side != image.size[1]:
        image = ImageOps.fit(image, (side, side), Image.Resampling.LANCZOS, centering=(.5, .5))
    return image


def _frame(base, size):
    frame = base.resize(size, Image.Resampling.LANCZOS, reducing_gap=2.0)
    if max(size) <= SHARPEN_MAX:
        frame = frame.filter(ImageFilter.UnsharpMask(radius=0.7, percent=150, threshold=2))
    return frame


def _dib_blob(rgba):
    width, height = rgba.size
    and_stride = ((width + 31) // 32) * 4
    header = struct.pack('<IiiHHIIiiII', 40, width, height * 2, 1, 32, 0,
                         width * height * 4 + and_stride * height, 0, 0, 0, 0)
    raw = rgba.tobytes('raw', 'BGRA')
    rows = [raw[y * width * 4:(y + 1) * width * 4] for y in range(height)]
    return header + b''.join(reversed(rows)) + b'\x00' * (and_stride * height)


def _save_ico(path, base):
    blobs, entries = [], []
    offset = 6 + 16 * len(ICO_SIZES)
    for size in ICO_SIZES:
        blob = _dib_blob(_frame(base, (size, size)).convert('RGBA'))
        blobs.append(blob)
        dimension = 0 if size >= 256 else size
        entries.append(struct.pack('<BBBBHHII', dimension, dimension, 0, 0, 1, 32, len(blob), offset))
        offset += len(blob)
    path.write_bytes(struct.pack('<HHH', 0, 1, len(ICO_SIZES)) + b''.join(entries) + b''.join(blobs))


def _font(size, bold=False):
    names = ('msyhbd.ttc', 'segoeuib.ttf', 'arialbd.ttf') if bold else ('msyh.ttc', 'segoeui.ttf', 'arial.ttf')
    for name in names:
        try:
            return ImageFont.truetype(str(Path('C:/Windows/Fonts') / name), size)
        except OSError:
            continue
    return ImageFont.load_default()


def _center_text(draw, center_x, top_y, text, font, fill):
    box = draw.textbbox((0, 0), text, font=font)
    draw.text((center_x - (box[2] - box[0]) // 2 - box[0], top_y), text, font=font, fill=fill)


def _wizard(base, size, with_text=True):
    canvas = Image.new('RGB', size, base.getpixel((0, 0)))
    if with_text:
        side = round(size[0] * 0.68)
        x = (size[0] - side) // 2
        y = round(size[1] * 0.17)
        canvas.paste(_frame(base, (side, side)), (x, y))
        draw = ImageDraw.Draw(canvas)
        _center_text(draw, size[0] // 2, y + side + 12, 'SmartVoice', _font(17, True), TITLE_FILL)
        _center_text(draw, size[0] // 2, y + side + 40, 'AI 桌面配音工作台', _font(10), SUBTITLE_FILL)
    else:
        side = round(min(size) * 0.8)
        canvas.paste(_frame(base, (side, side)), ((size[0] - side) // 2, (size[1] - side) // 2))
    return canvas


def prepare(source, destination):
    import os
    import tempfile
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    base = _load_square(source)
    # 先写临时文件再原子替换：中断不留半截图标，下次构建不误用坏图。
    with tempfile.TemporaryDirectory(dir=destination) as tmp:
        tmp = Path(tmp)
        _frame(base, (512, 512)).save(tmp / 'smartvoice.png', optimize=True)
        _save_ico(tmp / 'smartvoice.ico', base)
        _wizard(base, WIZARD_SIZE).save(tmp / 'wizard-image.bmp', 'BMP')
        _wizard(base, WIZARD_SMALL_SIZE, with_text=False).save(tmp / 'wizard-small.bmp', 'BMP')
        for name in ('smartvoice.png', 'smartvoice.ico', 'wizard-image.bmp', 'wizard-small.bmp'):
            os.replace(tmp / name, destination / name)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source')
    parser.add_argument('--output', default=str(Path(__file__).parent / 'assets'))
    args = parser.parse_args()
    prepare(args.source, args.output)
