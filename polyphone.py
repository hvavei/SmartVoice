"""G2PW 多音字上下文消歧推理（纯 numpy + onnxruntime，无 torch）。

复刻 https://github.com/GitYCC/g2pW 的 G2PWConverter 数据组装，
把整句送入模型，对多音字按上下文给出拼音，再转成 Azure zh-CN SAPI
数字声调（如 yin 2 hang 2，轻声 5）。

模型资产由 prepare_models.py 下载到 models/g2pw/。首次调用才加载，
普通配音/试听不会额外加载这些大文件。
"""
import os
import re
import json
import threading
from pathlib import Path
from functools import lru_cache
from collections import OrderedDict

import components
_component_root = None
try:
    _component_root = components.activate('g2pw')
except components.MissingComponent:
    pass  # 导入不因缺组件/资产失败；真正调用时 _load_model 给出明确提示
import numpy as np

MODEL_DIR = str(_component_root / 'models' / 'g2pw') if _component_root is not None else ''

BATCH = 64
WINDOW = 32  # 与随包 G2PW config.py 的训练上下文窗口一致
_init_lock = threading.Lock()
_ready = False
_window_cache = OrderedDict()
_window_lock = threading.Lock()

_tok = None
_sess = None
_labels = None
_char2phonemes = None
_chars = None
_monophonic = None
_s2t = None
_bopomofo_to_pinyin = None
_char_bopomofo = None
_pinyin_labels = None
_char_ids = None


def _lazy_init():
    global _ready
    if _ready:
        return
    with _init_lock:
        if not _ready:
            _load_model()
            # 所有资产加载成功才发布；失败后下次调用允许重试。
            _ready = True


def _load_model():
    global _tok, _sess, _labels, _char2phonemes, _chars, _monophonic, \
        _s2t, _bopomofo_to_pinyin, _char_bopomofo, _pinyin_labels, _char_ids
    from tokenizers import Tokenizer
    from tokenizers.models import WordPiece
    from tokenizers.pre_tokenizers import BertPreTokenizer
    import onnxruntime

    vocab_path = os.path.join(MODEL_DIR, "vocab.txt")
    if not MODEL_DIR or not os.path.isfile(vocab_path) or not os.path.isfile(os.path.join(MODEL_DIR, "g2pw.onnx")):
        raise RuntimeError("缺少多音字模型资产，请先运行 python prepare_models.py")

    vocab = {line: i for i, line in
             enumerate(Path(vocab_path).read_text(encoding="utf-8").splitlines())}
    _tok = Tokenizer(WordPiece(vocab, unk_token="[UNK]"))
    _tok.pre_tokenizer = BertPreTokenizer()

    so = onnxruntime.SessionOptions()
    so.intra_op_num_threads = 2
    so.graph_optimization_level = onnxruntime.GraphOptimizationLevel.ORT_ENABLE_ALL
    _sess = onnxruntime.InferenceSession(os.path.join(MODEL_DIR, "g2pw.onnx"), so)

    def _rows(name):
        p = os.path.join(MODEL_DIR, name)
        return [line.split("\t") for line in
                Path(p).read_text(encoding="utf-8").strip().splitlines()]

    poly = _rows("POLYPHONIC_CHARS.txt")
    _monophonic = dict(_rows("MONOPHONIC_CHARS.txt"))
    
    _bopomofo_to_pinyin = json.loads(Path(MODEL_DIR,
        "bopomofo_to_pinyin_wo_tune_dict.json").read_text(encoding="utf-8"))

    # 用转换后的拼音覆盖原生注音符号标签
    _labels = sorted({ph for _, ph in poly})
    _pinyin_labels = [_b2p(label) for label in _labels]
    
    _char2phonemes = {}
    label_ids = {label: i for i, label in enumerate(_labels)}
    for char, ph in poly:
        _char2phonemes.setdefault(char, set()).add(label_ids[ph])
    _chars = sorted(_char2phonemes.keys())
    _char_ids = {char: i for i, char in enumerate(_chars)}
    _char_bopomofo = json.loads(Path(MODEL_DIR,
        "char_bopomofo_dict.json").read_text(encoding="utf-8"))
    _s2t = dict(_rows("bert-base-chinese_s2t_dict.txt"))


