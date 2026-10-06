"""文档导入：移除字幕时间标签；纯文本/PDF/SRT 把固定宽度软换行合并成段（空行=段落边界）。"""
from pathlib import Path
import re

MAX_TEXT_BYTES = 32 * 1024**2  # 单文件上限：超限请拆分，避免一次性读入吃光内存

_SENT_END = '。！？!?…'
_MARKER = re.compile(r'^(?:\[[^\]]+\]|[^:：\s]{1,12}[:：]|#{1,6}\s|[-*•]\s|\d+[.)、]\s)')
_NEWLINES = re.compile(r'\r\n|[\r\u2028\u2029\x85\x0b\x0c]')
_SRT_TAG = re.compile(r'</?(?:font|b|i|u|s|ruby|rt|v)\b[^>]*>', re.I)
_SRT_OVERRIDE = re.compile(r'\{[^{}]*}')  # ASS 定位符 {\an8} 等：花括号块整段删
_TS_LINE = re.compile(r'^\s*(?:\d{1,2}:)?\d{1,2}:\d{2}[,\.]\d+\s*-->\s*(?:\d{1,2}:)?\d{1,2}:\d{2}')
_LRC_TAG = re.compile(r'\[(?:\d+:\d+(?::\d+(?:\.\d+)?)?(?:\.\d+)?|(?:ar|ti|al|by|offset|length|re|ve):[^\]]*)\]', re.I)


def _normalize_newlines(text):
    # \n / \r\n / \r / LS / PS / NEL / VT / FF 统一成 \n，split('\n') 才能全部分段
    return _NEWLINES.sub('\n', text)


def _utf16_no_bom_endian(sample):
    """无 BOM 的 UTF-16 检测：NUL 落在同一奇偶位（LE=奇数位，BE=偶数位）。"""
    nul_even = sample[::2].count(0)
    nul_odd = sample[1::2].count(0)
    total = nul_even + nul_odd
    # ≥8 个 NUL、占比 ≥5%、且 ≥70% 集中在同一侧，才判为 UTF-16，避免误伤其它编码
    if total >= 8 and total * 20 >= len(sample) and max(nul_even, nul_odd) * 10 >= total * 7:
        return 'utf-16-le' if nul_odd >= nul_even else 'utf-16-be'
    return None


def _utf16_cjk_guess(sample):
    """无 NUL、无 BOM、非 UTF-8 时的最后尝试：纯中文 UTF-16LE/BE 字节流无零字节，
    会被误判成 gb18030 解出乱码。样本 <64 字节或 CJK 占比不足 50% 不认：
    随机二进制恰好撞上 CJK 区（U+3400–U+9FFF 约占 32%），短样本不可信。"""
    if len(sample) < 64:
        return None
    best = None
    for encoding in ('utf-16-le', 'utf-16-be'):
        try:
            decoded = sample.decode(encoding)
        except UnicodeDecodeError:
            continue
        if not decoded or '\x00' in decoded:
            continue
        ratio = sum(1 for ch in decoded if '\u3400' <= ch <= '\u9fff') / len(decoded)
        if ratio > 0.5 and (best is None or ratio > best[1]):
            best = (encoding, ratio)
    return best[0] if best else None


def read_text(path):
    import storage
    data = Path(storage.long_path(path)).read_bytes()
    if len(data) > MAX_TEXT_BYTES:
        raise ValueError('文件过大（超过32MB），请拆分后导入')
    if data.startswith((b'\xff\xfe', b'\xfe\xff')):
        encodings = ('utf-16',)          # 带 BOM：解码器自行识别端序
    else:
        endian = _utf16_no_bom_endian(data[:4096])
        if endian:
            encodings = (endian,)
        else:
            # 先试严格 UTF-8，失败再试中文 UTF-16，最后才回退 gb18030（它几乎不报错，
            # 放前面会把 UTF-16 中文吞成乱码）。
            try:
                decoded = data.decode('utf-8-sig')
                if '\x00' not in decoded:
                    return _normalize_newlines(decoded)
            except UnicodeDecodeError:
                pass
            guess = _utf16_cjk_guess(data[:1 << 20])
            encodings = (guess,) if guess else ('gb18030',)
    for encoding in encodings:
        try:
            decoded = data.decode(encoding)
            if '\x00' in decoded:
                continue
            return _normalize_newlines(decoded)
        except UnicodeDecodeError:
            pass
    raise ValueError('无法识别文本编码，请另存为 UTF-8 后导入')


