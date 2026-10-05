# -*- coding: utf-8 -*-
"""STT：faster-whisper 封装（本地、离线、反幻觉）

相对旧版的改进：
  1. 关掉 condition_on_previous_text —— 上一句的错误会滚雪球，且拖慢解码；
  2. temperature=0 单温解码，不做 6 档回退（速度翻倍，结果更稳）；
  3. 用 faster-whisper 自带的 silero 静音过滤 + 逐段 no_speech_prob/avg_logprob 过滤，
     把 Whisper 在静音/噪声上"凭空造句"（谢谢观看、字幕由…提供）挡在门外；
  4. 常见幻觉句式黑名单兜底；
  5. 热词 initial_prompt（人名/术语），显著降低专有名词错误。
"""
import os
import time

import numpy as np

from voice_config import BASE

# 已知的 Whisper 幻觉句式：命中且音频较短时直接丢弃
_HALLUCINATION_MARKERS = (
    "谢谢观看", "感谢观看", "谢谢大家观看", "感謝觀看", "多謝觀看",
    "字幕由", "字幕组", "字幕組", "由 Amara", "amara.org",
    "请不吝点赞", "請不吝點贊", "訂閱", "订阅", "点赞订阅",
    "MING PAO", "明报", "明報",
    "thanks for watching", "thank you for watching", "subscribe",
    "www.", ".com", ".cn",
    "ご視聴ありがとうございました", "시청해주셔서 감사합니다",
)


class STTEngine:
    def __init__(self, cfg, log=print):
        self.cfg = cfg
        self.log = log
        self._model = None
        self.last_info = None

    # ---------- 模型 ----------
    @property
    def ready(self):
        return self._model is not None

    def load(self):
        if self._model is not None:
            return self._model
        t = time.monotonic()
        from faster_whisper import WhisperModel
        self.log("[STT] 加载 faster-whisper %s (%s/%s)..."
                 % (self.cfg.stt_model, self.cfg.stt_device, self.cfg.stt_compute_type))
        kw = dict(device=self.cfg.stt_device, compute_type=self.cfg.stt_compute_type)
        # 关键：先只认本地缓存。否则 huggingface_hub 每次启动都联网校验更新，
        # 国内网络下这一项能卡 40 秒（实测 39.3s -> 0.9s）。
        try:
            self._model = WhisperModel(self.cfg.stt_model, local_files_only=True, **kw)
            self.log("[STT] 已就绪（本地缓存）%.1fs" % (time.monotonic() - t))
        except Exception as e:
            self.log("[STT] 本地未找到模型（%s），改为联网下载，首次可能较慢..." % type(e).__name__)
            try:
                self._model = WhisperModel(self.cfg.stt_model, **kw)
            except Exception as e2:
                raise RuntimeError(
                    "无法加载 Whisper 模型 %r：本地缓存缺失且联网下载失败（%s）。"
                    "请检查网络，或手动把模型放到 %s"
                    % (self.cfg.stt_model, e2,
                       os.path.join(os.path.expanduser("~"), ".cache", "huggingface", "hub")))
            self.log("[STT] 模型下载并加载完成 %.1fs" % (time.monotonic() - t))
        return self._model

    def warmup(self):
        """喂 1 秒静音预热，避免第一句真实语音承担初始化开销。"""
        try:
            self.load()
            t = time.monotonic()
            self._decode(np.zeros(self.cfg.sample_rate, dtype=np.float32))
            self.log("[STT] 预热完成 %.2fs" % (time.monotonic() - t))
        except Exception as e:
            self.log("[STT] 预热失败（忽略）:", e)

    # ---------- 识别 ----------
    def _initial_prompt(self):
        p = self.cfg.stt_initial_prompt.strip()
        hot = self.cfg.stt_hotwords.strip()
        if hot:
            p = (p + " " + hot).strip()
        return p or None

    def _decode(self, audio):
        model = self.load()
        return model.transcribe(
            audio,
            language=self.cfg.stt_language or None,
            task="transcribe",
            beam_size=self.cfg.stt_beam_size,
            best_of=self.cfg.stt_beam_size,
            temperature=0.0,
            condition_on_previous_text=False,   # 防错误滚雪球
            initial_prompt=self._initial_prompt(),
            without_timestamps=True,
            compression_ratio_threshold=2.4,
            log_prob_threshold=-1.0,
            no_speech_threshold=0.6,
            vad_filter=bool(self.cfg.stt_vad_filter),
            vad_parameters=dict(
                threshold=0.5,
                min_speech_duration_ms=250,
                min_silence_duration_ms=200,
                speech_pad_ms=120,
            ),
        )

    def transcribe(self, audio):
        """audio: float32 16k 单声道 -> 文本（已去幻觉、去空白）"""
        if audio is None or len(audio) == 0:
            return ""
        t0 = time.monotonic()
        segments, info = self._decode(audio)
        pieces = []
        dropped = 0
        for seg in segments:
            # 逐段过滤：静音段/低置信段直接丢弃
            if getattr(seg, "no_speech_prob", 0.0) > 0.75:
                dropped += 1
                continue
            if getattr(seg, "avg_logprob", 0.0) < -1.2:
                dropped += 1
                continue
            txt = (seg.text or "").strip()
            if txt:
                pieces.append(txt)
        text = "".join(pieces).strip()
        self.last_info = {
            "language": getattr(info, "language", ""),
            "lang_prob": getattr(info, "language_probability", 0.0),
            "duration": getattr(info, "duration", len(audio) / self.cfg.sample_rate),
            "dropped": dropped,
            "elapsed": time.monotonic() - t0,
        }
        if not text:
            return ""
        # 非中文之间补空格，中文之间不补
        text = _join_spacing(text)
        if _looks_like_hallucination(text, info, len(audio) / self.cfg.sample_rate):
            self.log("[STT] 命中幻觉句式，丢弃: %r" % text[:40])
            return ""
        if _only_punctuation(text):
            return ""
        return text


def _join_spacing(text):
    """Whisper 对英文会输出带空格的分段；这里只做首尾清理，保留其原有空格。"""
    return " ".join(text.split()) if text.isascii() else text


def _only_punctuation(text):
    for ch in text:
        if ch.isalnum() or "\u4e00" <= ch <= "\u9fff":
            return False
    return True


def _looks_like_hallucination(text, info, duration):
    low = text.lower()
    hit = any(m.lower() in low for m in _HALLUCINATION_MARKERS)
    if not hit:
        return False
    # 只有"短音频 + 低语种置信度"时才当作幻觉，避免误杀正常内容
    lang_prob = getattr(info, "language_probability", 1.0) or 0.0
    return duration < 12.0 and lang_prob < 0.9


if __name__ == "__main__":
    # 自检：用静音确认不会凭空造句
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    from voice_config import load_config
    cfg = load_config()
    eng = STTEngine(cfg)
    eng.warmup()
    for secs in (1.0, 3.0):
        silence = np.zeros(int(cfg.sample_rate * secs), dtype=np.float32)
        t = time.monotonic()
        out = eng.transcribe(silence)
        print("静音 %.0fs -> %r (%.2fs)" % (secs, out, time.monotonic() - t))
