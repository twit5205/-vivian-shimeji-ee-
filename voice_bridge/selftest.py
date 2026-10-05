# -*- coding: utf-8 -*-
"""临时回归测试（不碰麦克风/扬声器/网络）：验证端点检测、分句、情绪解析。

用合成帧回放模拟真实音频，重点确认：
  - 句内换气（峰值掉下去但 VAD 概率仍高）不会把句子拦腰截断
  - 真正的长静音才会结束录音
  - 流式回复的句子切分与 [MOOD:x] 解析正确
"""
import sys
import threading
import time

import numpy as np

sys.stdout.reconfigure(encoding="utf-8")

from voice_config import load_config
from vad_recorder import Gate, record_utterance, normal_gate
from tts_engine import SentenceStreamer, clean_for_tts
from voice_assistant import Brain, send_to_bubble
from memory_db import MemoryDB

FAILED = []


def check(name, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + ("  " + str(extra) if extra else ""))
    if not cond:
        FAILED.append(name)


# ---------- 1. 配置 ----------
print("== 1. 配置 ==")
cfg = load_config()
print("  stt=%s/%s  tts=%s  barge_in=%s  endpoint=%dms  vad=%.2f/%.2f"
      % (cfg.stt_model, cfg.stt_compute_type, cfg.tts_engine, cfg.barge_in,
         cfg.endpoint_silence_ms, cfg.vad_threshold, cfg.vad_neg_threshold))
check("端点静音阈值已生效", 300 <= cfg.endpoint_silence_ms <= 1200, cfg.endpoint_silence_ms)
check("迟滞阈值 neg < pos", cfg.vad_neg_threshold < cfg.vad_threshold)

# ---------- 2. 分句 ----------
print("\n== 2. 句子切分 ==")
demo = "嗯……让我想想。其实啊，这件事我觉得可以换个思路，先看看日志再动手，你说呢？"
ss = SentenceStreamer(12, 45, 2)
got = []
for ch in demo:                                  # 模拟逐字流式到达
    got += ss.feed(ch)
got += ss.flush()
for i, s in enumerate(got, 1):
    print("  句%d: %r" % (i, s))
check("切出了多句", len(got) >= 3, len(got))
check("首句来得早(不超 20 字)", len(got[0]) <= 20, len(got[0]))
check("无空句", all(s.strip() for s in got))
check("内容无丢失", "".join(got).replace(" ", "") == clean_for_tts(demo).replace(" ", ""))

# ---------- 3. 文本清洗 ----------
print("\n== 3. TTS 文本清洗 ==")
dirty = "**[MOOD:happy]** 好呀 😊 `代码` https://a.com 你说呢？"
clean = clean_for_tts(dirty)
print("  ->", repr(clean))
check("去掉 MOOD", "MOOD" not in clean)
check("去掉 markdown", "*" not in clean and "`" not in clean)
check("去掉表情", "😊" not in clean)
check("去掉 URL", "http" not in clean)
check("保留正文", "你说呢" in clean and "好呀" in clean)


# ---------- 4. 假麦克风：回放合成帧 ----------
class FakeMic:
    """模拟 MicStream：只暴露 set_sink。"""

    def __init__(self):
        self.sink = None
        self.ready = threading.Event()

    def set_sink(self, fn):
        old = self.sink
        self.sink = fn
        if fn is not None:
            self.ready.set()
        return old


class Fr:
    __slots__ = ("data", "prob", "peak", "ts")

    def __init__(self, data, prob, peak):
        self.data, self.prob, self.peak, self.ts = data, prob, peak, time.monotonic()


def feed(mic, n, prob, peak, sr=16000, frame=512):
    """按帧回放。sink 被摘掉（录音已结束）即停止。"""
    fed = 0
    for _ in range(n):
        sink = mic.sink
        if sink is None:
            break
        data = (np.random.randn(frame) * peak / 3.0).astype(np.float32)
        try:
            sink(Fr(data, prob, peak))
        except TypeError:          # sink 在检查与调用之间被摘掉（录音已结束）
            break
        fed += 1
    return fed


print("\n== 4. 端点检测（句内换气不应截断）==")
cfg.set("idle_timeout_s", 6)
cfg.set("min_speech_ms", 350)
mic = FakeMic()
result = {}

