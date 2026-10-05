# -*- coding: utf-8 -*-
"""TTS：分句 + 合成/播放双线程流水线 + 可打断

相对旧版的改进：
  1. 按句播放：LLM 出第一句就送去合成，不必等整段回复生成完
     —— 首音延迟从"整段生成+整段合成"降到"首句生成+首句合成"；
  2. 合成与播放分离到两个线程：播第 1 句的同时后台已在合成第 2 句，
     句间不再有静默等待；
  3. 可打断：interrupt() 立刻 sd.stop() 并清空队列，插话即时生效；
  4. 用轮次号（turn id）淘汰过期音频，打断后不会"补播"残留句子。

后端：vva（GPT-SoVITS 音色模型，HTTP :9876）优先，不可用回退 Edge-TTS。
"""
import asyncio
import json
import os
import queue
import socket
import subprocess
import threading
import time
import urllib.request

import numpy as np
import soundfile as sf

from vad_recorder import resolve_device

# ---------- 分句 ----------
_HARD_END = "。！？!?…；;\n"
_SOFT_END = "，,、：: "
_TRAILING_KEEP = "”』」）)】》"


def clean_for_tts(text):
    """去掉不适合朗读的标记：markdown、表情、URL、MOOD 标签。"""
    if not text:
        return ""
    import re
    s = re.sub(r"\[MOOD:[^\]]*\]", "", text, flags=re.I)
    s = re.sub(r"https?://\S+", "", s)
    s = re.sub(r"`{1,3}", "", s)
    s = re.sub(r"\*{1,3}", "", s)
    s = re.sub(r"^\s*[#>\-•]+\s*", "", s, flags=re.M)
    out = []
    for ch in s:
        o = ord(ch)
        if 0x1F000 <= o <= 0x1FAFF or 0x2600 <= o <= 0x27BF or 0x1F1E6 <= o <= 0x1F1FF or o == 0xFE0F:
            continue
        out.append(ch)
    s = "".join(out)
    s = re.sub(r"[ \t\u3000]+", " ", s)
    return s.strip()


class SentenceStreamer:
    """流式文本 -> 可朗读的完整句子。

    规则：
      - 遇到硬标点（。！？；…换行）立即成句（最快出首音）；
      - 单句超过 max_chars 还没等到硬标点，就退到最近的软标点（，、：）切分；
      - 首句额外放宽：攒够 first_soft_chars 字就允许在软标点处切分，
        让"其实啊，我觉得……"这种长句的头一段早点发出去。
    """

    def __init__(self, first_soft_chars=12, max_chars=45, min_chars=2):
        self.first_soft_chars = int(first_soft_chars)
        self.max_chars = int(max_chars)
        self.min_chars = int(min_chars)
        self.buf = ""
        self.emitted = 0

    def feed(self, delta):
        if delta:
            self.buf += delta
        out = []
        while True:
            idx = self._cut()
            if idx <= 0:
                break
            piece = self.buf[:idx].strip()
            self.buf = self.buf[idx:]
            piece = clean_for_tts(piece)
            if piece:
                out.append(piece)
                self.emitted += 1
        return out

    def flush(self):
        out = []
        rest = clean_for_tts(self.buf.strip())
        self.buf = ""
        if rest:
            out.append(rest)
            self.emitted += 1
        return out

    def _cut(self):
        buf = self.buf
        if not buf.strip():
            return -1
        limit = min(len(buf), self.max_chars)
        for i in range(limit):
            if buf[i] in _HARD_END and i + 1 >= self.min_chars:
                end = i + 1
                while end < len(buf) and buf[end] in _TRAILING_KEEP:
                    end += 1
                return end
        n = len(buf)
        if n >= self.max_chars:
            for i in range(self.max_chars - 1, self.min_chars - 1, -1):
                if buf[i] in _SOFT_END:
                    return i + 1
            return self.max_chars
        if self.emitted == 0 and n >= self.first_soft_chars:
            for i in range(n - 1, self.min_chars - 1, -1):
                if buf[i] in _SOFT_END:
                    return i + 1
        return -1


# ---------- vva（GPT-SoVITS）后端 ----------
_vva_proc = None
_vva_tried = False


def vva_available(cfg):
    if cfg.vva_disabled:
        return False
    try:
        with socket.create_connection((cfg.vva_host, cfg.vva_port), timeout=1):
            return True
    except Exception:
        return False


