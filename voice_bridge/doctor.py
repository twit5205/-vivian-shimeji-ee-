# -*- coding: utf-8 -*-
"""小紫语音助手 —— 环境自检 / 阈值标定

用途：语音链路出问题时先跑它，一眼看出是哪一环坏了，并给出可调参数的建议值。

    python doctor.py                 # 基础自检（不联网、不出声、不录音）
    python doctor.py --net           # 追加：Edge-TTS 联网、DeepSeek 连通、STT 模型加载
    python doctor.py --tts           # 测量合成首音延迟（会出声）
    python doctor.py --mic 4         # 采集 4 秒麦克风，报告底噪并建议阈值
    python doctor.py --echo          # 播放一句测试语并测量外放回授（标定打断门限）
    python doctor.py --list          # 列出所有音频设备
"""
import argparse
import os
import socket
import sys
import threading
import time

sys.stdout.reconfigure(encoding="utf-8")

from voice_config import (API_FILE, BASE, CONF_DIR, PROJECT_ROOT, VOICE_CONF,
                          load_config)

VAD_MODEL = os.path.join(BASE, "silero_vad.onnx")

OK = "  [OK]  "
WARN = "  [!!]  "
BAD = "  [XX]  "
INFO = "  [--]  "
_fails = []


def ok(msg):
    print(OK + msg)


def warn(msg):
    print(WARN + msg)


def bad(msg):
    print(BAD + msg)
    _fails.append(msg)


def info(msg):
    print(INFO + msg)


def head(title):
    print("\n" + "=" * 66 + "\n" + title + "\n" + "=" * 66)


# ---------- 1. 运行环境 ----------
def check_deps():
    head("1. 依赖")
    import importlib
    need = [("numpy", True), ("sounddevice", True), ("soundfile", True),
            ("onnxruntime", True), ("faster_whisper", True), ("edge_tts", True),
            ("openai", True), ("scipy", False)]
    for name, required in need:
        try:
            m = importlib.import_module(name)
            ok("%-16s %s" % (name, getattr(m, "__version__", "?")))
        except Exception as e:
            (bad if required else warn)("%-16s 缺失: %s" % (name, e))
    info("Python %s" % sys.version.split()[0])


# ---------- 2. 配置文件 ----------
def check_config(cfg):
    head("2. 配置")
    info("配置文件: %s %s" % (VOICE_CONF, "(存在)" if os.path.exists(VOICE_CONF) else "(缺失，使用内置默认值)"))
    info("API 配置: %s %s" % (API_FILE, "(存在)" if os.path.exists(API_FILE) else "(缺失)"))
    info("人设文件: %s" % os.path.join(CONF_DIR, "character_prompt.txt"))
    print()
    info("VAD: 触发阈值 %.2f / 静音阈值 %.2f / 静音判定 %dms / 最小语音 %dms"
         % (cfg.vad_threshold, cfg.vad_neg_threshold, cfg.endpoint_silence_ms, cfg.min_speech_ms))
    info("STT: %s (%s/%s) 语言=%s 热词=%s"
         % (cfg.stt_model, cfg.stt_device, cfg.stt_compute_type, cfg.stt_language, cfg.stt_hotwords or "无"))
    info("TTS: engine=%s voice=%s 分句上限=%d字 首软切=%d字"
         % (cfg.tts_engine, cfg.tts_voice, cfg.tts_max_sentence_chars, cfg.tts_first_soft_chars))
    info("打断: %s (vad=%.2f peak=%.3f 连续%d帧 回声倍数=%.1f)"
         % ("开启" if cfg.barge_in else "关闭", cfg.barge_in_vad, cfg.barge_in_peak,
            cfg.barge_in_frames, cfg.barge_in_peak_ratio))
    if not cfg.barge_in:
        warn("插话打断已关闭：桌面宠物说完才能接话")


# ---------- 3. 音频设备 ----------
def check_devices(cfg, list_all=False):
    head("3. 音频设备")
    import sounddevice as sd
    if list_all:
        print(sd.query_devices())
    from vad_recorder import describe_device, resolve_device
    for kind in ("input", "output"):
        spec = cfg.input_device if kind == "input" else cfg.output_device
        idx = resolve_device(spec, kind)
        try:
            d = sd.query_devices(kind=kind)
            desc = describe_device(idx, kind)
            ok("%s: %s" % ("输入" if kind == "input" else "输出", desc))
            api = sd.query_hostapis(d["hostapi"])["name"]
            if "MME" in api and not spec:
                warn("当前走 %s（延迟偏高）；如需更低延迟，在 voice_config.properties 里设 %s_device=WASAPI"
                     % (api, kind))
        except Exception as e:
            bad("%s设备不可用: %s" % (kind, e))


