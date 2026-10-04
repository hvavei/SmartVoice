"""SmartVoice 两套主题调色板；主窗口与所有独立窗口共用，保证视觉一致。

暖白·初：按 AUDIT 描述还原最早黑字搭配（中性浅底 + 纯黑功能字 + 灰蓝描述）；
早年精确色值已不在仓库，此处为忠实重建。雾蓝：浅冷蓝。
"""

WARM = {
    "BG": "#f2f2f0",
    "PANEL": "#ffffff",
    "SEL": "#dbe9fa",
    "FG": "#000000",
    "MUTED": "#8a8a8a",
    "FEEDBACK": "#5b6b85",
    "ACCENT": "#1268c4",
    "ACCENT_D": "#0d4f96",
    "TROUGH": "#dcdcdc",
    "BORDER": "#d5d5d5",
    "GREEN": "#4e9b76",
    "GREY": "#a8a8a8",
    "HOVER": "#e2e2e2",
    "HEADING": "#e9e9e9",
    "TRACK": "#f7f7f7",
    "TOGGLE_ON": "#078a5e",
    "TOGGLE_OFF": "#dfe3e8",
    "TOGGLE_IDLE": "#9aa5b1",
    "OUTLINE": "#7d8b99",
    "WARN": "#a34100",
    "ROLE": ["#dbe9fa", "#e8f2fd", "#d5e7fa", "#eef4fb", "#e2eefa", "#d8ecfa"],
}

MIST = {
    "BG": "#e9eef4",
    "PANEL": "#ffffff",
    "SEL": "#d3e3f5",
    "FG": "#000000",
    "MUTED": "#7c8b9c",
    "FEEDBACK": "#47617a",
    "ACCENT": "#1f6fb8",
    "ACCENT_D": "#185a94",
    "TROUGH": "#d3dce6",
    "BORDER": "#c9d5e1",
    "GREEN": "#3f8f66",
    "GREY": "#9fb0c0",
    "HOVER": "#dce7f2",
    "HEADING": "#dfe8f2",
    "TRACK": "#f4f7fb",
    "TOGGLE_ON": "#1d8a56",
    "TOGGLE_OFF": "#d7e0ea",
    "TOGGLE_IDLE": "#93a3b5",
    "OUTLINE": "#6e8296",
    "WARN": "#a34100",
    "ROLE": ["#d3e3f5", "#e2eefb", "#c9def4", "#eaf1f9", "#d8e6f6", "#cfe2f6"],
}

THEMES = {"warm": WARM, "mist": MIST}
THEME_ORDER = ("warm", "mist")
THEME_LABELS = {"warm": "暖白·初", "mist": "雾蓝"}
DEFAULT_THEME = "warm"
ACTIVE = DEFAULT_THEME


def palette(name=None):
    """返回指定主题的调色板字典；缺省为当前生效主题。"""
    return THEMES.get(name or ACTIVE, THEMES[DEFAULT_THEME])


def apply(name, *namespaces):
    """切换当前主题并刷新 theme 模块属性；附带同步已 from-import 调色板的业务命名空间。"""
    key = name if name in THEMES else DEFAULT_THEME
    pal = THEMES[key]
    globals().update(pal)
    for ns in namespaces:
        if ns is None:
            continue
        target = ns if isinstance(ns, dict) else ns.__dict__
        for k, v in pal.items():
            if k in target:
                target[k] = v
    global ACTIVE
    ACTIVE = key
    return key


# 模块级名称恒为当前生效调色板的值；from theme import FG 的旧写法继续有效。
globals().update(THEMES[DEFAULT_THEME])