def reflow_text(text):
    """合并段内软换行，让段落宽度跟随文本框：
    - 空行分段原样保留；缩进行、纯数字行（字幕序号）不参与合并；
    - `[标记]`/`名字:`/列表/标题行另起段，避免吃掉上一段；
    - 段内行以句末标点收尾即成段；拼接时 ASCII 词边界补空格，其余直接相连。
    """
    if not text:
        return text
    out, para = [], []

    def flush():
        merged = []
        for piece in para:
            if not merged:
                merged = [piece]
            elif (merged[-1][-1:].isascii() and (merged[-1][-1:].isalnum() or merged[-1][-1:] in ',;:')
                  and piece[:1].isascii() and piece[:1].isalnum()):
                merged.append(' ' + piece)
            else:
                merged.append(piece)
        if merged:
            out.append(''.join(merged))
        para.clear()

    for raw in text.split('\n'):
        stripped = raw.strip()
        if not stripped:
            flush()
            out.append('')
        elif raw[:1].isspace() or stripped.isdigit():
            flush()
            out.append(raw)
        else:
            if _MARKER.match(stripped):
                flush()
            para.append(stripped)
            core = stripped.rstrip('"\'”’』」）)】')
            if core and core[-1] in _SENT_END:
                flush()
    flush()
    return '\n'.join(out)


def read_document(path):
    extension = Path(path).suffix.lower()
    if extension == '.pdf':
        import docutils
        text = _normalize_newlines(docutils.extract_pdf_text(path, ocr=True))
        if len(text) > MAX_TEXT_BYTES:
            raise ValueError('文档过大（超过32MB），请拆分后导入')
        return reflow_text(text)
    if extension == '.docx':
        import zipfile
        import xml.etree.ElementTree as ET
        import storage
        try:
            with zipfile.ZipFile(storage.long_path(path)) as z:
                try:
                    info = z.getinfo('word/document.xml')
                except KeyError:
                    raise ValueError('DOCX 缺少正文，请另存后导入')
                if info.file_size > MAX_TEXT_BYTES:
                    raise ValueError('文档过大（超过32MB），请拆分后导入')
                data = z.read('word/document.xml')
                if len(data) > MAX_TEXT_BYTES:
                    raise ValueError('文档过大（超过32MB），请拆分后导入')
        except zipfile.BadZipFile as e:
            raise ValueError('DOCX 已损坏，请另存后导入') from e
        try:
            root = ET.fromstring(data)
        except ET.ParseError as e:
            raise ValueError('DOCX 已损坏，请另存后导入') from e
        ns = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
        if not list(root.iter(ns + 'p')):
            strict = '{http://purl.oclc.org/ooxml/wordprocessingml/main}'
            if list(root.iter(strict + 'p')):
                ns = strict
        paragraphs = []

        def _para_text(element):
            # 修订删除 w:del 子树整体跳过（旧逻辑 iter 全后代，删除线文字被朗读）；
            # 修订插入 w:ins 是现行正文，保留。
            if element.tag == ns + 'del':
                return []
            if element.tag == ns + 't':
                return [element.text or '']
            if element.tag == ns + 'tab':
                return ['\t']
            if element.tag in (ns + 'br', ns + 'cr'):
                return ['\n']
            parts = []
            for child in element:
                parts.extend(_para_text(child))
            return parts

        for p in root.iter(ns + 'p'):
            paragraphs.append(''.join(_para_text(p)))
        return _normalize_newlines('\n'.join(paragraphs))
    text = read_text(path)
    if extension in ('.srt', '.vtt'):
        blocks = re.split(r'\n[ \t]*\n', text)
        output = []
        for block in blocks:
            lines = block.split('\n')
            if lines and lines[0].strip().isdigit() and len(lines) > 1 and '-->' in lines[1]:
                lines.pop(0)
            kept = []
            for line in lines:
                stripped = line.strip()
                # WebVTT 头与注释块不进台词。
                if extension == '.vtt' and (stripped == 'WEBVTT' or stripped.startswith('NOTE')):
                    continue
                if _TS_LINE.search(line):
                    continue
                kept.append(_SRT_TAG.sub('', _SRT_OVERRIDE.sub('', line)))
            output.append('\n'.join(kept))
        return reflow_text('\n\n'.join(output))
    if extension in ('.ass', '.ssa'):
        # 只取 Dialogue 行最后一个字段（台词），{\...} 定位符与 <...> 标签剥掉。
        out = []
        for line in text.split('\n'):
            if not line.strip().lower().startswith('dialogue:'):
                continue
            parts = line.split(',', 9)
            payload = parts[9] if len(parts) > 9 else ''
            out.append(_SRT_TAG.sub('', _SRT_OVERRIDE.sub('', payload)).strip())
        return reflow_text('\n'.join(out))
    if extension == '.lrc':
        out = []
        for line in text.split('\n'):
            line = re.sub(r'\]\s*\[', '] [', line)  # 同行多标签先隔开，避免粘连
            line = re.sub(r'(?<=\S)(?=\[)', ' ', line)  # 文本与标签相邻同样隔开
            line = _LRC_TAG.sub('', line)
            line = re.sub(r'<\d+:\d+(?:\.\d+)?>', ' ', line)  # 词级时间戳：空格代替
            out.append(line.strip())
        return '\n'.join(out)
    # JSON/CSV 保持原始行/空行，避免擅自将结构化数据改写为台词。
    if extension in ('.json', '.csv'):
        return text
    # TXT/Markdown/未知扩展名：合并源文件固定宽度软换行，段落随文本框宽度重排。
    return reflow_text(text)
