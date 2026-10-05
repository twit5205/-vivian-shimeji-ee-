# -*- coding: utf-8 -*-
"""麦克风输入 + silero VAD 端点检测

设计要点（相对旧版 record_speech 的改进）：
  1. 常开单路 InputStream：录音与"打断检测"共用一路输入，避免设备争用与反复开关的延迟；
  2. 迟滞端点判定：判定静音需要"概率低 **且** 峰值低"，修掉旧版 `or` 逻辑导致
     句内换气/音节低谷被误判成说完、把句子拦腰截断的问题；
  3. 自适应回声基线：播放语音时先测外放回授量，按倍数抬高触发门限，
     外放场景也能安全插话；
  4. 触发即回调：插话一被确认就立刻通知上层停掉 TTS，而不是等整句录完。
"""
import threading
import time
from collections import deque

import numpy as np
import onnxruntime as ort
import sounddevice as sd


# ---------- 设备选择 ----------
def resolve_device(spec, kind="input"):
    """把配置里的设备描述解析成 sounddevice 设备号。

    空字符串 -> None（= 跟随系统默认设备）
    纯数字   -> 该序号
    其它     -> 按名称片段匹配（命中多个时优先 WASAPI，延迟更低）
    """
    if spec is None:
        return None
    spec = str(spec).strip()
    if not spec:
        return None
    if spec.isdigit():
        return int(spec)
    want = spec.lower()
    best = None
    try:
        devs = sd.query_devices()
        hostapis = sd.query_hostapis()
    except Exception:
        return None
    for idx, dev in enumerate(devs):
        ch = dev["max_input_channels"] if kind == "input" else dev["max_output_channels"]
        if ch <= 0:
            continue
        if want not in dev["name"].lower():
            continue
        try:
            api_name = hostapis[dev["hostapi"]]["name"]
        except Exception:
            api_name = ""
        if "WASAPI" in api_name:
            return idx
        if best is None:
            best = idx
    return best


def describe_device(idx, kind="input"):
    """返回人类可读的设备描述。idx=None 表示系统默认。"""
    try:
        dev = sd.query_devices(idx, kind=kind) if idx is not None else sd.query_devices(kind=kind)
        api = sd.query_hostapis(dev["hostapi"])["name"]
        return "[%d] %s (%s)" % (dev["index"], dev["name"], api)
    except Exception as e:
        return "系统默认（查询失败: %s）" % e


# ---------- silero VAD ----------
class SileroVAD:
    """silero-vad ONNX 封装（v5 接口：input/state/sr -> output/stateN）。

    注意：state 是循环状态，每段录音开始前必须 reset()。
    """

    def __init__(self, model_path, sample_rate=16000):
        self.sess = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])
        self.sample_rate = sample_rate
        self.state = np.zeros((2, 1, 128), dtype=np.float32)
        self._sr = np.array(sample_rate, dtype=np.int64)

    def reset(self):
        self.state = np.zeros((2, 1, 128), dtype=np.float32)

    def prob(self, frame):
        x = np.asarray(frame, dtype=np.float32).reshape(1, -1)
        out, self.state = self.sess.run(None, {"input": x, "state": self.state, "sr": self._sr})
        return float(out[0][0])


# ---------- 触发门限 ----------
class Gate:
    """一次录音的触发门限。

    vad / peak        : 普通阈值
    frames            : 连续多少帧达标才算触发
    peak_ratio        : 峰值还需高于自适应回声基线这么多倍（0 = 不启用）
    arm_ms            : 开始后多久才武装触发（这段时间用于测回声基线）
    """

    def __init__(self, vad, peak, frames=3, peak_ratio=0.0, arm_ms=0):
        self.vad = float(vad)
        self.peak = float(peak)
        self.frames = int(frames)
        self.peak_ratio = float(peak_ratio)
        self.arm_ms = float(arm_ms)


def normal_gate(cfg):
    return Gate(cfg.vad_threshold, cfg.peak_floor, cfg.trigger_frames)


def barge_in_gate(cfg):
    return Gate(cfg.barge_in_vad, cfg.barge_in_peak, cfg.barge_in_frames,
                cfg.barge_in_peak_ratio, cfg.barge_in_arm_ms)


# ---------- 常开麦克风流 ----------
class _Frame(object):
    __slots__ = ("data", "prob", "peak", "ts")

    def __init__(self, data, prob, peak, ts):
        self.data = data
        self.prob = prob
        self.peak = peak
        self.ts = ts


class MicStream:
    """常开麦克风输入流：单路 InputStream，帧派发给当前 sink。

    同一时刻只应有一个 sink（录音 或 打断检测），由上层保证。
    VAD 推理放在音频回调里完成（512 样本实测 <1ms），
    这样上层拿到的帧已经带好概率，不必二次计算。
    """

    def __init__(self, cfg, vad, log=print):
        self.cfg = cfg
        self.vad = vad
        self.log = log
        self._sink = None
        self._lock = threading.Lock()
        self._stream = None
        self.overflows = 0
        self.frames_seen = 0

    def set_sink(self, fn):
        """fn(frame) 或 None。返回旧的 sink。"""
        with self._lock:
            old = self._sink
            self._sink = fn
        return old

    def reset_vad(self):
        """重置 silero 循环状态。每段录音开始前必须调用，否则会串上一段的上下文。"""
        try:
            self.vad.reset()
        except Exception:
            pass

    def start(self, device=None):
        if self._stream is not None:
            return
        self._stream = sd.InputStream(
            samplerate=self.cfg.sample_rate,
            channels=1,
            dtype="float32",
            blocksize=self.cfg.frame_samples,
            device=device,
            callback=self._callback,
        )
        self._stream.start()

    def stop(self):
        s, self._stream = self._stream, None
        if s is not None:
            try:
                s.stop()
                s.close()
            except Exception:
                pass

    def _callback(self, indata, frame_count, time_info, status):
        if status:
            self.overflows += 1
        mono = indata[:, 0]
        with self._lock:
            sink = self._sink
        if sink is None:
            return
        try:
            p = self.vad.prob(mono)
        except Exception:
            return
        self.frames_seen += 1
        sink(_Frame(mono.copy(), p, float(np.abs(mono).max()), time.monotonic()))


