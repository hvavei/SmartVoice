"""后台任务泵：提交、进度事件、done/fail 路由、取消序号，一处实现。

约定（与旧散装逻辑完全一致）：
- 工作线程只往队列放 `(kind, seq, payload)`，主线程轮询回放，永不跨线程碰 Tk。
- 路由与计数语义由调用方的 handle 实现，本模块只负责泵 mechanics：
  入队、在途计数、轮询定时、序号；handle 返回是否需要重绘。
- Tk 的 after/after_cancel 由调用方注入；TclError 仅用于守卫销毁中的 root。
"""
import queue
import sys
import threading
import time
import tkinter as tk
import weakref
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import thread as _executor_thread

if sys.version_info < (3, 14):
    # DaemonPool 钉死 3.14 私有线程 API：低版本 import 即 AttributeError，
    # 后台全起不来；早报错、不装糊涂。
    raise RuntimeError("SmartVoice 源码运行需要 Python 3.14")

POLL_INTERVAL_MS = 50
POLL_BATCH = 100
POLL_BUDGET_S = 0.008


class DaemonPool(ThreadPoolExecutor):
    """与 ThreadPoolExecutor 同语义，工作线程为 daemon：退出时不钉死解释器。

    在途任务靠调用方取消令牌与网络超时收敛；磁盘/缓存写用原子替换，半路 abandonned 不 corrupt。
    实现钉死 CPython 3.14 的 _adjust_thread_count（仅加 daemon=True），本仓库只验证 3.14。
    """

    def _adjust_thread_count(self):
        if self._idle_semaphore.acquire(timeout=0):
            return

        def weakref_cb(_, q=self._work_queue):
            q.put(None)

        num_threads = len(self._threads)
        if num_threads < self._max_workers:
            thread_name = '%s_%d' % (self._thread_name_prefix or self, num_threads)
            t = threading.Thread(name=thread_name, daemon=True,
                                 target=_executor_thread._worker,
                                 args=(weakref.ref(self, weakref_cb),
                                       self._create_worker_context(),
                                       self._work_queue))
            t.start()
            self._threads.add(t)
            _executor_thread._threads_queues[t] = self._work_queue


class TaskManager:
    def __init__(self, schedule_fn, cancel_fn):
        self.queue = queue.Queue()
        self.inflight = 0
        self.seq = 0
        self._polling = False
        self._schedule_fn = schedule_fn
        self._cancel_fn = cancel_fn
        self.handle = None      # (kind, seq, payload) -> bool dirty；调用方设置
        self.render = None      # dirty 刷新回调；调用方设置
        self.is_active = None   # () -> bool 是否仍需渲染；调用方设置

    def put(self, kind, seq, payload):
        self.queue.put((kind, seq, payload))

    def submit(self, work):
        """主线程调用：序号绑定当前值，在途+1，起守护线程，唤醒轮询。返回本次序号。"""
        seq = self.seq
        self.inflight += 1
        threading.Thread(target=work, daemon=True).start()
        self.kick()
        return seq

    def preempt(self):
        """作废上一任务序号（后台回来的旧结果直接丢弃），返回新序号。"""
        self.seq += 1
        return self.seq

    def kick(self):
        if self._polling:
            return
        self._polling = True
        try:
            self._schedule_fn(POLL_INTERVAL_MS, self.poll)
        except tk.TclError:
            self._polling = False

    def poll(self):
        self._polling = False
        dirty = False
        started = time.monotonic()
        try:
            try:
                # 避免高频网络回调长期占住 Tk 主线程。
                for _ in range(POLL_BATCH):
                    if time.monotonic() - started > POLL_BUDGET_S:
                        break
                    kind, seq, payload = self.queue.get_nowait()
                    if self.handle is not None and self.handle(kind, seq, payload):
                        dirty = True
            except queue.Empty:
                pass
            if dirty and self.is_active is not None and self.is_active():
                if self.render is not None:
                    self.render()
        except Exception:
            # 单条事件处理异常不许打断轮询链，否则事件永久滞留、进度冻结。
            pass
        finally:
            try:
                if self.inflight > 0 or not self.queue.empty():
                    self.kick()
            except tk.TclError:
                pass
