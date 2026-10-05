# -*- coding: utf-8 -*-
"""小紫语音助手 —— 语音输入输出闭环（流式 + 可打断）

链路：
    麦克风(常开单路) → VAD(silero) 端点检测 → STT(faster-whisper, 本地离线)
    → LLM(DeepSeek, 流式) → 句子级 TTS(vva 音色 / Edge 回退, 边合成边播)
    → 桥接 ChatBubble（Java 桌宠显示文字 + 情绪动作）

相比旧版的关键改进：
    1. 端点检测改迟滞判定，修掉"句内换气被当成说完"导致的半句截断；
    2. LLM 改流式 + 句子级 TTS：不等整段生成完，首音明显提前；
    3. 合成与播放分线程流水线，句间不再空等；
    4. 播放期间继续监听：用户一开口立刻停播，并直接接住这句话；
    5. STT 反幻觉（静音过滤 + 逐段置信度 + 幻觉句式黑名单）；
    6. 每轮打印分阶段耗时，便于定位瓶颈。

常用命令行：
    python voice_assistant.py                  # 正常语音模式
    python voice_assistant.py --text           # 文本模式（不录音，验证 LLM+TTS 链路）
    python voice_assistant.py --no-tts         # 只识别与生成，不发声
    python voice_assistant.py --no-barge-in    # 关闭插话打断（外放回声大时用）
    python voice_assistant.py --once           # 只处理一轮后退出
    python voice_assistant.py --list-devices   # 列出音频设备
    python doctor.py                           # 环境自检
"""
import argparse
import json
import os
import re
import socket
import sys
import threading
import time

from openai import OpenAI

from memory_db import MemoryDB
from stt_engine import STTEngine
from tts_engine import SentenceStreamer, Speaker, clean_for_tts, ensure_vva_service
from vad_recorder import (MicStream, SileroVAD, barge_in_gate, describe_device,
                          normal_gate, record_utterance, resolve_device)
from voice_config import (API_FILE, BASE, CONF_DIR, PROMPT_FILE, StageTimer,
                          load_config)

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

VAD_MODEL = os.path.join(BASE, "silero_vad.onnx")

MOODS = ("happy", "thinking", "surprised", "sad", "angry", "farewell", "default")


# ---------- 配置读取 ----------
def load_prop(key, path=API_FILE):
    """从 properties 文件读单个键（UTF-8）。"""
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith(key + "="):
                return line[len(key) + 1:].strip()
    return ""


