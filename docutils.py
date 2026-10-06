"""PDF 文本/OCR 与规范音频拼接（成熟解析器 + ffmpeg）。

- PDF：pypdf 提取文本（含 ToUnicode 字体映射）；无文本页用 pypdfium2
  渲染成位图再交给 RapidOCR。
- 多人音频：用 ffmpeg 把每段统一解码、重采样到同一采样率/声道，
  再重编码 MP3 拼接，避免字节拼接导致的播放器不兼容与采样率混杂。
"""
import threading

_OCR = None
_OCR_LOCK = threading.Lock()


def extract_pdf_text(path, ocr=False):
    import pypdf
    import storage
    path = storage.long_path(path)
    try:
        reader = pypdf.PdfReader(path)
    except Exception as e:
        # 重命名 txt / 加密 / 截断文件：原始异常穿透只剩堆栈，转用户可读错。
        raise ValueError('PDF 已损坏或已加密，请另存后导入') from e
    try:
        try:
            pages = list(reader.pages)
        except Exception as e:
            raise ValueError('PDF 已损坏或已加密，请另存后导入') from e
        pages_text = [""] * len(pages)
        needs_ocr = []
        for i, page in enumerate(pages):
            try:
                text = page.extract_text(extraction_mode='layout') or ""
            except TypeError:
                text = page.extract_text() or ''
            except Exception:
                text = ""
            if text.strip():
                pages_text[i] = text
            else:
                needs_ocr.append(i)
    finally:
        try:
            reader.close()  # 文本已提取完，尽早释放整份PDF字节，OCR只用path不再依赖reader
        except Exception:
            pass
    if ocr and needs_ocr:
        try:
            ocr_map = _ocr_pages(path, needs_ocr)
        except Exception as e:
            # 组件缺失/模型损坏/依赖异常：已有文本页照常返回，不整篇作废；全空才明确报错。
            if all(not text for text in pages_text):
                raise RuntimeError(f'OCR 不可用（{e}），扫描页无法导入') from e
            ocr_map = {}
        for i in needs_ocr:
            pages_text[i] = ocr_map.get(i, "")
    return "\n\n".join(pages_text)


def _ocr_pages(path, page_indices):
    ocr = create_ocr()          # 必须先激活OCR组件：标准版的 numpy 随组件分发，晚于导入会 ModuleNotFoundError
    import numpy as np
    import pypdfium2 as pdfium
    import storage
    path = storage.long_path(path)
    result = {}
    with pdfium.PdfDocument(path) as pdf:
        for idx in page_indices:
            try:
                page = pdf[idx]
                try:
                    bitmap = page.render(scale=2.0)
                    try:
                        with bitmap.to_pil() as img:
                            out, _ = ocr(np.array(img.convert('RGB'))[:, :, ::-1].copy())
                        if out:
                            result[idx] = ocr_paragraphs(out)
                    finally:
                        bitmap.close()
                finally:
                    page.close()
            except Exception:
                # 单页失败只丢这一页：旧逻辑直接 raise，已 OCR 好的页跟着全作废；
                # 全空的兜底由调用方统一报错，这里只收部分成果。
                continue
    return result


def create_ocr():
    """显式绑定 OCR 子模块，避免旧版库动态裸导入在冻结程序中失效；进程内单例复用。"""
    global _OCR
    if _OCR is not None:
        return _OCR
    with _OCR_LOCK:
        if _OCR is not None:
            return _OCR
        import components
        components.activate('ocr')
        from rapidocr_onnxruntime import RapidOCR
        from rapidocr_onnxruntime.ch_ppocr_v3_det import TextDetector
        from rapidocr_onnxruntime.ch_ppocr_v3_rec import TextRecognizer
        from rapidocr_onnxruntime.ch_ppocr_v2_cls import TextClassifier

        class PackagedOCR(RapidOCR):
            @staticmethod
            def init_module(module_name, class_name):
                return {'TextDetector': TextDetector, 'TextRecognizer': TextRecognizer,
                        'TextClassifier': TextClassifier}[class_name]
        _OCR = PackagedOCR()
        return _OCR