def _b2p(bopomofo):
    if not bopomofo or bopomofo[-1] not in "12345":
        return None
    comp = _bopomofo_to_pinyin.get(bopomofo[:-1])
    return comp + bopomofo[-1] if comp else None


def _wordize_and_map(text):
    words, text2word, word2text = [], [], []
    while text:
        m = re.match(r"^ +", text)
        if m:
            text2word += [None] * len(m.group(0))
            text = text[len(m.group(0)):]
            continue
        m = re.match(r"^[a-zA-Z0-9]+", text)
        if m:
            en = m.group(0)
            start = len(text2word)
            word2text.append((start, start + len(en)))
            text2word += [len(words)] * len(en)
            words.append(en)
            text = text[len(en):]
        else:
            start = len(text2word)
            word2text.append((start, start + 1))
            text2word.append(len(words))
            words.append(text[0])
            text = text[1:]
    return words, text2word, word2text


def _tokenize_and_map(text):
    words, text2word, word2text = _wordize_and_map(text)
    tokens, token2text = [], []
    for word, (ws, we) in zip(words, word2text):
        wt = _tok.encode(word).tokens
        if not wt or wt == ["[UNK]"]:
            token2text.append((ws, we))
            tokens.append("[UNK]")
        else:
            cur = ws
            for t in wt:
                tlen = len(re.sub(r"^##", "", t))
                token2text.append((cur, cur + tlen))
                cur += tlen
                tokens.append(t)
    text2token = list(text2word)
    for i, (ts, te) in enumerate(token2text):
        for p in range(ts, te):
            text2token[p] = i
    return tokens, text2token, token2text


def _truncate(text, tokens, text2token, token2text, query_id, max_len=512):
    trunc = max_len - 2
    if len(tokens) <= trunc:
        return text, query_id, tokens, text2token, token2text
    tp = text2token[query_id]
    ts = tp - trunc // 2
    te = ts + trunc
    front = -ts
    back = te - len(tokens)
    if front > 0:
        ts += front
        te += front
    elif back > 0:
        ts -= back
        te -= back
    s, e = token2text[ts][0], token2text[te - 1][1]
    return (text[s:e], query_id - s, tokens[ts:te],
            [i - ts if i is not None else None for i in text2token[s:e]],
            [(a - s, b - s) for a, b in token2text[ts:te]])


def disambiguate(text):
    """复用短文稿推理结果；返回副本，避免调用者污染缓存。"""
    if not re.search(r"[\u3400-\u9fff]", text):
        return [None] * len(text)
    if len(text) <= 2000:
        return list(_cached_disambiguate(text))
    return _disambiguate(text)


@lru_cache(maxsize=64)
def _cached_disambiguate(text):
    return tuple(_disambiguate(text))


