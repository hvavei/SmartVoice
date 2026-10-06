"""任务进度纯计算：分段聚合、highwater 单调、文案拼装。无 Tk 依赖。

继承 dict 以兼容旧读写（`p["done"]`、`p.get("active")` 照常工作），
新增的 begin/apply_*/render/finish 把原来散在 App 里的进度算法收归一处，
可毫秒级直测；刷控件（_prog_ui/prog_lab/Canvas）仍留在 App。
"""
import time


class ProgressModel(dict):
    def __init__(self):
        super().__init__(active=False, mode=None, total=0, done=0, seq=None,
                         stage="", index=1, received=0, bytes_total=0,
                         started=0.0, finished=False, segments={}, completed=set(),
                         post_done=0, highwater=0.0, after=None)

    def begin(self, mode, total, seq, label):
        self.update(active=True, mode=mode, total=max(1, total), done=0, seq=seq,
                    stage=label, index=1, received=0, bytes_total=0,
                    started=time.monotonic(), finished=False,
                    segments={}, completed=set(), post_done=0, highwater=0.0)

    def cancel(self):
        self["active"] = False

    def finish(self):
        self["finished"] = True

    def apply_segment(self, index):
        self["completed"].add(index)
        self["done"] = len(self["completed"])

    def apply_synth_complete(self):
        self["completed"] = set(range(1, self["total"] + 1))
        self["done"] = self["total"]

    def apply_post_complete(self, payload):
        self["post_done"] = payload

    def apply_phase(self, label, index):
        self.update(stage=label, index=index, received=0, bytes_total=0)

    def apply_progress(self, received, total, index=None):
        if index is None:
            index = self["index"]
        segment = self["segments"].setdefault(index, {})
        if total > 0:
            segment["fraction"] = max(segment.get("fraction", 0), min(.99, received / total))
        self.update(received=received, bytes_total=total, index=index)
        self["stage"] = f"接收第{index}段" if self["mode"] == "multi" else "接收音频"

    def render(self, now=None):
        """返回 (value, text)；finished 时返回 None（调用方直接返回）。"""
        if self.get("finished"):
            return None
        now = time.monotonic() if now is None else now
        multi = self["mode"] == "multi"
        received, total_bytes = self["received"], self["bytes_total"]
        value = self["done"] + sum(s.get("fraction", 0) for i, s in self["segments"].items()
                                   if i not in self["completed"]) + self["post_done"]
        value = max(self["highwater"], value)
        self["highwater"] = value
        text = self["stage"]
        if received or total_bytes:
            if total_bytes > 0:
                text += f" {min(100, received * 100 // total_bytes)}%"
            text += f" · {received / 1024:.1f} KB"
        if multi:
            text = f"已完成 {self['done']}/{self['total']}段 · " + text
        elif not total_bytes and self["index"] > 0 and not self["done"]:
            text += " · 等待云端进度"
        text += f" · {int(now - self['started'])}秒"
        return value, text