th = threading.Thread(target=lambda: result.update(
    audio=record_utterance(mic, cfg, gate=normal_gate(cfg), label="TEST",
                           log=lambda *a: None, idle_timeout=6)))
th.start()
mic.ready.wait(3)
time.sleep(0.05)
feed(mic, 10, 0.02, 0.005)          # 底噪
feed(mic, 20, 0.90, 0.200)          # 说话
feed(mic, 6, 0.80, 0.010)           # 句内换气：峰值掉到很低，但 VAD 概率仍高（旧版会在这里截断）
feed(mic, 20, 0.90, 0.200)          # 继续说
tail = feed(mic, 30, 0.02, 0.005)   # 真正静音，录到 20 帧（650ms）就该收工
th.join(8)
audio = result.get("audio")
ok = audio is not None
check("成功捕获一整句", ok, None if not ok else "%.2fs" % (len(audio) / 16000.0))
check("静音满阈值即收工(未吃满 30 帧)", tail <= 25, "%d 帧" % tail)
if ok:
    dur = len(audio) / 16000.0
    # 换气 6 帧 + 前后各 20 帧说话 ≈ 46 帧 ≈ 1.47s；若在换气处截断则只有 ~0.96s
    check("换气处没有被截断", dur > 1.20, "%.2fs" % dur)
    check("录音时长合理", 1.2 < dur < 2.5, "%.2fs" % dur)

print("\n== 5. 插话门限（外放回声不应误触发）==")
from vad_recorder import barge_in_gate
mic2 = FakeMic()
res2 = {}
stop = threading.Event()
th2 = threading.Thread(target=lambda: res2.update(
    audio=record_utterance(mic2, cfg, gate=barge_in_gate(cfg), label="BI",
                           log=lambda *a: None, stop_event=stop, idle_timeout=6)))
th2.start()
mic2.ready.wait(3)
time.sleep(0.05)
# 回声基线：中等峰值（模拟外放回授）→ 不应触发
feed(mic2, 12, 0.30, 0.050)
feed(mic2, 12, 0.40, 0.060)
check("回声未误触发", True)
stop.set()                            # 播放结束
th2.join(6)
check("播放结束后立即返回 None", res2.get("audio") is None)

print("\n== 6. 流式回复解析（假 LLM）==")


class _Delta:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content):
        self.delta = _Delta(content)


class _Chunk:
    def __init__(self, content):
        self.choices = [_Choice(content)]


class _FakeStream:
    def __init__(self, pieces):
        self.pieces = pieces

    def __iter__(self):
        for p in self.pieces:
            yield _Chunk(p)


class _FakeCompletions:
    def __init__(self, pieces):
        self.pieces = pieces

    def create(self, **kw):
        return _FakeStream(self.pieces)


class _FakeClient:
    def __init__(self, pieces):
        self.chat = type("C", (), {"completions": _FakeCompletions(pieces)})()


brain = Brain(cfg, "sk-test", log=lambda *a: None, memory=MemoryDB(":memory:"))
brain._export_memory_summary = lambda: None
brain.client = _FakeClient(["[MOOD:", "happy]\n", "当然可以。", "不过要先看日志，", "你觉得呢？"])
events = list(brain.stream_reply("帮我看看"))
sents = [p for k, p in events if k == "sentence"]
moods = [p for k, p in events if k == "mood"]
done = [p for k, p in events if k == "done"]
print("  mood:", moods, "\n  sentences:", sents, "\n  done:", done)
check("解析出情绪", moods == ["happy"], moods)
check("切出 2~3 句", 2 <= len(sents) <= 3, sents)
check("句子里没有 MOOD 残留", not any("MOOD" in s for s in sents))
check("收尾含全文", done and done[0][0].startswith("当然可以"), done)
check("历史已记录", len(brain.history) == 2, brain.history)

print("\n== 7. 桌宠桥接（未启动时应静默失败）==")
check("桥接不可用时返回 False", send_to_bubble("测试", "happy", cfg) is False)


# ---------- 8. 旧版逻辑对照（防止回退到有缺陷的判定）----------
print("\n== 8. 端点判定：旧版 vs 新版（轻声说话场景）==")


