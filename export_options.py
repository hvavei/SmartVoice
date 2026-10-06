"""导出参数纯校验：严格版抛错供入口明确报错，宽容版钳位回退永不抛。无 Tk 依赖。"""

FORMATS = ('mp3', 'wav')
DEFAULT_EXPORT_OPTIONS = {'directory': '', 'name': '配音', 'format': 'mp3',
                          'leading_ms': 350, 'trailing_ms': 450, 'normalize': False}


def _to_int(value, default, lo=None, hi=None):
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    if lo is not None and v < lo:
        v = lo
    if hi is not None and v > hi:
        v = hi
    return v


def _to_bool(value):
    if isinstance(value, str):
        return value.strip().lower() in ('1', 'true', 'yes', 'on')
    return bool(value)


def _to_str(value, default):
    return default if value is None else str(value)


def validate_export_options(directory, name, fmt, leading_ms, trailing_ms, normalize):
    """严格版：类型/范围非法抛 ValueError，错误文案与旧内联逻辑一致。"""
    try:
        leading_ms = int(leading_ms)
        trailing_ms = int(trailing_ms)
    except (TypeError, ValueError):
        raise ValueError('首尾留白范围为0～5000毫秒')
    options = {'directory': str(directory or '').strip(), 'name': name, 'format': str(fmt).lower(),
               'leading_ms': leading_ms, 'trailing_ms': trailing_ms,
               'normalize': _to_bool(normalize)}
    if options['format'] not in FORMATS or not options['directory']:
        raise ValueError('请设置有效输出目录和 MP3/WAV 格式')
    if not all(0 <= options[k] <= 5000 for k in ('leading_ms', 'trailing_ms')):
        raise ValueError('首尾留白范围为0～5000毫秒')
    return options


def coerce_export_options(options):
    """宽容版：坏值钳位/回退，永不抛异常，供保存配置与项目比对使用。"""
    if not isinstance(options, dict):
        options = {}
    fmt = _to_str(options.get('format', 'mp3'), 'mp3').lower()
    return {'directory': _to_str(options.get('directory', ''), ''),
            'name': _to_str(options.get('name', '配音'), '配音'),
            'format': fmt if fmt in FORMATS else 'mp3',
            'leading_ms': _to_int(options.get('leading_ms', 350), 350, 0, 5000),
            'trailing_ms': _to_int(options.get('trailing_ms', 450), 450, 0, 5000),
            'normalize': _to_bool(options.get('normalize', False))}
