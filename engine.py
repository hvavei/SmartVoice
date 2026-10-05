"""朗读引擎层: Azure REST + Edge免费 + OpenAI兼容 + 火山引擎, 带断线重连."""
import json
import os
import re
import sys
import threading
import uuid
import tempfile

from functools import lru_cache
import appmeta
import storage
import voice_tasks as workflow

ENGINE_CHOICES = ["Azure(填Key)", "Edge免费(免Key)", "OpenAI兼容(填Key)", "火山引擎(填Token)"]


def kind_of(engine_name):
    """引擎归类: azure / edge / openai / volc；未知值回Azure。"""
    return storage.engine_kind(engine_name)


_SESS = None
_THREAD_SESS = threading.local()


def _sess():
    """每个合成线程复用连接，避免并发共享 requests.Session 的可变状态。"""
    import requests
    if _SESS is not None:  # 离线冒烟替身，避免访问真实网络。
        return _SESS
    if not hasattr(_THREAD_SESS, "session"):
        from requests.adapters import HTTPAdapter
        s = requests.Session()
        adapter = HTTPAdapter(pool_connections=4, pool_maxsize=4, max_retries=0)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        s.headers.update({"User-Agent": f"{appmeta.NAME}/{appmeta.VERSION}"})
        _THREAD_SESS.session = s
    return workflow.Session(_THREAD_SESS.session)


class SynthesisCancelled(workflow.Cancelled):
    """任务已被用户停止或替换，不应继续重试旧请求。"""


def _app_dir():
    # 打包成exe后, 配置/输出要放在exe所在目录, 不能放临时解压目录
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


PROGRAM_DIR = _app_dir()
APP_DIR = str(storage.CACHE_DIR)
CONFIG_FILE = str(storage.SETTINGS_FILE)
EDGE_CACHE = os.path.join(APP_DIR, "voices_cache_edge.json")
OPENAI_CACHE = os.path.join(APP_DIR, "voices_cache_openai.json")
VOLC_CACHE = os.path.join(APP_DIR, "voices_cache_volc.json")
DEFAULT_OPENAI_SPEECH = "https://api.openai.com/v1/audio/speech"
DEFAULT_VOLC_TTS = "https://openspeech.bytedance.com/api/v1/tts"
OPENAI_MODEL_DEFAULT = "tts-1"
AZURE_OUTPUT_FORMAT = "audio-24khz-96kbitrate-mono-mp3"


BUILTIN_OPENAI_VOICES = {
    "Alloy女 alloy": "alloy",
    "Ash男 ash": "ash",
    "Ballad男 ballad": "ballad",
    "Coral女 coral": "coral",
    "Echo男 echo": "echo",
    "Fable中 fable": "fable",
    "Nova女 nova": "nova",
    "Onyx男 onyx": "onyx",
    "Sage女 sage": "sage",
    "Shimmer女 shimmer": "shimmer",
    "Verse男 verse": "verse",
}

BUILTIN_VOLC_VOICES = {
    "通用女 BV001": "BV001_streaming",
    "通用男 BV002": "BV002_streaming",
    "直播女 BV007": "BV007_streaming",
    "直播男 BV102": "BV102_streaming",
    "亲切女 BV056": "BV056_streaming",
    "磁性男 BV119": "BV119_streaming",
    "童声 BV051": "BV051_streaming",
    "播音女 BV701": "BV701_streaming",
}

# v1.9: 云南直连 eastasia, 不再横跨大西洋; 所有默认/纠错都以 eastasia 为准
DEFAULT_REGION = "eastasia"

BUILTIN_VOICES = {
    "云希男声": "zh-CN-YunxiNeural",
    "云健男声": "zh-CN-YunjianNeural",
    "云扬男声": "zh-CN-YunyangNeural",
    "云夏男声": "zh-CN-YunxiaNeural",
    "云枫男声": "zh-CN-YunfengNeural",
    "云皓男声": "zh-CN-YunhaoNeural",
    "晓晓女声": "zh-CN-XiaoxiaoNeural",
    "晓伊女声": "zh-CN-XiaoyiNeural",
    "晓辰女声": "zh-CN-XiaochenNeural",
    "晓涵女声": "zh-CN-XiaohanNeural",
    "晓墨女声": "zh-CN-XiaomoNeural",
    "晓睿女声": "zh-CN-XiaoruiNeural",
    "晓双女声": "zh-CN-XiaoshuangNeural",
    "晓颜女声": "zh-CN-XiaoyanNeural",
    "港-晓曼女声": "zh-HK-HiuMaanNeural",
    "港-云龙男声": "zh-HK-WanLungNeural",
    "港-晓佳女声": "zh-HK-HiuGaaiNeural",
    "台-晓臻女声": "zh-TW-HsiaoChenNeural",
    "台-云哲男声": "zh-TW-YunJheNeural",
    "英-Jenny女声": "en-US-JennyNeural",
    "英-Guy男声": "en-US-GuyNeural",
    "英-Aria女声": "en-US-AriaNeural",
    "英-Davis男声": "en-US-DavisNeural",
    "英-Sonia女声": "en-GB-SoniaNeural",
    "英-Ryan男声": "en-GB-RyanNeural",
    "日-Nanami女声": "ja-JP-NanamiNeural",
    "日-Keita男声": "ja-JP-KeitaNeural",
    "日-Aoi女声": "ja-JP-AoiNeural",
    "日-Daichi男声": "ja-JP-DaichiNeural",
}


