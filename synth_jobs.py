"""合成任务编排：串行/双路并发收割、序号取消、缓存复用、进度回调接线。无 Tk 依赖。

调用方（App）按次组装 JobContext 注入能力，本模块只跑纯编排逻辑，
可直接单测，无需 Tk/App。
"""
from concurrent.futures import FIRST_COMPLETED, wait
import threading

import engine
import voice_tasks as workflow


class JobContext:
    """单次 run_jobs 调用的能力包（全部由调用方注入， Tk-free 可伪造）。

    - is_current(seq): 序号是否仍有效（App 侧读实时 _seq）。
    - emit(kind, seq, payload): 进度/缓存事件入队。
    - cache: load(key)/save(key, data)，OSError 视为可丢弃。
    - callbacks(seq, index, token): 返回 (on_progress, on_stage)。
    - synth(text, per_segment, voice, on_progress, on_stage): 实际合成。
    - pool: submit(fn, index) 并发执行器。
    - token(): 默认取消令牌（snap 自带 _cancel 时优先用 snap 的）。
    """

    def __init__(self, *, is_current, emit, cache, callbacks, synth, pool, token):
        self.is_current = is_current
        self.emit = emit
        self.cache = cache
        self.callbacks = callbacks
        self.synth = synth
        self.pool = pool
        self.token = token


def run_jobs(ctx, seq, jobs, snap):
    """Azure 最多两路在途请求；输出按原文排序，回调按段编号聚合。

    点停止（取消）后不再提交新段，未完成段返回 None，已完成段照常拼接输出。
    """
    halted = threading.Event()
    results = [None] * len(jobs)

    def run(index):
        if not ctx.is_current(seq) or halted.is_set():
            raise engine.SynthesisCancelled("合成已取消")
        voice, _, text = jobs[index]
        token = snap.get('_cancel') or ctx.token()
        token.check()
        key = workflow.fingerprint(text, voice, snap)
        cached = ctx.cache.load(key)
        if cached is not None:
            if snap.get('_diag'):
                snap['_diag'].event('segment_cache', index + 1, cached=True)
            ctx.emit('cache_segment', seq, key)
            ctx.emit('segment', seq, index + 1)
            return cached
        progress, stage = ctx.callbacks(seq, index + 1, token)

        def checked(callback):
            def call(*args):
                if halted.is_set():
                    raise engine.SynthesisCancelled("合成已取消")
                return callback(*args)
            return call
        per_segment = dict(snap, _index=index + 1)
        data = ctx.synth(text, per_segment, voice,
                         on_progress=checked(progress), on_stage=checked(stage))
        try:
            ctx.cache.save(key, data)
        except OSError:
            # 缓存是优化项: 磁盘满/权限不足只丢缓存, 不判死已合成的段
            if snap.get('_diag'):
                snap['_diag'].event('cache_write_failed', error_type='OSError')
        ctx.emit('cache_segment', seq, key)
        if not ctx.is_current(seq):
            raise engine.SynthesisCancelled("合成已取消")
        ctx.emit("segment", seq, index + 1)
        return data

    if len(jobs) < 2 or engine.kind_of(snap["engine"]) != "azure":
        for i in range(len(jobs)):
            try:
                results[i] = run(i)
            except (workflow.Cancelled, engine.SynthesisCancelled):
                break  # 取消：已合成的段保留，未完成段留空
        return results
    pending, next_index = {}, 0
    try:
        while pending or next_index < len(jobs):
            if not ctx.is_current(seq):
                raise engine.SynthesisCancelled("合成已取消")
            while len(pending) < 2 and next_index < len(jobs):
                pending[ctx.pool.submit(run, next_index)] = next_index
                next_index += 1
            finished, _ = wait(pending, timeout=.05, return_when=FIRST_COMPLETED)
            for future in finished:
                index = pending.pop(future)
                results[index] = future.result()
    except (workflow.Cancelled, engine.SynthesisCancelled):
        # 取消：短暂收割在途段，刚好完成的照常输出，其余留空
        if pending:
            done_now, _ = wait(pending, timeout=.05)
            for future in done_now:
                index = pending.pop(future)
                try:
                    results[index] = future.result()
                except Exception:
                    pass
    finally:
        halted.set()
        for future in pending:
            future.cancel()
    return results
