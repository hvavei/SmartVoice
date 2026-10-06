"""本机音频播放：winmm MCI 封装与时钟格式化。无 Tk 依赖。"""
import os
import time


def format_clock(ms):
    """毫秒 -> MM:SS，负值按 0 处理。"""
    seconds = max(0, ms // 1000)
    return f'{seconds//60:02d}:{seconds%60:02d}'


class MciPlayer:
    """通过 winmm 播放已完整落盘的 PCM WAV，使用毫秒时长确认播放结束。"""

    def __init__(self):
        self.alias = "ttsstudio"
        self.opened = False
        self.length_ms = 0
        self.started = 0.0
        self._stopped_since = None
        self._ended_since = None

    @staticmethod
    def _winmm():
        import platform
        if platform.system() != "Windows":
            raise RuntimeError("内置播放仅支持Windows本机")
        try:
            from ctypes import windll
            return windll.winmm
        except Exception as e:
            raise RuntimeError(f"系统音频组件不可用: {e}")

    def _cmd(self, cmd):
        from ctypes import create_unicode_buffer
        winmm = self._winmm()
        buf = create_unicode_buffer(256)
        err = winmm.mciSendStringW(cmd, buf, 256, 0)
        if err != 0:
            try:
                winmm.mciGetErrorStringW(err, buf, 256)
                msg = buf.value or f"错误码{err}"
            except Exception:
                msg = f"错误码{err}"
            raise RuntimeError(f"播放失败: {msg}")
        return buf.value.strip()

    def play_file(self, path):
        self.stop()
        try:
            self._cmd(f'open "{os.path.abspath(path)}" type waveaudio alias {self.alias}')
            self.opened = True
            self._cmd(f"set {self.alias} time format milliseconds")
            self.length_ms = int(self._cmd(f"status {self.alias} length"))
            if self.length_ms <= 0:
                raise RuntimeError("播放音频时长为0")
            self._cmd(f"seek {self.alias} to start")
            self.started = time.monotonic()
            # 让驱动播放到文件真实 EOF，不用毫秒取整后的长度截断最后一帧。
            self._cmd(f"play {self.alias} from 0")
        except Exception:
            self.stop()
            raise

    def is_playing(self):
        if not self.opened:
            return False
        mode = self._cmd(f"status {self.alias} mode").lower()
        if mode in ("playing", "paused"):
            self._stopped_since = None
            self._ended_since = None
            return True
        now = time.monotonic()
        if now - self.started < 0.5:
            return True  # 驱动启动期间短暂的 stopped 不是播放完毕。
        position = int(self._cmd(f"status {self.alias} position"))
        if position + 2 < self.length_ms:
            self._ended_since = None
            if self._stopped_since is None:
                self._stopped_since = now
            if now - self._stopped_since < 0.8:
                return True  # 短暂状态切换不能触发 App 立即 stop/close。
            raise RuntimeError(f"音频播放提前结束: {position}/{self.length_ms} ms，可重试播放或打开导出文件")
        self._stopped_since = None
        if self._ended_since is None:
            self._ended_since = now
        # EOF 后保留设备片刻，让已提交的尾部样本排空，而不是立即 close。
        return now - self._ended_since < 0.3

    def toggle_pause(self):
        if not self.opened:
            return
        # stopped 态发 pause 会弹 MCI 错误：只在 playing/paused 才发指令。
        mode = self._cmd(f'status {self.alias} mode').lower()
        if mode not in ("playing", "paused"):
            return
        self._cmd(f'{"resume" if mode == "paused" else "pause"} {self.alias}')

    def position_ms(self):
        return int(self._cmd(f'status {self.alias} position')) if self.opened else 0

    def stop(self):
        # 幂等停止: 没open也尝试close残留别名, 永不抛异常
        for cmd in (f"stop {self.alias}", f"close {self.alias}"):
            try:
                self._cmd(cmd)
            except Exception:
                pass
        self.opened = False
        self.length_ms = 0
        self._stopped_since = None
        self._ended_since = None