# ---------- 4. 模型与服务 ----------
def check_model_files(cfg):
    head("4. 模型与本地服务")
    if os.path.exists(VAD_MODEL):
        try:
            import onnxruntime as ort
            s = ort.InferenceSession(VAD_MODEL, providers=["CPUExecutionProvider"])
            ok("silero VAD: %s (输入 %s)" % (os.path.basename(VAD_MODEL),
                                            [i.name for i in s.get_inputs()]))
        except Exception as e:
            bad("silero VAD 加载失败: %s" % e)
    else:
        bad("缺少 %s（VAD 无法工作）" % VAD_MODEL)

    cache = os.path.join(os.environ.get("USERPROFILE", ""), ".cache", "huggingface", "hub")
    hit = []
    if os.path.isdir(cache):
        hit = [d for d in os.listdir(cache) if "whisper" in d.lower()]
    if hit:
        ok("Whisper 模型已缓存: %s" % ", ".join(hit))
    else:
        warn("未发现本地 Whisper 缓存，首次运行会联网下载 %s（约数百 MB）" % cfg.stt_model)

    if cfg.vva_disabled:
        warn("vva 音色后端已禁用，将使用 Edge-TTS")
    else:
        alive = False
        try:
            with socket.create_connection((cfg.vva_host, cfg.vva_port), timeout=1):
                alive = True
        except Exception:
            pass
        if alive:
            ok("vva 合成服务在运行 (:%d)" % cfg.vva_port)
        else:
            py_ok = os.path.exists(cfg.vva_python)
            ui_ok = os.path.exists(cfg.vva_ui)
            (ok if (py_ok and ui_ok) else warn)(
                "vva 服务未运行%s" % ("（语音桥会自动拉起）" if (py_ok and ui_ok) else "，且文件缺失，将回退 Edge-TTS"))
            info("  python: %s %s" % (cfg.vva_python, "存在" if py_ok else "缺失"))
            info("  tts_ui: %s %s" % (cfg.vva_ui, "存在" if ui_ok else "缺失"))


def check_bridge(cfg):
    head("5. 桌宠桥接")
    try:
        with socket.create_connection((cfg.bubble_host, cfg.bubble_port), timeout=1.5):
            ok("Java 桌宠桥接可用 (%s:%d)" % (cfg.bubble_host, cfg.bubble_port))
    except Exception:
        warn("桌宠未启动 (%s:%d 未监听)。语音桥仍可运行，只是气泡不会显示"
             % (cfg.bubble_host, cfg.bubble_port))


def check_key(cfg):
    head("6. 大模型 API")
    from voice_assistant import load_prop
    key = load_prop("api_key")
    if not key:
        bad("conf/ai_config.properties 里没有 api_key")
        return
    ok("api_key 已配置 (%s...%s, 共 %d 字符)" % (key[:6], key[-4:], len(key)))
    info("base_url=%s  model=%s" % (cfg.llm_base_url, cfg.llm_model))


# ---------- 7. 联网自检 ----------
def check_net(cfg):
    head("7. 联网自检")
    # Edge-TTS
    try:
        import asyncio
        import edge_tts
        voices = asyncio.run(edge_tts.list_voices())
        zh = [v["ShortName"] for v in voices if v["Locale"].startswith("zh-CN")]
        ok("Edge-TTS 可用，中文音色 %d 个（当前 %s）" % (len(zh), cfg.tts_voice))
    except Exception as e:
        warn("Edge-TTS 联网失败: %s（vva 可用时不影响）" % e)
    # DeepSeek
    try:
        from openai import OpenAI
        from voice_assistant import load_prop
        key = load_prop("api_key")
        if key:
            c = OpenAI(base_url=cfg.llm_base_url, api_key=key)
            r = c.chat.completions.create(
                model=cfg.llm_model, max_tokens=5, temperature=0,
                messages=[{"role": "user", "content": "只回复：ok"}])
            ok("DeepSeek 连通，返回: %r" % r.choices[0].message.content.strip()[:20])
    except Exception as e:
        warn("DeepSeek 调用失败: %s" % e)
    # STT 加载 + 静音幻觉自检
    try:
        import numpy as np
        from stt_engine import STTEngine
        eng = STTEngine(cfg)
        t = time.monotonic()
        eng.load()
        load_s = time.monotonic() - t
        ok("Whisper 模型加载成功 %.1fs" % load_s)
        t = time.monotonic()
        out = eng.transcribe(np.zeros(cfg.sample_rate * 3, dtype=np.float32))
        ok("3 秒静音识别结果: %r (%.2fs)" % (out, time.monotonic() - t))
        if out:
            warn("静音竟然识别出文字，属于幻觉；可在 voice_config.properties 里调高 min_rms")
        else:
            ok("静音无幻觉输出")
    except Exception as e:
        bad("Whisper 自检失败: %s" % e)


