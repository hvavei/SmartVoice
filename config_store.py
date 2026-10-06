"""配置暂存/去抖/后台落盘：从 App 抽出的无 Tk 依赖模块。

Tk 相关的定时器与落盘结果回主线程，由调用方以回调注入：
- save_fn(cfg)：同步落盘（在工作线程执行）。
- schedule_fn(ms, callback)：主线程定时，返回句柄。
- cancel_fn(handle)：取消定时。
- report_fn(tag, error)：落盘结果通知，只做线程安全的入队，不碰 Tk/定时器；
  主线程的轮询唤醒由调用方在 flush() 后同步触发（跨线程 root.after 会卡死轮询标志）。
"""
from task_manager import DaemonPool

try:
    from tkinter import TclError
except ImportError:  # pragma: no cover —— 纯逻辑单测无 Tk 时退化为 Exception
    TclError = Exception

DEBOUNCE_MS = 350


class ConfigStore:
    def __init__(self, save_fn, schedule_fn, cancel_fn, report_fn):
        self._save_fn = save_fn
        self._schedule_fn = schedule_fn
        self._cancel_fn = cancel_fn
        self._report_fn = report_fn
        self.pending = None
        self._after = None
        self.pool = DaemonPool(max_workers=1, thread_name_prefix="config")

    def schedule(self, cfg, callback):
        """暂存新配置并去抖；频繁输入只落最后一次。到点后调 callback（App 侧做计数与唤醒）。"""
        self.pending = cfg
        if self._after is not None:
            try:
                self._cancel_fn(self._after)
            except TclError:
                pass  # 销毁中的 root：定时器随窗口一起没了，无需取消。
        self._after = self._schedule_fn(DEBOUNCE_MS, callback)

    def flush(self):
        """取消去抖定时，取出暂存并送后台落盘，返回 Future（调用方有界等待）；无暂存返回 None。
        不做计数与唤醒，由调用方负责。"""
        if self._after is not None:
            try:
                self._cancel_fn(self._after)
            except TclError:
                pass
            self._after = None
        if self.pending is None:
            return
        cfg, self.pending = self.pending, None
        save_fn, report_fn = self._save_fn, self._report_fn

        def write():
            try:
                save_fn(cfg)
            except (KeyboardInterrupt, SystemExit) as e:
                # 先记名再抛：调用方同时看 report 事件与 Future，不许静默“成功”。
                report_fn("config_done", type(e).__name__)
                raise
            except BaseException as e:  # noqa: BLE001 —— 落盘失败记诊断，不炸后台线程
                if isinstance(e, Exception):
                    report_fn("config_done", str(e))
                    return
                # 中断类异常（旧 except Exception 兜不住）：记类型名再原样抛出，
                # 不冒充“落盘成功”。
                report_fn("config_done", type(e).__name__)
                raise
            report_fn("config_done", None)

        return self.pool.submit(write)

    def submit(self, fn, *args, **kwargs):
        """复用单线程池跑其它轻后台任务（如诊断收尾），与配置落盘串行。"""
        return self.pool.submit(fn, *args, **kwargs)

    def shutdown(self, wait=True):
        if self._after is not None:
            try:
                self._cancel_fn(self._after)
            except TclError:
                pass
            self._after = None
        self.pool.shutdown(wait=wait)