def ocr_paragraphs(rows):
    """保留OCR阅读行序，用明显的纵向间隙标记段落，不机械按标点拆句。

    扫描图没有原始段落结构，多栏版面不能保证无损还原。
    """
    import statistics
    if not rows:
        return ''
    heights = [max(p[1] for p in box) - min(p[1] for p in box) for box, *_ in rows]
    typical = max(1, statistics.median(heights))
    pieces, bottom = [], None
    for box, text, *_ in rows:
        top = min(p[1] for p in box)
        if pieces:
            pieces.append('\n\n' if top - bottom > typical * .9 else '\n')
        pieces.append(text)
        bottom = max(p[1] for p in box)
    return ''.join(pieces)


def _ffmpeg_path():
    try:
        import imageio_ffmpeg
    except ImportError:
        raise RuntimeError('FFmpeg 缺失，无法合成音频') from None
    return imageio_ffmpeg.get_ffmpeg_exe()


def prepare_playback_audio(data, leading_ms=350, trailing_ms=450, normalize=False):
    """一次解码/拼接，输出带首尾静音的 MP3 和供本机播放的 PCM WAV。

    PCM 播放避开 MCI 对 MP3 时长/帧索引的兼容问题；不裁剪语音或改变音调。
    常规 <=20 段仅启动一次 FFmpeg，长文分组避免 Windows 命令行长度限制。
    """
    import os
    import tempfile
    from pathlib import Path
    parts = [bytes(data)] if isinstance(data, (bytes, bytearray, memoryview)) else [bytes(p) for p in data]
    if not parts or any(not part for part in parts):
        raise RuntimeError("音频为空或存在空片段，无法完整播放")
    ffmpeg = _ffmpeg_path()

    def inputs_and_graph(paths):
        args, filters = [], []
        for i, path in enumerate(paths):
            args.extend(["-i", path])
            filters.append(f"[{i}:a]aresample=24000,aformat=sample_fmts=fltp:channel_layouts=mono,asetpts=PTS-STARTPTS[a{i}]")
        labels = "".join(f"[a{i}]" for i in range(len(paths)))
        filters.append(f"{labels}concat=n={len(paths)}:v=0:a=1[joined]")
        return args, ";".join(filters)

    with tempfile.TemporaryDirectory() as tmp:
        paths = []
        for i, part in enumerate(parts):
            path = Path(tmp, f"source-{i}.bin")
            path.write_bytes(part)
            paths.append(str(path))
        if len(paths) > 20:
            groups = []
            for start in range(0, len(paths), 20):
                args, graph = inputs_and_graph(paths[start:start + 20])
                path = os.path.join(tmp, f"group-{start}.wav")
                _run(ffmpeg, ["-y", "-nostdin", "-v", "error", "-xerror"] + args +
                     ["-filter_complex", graph, "-map", "[joined]", "-c:a", "pcm_s16le", path])
                groups.append(path)
            listing = Path(tmp, "groups.txt")
            listing.write_text("".join(f"file '{os.path.basename(p)}'\n" for p in groups), encoding="utf-8")
            args = ["-f", "concat", "-safe", "0", "-i", str(listing)]
            graph = "[0:a]anull[joined]"
        else:
            args, graph = inputs_and_graph(paths)
        effect = 'loudnorm=I=-18:TP=-1.5:LRA=9,aresample=24000,' if normalize else ''
        graph += (f";[joined]{effect}adelay={int(leading_ms)}:all=1,"
                  f"apad=pad_dur={trailing_ms / 1000:.3f},asplit=2[export][play]")
        mp3, wav = Path(tmp, "out.mp3"), Path(tmp, "play.wav")
        _run(ffmpeg, ["-y", "-nostdin", "-v", "error", "-xerror"] + args +
             ["-filter_complex", graph, "-map", "[export]", "-c:a", "libmp3lame", "-b:a", "128k", str(mp3),
              "-map", "[play]", "-c:a", "pcm_s16le", str(wav)])
        return mp3.read_bytes(), wav.read_bytes()


def _run(ffmpeg, args):
    import subprocess
    try:
        p = subprocess.run([ffmpeg] + args, capture_output=True, timeout=600,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except FileNotFoundError:
        raise RuntimeError('FFmpeg 缺失，无法合成音频') from None
    except subprocess.TimeoutExpired:
        raise RuntimeError('ffmpeg 超时（超过10分钟）已中止，请检查输入音频是否异常') from None
    if p.returncode != 0:
        import re
        detail = re.sub(r'[A-Za-z]:\\[^"\s]*|\\\\[^"\s]*', '<path>',
                        p.stderr.decode(errors='ignore')[-400:])
        raise RuntimeError(f"ffmpeg 失败: {detail}")