def ensure_vva_service(cfg, log=print):
    """服务未就绪则自动拉起 tts_ui.py；失败静默（回退 Edge-TTS）。"""
    global _vva_proc, _vva_tried
    if cfg.vva_disabled or not cfg.vva_autostart:
        return False
    if vva_available(cfg):
        return True
    if _vva_tried:
        return vva_available(cfg)
    _vva_tried = True
    if not os.path.exists(cfg.vva_python) or not os.path.exists(cfg.vva_ui):
        log("[TTS] vva 服务文件缺失，使用 Edge-TTS 回退")
        return False
    try:
        CREATE_NO_WINDOW = 0x08000000
        _vva_proc = subprocess.Popen(
            [cfg.vva_python, "-I", cfg.vva_ui],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )
        log("[TTS] 正在自动启动 vva 合成服务...")
        deadline = time.monotonic() + cfg.vva_startup_timeout_s
        while time.monotonic() < deadline:
            time.sleep(0.5)
            if vva_available(cfg):
                log("[TTS] vva 合成服务已就绪 (:%d)" % cfg.vva_port)
                return True
        log("[TTS] vva 服务启动超时，使用 Edge-TTS 回退")
        return False
    except Exception as e:
        log("[TTS] 自动启动 vva 服务失败:", e)
        return False


