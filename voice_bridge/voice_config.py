# -*- coding: utf-8 -*-
"""小紫语音助手 - 统一配置层

配置来源（优先级从低到高）：
  1. 本文件的 DEFAULTS
  2. conf/voice_config.properties（可选，缺失即全用默认）
  3. 环境变量 VOICE_<KEY>（大写），例如 VOICE_BARGE_IN=0

用法：
  from voice_config import load_config
  cfg = load_config()
  cfg.stt_model, cfg.barge_in, cfg.endpoint_silence_ms ...
"""
import os
import time

BASE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(BASE)              # 语音桥上级 = 项目根目录
CONF_DIR = os.path.join(PROJECT_ROOT, "conf")
VOICE_CONF = os.path.join(CONF_DIR, "voice_config.properties")
API_FILE = os.path.join(CONF_DIR, "ai_config.properties")     # 与 Java 端共用
PROMPT_FILE = os.path.join(CONF_DIR, "character_prompt.txt")  # 人设

# 键名 -> 默认值（默认值的类型即该键的类型）
DEFAULTS = {
    # ---- 音频采集 ----
    "sample_rate": 16000,             # 采样率，silero/whisper 均按 16k
    "frame_samples": 512,             # 每帧样本数（32ms @16k）
    "input_device": "",               # 空 = 跟随系统默认；可填设备序号或名称片段（如 WASAPI）
    "output_device": "",              # 空 = 跟随系统默认；可填设备序号或名称片段

    # ---- VAD 端点检测 ----
    "vad_threshold": 0.5,             # 判定"有语音"的概率阈值
    "vad_neg_threshold": 0.30,        # 判定"静音"的概率阈值（低于 vad_threshold，形成迟滞）
    "trigger_frames": 3,              # 连续多少帧达标才算说话开始
    "peak_floor": 0.030,              # 峰值下限：低于此值一律视为底噪
    "endpoint_silence_ms": 650,       # 静音多久判定说完（越小越快，越大越不容易被抢话）
    "min_speech_ms": 350,             # 小于此长度的语音丢弃（多半是咳嗽/敲键盘）
    "pre_pad_ms": 250,                # 触发前保留的前垫，防止吞掉首字
    "max_utterance_s": 20,            # 单句最长录音时长
    "idle_timeout_s": 30,             # 空闲多久没说话就重新开始监听（避免死等）
    "min_rms": 0.0035,                # 整句 RMS 低于此值直接丢弃，不进 STT（防幻觉、省时间）

    # ---- STT（faster-whisper）----
    "stt_model": "small",             # small / medium / large-v3-turbo
    "stt_device": "cpu",              # cpu / cuda
    "stt_compute_type": "int8",       # int8 / int8_float16 / float16
    "stt_language": "zh",
    "stt_beam_size": 1,
    "stt_initial_prompt": "以下是普通话的句子。",
    "stt_hotwords": "薇薇安，班希，新艾利都，反舌鸟，怪盗",  # 热词：人名/术语，降低专有名词错误
    "stt_vad_filter": True,           # 交给 faster-whisper 内置 silero 再清一遍静音（防幻觉）

    # ---- LLM ----
    "llm_base_url": "https://api.deepseek.com/v1",
    "llm_model": "deepseek-chat",
    "llm_max_tokens": 500,
    "llm_temperature": 0.8,
    "llm_history_turns": 20,
    "mood_instruction": ("在你的回复开头，用 [MOOD:xxx] 单独占一行标记语气情绪，"
                         "可选值：happy/thinking/surprised/sad/angry/farewell/default。"
                         "其余内容才是你对我说的话，情绪尽力自然贴合内容。"),

    # ---- TTS ----
    "tts_engine": "auto",             # auto（vva 优先，失败回退 edge）/ vva / edge
    "tts_voice": "zh-CN-XiaoxiaoNeural",
    "tts_rate": "+0%",                # Edge-TTS 语速，如 +15%
    "tts_volume": "+0%",
    "tts_first_soft_chars": 12,       # 首句：攒够这么多字就允许在逗号处切分（降低首音延迟）
    "tts_max_sentence_chars": 45,     # 单句上限，超出则在逗号/硬切，避免单次合成过慢
    "tts_min_sentence_chars": 2,      # 小于此长度的片段不单独合成
    "vva_host": "127.0.0.1",
    "vva_port": 9876,
    "vva_ref": "ref0_3s_5s.wav",
    "vva_reftext": "会有点累吗？需要休息一下吗？渴吗？饿吗？需要吃点东西。",
    "vva_gpt": "vva-e15.ckpt",
    "vva_sovits": "vva_e8_s64.pth",
    "vva_disabled": False,
    "vva_autostart": True,            # 服务未就绪时自动拉起 tts_ui.py
    'vva_python': os.environ.get('VOICE_VVA_PYTHON', ''),
    'vva_ui': os.environ.get('VOICE_VVA_UI', ''),
    "vva_startup_timeout_s": 60,

    # ---- 打断（barge-in）----
    "barge_in": True,                 # 播放语音时是否允许用户插话打断
    "barge_in_vad": 0.65,             # 打断判定用的概率阈值（高于普通阈值，抗回声）
    "barge_in_peak": 0.080,           # 打断判定用的峰值阈值
    "barge_in_frames": 4,             # 打断需连续达标帧数（约 128ms，抗瞬时噪声）
    "barge_in_peak_ratio": 2.5,       # 峰值还需高于"回声基线"这么多倍（自适应外放回授）
    "barge_in_arm_ms": 180,           # 播放开始后多久才武装打断（跳过起播爆音）

    # ---- 桌宠桥接 ----
    "bubble_host": "127.0.0.1",
    "bubble_port": 8800,
    "bubble_timeout_s": 2,

    # ---- 运行开关 ----
    "startup_announce": "语音已启动",
    "announce_on_first_speech": True, # 首次识别到有效输入后再播启动提示（避免启动即外放）
    "log_latency": True,              # 每轮打印分阶段耗时
}