# ---------- 单句录音 ----------
def record_utterance(mic, cfg, *, gate=None, stop_event=None, on_trigger=None,
                     max_seconds=None, idle_timeout=None, label="VAD", log=print):
    """从 mic 捕获一句完整语音。

    mic          : MicStream（需要 set_sink；有 reset_vad 时会自动重置 VAD 状态）
    gate         : 触发门限，默认普通门限；播放期间用 barge_in_gate(cfg)
    stop_event   : 置位后若还没触发就立即返回 None（用于"播放结束就不再等"）
    on_trigger   : 确认说话开始时的回调（用于立刻打断 TTS），在音频回调线程里执行，
                   必须极快且不能阻塞——只做置标志/停播放这类动作
    max_seconds  : 覆盖 cfg.max_utterance_s
    idle_timeout : 覆盖 cfg.idle_timeout_s

    返回 float32 单声道一维数组（16k），未捕获到有效语音返回 None。
    """
    gate = gate or normal_gate(cfg)
    sr = cfg.sample_rate
    frame_ms = 1000.0 * cfg.frame_samples / sr
    max_frames = int((max_seconds or cfg.max_utterance_s) * 1000 / frame_ms)
    idle_frames = int((idle_timeout or cfg.idle_timeout_s) * 1000 / frame_ms)
    pre_pad_frames = max(1, int(cfg.pre_pad_ms / frame_ms))
    endpoint_frames = max(1, int(cfg.endpoint_silence_ms / frame_ms))
    min_voiced_frames = max(1, int(cfg.min_speech_ms / frame_ms))

    st = {
        "triggered": False,
        "trigger_run": 0,
        "silent_run": 0,
        "voiced": 0,
        "seen": 0,
        "t_start": 0.0,
        "peak_base": 0.0,
        "base_n": 0,
        "last_prob": 0.0,
        "last_peak": 0.0,
        "done": threading.Event(),
    }
    frames = []
    ring = deque(maxlen=pre_pad_frames)
    t0 = time.monotonic()

    def _end():
        st["done"].set()

    def _sink(fr):
        if st["done"].is_set():
            return
        if not st["triggered"]:
            # 未触发阶段：超时/被叫停就直接放弃
            if stop_event is not None and stop_event.is_set():
                _end()
                return
            if st["seen"] >= idle_frames or (time.monotonic() - t0) > (idle_timeout or cfg.idle_timeout_s):
                _end()
                return
            st["seen"] += 1
            # 武装期：只采回声基线，不判定触发
            if (time.monotonic() - t0) * 1000 < gate.arm_ms:
                st["peak_base"] += fr.peak
                st["base_n"] += 1
                return
            ring.append(fr.data)
            st["last_prob"] = fr.prob
            st["last_peak"] = fr.peak
            thr = gate.peak
            if gate.peak_ratio > 0 and st["base_n"] > 0:
                thr = max(thr, (st["peak_base"] / st["base_n"]) * gate.peak_ratio)
            if fr.prob >= gate.vad and fr.peak >= thr:
                st["trigger_run"] += 1
            else:
                st["trigger_run"] = 0
            if st["trigger_run"] >= gate.frames:
                st["triggered"] = True
                st["t_start"] = time.monotonic()
                frames.extend(list(ring))
                if on_trigger is not None:
                    try:
                        on_trigger()
                    except Exception:
                        pass
                log("[%s] 检测到说话开始 (peak=%.3f vad=%.2f)" % (label, fr.peak, fr.prob))
            return

        # 已触发：收帧 + 迟滞判定结束
        frames.append(fr.data)
        st["last_prob"] = fr.prob
        st["last_peak"] = fr.peak
        quiet = fr.prob < cfg.vad_neg_threshold and fr.peak < cfg.peak_floor
        if quiet:
            st["silent_run"] += 1
        else:
            st["silent_run"] = 0
            st["voiced"] += 1
        if st["silent_run"] >= endpoint_frames:
            _end()
        elif len(frames) >= max_frames:
            log("[%s] 达到最长录音时长 %.0fs，强制结束" % (label, max_seconds or cfg.max_utterance_s))
            _end()

    # 先重置 VAD 循环状态，再挂 sink（避免用上一段的残留状态判首帧）
    reset = getattr(mic, "reset_vad", None)
    if callable(reset):
        reset()
    old_sink = mic.set_sink(_sink)
    try:
        while not st["done"].is_set():
            if st["done"].wait(0.05):
                break
            if stop_event is not None and stop_event.is_set() and not st["triggered"]:
                break
    finally:
        mic.set_sink(old_sink)

    if not st["triggered"] or not frames:
        return None

    audio = np.concatenate(frames).astype(np.float32)
    dur = len(audio) / float(sr)
    if st["voiced"] < min_voiced_frames:
        log("[%s] 有效语音过短(%.2fs)，丢弃" % (label, dur))
        return None
    rms = float(np.sqrt(np.mean(audio ** 2))) if len(audio) else 0.0
    if rms < cfg.min_rms:
        log("[%s] 整句音量过低(rms=%.4f)，丢弃" % (label, rms))
        return None
    log("[%s] 录音结束 %.2fs (voiced=%.2fs rms=%.4f)" % (label, dur, st["voiced"] * frame_ms / 1000.0, rms))
    return audio