def load_prompt():
    if os.path.exists(PROMPT_FILE):
        with open(PROMPT_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    return "你是一只可爱的桌面精灵少女，住在用户的电脑桌面。你叫\"小紫\"。性格活泼温柔。回复简短可爱。"


# ---------- 桥接 ----------
def send_to_bubble(text, mood="", cfg=None):
    """把回复文本+情绪发给 Java 桌宠的桥接监听，触发 ChatBubble + 情绪动作。

    协议保持不变（127.0.0.1:8800，JSON {"text","mood"}），Java 端无需改动。
    """
    host = cfg.bubble_host if cfg else "127.0.0.1"
    port = cfg.bubble_port if cfg else 8800
    timeout = cfg.bubble_timeout_s if cfg else 2
    try:
        payload = json.dumps({"text": text, "mood": mood}, ensure_ascii=False).encode("utf-8")
        s = socket.create_connection((host, port), timeout=timeout)
        try:
            s.sendall(payload)
        finally:
            s.close()
        return True
    except Exception:
        return False


# ---------- 大脑 ----------
class Brain:
    """LLM 会话 + 长期记忆。stream_reply() 边收边切句，供 TTS 流水线消费。"""

    def __init__(self, cfg, api_key, log=print, memory=None):
        self.cfg = cfg
        self.log = log
        self.client = OpenAI(base_url=cfg.llm_base_url, api_key=api_key)
        self.prompt = load_prompt()
        self.history = []
        self.memory = memory if memory is not None else MemoryDB()
        try:
            st = self.memory.stats()
            log("[Mem] 记忆库已加载：%d 条事实 / %d 条对话" % (st["facts"], st["conv"]))
        except Exception as e:
            log("[Mem] 记忆库初始化失败（继续运行）:", e)

    def _messages(self, user_text):
        try:
            system = self.memory.build_prompt(self.prompt)
        except Exception as e:
            self.log("[Mem] build_prompt 失败，用纯人设:", e)
            system = self.prompt
        msgs = [{"role": "system", "content": system},
                {"role": "system", "content": self.cfg.mood_instruction}]
        msgs += self.history[-self.cfg.llm_history_turns:]
        msgs.append({"role": "user", "content": user_text})
        return msgs

    def stream_reply(self, user_text):
        """流式生成。yield 事件：

            ("mood", mood)          解析到情绪标签（可能没有）
            ("sentence", text)      可以立刻送 TTS 的完整句子
            ("done", (reply, mood)) 全文收尾
        """
        streamer = SentenceStreamer(self.cfg.tts_first_soft_chars,
                                    self.cfg.tts_max_sentence_chars,
                                    self.cfg.tts_min_sentence_chars)
        stream = self.client.chat.completions.create(
            model=self.cfg.llm_model,
            max_tokens=self.cfg.llm_max_tokens,
            temperature=self.cfg.llm_temperature,
            messages=self._messages(user_text),
            stream=True,
        )
        raw_parts = []
        mood = "default"
        mood_resolved = False
        head = ""

        for chunk in stream:
            if not getattr(chunk, "choices", None):
                continue
            delta = chunk.choices[0].delta
            piece = getattr(delta, "content", None)
            if not piece:
                continue
            raw_parts.append(piece)

            if mood_resolved:
                for s in streamer.feed(piece):
                    yield ("sentence", s)
                continue

            # 开头可能是 [MOOD:xxx]，且可能被拆成多个 chunk 送达
            head += piece
            m = re.match(r"\s*\[MOOD:(\w+)\]\s*", head, re.I)
            if m:
                mood = m.group(1).lower()
                if mood not in MOODS:
                    mood = "default"
                mood_resolved = True
                yield ("mood", mood)
                rest = head[m.end():]
                head = ""
                for s in streamer.feed(rest):
                    yield ("sentence", s)
                continue
            if len(head) > 40 or "\n" in head:
                # 开头不像情绪标签 → 按正文处理，避免吞掉内容
                mood_resolved = True
                for s in streamer.feed(head):
                    yield ("sentence", s)
                head = ""

        if not mood_resolved and head:
            for s in streamer.feed(head):
                yield ("sentence", s)
        for s in streamer.flush():
            yield ("sentence", s)

        raw = "".join(raw_parts).strip()
        reply = re.sub(r"^\s*\[MOOD:\w+\]\s*", "", raw, flags=re.I).strip() or raw
        if mood == "default":
            m2 = re.match(r"\s*\[MOOD:(\w+)\]", raw, re.I)
            if m2 and m2.group(1).lower() in MOODS:
                mood = m2.group(1).lower()

        self.history.append({"role": "user", "content": user_text})
        self.history.append({"role": "assistant", "content": reply})
        try:
            self.memory.record_turn(user_text, reply)
            self._export_memory_summary()
        except Exception as e:
            self.log("[Mem] 记录对话失败:", e)
        yield ("done", (reply, mood))

    def _export_memory_summary(self):
        """导出记忆摘要到 conf/memory_summary.txt，供 Java 端打字聊天读取。"""
        try:
            summary = self.memory.build_prompt("", inject_time=True, max_facts=8, max_recent=0)
            summary = summary.replace("[额外背景]", "小紫记得：")
            with open(os.path.join(CONF_DIR, "memory_summary.txt"), "w", encoding="utf-8") as f:
                f.write(summary)
        except Exception as e:
            self.log("[Mem] 导出摘要失败:", e)


# ---------- 不发声的假播放器（--no-tts）----------
class NullSpeaker:
    out_device = None
    turn_interrupted = False
    first_audio_latency = None

    def __init__(self, log=print):
        self.log = log

    def begin_turn(self, t_ref=None):
        return 0

    def say(self, text):
        t = clean_for_tts(text)
        if t:
            self.log("[TTS-跳过] " + t)

    def end_turn(self, timeout=0):
        return True

    def interrupt(self):
        pass


# ---------- 会话 ----------
class Session:
    def __init__(self, cfg, log=print):
        self.cfg = cfg
        self.log = log
        self.mic = None
        self.vad = None
        self.stt = None
        self.speaker = None
        self.brain = None
        self._first_audio_lat = None

    # ---- 启动 ----
    def start(self, need_mic=True, need_tts=True):
        cfg = self.cfg
        self.vad = SileroVAD(VAD_MODEL, cfg.sample_rate)
        if need_mic:
            dev = resolve_device(cfg.input_device, "input")
            self.mic = MicStream(cfg, self.vad, log=self.log)
            self.mic.start(device=dev)
            self.log("[VAD] 输入设备: %s" % describe_device(dev, "input"))
        self.stt = STTEngine(cfg, log=self.log)
        self.stt.load()
        self.stt.warmup()
        if need_tts:
            try:
                ensure_vva_service(cfg, log=self.log)
            except Exception as e:
                self.log("[TTS] vva 服务检查异常（忽略）:", e)
            self.speaker = Speaker(cfg, log=self.log, on_first_audio=self._on_first_audio)
            self.log("[TTS] 输出设备: %s" % describe_device(self.speaker.out_device, "output"))
        else:
            self.speaker = NullSpeaker(log=self.log)
        key = load_prop("api_key")
        if not key:
            raise RuntimeError("未找到 api_key（conf/ai_config.properties）")
        self.brain = Brain(cfg, key, log=self.log)
        self.log("[Init] 人设已加载（%d 字符）" % len(self.brain.prompt))

    def close(self):
        if self.mic:
            self.mic.stop()

    def _on_first_audio(self, lat):
        self._first_audio_lat = lat

    def _announce(self):
        cfg = self.cfg
        if not cfg.startup_announce:
            return
        self.log("[Sound] 语音通道就绪提示")
        self.speaker.begin_turn()
        self.speaker.say(cfg.startup_announce)
        self.speaker.end_turn()

    def _on_barge_in(self):
        self.log("[BargeIn] 检测到插话，停止播放")
        self.speaker.interrupt()

    # ---- 生成 + 边出句边送 TTS ----
    def generate_and_speak(self, user_text, t_ref=None):
        cfg = self.cfg
        speaker = self.speaker
        speaker.begin_turn(t_ref=t_ref)
        self._first_audio_lat = None
        mood = "default"
        reply = ""
        first_sentence_at = None
        for kind, payload in self.brain.stream_reply(user_text):
            if kind == "mood":
                mood = payload
            elif kind == "sentence":
                if first_sentence_at is None:
                    first_sentence_at = time.monotonic()
                speaker.say(payload)
            elif kind == "done":
                reply, mood = payload
        if cfg.log_latency and first_sentence_at and t_ref:
            self.log("[Lat] 首句就绪 %.2fs" % (first_sentence_at - t_ref))
        ok = send_to_bubble(reply, mood, cfg)
        self.log("[LLM] 回复(%s): %s" % (mood, reply))
        self.log("[Bridge] 气泡: %s" % ("已发送" if ok else "未连接（桌宠未启动）"))
        return reply, mood

    # ---- 等播放结束，同时监听插话 ----
    def await_playback(self):
        """返回：被用户插话打断时捕获到的语音数组；正常播完返回 None。"""
        cfg = self.cfg
        speaker = self.speaker
        if not cfg.barge_in or self.mic is None:
            speaker.end_turn()
            return None

        done = threading.Event()

        def _waiter():
            speaker.end_turn()
            done.set()

        th = threading.Thread(target=_waiter, daemon=True)
        th.start()
        audio = record_utterance(
            self.mic, cfg,
            gate=barge_in_gate(cfg), stop_event=done,
            on_trigger=self._on_barge_in,
            label="BargeIn", log=self.log,
            max_seconds=cfg.max_utterance_s, idle_timeout=180,
        )
        th.join(timeout=5)
        if audio is not None:
            self.log("[BargeIn] 已打断，直接接住这句话")
        return audio

    # ---- 对话（可因插话连续多轮）----
    def converse(self, audio, text_mode=False, first_turn_hook=None):
        while True:
            if text_mode:
                try:
                    user_text = input("你说> ").strip()
                except (EOFError, KeyboardInterrupt):
                    return None
                if not user_text:
                    return None
                t_ref = time.monotonic()
                timer = None
            else:
                timer = StageTimer()
                timer.mark("说完")
                t_ref = time.monotonic()
                user_text = self.stt.transcribe(audio)
                timer.mark("STT")
                self.log("[STT] 识别文本: %s" % user_text)
                if not user_text:
                    return None
                if first_turn_hook:
                    first_turn_hook()
                    first_turn_hook = None

            self.generate_and_speak(user_text, t_ref=t_ref)

            if text_mode:
                self.speaker.end_turn()
                self._report_latency(None)
                continue

            timer.mark("回复就绪")
            next_audio = self.await_playback()
            timer.mark("播放结束")
            self._report_latency(timer)
            if next_audio is None:
                return None
            audio = next_audio

    def _report_latency(self, timer):
        if not self.cfg.log_latency:
            return
        parts = []
        if timer:
            parts.append(timer.summary())
        if self._first_audio_lat is not None:
            parts.append("端到端首音 %.2fs" % self._first_audio_lat)
        if parts:
            self.log("[Lat] " + " | ".join(parts))

    # ---- 主循环 ----
    def run(self, once=False):
        cfg = self.cfg
        hook = self._announce if cfg.startup_announce else None
        self.log("[Init] 开始语音循环，请对着麦克风说话（Ctrl+C 退出）")
        while True:
            audio = record_utterance(self.mic, cfg, gate=normal_gate(cfg),
                                     label="VAD", log=self.log,
                                     idle_timeout=cfg.idle_timeout_s)
            if audio is None:
                continue
            self.converse(audio, first_turn_hook=hook)
            hook = None
            if once:
                return


# ---------- 入口 ----------
def build_argparser():
    p = argparse.ArgumentParser(description="小紫语音助手（VAD → STT → LLM → TTS）")
    p.add_argument("--text", action="store_true", help="文本模式：键盘输入，验证 LLM+TTS 链路")
    p.add_argument("--no-tts", action="store_true", help="只识别与生成，不合成播放")
    p.add_argument("--no-barge-in", action="store_true", help="关闭插话打断")
    p.add_argument("--no-announce", action="store_true", help="不播启动提示音")
    p.add_argument("--once", action="store_true", help="只处理一轮后退出")
    p.add_argument("--device", default=None, help="覆盖输入设备（序号或名称片段，如 WASAPI）")
    p.add_argument("--list-devices", action="store_true", help="列出音频设备后退出")
    return p


def main():
    args = build_argparser().parse_args()
    cfg = load_config()
    if args.device:
        cfg.set("input_device", args.device)
    if args.no_barge_in:
        cfg.set("barge_in", False)
    if args.no_announce:
        cfg.set("startup_announce", "")

    if args.list_devices:
        import sounddevice as sd
        print(sd.query_devices())
        print("默认输入/输出:", sd.default.device)
        return

    session = Session(cfg)
    try:
        session.start(need_mic=not args.text, need_tts=not args.no_tts)
        if args.text:
            print("[模式] 文本模式：直接输入文字，回车发送，Ctrl+C 退出")
            session.converse(None, text_mode=True)
        else:
            session.run(once=args.once)
    except KeyboardInterrupt:
        print("\n[Exit] 语音助手已退出")
    finally:
        session.close()


if __name__ == "__main__":
    main()