# ---------- 8. 合成延迟 ----------
def check_tts_latency(cfg):
    head("8. 合成首音延迟（会出声，约 3 秒）")
    from tts_engine import Speaker, vva_available
    using_vva = vva_available(cfg)
    info("当前后端: %s" % ("vva 音色 (:9876)" if using_vva else "Edge-TTS (%s)" % cfg.tts_voice))
    spk = Speaker(cfg, log=info)
    try:
        spk.begin_turn()
        spk.say("测试语音。")
        spk.end_turn()
    except Exception as e:
        bad("合成失败: %s" % e)
        return
    lat = spk.first_audio_latency
    if lat is None:
        bad("没有产生任何音频（合成或播放失败）")
        return
    if lat < 0.8:
        ok("首音 %.2fs —— 很快" % lat)
    elif lat < 1.8:
        ok("首音 %.2fs —— 正常" % lat)
    else:
        warn("首音 %.2fs —— 偏慢。实测 Edge-TTS 固有延迟约 1.5~3.2s/句；"
             "建议让 vva 音色服务常驻以明显降低首音延迟" % lat)
    info("语音链路的端到端首音 ≈ LLM 首句时间 + 该值")


# ---------- 9. 麦克风标定 ----------
def calibrate_mic(cfg, seconds=4.0):
    head("9. 麦克风采集（%.0f 秒）" % seconds)
    import numpy as np
    from vad_recorder import MicStream, SileroVAD, describe_device, resolve_device
    dev = resolve_device(cfg.input_device, "input")
    info("设备: %s" % describe_device(dev, "input"))
    vad = SileroVAD(VAD_MODEL, cfg.sample_rate)
    mic = MicStream(cfg, vad)
    peaks, probs, rms_list = [], [], []
    lock = threading.Lock()

    def sink(fr):
        with lock:
            peaks.append(fr.peak)
            probs.append(fr.prob)
            rms_list.append(float(np.sqrt(np.mean(fr.data ** 2))))

    try:
        mic.start(device=dev)
    except Exception as e:
        bad("打开麦克风失败: %s" % e)
        return
    print("\n  请正常说话 2 秒，再安静 2 秒（不说话也无妨，用于测底噪）...")
    mic.set_sink(sink)
    time.sleep(seconds)
    mic.set_sink(None)
    mic.stop()
    if not peaks:
        bad("没有采集到任何音频帧（设备被占用？）")
        return
    p = np.array(peaks)
    pr = np.array(probs)
    r = np.array(rms_list)
    quiet = p < np.percentile(p, 50)
    print()
    info("帧数 %d  峰值 max=%.3f p50=%.3f p90=%.3f" % (len(p), p.max(), np.percentile(p, 50), np.percentile(p, 90)))
    info("RMS  max=%.4f p50=%.4f" % (r.max(), np.percentile(r, 50)))
    info("VAD  max=%.2f p50=%.2f p90=%.2f" % (pr.max(), np.percentile(pr, 50), np.percentile(pr, 90)))
    noise = float(np.percentile(p[quiet], 90)) if quiet.any() else float(p.min())
    suggest_floor = max(0.010, round(noise * 2.5, 3))
    info("估算底噪峰值 ≈ %.3f" % noise)
    if p.max() < 0.06:
        warn("说话峰值偏低(%.3f)，建议 peak_floor=%.3f、barge_in_peak=%.3f，或调高麦克风增益"
             % (p.max(), suggest_floor, max(0.06, round(p.max() * 0.6, 3))))
    else:
        ok("麦克风电平正常（说话峰值 %.3f）" % p.max())
    print()
    info("建议写入 conf/voice_config.properties：")
    info("  peak_floor=%s" % suggest_floor)
    info("  min_rms=%.4f" % max(0.002, round(float(np.percentile(r[quiet], 90)) * 1.5, 4)))


