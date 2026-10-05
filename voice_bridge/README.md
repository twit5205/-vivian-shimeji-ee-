# 小紫语音桥（voice_bridge）

桌宠的语音输入输出闭环：说话 → 识别 → 大模型 → 语音回答 + 气泡 + 情绪动作。

```
麦克风(常开单路)
   │  silero VAD 端点检测（迟滞判定 + 前垫/后垫）
   ▼
STT  faster-whisper（本地离线，反幻觉）
   ▼
LLM  DeepSeek（流式）── 边收边按标点切句
   ▼
TTS  vva 音色(GPT-SoVITS) ──失败回退──► Edge-TTS
   │  合成线程 / 播放线程 分离，边合成边播
   ▼
扬声器  +  127.0.0.1:8800 桥接 → Java 桌宠 ChatBubble + 情绪动作
   ▲
   └── 播放期间持续监听：用户一开口立刻停播，并直接接住这句话
```

## 快速开始

```bat
start_voice.bat        :: 启动桌宠 + 语音桥
doctor_voice.bat       :: 语音链路自检（出问题先跑这个）
```

单独调试：

```bat
cd voice_bridge
python doctor.py --net --mic 4     :: 依赖/设备/模型/联网/麦克风电平
python voice_assistant.py --text   :: 文本模式，验证 LLM+TTS（不占麦克风）
python voice_assistant.py --once   :: 只处理一轮，方便看日志
python selftest.py                 :: 离线回归测试（无需麦克风/网络）
```

## 文件

| 文件 | 作用 |
|---|---|
| `voice_assistant.py` | 主编排：会话循环、流式生成、气泡桥接、记忆回写 |
| `vad_recorder.py` | 常开麦克风流 + silero VAD + 端点检测 + 打断门限 |
| `stt_engine.py` | faster-whisper 封装（静音过滤、逐段置信度、幻觉黑名单、热词） |
| `tts_engine.py` | 分句器 + 合成/播放双线程流水线 + 打断 |
| `voice_config.py` | 全部可调参数与默认值、耗时埋点 |
| `memory_db.py` | 长期记忆（SQLite：用户画像 + 对话历史） |
| `doctor.py` | 自检与阈值标定 |
| `selftest.py` | 离线回归测试 |

配置读写：`conf/voice_config.properties`（可选，改完重启生效）。
API Key 仍在 `conf/ai_config.properties`（与 Java 端共用）。

## 实测性能基线（本机，CPU，whisper-small/int8）

| 环节 | 实测 | 说明 |
|---|---|---|
| STT 模型加载 | **3.5s** | 改为只认本地缓存后。之前每次启动去 huggingface.co 校验更新，国内网络下要 **39s** |
| STT 识别 | ~0.02s / 静音，<1s / 正常短句 | 静音被 vad_filter 全部过滤，所以极快 |
| LLM 首句就绪 | **~1.4s** | deepseek-chat 流式，真实网络 |
| TTS 合成（Edge-TTS） | **1.5~3.2s / 句**（抖动大） | 这是目前最大的单点延迟；改用 vva 音色后端会明显更快 |
| 端到端首音 | ≈ LLM 首句 + 该句合成 | 例：1.4 + 2.0 ≈ 3.4s |

> 结论：想再快，优先让 **vva 音色服务常驻**（`python doctor.py --tts` 可对比两个后端的首音延迟）。
> Edge-TTS 是"零部署的兜底方案"，不是低延迟方案。

## 延迟构成与调优

每轮日志会打印 `[Lat] 说完 0.11s | STT 0.42s | 回复就绪 1.05s | 播放结束 4.20s | 端到端首音 1.35s`。

| 想改善 | 调什么 |
|---|---|
| 反应慢、说完要等半天 | 调小 `endpoint_silence_ms`（650 → 400） |
| 话没说完就被抢话 | 调大 `endpoint_silence_ms`（650 → 900） |
| 首句出声慢 | 调小 `tts_max_sentence_chars`、`tts_first_soft_chars`；或改用 vva 后端 |
| 识别错字多 | 把错词加进 `stt_hotwords`；或换 `stt_model=medium`（更慢） |
| 识别出"谢谢观看"这类幻觉 | 调大 `min_rms`、`peak_floor`（用 `doctor.py --mic` 标定） |
| 环境噪声大误触发 | 用 `doctor.py --mic 4` 看底噪，把 `peak_floor` 提到实测底噪的 2~3 倍 |
| 输入延迟偏高 | `doctor.py` 会提示：把 `input_device`/`output_device` 设为 `WASAPI` |

## 插话打断（barge-in）

- 默认开启。播放期间麦克风不关，检测到用户说话立刻 `sd.stop()` 停播，并把这句话当作下一轮输入。
- **外放场景必须注意回声**：先跑 `python doctor.py --echo`，它会播放一句测试语并测出"扬声器声音被麦克风收回去"的量，给出建议门限。
- 回授很强（建议戴耳机）时，先设 `barge_in=0`；此时桌宠说完才能接话。
- 打断采用三重条件抗误触：VAD 概率 ≥ `barge_in_vad` **且** 峰值 ≥ `barge_in_peak` **且** 峰值高于实测回声基线 `barge_in_peak_ratio` 倍，并需连续 `barge_in_frames` 帧达标。

## 隐私

麦克风在程序运行期间是**常开**的（打断检测需要），但：

- STT 全程本地运行，音频不出本机；
- 只有识别出的**文字**会发给 DeepSeek；
- 退出语音桥（关掉那个黑窗口）即停止采集。

## 排错

```bat
python doctor.py --net --mic 4 --echo
```

按输出里的 `[XX]` 逐条修。最常见三类：

1. **气泡不显示** → 桌宠没启动，`127.0.0.1:8800` 未监听（语音仍正常，只是没气泡）。
2. **vva 服务未运行** → 会在启动时自动拉起（首次加载模型较慢，最多等 60s）；失败则自动回退 Edge-TTS。
3. **麦克风打不开** → 设备被其它程序独占，用 `python voice_assistant.py --list-devices` 换个设备号。