def _disambiguate(text):
    _lazy_init()
    # 某些 Unicode 字符 lower() 后会变长，必须保持原文偏移一一对应。
    text = "".join(c.lower() if len(c.lower()) == 1 else c for c in text)
    text = "".join(_s2t.get(c, c) if len(_s2t.get(c, c)) == 1 else c for c in text)
    chars = list(text)

    result = [None] * len(chars)
    queries = []  # (sent_text, query_id)
    for i, c in enumerate(chars):
        if c in _char_ids:
            queries.append((text, i))
        elif c in _monophonic:
            result[i] = _b2p(_monophonic[c])
        elif c in _char_bopomofo:
            py = _b2p_first(_char_bopomofo[c])
            if py:
                result[i] = py

    if not queries:
        return result

    inputs = {k: [] for k in ("input_ids", "token_type_ids", "attention_mask",
                              "phoneme_mask", "char_ids", "position_ids")}
    # 同一局部上下文/目标位置只推理一次，不能仅按单字缓存读音。
    pending = {}
    for sent, qid in queries:
        start = max(0, qid - WINDOW // 2)
        end = min(len(sent), qid + WINDOW // 2)
        key = (sent[start:end], qid - start)
        with _window_lock:
            if key in _window_cache:
                result[qid] = _window_cache[key]
                _window_cache.move_to_end(key)
                continue
        pending.setdefault(key, []).append(qid)

    batch_keys = []
    def flush_batch():
        predictions = _predict(inputs)
        with _window_lock:
            for key, prediction in zip(batch_keys, predictions):
                for qid in pending[key]:
                    result[qid] = prediction
                _window_cache[key] = prediction
                _window_cache.move_to_end(key)
            while len(_window_cache) > 1024:
                _window_cache.popitem(last=False)
        for values in inputs.values():
            values.clear()
        batch_keys.clear()

    for key in pending:
        window, query_id = key
        tokens, text2token, token2text = _tokenize_and_map(window)
        stext, sqid, stokens, stext2token, stoken2text = _truncate(
            window, tokens, text2token, token2text, query_id)
        proc = ["[CLS]"] + stokens + ["[SEP]"]
        token_ids = [_tok.token_to_id(token) for token in proc]
        token_type_ids = [0] * len(token_ids)
        attention_mask = [1] * len(token_ids)
        qchar = stext[sqid]
        phoneme_mask = [1 if i in _char2phonemes[qchar] else 0
                        for i in range(len(_labels))]
        char_id = _char_ids[qchar]
        position_id = stext2token[sqid] + 1  # [CLS] 占位
        for k, val in zip(inputs, (token_ids, token_type_ids, attention_mask,
                                   phoneme_mask, [char_id], [position_id])):
            inputs[k].append(val)
        batch_keys.append(key)
        if len(inputs["input_ids"]) >= BATCH:
            flush_batch()
    if inputs["input_ids"]:
        flush_batch()
    return result


def _predict(inputs):
    length = max(len(x) for x in inputs["input_ids"])
    pad = 0
    arr = {}
    arr["input_ids"] = np.array(
        [x + [pad] * (length - len(x)) for x in inputs["input_ids"]], dtype=np.int64)
    arr["token_type_ids"] = np.array(
        [x + [0] * (length - len(x)) for x in inputs["token_type_ids"]], dtype=np.int64)
    arr["attention_mask"] = np.array(
        [x + [0] * (length - len(x)) for x in inputs["attention_mask"]], dtype=np.int64)
    arr["phoneme_mask"] = np.array(inputs["phoneme_mask"], dtype=np.float32)
    arr["char_ids"] = np.array(inputs["char_ids"], dtype=np.int64)
    arr["position_ids"] = np.array(inputs["position_ids"], dtype=np.int64)
    if arr["char_ids"].ndim == 2:
        arr["char_ids"] = arr["char_ids"].reshape(-1)
    if arr["position_ids"].ndim == 2:
        arr["position_ids"] = arr["position_ids"].reshape(-1)
    probs = _sess.run(None, arr)[0]
    preds = probs.argmax(-1).tolist()
    return [_pinyin_labels[p] for p in preds]


def _b2p_first(lst):
    if not lst:
        return None
    return _b2p(lst[0])


def _pinyin_to_sapi(py):
    """pinyin(如 yin2/hang2) -> SAPI(sapi: yin 2 / hang 2)。"""
    if not py:
        return None
    # 暴力清理所有非字母数字字符，仅保留拼音字母和声调数字
    clean = "".join([c for c in py.lower() if c.isalnum()])
    m = re.fullmatch(r"([a-z]+)([1-5])", clean)
    return f"{m.group(1)} {m.group(2)}" if m else None


def annotate_sapi(text):
    """只给模型词表中的多音字注音，普通字保留 Azure 自然韵律。"""
    result = disambiguate(text)
    return text, {i: phone for i, p in enumerate(result)
                  if p and _s2t.get(text[i], text[i]) in _char_ids
                  if (phone := _pinyin_to_sapi(p)) is not None}