# ---------- 9. 外放回授标定 ----------
def calibrate_echo(cfg):
    head("10. 外放回授标定（打断门限）")
    import numpy as np
    from tts_engine import Speaker, vva_available
    from vad_recorder import MicStream, SileroVAD, describe_device, resolve_device
    info("将播放一句测试语，同时测量麦克风收到的回授（请保持环境安静、不要说话）")
    dev = resolve_device(cfg.input_device, "input")
    vad = SileroVAD(VAD_MODEL, cfg.sample_rate)
    mic = MicStream(cfg, vad)
    peaks, probs = [], []
    lock = threading.Lock()

    def sink(fr):
        with lock:
            peaks.append(fr.peak)
            probs.append(fr.prob)

    try:
        mic.start(device=dev)
    except Exception as e:
        bad("打开麦克风失败: %s" % e)
        return
    # 先测 0.6s 静默底噪
    mic.set_sink(sink)
    time.sleep(0.6)
    with lock:
        base = float(np.percentile(peaks, 90)) if peaks else 0.0
        peaks.clear()
        probs.clear()
    info("静默底噪峰值 ≈ %.3f" % base)

    speaker = Speaker(cfg, log=info)
    speaker.begin_turn()
    speaker.say("这是一句测试语音，用来测量扬声器回授到麦克风的音量。")
    t_end = time.monotonic() + 12
    while speaker.speaking is False and time.monotonic() < t_end:
        time.sleep(0.05)
    while speaker.speaking and time.monotonic() < t_end:
        time.sleep(0.05)
    speaker.end_turn()
    time.sleep(0.2)
    mic.set_sink(None)
    mic.stop()

    with lock:
        pk = np.array(peaks) if peaks else np.array([0.0])
        pb = np.array(probs) if probs else np.array([0.0])
    bleed_peak = float(np.percentile(pk, 95))
    bleed_prob = float(np.percentile(pb, 95))
    print()
    info("播放期间麦克风收到：峰值 p95=%.3f  VAD p95=%.2f" % (bleed_peak, bleed_prob))
    if bleed_peak < 0.02 and bleed_prob < 0.2:
        ok("几乎无回授（戴耳机或音量很小），打断门限可保持默认")
    elif bleed_peak < 0.10:
        warn("存在明显回授：建议 barge_in_peak=%.3f（当前 %.3f），barge_in_vad=%.2f"
             % (max(0.08, round(bleed_peak * 2.5, 3)), cfg.barge_in_peak, max(0.6, round(bleed_prob + 0.15, 2))))
    else:
        bad("回授很强(%.3f)：外放时打断极易误触发，建议戴耳机，或先设 barge_in=0" % bleed_peak)
    info("可直接写入 conf/voice_config.properties 对应项")


# ---------- main ----------
def main():
    ap = argparse.ArgumentParser(description="小紫语音助手环境自检")
    ap.add_argument("--net", action="store_true", help="追加联网检查（Edge-TTS / DeepSeek / Whisper）")
    ap.add_argument("--tts", action="store_true", help="测量合成首音延迟（会出声）")
    ap.add_argument("--mic", type=float, default=None, metavar="SEC", help="采集麦克风 SEC 秒并建议阈值")
    ap.add_argument("--echo", action="store_true", help="播放测试语并标定外放回授门限")
    ap.add_argument("--list", action="store_true", help="列出全部音频设备")
    args = ap.parse_args()

    cfg = load_config()
    print("小紫语音助手自检  |  项目根 %s" % PROJECT_ROOT)
    check_deps()
    check_config(cfg)
    check_devices(cfg, list_all=args.list)
    check_model_files(cfg)
    check_bridge(cfg)
    check_key(cfg)
    if args.net:
        check_net(cfg)
    if args.tts:
        check_tts_latency(cfg)
    if args.mic:
        calibrate_mic(cfg, args.mic)
    if args.echo:
        calibrate_echo(cfg)

    head("结论")
    if _fails:
        bad("发现 %d 个致命问题（见上方 [XX]）：" % len(_fails))
        for f in _fails:
            print("      - " + f)
        print("\n  修掉这些再启动 start_voice.bat")
        return 1
    ok("没有发现致命问题。")
    if not args.net and not args.mic:
        info("想更彻底：python doctor.py --net --mic 4")
    return 0


if __name__ == "__main__":
    sys.exit(main())