class Config:
    """类型跟随默认值的只读配置对象。"""

    def __init__(self, values=None):
        self._v = dict(DEFAULTS)
        if values:
            self._v.update(values)

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        v = self._v
        if name not in v:
            raise AttributeError("未知配置项: %s" % name)
        default = DEFAULTS.get(name)
        raw = v[name]
        try:
            if isinstance(default, bool):
                return _as_bool(raw)
            if isinstance(default, int):
                return int(float(raw))
            if isinstance(default, float):
                return float(raw)
            return str(raw)
        except (TypeError, ValueError):
            return default

    def as_dict(self):
        return {k: getattr(self, k) for k in DEFAULTS}

    def set(self, key, value):
        """运行时覆盖某一项（用于命令行开关）。"""
        if key not in DEFAULTS:
            raise KeyError(key)
        self._v[key] = value
        return self


def _as_bool(raw):
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in ("1", "true", "yes", "on", "y", "是", "开")


def _parse_properties(path):
    out = {}
    if not os.path.exists(path):
        return out
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or line.startswith("//") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            out[key.strip()] = val.strip()
    return out


def load_config(path=None):
    """读取 properties + 环境变量覆盖，返回 Config。"""
    values = _parse_properties(path or VOICE_CONF)
    for key in DEFAULTS:
        env = os.environ.get("VOICE_" + key.upper())
        if env is not None and env != "":
            values[key] = env
    return Config(values)


# ---------- 耗时埋点 ----------
class StageTimer:
    """记录一轮对话各阶段耗时，最后输出一行汇总。"""

    def __init__(self):
        self.t0 = time.monotonic()
        self.marks = []

    def mark(self, name):
        self.marks.append((name, time.monotonic()))
        return self.marks[-1][1]

    def since(self, name):
        for n, t in self.marks:
            if n == name:
                return time.monotonic() - t
        return 0.0

    def summary(self):
        if not self.marks:
            return ""
        parts = []
        prev_t = self.t0
        for name, t in self.marks:
            parts.append("%s %.2fs" % (name, t - prev_t))
            prev_t = t
        parts.append("合计 %.2fs" % (self.marks[-1][1] - self.t0))
        return " | ".join(parts)