def load_json(path, default):
    try:
        is_config = os.path.abspath(path) == os.path.abspath(CONFIG_FILE)
        if is_config:
            storage.migrate_legacy(PROGRAM_DIR)
        # utf-8-sig：无 BOM 行为同 utf-8，带 BOM 时自动剥离，避免首个键名带 \ufeff
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        return storage.unlock_settings(data) if is_config else data
    except json.JSONDecodeError:
        # 损坏的JSON：留.bak备份供排查，再回退默认值，避免无声吞掉现场。
        try:
            os.replace(path, path + ".bak")
        except OSError:
            pass
        return default
    except Exception:
        return default


def save_json(path, obj):
    """同目录临时文件 + 原子替换，写入失败时保留上一份完整配置。"""
    path = os.path.abspath(path)
    if path == os.path.abspath(CONFIG_FILE):
        obj = storage.protect_settings(obj)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                         dir=os.path.dirname(path), delete=False) as f:
            tmp = f.name
            json.dump(obj, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if tmp and os.path.exists(tmp):
            os.remove(tmp)


def get_voices(region=None):
    # 无区域来源的旧通用缓存不再自动混入另一个区域的人声。
    cached = load_json(voice_cache_path(region), {})
    if isinstance(cached, dict) and cached and all(
            isinstance(k, str) and k and isinstance(v, str) and v for k, v in cached.items()):
        return cached
    return dict(BUILTIN_VOICES)


def voice_cache_path(region):
    rg = normalize_region(region)
    return os.path.join(APP_DIR, f"voices_cache_{rg}.json")


@lru_cache(maxsize=8)
def azure_voice_metadata(region):
    rg = normalize_region(region)
    path = os.path.join(APP_DIR, f"voices_metadata_{rg}.json")
    data = load_json(path, {})
    if not isinstance(data, dict) or data.get('region') != rg:
        return {}
    voices = data.get('voices', {})
    return voices if isinstance(voices, dict) else {}


def voice_capabilities(voice_id, region=DEFAULT_REGION):
    """类型≠能力。风格/角色来自接口，SSML依据独立的模型族策略。

    未知模型族保守降级到原生文本，绝不因为 VoiceType=Neural 就注入 phoneme。
    """
    meta = azure_voice_metadata(region).get(voice_id, {})
    family = voice_id.partition(':')[2]
    classic = (not family and (meta.get('VoiceType') == 'Neural' or voice_id in BUILTIN_VOICES.values()))
    return {'type': meta.get('VoiceType', '未知'), 'status': meta.get('Status', '未知'),
            'styles': [v for v in (meta.get('StyleList') or []) if isinstance(v, str)],
            'roles': [v for v in (meta.get('RolePlayList') or []) if isinstance(v, str)],
            'rate': classic, 'pitch': classic, 'volume': classic,
            'phoneme': classic and lang_of(voice_id) == 'zh-CN',
            'source': 'Azure列表+模型族策略' if meta else '内置模型族策略'}


def validate_voice_parameters(voice_id, snap):
    if kind_of(snap.get('engine')) != 'azure':
        return
    caps = voice_capabilities(voice_id, snap.get('region', DEFAULT_REGION))
    errors = []
    if snap.get('style', '默认') != '默认' and snap['style'] not in caps['styles']:
        errors.append('风格 ' + snap['style'])
    if snap.get('role', '默认') != '默认' and snap['role'] not in caps['roles']:
        errors.append('角色扮演 ' + snap['role'])
    for name, default in (('rate', '100%'), ('pitch', '+0Hz'), ('vol', '+0%')):
        enabled = caps['volume' if name == 'vol' else name]
        value = snap.get(name, default)
        changed = _signed_pct(value) != '+0%' if name == 'rate' else value != default
        if changed and not enabled:
            errors.append({'rate': '语速', 'pitch': '音高', 'vol': '音量'}[name])
    if errors:
        raise ValueError(f"{voice_id} 不支持或尚未确认支持：{'、'.join(errors)}。请复位这些参数或刷新人声能力。")


def normalize_region(region):
    """' EastAsia '/'东亚'等写法统一成 'eastasia'; 空则回默认."""
    t = (region or "").strip().lower().replace(" ", "").replace("_", "").replace("-", "")
    alias = {"dongya": "eastasia", "yazhou": "eastasia",
             "dongnanya": "southeastasia"}
    t = alias.get(t, t)
    return t or DEFAULT_REGION


_REGEX_ENDPOINT_REGION = re.compile(r"https?://([a-z0-9]+)\.(tts\.speech|api\.cognitive)", re.IGNORECASE)
_REGEX_SIGNED_PCT = re.compile(r"^([+-]?)(\d+)%$")
EDGE_CHUNK_TIMEOUT = 120  # 流中途单块读取上限(秒)：120 秒无任何分片即判定卡死，重试；长文靠持续来块不受影响


async def _stream_chunks(stream, timeout):
    """逐块限时取流：卡死的连接按块超时抛出 TimeoutError，走外层重试；正常流不受影响。"""
    import asyncio
    it = stream.__aiter__()
    try:
        while True:
            try:
                chunk = await asyncio.wait_for(it.__anext__(), timeout)
            except StopAsyncIteration:
                return
            yield chunk
    finally:
        aclose = getattr(it, 'aclose', None)
        if aclose is not None:
            try:
                await aclose()
            except Exception:
                pass


def region_of_endpoint(endpoint):
    """从终结点反解region, 如 https://eastasia.tts.speech... -> eastasia."""
    m = _REGEX_ENDPOINT_REGION.search((endpoint or "").strip().lower())
    return m.group(1) if m else ""


def default_tts_endpoint(region):
    return f"https://{normalize_region(region)}.tts.speech.microsoft.com/cognitiveservices/v1"


def fix_endpoint(endpoint, region):
    """Portal通用地址/Token地址/列表地址 -> 自动换算成合成地址.

    v1.9根治401: 终结点host里的region必须与Region栏一致, 不一致直接按Region重建.
    (截图里Region=eastus配eastasia Key, 合成打到错误机房回401空包, 就是这个坑)
    """
    rg = normalize_region(region)
    ep = (endpoint or "").strip()
    if "/cognitiveservices/v1" not in ep or "tts.speech" not in ep:
        return default_tts_endpoint(rg)
    host_rg = region_of_endpoint(ep)
    if host_rg and host_rg != rg:
        return default_tts_endpoint(rg)
    return ep


def lang_of(voice_id):
    p = voice_id.split("-")
    return "-".join(p[:2]) if len(p) >= 2 else "zh-CN"


def xml_escape(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;").replace("'", "&apos;"))


STYLES = ["默认", "cheerful", "excited", "friendly", "hopeful", "serious",
          "calm", "gentle", "lyrical", "sad", "angry", "fearful",
          "disgruntled", "embarrassed", "depressed", "empathetic",
          "narration-professional", "newscast", "customerservice", "chat",
          "advertisement_upbeat", "sports_commentary", "documentary-narration"]
ROLES = ["默认", "Girl", "Boy", "YoungAdultFemale", "YoungAdultMale",
         "OlderAdultFemale", "OlderAdultMale", "SeniorFemale", "SeniorMale"]
DEGREES = ["50%", "100%", "150%", "200%"]


def auth_hint(status, endpoint, region):
    host = region_of_endpoint(endpoint) or "?"
    rg = normalize_region(region)
    return (f"HTTP {status} endpoint={endpoint} "
            f"(终结点机房={host}, Region栏={rg}): "
            f"Key与机房不匹配? eastasia的Key必须配Region=eastasia, "
            f"把界面Region改成eastasia再点刷新/试听")


def g2p_phoneme_annotator(text):
    """G2PW 上下文多音字消歧，输出已转义的 zh-CN SSML。

    用 onnxruntime 跑 G2PW 模型，对整句多音字按语境给拼音并注入
    <phoneme alphabet='sapi'>；非多音字保留原文。模型资产缺失或加载失败
    时静默回退为纯转义，绝不让合成中断。
    """
    try:
        import components
        if not components.available('g2pw'):
            return xml_escape(text)
        import polyphone
        result, annot = polyphone.annotate_sapi(text)
        if result != text:
            return xml_escape(text)  # 标注器只允许提供偏移，不能改写/删掉原稿。
        annot = {pos: phone for pos, phone in annot.items()
                 if isinstance(pos, int) and 0 <= pos < len(text)
                 and '\u3400' <= text[pos] <= '\u9fff'
                 and isinstance(phone, str) and re.fullmatch(r"[a-z]+ [1-5]", phone)}
        if annot:
            out, i = [], 0
            # 相邻注音字合并为一个短语，减少逐字标签边界；标点不跨越。
            positions = sorted(annot)
            n = 0
            while n < len(positions):
                start = positions[n]
                end = start + 1
                phones = [annot[start]]
                n += 1
                while n < len(positions) and positions[n] == end:
                    phones.append(annot[end])
                    end += 1
                    n += 1
                out.append(xml_escape(result[i:start]))
                out.append(f"<phoneme alphabet='sapi' ph='{' '.join(phones)}'>{xml_escape(result[start:end])}</phoneme>")
                i = end
            out.append(xml_escape(result[i:]))
            return "".join(out)
    except Exception:
        pass
    return xml_escape(text)


_QUOTE_PATTERN = re.compile(r'(“[^“”]*”|「[^「」]*」|『[^『』]*』|"[^"\n]*")')


def format_dialogue_lines(raw_text, slot_names=None):
    """机械分行排版（0猜测）：
    - 引号内对话 -> 独立成行，连同引号原样保留，不加前缀（默认人声，指定角色请标 [角色名]）
    - 引号外叙述 -> 独立成行；slot_names 给定时前置第一个启用角色名，否则不加前缀
    - `[角色]` 行、前缀属于槽位的 `角色名:台词` 原样保留，可反复执行。
    slot_names=None 视为默认旁白，保证裸调用幂等；GUI 传启用槽位名（可能为空=不加前缀）。
    """
    names = ['旁白'] if slot_names is None else [n for n in slot_names if n]
    prefix = f"{names[0]}:" if names else ""
    lines = []
    text = raw_text or ""

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue

        # 已有明确格式：[角色]台词，或前缀命中槽位名/合法数字槽位(非时间格式)的 行
        if re.match(r"^\[.+?\]", line):
            lines.append(line)
            continue
        m = re.match(r"^([^:：\s]{1,12})[:：]", line)
        if m and _role_prefix_hit(m.group(1), line[m.end():], names):
            lines.append(line)
            continue

        parts = _QUOTE_PATTERN.split(line)
        if len(parts) <= 1:
            lines.append(prefix + line)
            continue

        for part in parts:
            p = part.strip()
            if not p:
                continue
            if _QUOTE_PATTERN.fullmatch(p):
                # 对话保留引号原样：不发明标签，交给默认人声或用户显式 [角色名]
                lines.append(p)
            else:
                lines.append(prefix + p)

    return "\n".join(lines)


def _role_prefix_hit(name, body, names):
    """`名字:` 前缀是否是已知角色：名字属于槽位，或 1..N 数字槽位且正文不像时间。"""
    if name in names:
        return True
    rest = body.strip()
    return bool(re.fullmatch(r"\d{1,2}", name) and names
                and 1 <= int(name) <= len(names) and not rest[:1].isdigit())


def _build_ssml(voice_id, text, rate, pitch, volume,
                style="默认", styledegree="100%", role="默认", annotate=True, capabilities=None):
    """保留标点与用户风格；首尾缓冲在最终音频上统一添加。"""
    # 先转义 XML 特殊字符，再多音字 phoneme 安全包裹
    caps = capabilities if capabilities is not None else voice_capabilities(voice_id)
    natural_text = g2p_phoneme_annotator(text) if annotate and caps['phoneme'] else xml_escape(text)
    # UI 无符号百分比是基准倍速；Azure SSML 百分比则是相对增减。
    # 100% -> +0%，80% -> -20%，显式 +20% 保持原义。
    azure_rate = _signed_pct(rate)
    attrs = []
    for name, value, default in (("rate", azure_rate, "+0%"), ("pitch", pitch, "+0Hz"),
                                  ("volume", volume, "+0%")):
        if value != default:
            attrs.append(f"{name}='{xml_escape(value)}'")
    inner = f"<prosody {' '.join(attrs)}>{natural_text}</prosody>" if attrs else natural_text
    if style != "默认" or role != "默认":
        attrs = ""
        if style != "默认":
            try:
                deg = float(styledegree.strip().strip("%")) / 100.0
            except Exception:
                deg = 1.0
            deg = min(2.0, max(0.01, deg))
            attrs += f" style='{xml_escape(style)}' styledegree='{deg:g}'"
        if role != "默认":
            attrs += f" role='{xml_escape(role)}'"
        inner = f"<mstts:express-as{attrs}>{inner}</mstts:express-as>"

    return (f"<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' xml:lang='{xml_escape(lang_of(voice_id))}'"
            f" xmlns:mstts='http://www.w3.org/2001/mstts'>"
            f"<voice name='{xml_escape(voice_id)}'>{inner}</voice></speak>")


def synth_azure(text, voice_id, key, endpoint, rate="+0%", pitch="+0Hz",
                volume="+0%", style="默认", styledegree="100%", role="默认",
                attempts=3, region="", on_progress=None, on_stage=None, annotate=True):
    import requests
    endpoint = fix_endpoint(endpoint, region or region_of_endpoint(endpoint))
    text = (text or "").strip()
    if not text:
        raise RuntimeError("文本为空")
    caps = voice_capabilities(voice_id, region or DEFAULT_REGION)
    annotate = annotate and caps['phoneme']
    if annotate:
        import components
        if not components.available('g2pw'):
            annotate = False
            if on_stage:
                on_stage('G2PW未安装，使用Azure原生发音')
    if on_stage:
        on_stage("解析文本与多音字" if annotate else "准备原生发音文本")
    ssml = _build_ssml(voice_id, text, rate, pitch, volume,
                       style, styledegree, role, annotate=annotate, capabilities=caps)
    headers = {"Ocp-Apim-Subscription-Key": key,
               "Content-Type": "application/ssml+xml",
               "X-Microsoft-OutputFormat": AZURE_OUTPUT_FORMAT}
    last_err = ""
    for i in range(1, attempts + 1):
        if on_stage:
            on_stage(f"等待 Azure 合成 · 请求 {i}/{attempts}")
        try:
            r = _sess().post(endpoint, headers=headers,
                             data=ssml.encode("utf-8"), timeout=(10, 60), stream=True)
        except requests.exceptions.ConnectTimeout:
            last_err = f"连接超时(第{i}/{attempts}次)"
        except requests.exceptions.ReadTimeout:
            last_err = f"读取超时(第{i}/{attempts}次)"
        except requests.exceptions.ConnectionError as e:
            last_err = f"连接中断(第{i}/{attempts}次): {e}"
        except workflow.Cancelled:
            raise
        except Exception as e:
            last_err = f"请求异常(第{i}/{attempts}次): {e}"
        else:
            try:
                ctype = r.headers.get("Content-Type", "").lower()
                if r.status_code == 200:
                    if not ("audio" in ctype or "octet-stream" in ctype):
                        raise RuntimeError(f"Azure返回非音频响应: {ctype or '<缺少类型>'}")
                    if on_stage:
                        on_stage("接收音频")
                    data = _read_response_bytes(r, on_progress)
                    if data:
                        return data
                    last_err = f"空音频(第{i}/{attempts}次)"
                elif r.status_code in (400, 401, 403):
                    detail = r.text[:300] if r.text else "<空包>"
                    raise RuntimeError(f"HTTP {r.status_code} voice={voice_id}: {detail}。"
                                       + auth_hint(r.status_code, endpoint, region))
                else:
                    last_err = f"HTTP {r.status_code}(第{i}/{attempts}次)"
            except requests.exceptions.RequestException as e:
                last_err = f"音频接收中断(第{i}/{attempts}次): {e}"
            finally:
                r.close()
        if i < attempts:
            if on_stage:
                on_stage(f"等待重试 · {i}秒")
            workflow.retry_wait(i)
    raise RuntimeError(last_err + "。已自动重连, 不行切Edge免费引擎先顶上")


def refresh_voices_azure(key, region, endpoint):
    import requests
    from datetime import datetime, timezone
    rg = normalize_region(region)
    url = f"https://{rg}.tts.speech.microsoft.com/cognitiveservices/voices/list"
    last_err = ""
    data = None
    for i in range(1, 4):
        try:
            r = _sess().get(url, headers={"Ocp-Apim-Subscription-Key": key}, timeout=(8, 30))
            try:
                r.encoding = "utf-8"
                if r.status_code == 200:
                    data = r.json()
                    break
                if r.status_code in (401, 403):
                    raise RuntimeError(auth_hint(r.status_code, url, rg))
                last_err = f"人声列表 HTTP {r.status_code} (第{i}/3次)"
                if r.status_code not in (408, 429, 500, 502, 503, 504):
                    break
            finally:
                r.close()
        except (requests.RequestException, ValueError) as e:
            last_err = f"拉取人声失败(第{i}/3次): {type(e).__name__}"
        if i < 3:
            workflow.retry_wait(i)
    if data is None:
        raise RuntimeError(last_err)
    if not isinstance(data, list):
        raise RuntimeError("Azure 人声列表响应结构无效，已保留原缓存")
    table, metadata = {}, {}
    for v in data:
        if not isinstance(v, dict):
            continue
        short = v.get("ShortName", "")
        if not isinstance(short, str) or not short:
            continue
        local = v.get("LocalName", "") or v.get("DisplayName", "")
        gender = v.get("Gender", "")
        g = "女" if gender == "Female" else ("男" if gender == "Male" else gender)
        table[f"{local}{g} {short}" if local else short] = short
        metadata[short] = {name: v.get(name) for name in (
            'VoiceType', 'Status', 'Locale', 'SampleRateHertz', 'StyleList', 'RolePlayList')}
    if not table:
        raise RuntimeError("Azure 返回空人声列表，已保留原缓存")
    table = dict(sorted(table.items(), key=lambda kv: (kv[1][:2] != "zh", kv[1])))
    azure_voice_metadata.cache_clear()
    save_json(voice_cache_path(rg), table)
    save_json(os.path.join(APP_DIR, f"voices_metadata_{rg}.json"), {
        'source': url, 'region': rg, 'retrieved_at': datetime.now(timezone.utc).isoformat(), 'voices': metadata})
    # 写盘前后双清：期间任何并发读缓存的调用方都不会把旧元数据再缓存回去
    azure_voice_metadata.cache_clear()
    return table


# ---------------- 多人配音算法(纯函数, 可离线单测) ----------------
MAX_SYNTH_CHARS = 600


def split_synthesis_text(text, max_chars=MAX_SYNTH_CHARS):
    """优先按句末切块，保留全部字符；超长无标点行按上限切分。"""
    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    pos, total = 0, len(text)
    while total - pos > max_chars:
        end = 0
        for marks in ("。！？!?；;\n", "，,、 \t"):
            candidates = [text.rfind(mark, pos + max_chars // 2, pos + max_chars) for mark in marks]
            if max(candidates) >= 0:
                end = max(candidates) + 1
                break
        end = end or pos + max_chars
        yield text[pos:end]
        pos = end
    if pos < total:
        yield text[pos:]


def plan_azure_jobs(jobs):
    return [(voice, role, part) for voice, role, text in merge_dub_jobs(jobs, max_chars=MAX_SYNTH_CHARS)
            for part in split_synthesis_text(text) if part.strip()]


def merge_dub_jobs(jobs, max_chars=1800):
    """合并连续同角色、同人声的短行，保留换行与角色边界。

    限制合并长度，避免把长篇文稿合为一次超长云端请求。
    已有的超长单行由调用方保持原样。
    """
    merged = []
    for voice, role, body in jobs:
        if (merged and merged[-1][:2] == (voice, role)
                and len(merged[-1][2]) + 1 + len(body) <= max_chars):
            merged[-1] = (voice, role, merged[-1][2] + "\n" + body)
        else:
            merged.append((voice, role, body))
    return merged


def parse_dub_script(text, slot_names):
    """配音脚本解析: `角色名:台词` / `角色名：台词` / `[角色名]台词` / `N:台词`.

    slot_names: 启用的槽位名列表(顺序即槽位号1..N).
    前缀必须命中槽位名或 1..N 数字槽位(且正文不像时间)，否则整行按无标记原文
    交给默认人声——`他说：`/`12:30`/URL 等冒号行不再被误认成角色。
    返回 [(slot_index|None, 台词)]: None表示无标记, 调用方用默认人声.
    """
    segs = []
    names = list(slot_names or [])
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        idx, body = None, line
        m = re.match(r"^\[(.+?)\]\s*(.+)$", line)
        if m:
            cand, rest = m.group(1).strip(), m.group(2).strip()
            if cand in names:
                idx, body = names.index(cand), rest
            elif re.fullmatch(r"\d{1,2}", cand) and 1 <= int(cand) <= len(names):
                idx, body = int(cand) - 1, rest
        else:
            m = re.match(r"^([^:：\s]{1,12})[:：]\s*(.+)$", line)
            if m and _role_prefix_hit(m.group(1), m.group(2), names):
                cand = m.group(1)
                idx = names.index(cand) if cand in names else int(cand) - 1
                body = m.group(2).strip()
        segs.append((idx, body))
    return segs


def _signed_pct(s):
    """'100%'->'+0%' (Azure/Edge 只认带符号的百分比)."""
    t = (s or "").strip()
    m = _REGEX_SIGNED_PCT.match(t)
    if not m:
        return "+0%"
    sign, num = m.group(1), int(m.group(2))
    if sign:
        return t
    return f"{num - 100:+d}%"


def synth_edge(text, voice_id, rate="+0%", pitch="+0Hz", volume="+0%", attempts=3, on_progress=None, on_stage=None):
    """免费引擎, 无需Key(不支持风格/角色, 会自动忽略). 返回mp3字节."""
    import asyncio
    import edge_tts
    text = (text or "").strip()
    if not text:
        raise RuntimeError("文本为空")
    rate = _signed_pct(rate)
    volume = _signed_pct(volume) if _REGEX_SIGNED_PCT.match((volume or "").strip()) else volume

    def _once():
        async def _run():
            comm = edge_tts.Communicate(text=text, voice=voice_id, rate=rate,
                                        pitch=pitch, volume=volume)
            buf = bytearray()
            gen = comm.stream()
            try:
                async for chunk in _stream_chunks(gen, EDGE_CHUNK_TIMEOUT):
                    if chunk["type"] == "audio":
                        if not buf and on_stage:
                            on_stage("接收音频")
                        buf.extend(chunk["data"])
                        if on_progress:
                            try:
                                on_progress(len(buf), 0)
                            except workflow.Cancelled:
                                raise
                            except Exception:
                                pass
            finally:
                aclose = getattr(gen, 'aclose', None)
                if aclose is not None:
                    try:
                        await aclose()
                    except Exception:
                        pass
            return bytes(buf)
        return asyncio.run(_run())

    last_err = ""
    for i in range(1, attempts + 1):
        if on_stage:
            on_stage(f"等待 Edge 合成 · 请求 {i}/{attempts}")
        try:
            data = _once()
        except ValueError:
            raise
        except workflow.Cancelled:
            raise
        except Exception as e:
            # 任何异常都要带类型与摘要：笼统标“超时”会掩盖 403/网络重置等真因
            last_err = f"Edge合成异常(第{i}/{attempts}次): {type(e).__name__}: {str(e)[:200]}"
            if i < attempts:
                if on_stage:
                    on_stage(f"等待重试 · {i}秒")
                workflow.retry_wait(i)
            continue
        if data:
            return data
        last_err = f"Edge空音频(第{i}/{attempts}次)"
        if i < attempts:
            if on_stage:
                on_stage(f"等待重试 · {i}秒")
            workflow.retry_wait(i)
    raise RuntimeError(last_err + "。已自动重试, 不行就切Azure引擎")


def list_edge_voices():
    """Edge免费人声列表, 免Key."""
    import asyncio
    import edge_tts
    voices = asyncio.run(edge_tts.list_voices())
    table = {}
    for v in voices:
        short = v.get("ShortName", "")
        if not short or "Neural" not in short:
            continue
        core = short.split("-", 2)[2] if short.count("-") >= 2 else short
        base = core.split(":")[0].replace("MultilingualNeural", "").replace("Neural", "")
        gender = v.get("Gender", "")
        g = "女" if gender == "Female" else ("男" if gender == "Male" else gender)
        table[f"{base}{g} {short}"] = short
    table = dict(sorted(table.items(), key=lambda kv: (kv[1][:2] != "zh", kv[1])))
    save_json(EDGE_CACHE, table)
    return table


def get_edge_voices():
    cached = load_json(EDGE_CACHE, {})
    if isinstance(cached, dict) and len(cached) > 10:
        return cached
    return dict(BUILTIN_VOICES)


def openai_speech_url(endpoint):
    ep = (endpoint or "").strip().rstrip("/")
    if not ep:
        return DEFAULT_OPENAI_SPEECH
    if ep.endswith("/audio/speech"):
        return ep
    if ep.endswith("/v1"):
        return ep + "/audio/speech"
    return ep + "/v1/audio/speech"


def openai_rate_to_speed(rate):
    """界面语速百分比 -> OpenAI speed 0.25~4.0, 100%=1.0."""
    t = (rate or "100%").strip()
    m = re.match(r"^([+-]?)(\d+)%$", t)
    if not m:
        return 1.0
    sign, num = m.group(1), int(m.group(2))
    pct = num if not sign else (100 + num if sign == "+" else 100 - num)
    return min(4.0, max(0.25, pct / 100.0))


def get_openai_voices():
    cached = load_json(OPENAI_CACHE, {})
    if isinstance(cached, dict) and cached:
        return cached
    return dict(BUILTIN_OPENAI_VOICES)


def list_openai_voices():
    table = dict(BUILTIN_OPENAI_VOICES)
    save_json(OPENAI_CACHE, table)
    return table


MAX_RESPONSE_BYTES = 256 * 1024**2  # 单段音频响应内存上限：异常响应不许无限吃内存


def _read_response_bytes(response, on_progress=None):
    """接收进度仅代表传输字节；无可信总长度时 total=0，不估算云端进度。"""
    import requests
    try:
        total = max(0, int(response.headers.get("Content-Length", 0)))
    except (TypeError, ValueError):
        total = 0
    if response.headers.get("Content-Encoding", "identity").lower() != "identity":
        total = 0  # iter_content 会解压，不能使用压缩前的长度计算百分比。
    data = bytearray()
    if on_progress:
        on_progress(0, total)
    for chunk in response.iter_content(chunk_size=4096):
        if chunk:
            data.extend(chunk)
            if len(data) > MAX_RESPONSE_BYTES:
                raise requests.exceptions.ChunkedEncodingError(
                    f"音频响应超过 {MAX_RESPONSE_BYTES // (1024 * 1024)}MB 上限，判定为异常响应")
            if on_progress:
                on_progress(len(data), total)
    if total and len(data) != total:
        raise requests.exceptions.ChunkedEncodingError(
            f"音频响应不完整: 收到 {len(data)} / {total} 字节")
    return bytes(data)


def synth_openai(text, voice_id, key, endpoint="", rate="+0%", attempts=3,
                 model=OPENAI_MODEL_DEFAULT, on_progress=None, on_stage=None):
    """OpenAI及兼容 /v1/audio/speech. Key必填, endpoint可指向本地/中转."""
    import requests
    text = (text or "").strip()
    if not text:
        raise RuntimeError("文本为空")
    key = (key or "").strip()
    if not key:
        raise RuntimeError("OpenAI兼容引擎请先填Key")
    url = openai_speech_url(endpoint)
    payload = json.dumps({
        "model": model or OPENAI_MODEL_DEFAULT,
        "voice": voice_id or "alloy",
        "input": text,
        "response_format": "mp3",
        "speed": openai_rate_to_speed(rate),
    }, ensure_ascii=False).encode("utf-8")
    headers = {"Authorization": "Bearer " + key,
               "Content-Type": "application/json"}
    last_err = ""
    for i in range(1, attempts + 1):
        if on_stage:
            on_stage(f"等待 OpenAI 合成 · 请求 {i}/{attempts}")
        try:
            r = _sess().post(url, headers=headers, data=payload, timeout=(10, 60), stream=True)
        except requests.exceptions.ConnectTimeout:
            last_err = f"OpenAI连接超时(第{i}/{attempts}次)"
        except requests.exceptions.ReadTimeout:
            last_err = f"OpenAI读取超时(第{i}/{attempts}次)"
        except requests.exceptions.ConnectionError as e:
            last_err = f"OpenAI连接中断(第{i}/{attempts}次): {e}"
        except workflow.Cancelled:
            raise
        except Exception as e:
            last_err = f"OpenAI请求异常(第{i}/{attempts}次): {e}"
        else:
            try:
                ctype = r.headers.get("Content-Type", "").lower()
                if r.status_code == 200 and ("audio" in ctype or "octet-stream" in ctype):
                    if on_stage:
                        on_stage("接收音频")
                    data = _read_response_bytes(r, on_progress)
                    if data:
                        return data
                    last_err = f"OpenAI空音频(第{i}/{attempts}次)"
                elif r.status_code in (400, 401, 403):
                    detail = r.text[:300] if r.text else "<空包>"
                    raise RuntimeError(f"OpenAI HTTP {r.status_code} voice={voice_id}: {detail}。"
                                       "检查Key、地址是否为 /v1/audio/speech 兼容口")
                else:
                    last_err = f"OpenAI HTTP {r.status_code}(第{i}/{attempts}次)"
            except requests.exceptions.RequestException as e:
                last_err = f"OpenAI接收中断(第{i}/{attempts}次): {e}"
            finally:
                r.close()
        if i < attempts:
            if on_stage:
                on_stage(f"等待重试 · {i}秒")
            workflow.retry_wait(i)
    raise RuntimeError(last_err + "。已自动重连, 不行切Azure/Edge")


def get_volc_voices():
    cached = load_json(VOLC_CACHE, {})
    if isinstance(cached, dict) and cached:
        return cached
    return dict(BUILTIN_VOLC_VOICES)


def list_volc_voices():
    table = dict(BUILTIN_VOLC_VOICES)
    save_json(VOLC_CACHE, table)
    return table


def synth_volc(text, voice_id, token, appid="", endpoint="", rate="+0%",
               pitch="+0Hz", volume="+0%", attempts=3, on_progress=None, on_stage=None):
    """火山引擎 TTS HTTP. token=Access Token, appid 可放 Region 栏."""
    import requests
    text = (text or "").strip()
    if not text:
        raise RuntimeError("文本为空")
    token = (token or "").strip()
    if not token:
        raise RuntimeError("火山引擎请先填Token")
    url = (endpoint or "").strip() or DEFAULT_VOLC_TTS
    try:
        pitch_n = int(re.sub(r"[^0-9+\-]", "", pitch or "0") or "0")
    except Exception:
        pitch_n = 0
    try:
        vol_n = int(re.sub(r"[^0-9+\-]", "", volume or "0") or "0")
    except Exception:
        vol_n = 0
    body = {
        "app": {"appid": (appid or "").strip(), "token": token, "cluster": "volcano_tts"},
        "user": {"uid": "smartvoice"},
        "audio": {
            "voice_type": voice_id or "BV001_streaming",
            "encoding": "mp3",
            "speed_ratio": openai_rate_to_speed(rate),
            "rate": 24000,
            "pitch_ratio": min(2.0, max(0.1, 1.0 + pitch_n / 12.0)),
            "volume_ratio": min(3.0, max(0.1, 1.0 + vol_n / 100.0)),
        },
        "request": {
            "reqid": str(uuid.uuid4()),
            "text": text,
            "text_type": "plain",
            "operation": "query",
        },
    }
    headers = {"Authorization": "Bearer;" + token,
               "Content-Type": "application/json"}
    last_err = ""
    for i in range(1, attempts + 1):
        if on_stage:
            on_stage(f"等待火山合成 · 请求 {i}/{attempts}")
        try:
            r = _sess().post(url, headers=headers,
                             data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                             timeout=(10, 60), stream=True)
        except requests.exceptions.ConnectTimeout:
            last_err = f"火山连接超时(第{i}/{attempts}次)"
        except requests.exceptions.ReadTimeout:
            last_err = f"火山读取超时(第{i}/{attempts}次)"
        except requests.exceptions.ConnectionError as e:
            last_err = f"火山连接中断(第{i}/{attempts}次): {e}"
        except workflow.Cancelled:
            raise
        except Exception as e:
            last_err = f"火山请求异常(第{i}/{attempts}次): {e}"
        else:
            try:
                if r.status_code in (400, 401, 403):
                    detail = r.text[:300] if r.text else "<空包>"
                    raise RuntimeError(f"火山 HTTP {r.status_code}: {detail}。检查Token与AppID")
                if r.status_code != 200:
                    last_err = f"火山 HTTP {r.status_code}(第{i}/{attempts}次)"
                else:
                    if on_stage:
                        on_stage("接收响应")
                    raw = _read_response_bytes(r, on_progress)
                    try:
                        d = json.loads(raw)
                    except ValueError as e:
                        raise RuntimeError("火山返回非JSON响应，未作为音频保存") from e
                    if not isinstance(d, dict):
                        raise RuntimeError("火山响应结构无效")
                    audio = d.get("data") or ""
                    if audio:
                        import base64
                        if on_stage:
                            on_stage("解码音频")
                        try:
                            return base64.b64decode(audio)
                        except Exception as e:
                            raise RuntimeError(f"火山音频解码失败: {e}")
                    last_err = "火山未返回音频: " + str(d.get("message") or d.get("code") or "<空>")[:200]
            except requests.exceptions.RequestException as e:
                last_err = f"火山接收中断(第{i}/{attempts}次): {e}"
            finally:
                r.close()
        if i < attempts:
            if on_stage:
                on_stage(f"等待重试 · {i}秒")
            workflow.retry_wait(i)
    raise RuntimeError(last_err + "。已自动重连, 不行切Azure/Edge")