def old_endpoint(frames):
    """旧版 record_speech 判定：结束条件是 `prob<0.5 或 peak<0.05`。"""
    VAD_TH, PEAK_TH, TRIGGER, PEAK_TRIGGER, SILENCE = 0.5, 0.05, 3, 2, 22
    triggered, tr, ptr, sr, out = False, 0, 0, 0, 0
    for p, pk in frames:
        if not triggered:
            tr = tr + 1 if p >= VAD_TH else 0
            ptr = ptr + 1 if pk >= PEAK_TH else 0
            if tr >= TRIGGER or ptr >= PEAK_TRIGGER:
                triggered = True
        else:
            out += 1
            if p < VAD_TH or pk < PEAK_TH:
                sr += 1
            else:
                sr = 0
            if sr >= SILENCE:
                break
    return out


soft = ([(0.02, 0.005)] * 10 + [(0.90, 0.200)] * 25
        + [(0.90, 0.040)] * 94 + [(0.02, 0.005)] * 40)     # 轻声说 3 秒（峰值仅 0.04）
o = old_endpoint(soft) * 32.0 / 1000.0
mic3 = FakeMic()
res3 = {}


def _run_new():
    th3 = threading.Thread(target=lambda: res3.update(
        audio=record_utterance(mic3, cfg, gate=normal_gate(cfg), label="T3",
                               log=lambda *a: None, idle_timeout=8)))
    th3.start()
    mic3.ready.wait(3)
    time.sleep(0.05)
    for p, pk in soft:
        sink = mic3.sink
        if sink is None:
            break
        data = (np.random.randn(512) * pk / 3.0).astype(np.float32)
        try:
            sink(Fr(data, p, pk))
        except TypeError:
            break
    th3.join(8)


_run_new()
n = len(res3["audio"]) / 16000.0 if res3.get("audio") is not None else 0.0
print("  旧版收录 %.2fs / 新版收录 %.2fs（理想 ≈ %.2fs）" % (o, n, (10 + 25 + 94) * 0.032))
check("旧版确实会提前截断", o < 2.0, "%.2fs" % o)
check("新版完整收录轻声内容", n > 3.5, "%.2fs" % n)

print("\n== 9. 会话编排（假 STT/LLM/TTS，验证胶水层）==")
from voice_assistant import NullSpeaker, Session


class FakeSTT:
    def transcribe(self, audio):
        return "你好呀"

    def load(self):
        pass

    def warmup(self):
        pass


class FakeBrain:
    def stream_reply(self, text):
        yield ("mood", "happy")
        yield ("sentence", "当然可以。")
        yield ("sentence", "不过要先看日志。")
        yield ("done", ("当然可以。不过要先看日志。", "happy"))


class RecordingSpeaker(NullSpeaker):
    def __init__(self):
        super().__init__(log=lambda *a: None)
        self.said = []
        self.turns = 0

    def begin_turn(self, t_ref=None):
        self.turns += 1
        return self.turns

    def say(self, text):
        self.said.append(text)


sess = Session(cfg, log=lambda *a: None)
sess.stt = FakeSTT()
sess.brain = FakeBrain()
spk = RecordingSpeaker()
sess.speaker = spk
sess.mic = None                                    # 关闭打断，走"播完即结束"分支
out = sess.converse(np.ones(16000, dtype=np.float32))
check("一轮结束后返回 None", out is None)
check("开启了 1 个轮次", spk.turns == 1, spk.turns)
check("两句都送到了 TTS", spk.said == ["当然可以。", "不过要先看日志。"], spk.said)


class EmptySTT(FakeSTT):
    def transcribe(self, audio):
        return ""


sess2 = Session(cfg, log=lambda *a: None)
sess2.stt = EmptySTT()
sess2.brain = FakeBrain()
spk2 = RecordingSpeaker()
sess2.speaker = spk2
sess2.mic = None
out2 = sess2.converse(np.ones(16000, dtype=np.float32))
check("识别为空则不发声", out2 is None and spk2.said == [], spk2.said)

print("\n" + ("=" * 46))
print("失败项: %d %s" % (len(FAILED), FAILED if FAILED else ""))
sys.exit(1 if FAILED else 0)