def synthesize_vva(cfg, text, out_path):
    """调用 vva HTTP 接口合成到 out_path；失败返回 None。"""
    url = "http://%s:%d/api/synth" % (cfg.vva_host, cfg.vva_port)
    body = json.dumps({
        "ref": cfg.vva_ref, "reftext": cfg.vva_reftext,
        "gpt": cfg.vva_gpt, "sovits": cfg.vva_sovits,
        "target": text,
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            res = json.loads(r.read().decode("utf-8"))
        if not res.get("ok"):
            return None
        audio_url = "http://%s:%d%s" % (cfg.vva_host, cfg.vva_port, res["file"].split("?")[0])
        with urllib.request.urlopen(audio_url, timeout=30) as r:
            data = r.read()
        with open(out_path, "wb") as f:
            f.write(data)
        return out_path
    except Exception:
        return None


# ---------- 播放器 ----------
class Speaker:
    """分句 TTS 流水线：say() 入队 -> 合成线程 -> 播放线程。"""

    def __init__(self, cfg, log=print, on_first_audio=None):
        self.cfg = cfg
        self.log = log
        self.on_first_audio = on_first_audio
        self.out_device = resolve_device(cfg.output_device, "output")
        self._text_q = queue.Queue()
        self._audio_q = queue.Queue()
        self._lock = threading.Lock()
        self._turn = 0
        self._interrupted_turn = None
        self._turn_end = threading.Event()
        self._turn_end.set()
        self._t_ref = time.monotonic()
        self._first_audio_t = None
        self._seq = 0
        self._loop = None
        self._speaking = threading.Event()
        self._engine_logged = False
        self._sweep_temp()
        for fn in (self._synth_loop, self._play_loop):
            threading.Thread(target=fn, daemon=True).start()

    def _sweep_temp(self):
        """清掉上次异常退出遗留的临时音频（_tts_*.wav / .mp3）。"""
        import glob
        n = 0
        for pat in ("_tts_*.wav", "_tts_*.mp3"):
            for f in glob.glob(os.path.join(BASE, pat)):
                self._cleanup(f)
                n += 1
        if n:
            self.log("[TTS] 清理残留临时音频 %d 个" % n)

    # ---- 对外 ----
    def begin_turn(self, t_ref=None):
        with self._lock:
            self._turn += 1
            self._interrupted_turn = None
            self._first_audio_t = None
            self._t_ref = t_ref if t_ref is not None else time.monotonic()
            self._turn_end.clear()
            return self._turn

    def say(self, text):
        clean = clean_for_tts(text)
        if not clean:
            return
        with self._lock:
            if self._interrupted_turn == self._turn:
                return
            turn = self._turn
        self._text_q.put((turn, clean))

    def end_turn(self, timeout=180):
        with self._lock:
            turn = self._turn
        self._text_q.put((turn, None))
        self._turn_end.wait(timeout)
        interrupted = self.turn_interrupted
        self._turn_end.clear()
        return not interrupted

    def interrupt(self):
        """立刻停止播放并清空待播句子（插话打断）。"""
        with self._lock:
            self._interrupted_turn = self._turn
        try:
            import sounddevice as sd
            sd.stop()
        except Exception:
            pass
        self._turn_end.set()
        self._drain()

    @property
    def turn_interrupted(self):
        with self._lock:
            return self._interrupted_turn == self._turn

    @property
    def first_audio_latency(self):
        with self._lock:
            if self._first_audio_t is None:
                return None
            return self._first_audio_t - self._t_ref

    @property
    def speaking(self):
        return self._speaking.is_set()

    # ---- 内部 ----
    def _stale(self, turn):
        with self._lock:
            return turn != self._turn or self._interrupted_turn == turn

    def _tmp(self, ext):
        self._seq += 1
        return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "_tts_%d_%d.%s" % (os.getpid(), self._seq, ext))

    def _cleanup(self, path):
        if path:
            try:
                os.remove(path)
            except OSError:
                pass

    def _drain(self):
        for q in (self._text_q, self._audio_q):
            while True:
                try:
                    item = q.get_nowait()
                except queue.Empty:
                    break
                if q is self._audio_q and item[1]:
                    self._cleanup(item[1])

    def _synthesize(self, text):
        eng = self.cfg.tts_engine
        if eng in ("auto", "vva") and vva_available(self.cfg):
            path = synthesize_vva(self.cfg, text, self._tmp("wav"))
            if path:
                if not self._engine_logged:
                    self.log("[TTS] 使用 vva 音色模型")
                    self._engine_logged = True
                return path
            if eng == "vva":
                return None
            self.log("[TTS] vva 合成失败，本句回退 Edge-TTS")
        if eng not in ("auto", "vva", "edge"):
            self.log("[TTS] 未知 tts_engine=%r，按 edge 处理" % eng)
        return self._synthesize_edge(text)

    def _synthesize_edge(self, text):
        import edge_tts
        path = self._tmp("mp3")
        if self._loop is None:
            self._loop = asyncio.new_event_loop()
        if not self._engine_logged:
            self.log("[TTS] 使用 Edge-TTS (%s)" % self.cfg.tts_voice)
            self._engine_logged = True

        async def _go():
            kw = {"rate": self.cfg.tts_rate}
            if self.cfg.tts_volume:
                kw["volume"] = self.cfg.tts_volume
            c = edge_tts.Communicate(text, self.cfg.tts_voice, **kw)
            await c.save(path)

        self._loop.run_until_complete(_go())
        return path

    def _synth_loop(self):
        while True:
            turn, text = self._text_q.get()
            if text is None:
                self._audio_q.put((turn, None))
                continue
            if self._stale(turn):
                continue
            t0 = time.monotonic()
            try:
                path = self._synthesize(text)
            except Exception as e:
                self.log("[TTS] 合成异常:", e)
                path = None
            if path is None:
                continue
            if self._stale(turn):
                self._cleanup(path)
                continue
            self.log("[TTS] 合成 %.2fs: %s" % (time.monotonic() - t0, text[:24]))
            self._audio_q.put((turn, path))

    def _play_loop(self):
        import sounddevice as sd
        while True:
            turn, path = self._audio_q.get()
            if path is None:
                if not self._stale(turn):
                    self._turn_end.set()
                continue
            if self._stale(turn):
                self._cleanup(path)
                continue
            try:
                data, sr = sf.read(path, dtype="float32")
                with self._lock:
                    if self._first_audio_t is None:
                        self._first_audio_t = time.monotonic()
                        fire = self.on_first_audio
                    else:
                        fire = None
                if fire:
                    try:
                        fire(self._first_audio_t - self._t_ref)
                    except Exception:
                        pass
                self._speaking.set()
                sd.play(np.asarray(data, dtype=np.float32), sr, device=self.out_device)
                sd.wait()
            except Exception as e:
                self.log("[TTS] 播放异常:", e)
            finally:
                self._speaking.clear()
                self._cleanup(path)


# ---------- 自检 ----------
if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    from voice_config import load_config

    print("== 分句自检 ==")
    ss = SentenceStreamer(12, 45, 2)
    demo = "嗯……让我想想。其实啊，这件事我觉得可以换个思路，先看看日志再动手，你说呢？"
    got = []
    for ch in demo:                      # 模拟逐字流式到达
        got += ss.feed(ch)
    got += ss.flush()
    for i, s in enumerate(got, 1):
        print("  句%d: %r" % (i, s))

    cfg = load_config()
    print("\n== 合成自检 ==")
    spk = Speaker(cfg)
    spk.begin_turn()
    spk.say("这是一句测试语音，用于确认播放链路正常。")
    ok = spk.end_turn()
    print("播放完成, interrupted=%s, 首音延迟=%s" % (not ok, spk.first_audio_latency))
