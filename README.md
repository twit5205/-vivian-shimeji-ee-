# Shimeji-ee · 薇薇安 AI 桌宠

把经典的 [Shimeji-ee](https://github.com/asdfman/Shimeji-ee) 桌面宠物，改造成一个**能听、能说、有情绪、记得住你**的 AI 桌宠。

桌面上会站着一位「薇薇安」：她会自己呼吸、眨眼、发呆；你对她说话，她会听懂、会思考、会用语音回答你，还会根据聊天的情绪切换表情动作。

```
你说话  →  本地识别  →  DeepSeek 流式回答  →  语音合成播放
                    ↘  气泡文字  +  情绪动作（开心/思考/惊讶/失落）
```

---

## 目录

- [这是什么](#这是什么)
- [核心特性](#核心特性)
- [环境要求](#环境要求)
- [快速开始](#快速开始)
- [目录结构](#目录结构)
- [配置说明](#配置说明)
- [工作原理](#工作原理)
- [实测性能基线](#实测性能基线)
- [情绪动作（阶段2）](#情绪动作阶段2)
- [隐私与安全](#隐私与安全)
- [常见问题](#常见问题)
- [许可与致谢](#许可与致谢)

---

## 这是什么

原版 Shimeji-ee 是一只会在桌面上乱走、会爬窗口、会把你窗口丢来丢去的 Java 桌宠。它的动作完全由本地 XML 规则驱动，不会说话，也不会理解你。

这个项目在它之上补了一整条 **AI 语音链路**，分成两个阶段：

| 阶段 | 内容 | 状态 |
|---|---|---|
| 阶段 1 | 语音对话：麦克风常开 → 本地识别 → 大模型 → 语音回答 + 气泡 | 已完成 |
| 阶段 2 | 情绪动作：回答里的情绪标记 → 自动切换桌宠表情 | 已完成 |
| 阶段 3 | 长期记忆：SQLite 保存用户画像与历史（代码已就位） | 部分完成 |

Java 侧（`Shimeji-ee.jar`）负责窗口、动画与精灵渲染；Python 侧（`voice_bridge/`）负责全部音频与 AI 逻辑。两者通过本机 HTTP 端口 `127.0.0.1:8800` 通信。

> **说明**：Java 侧的修改以**编译产物** `Shimeji-ee.jar` 的形式提供，本仓库不包含 `com/group_finity/` 的 Java 源码。

---

## 核心特性

**语音**

- 麦克风常开单路采集，silero VAD 做端点检测（迟滞判定 + 前垫/后垫），无需按键。
- STT 使用 `faster-whisper` **全程本地离线**推理，音频不出本机。
- 支持热词（人名、专有名词），显著降低错字率。
- 反幻觉：RMS 门限 + `vad_filter` 双重过滤，避免静音时识别出「谢谢观看」这类幻听。

**对话**

- DeepSeek 流式接口，**边收边按标点切句**，首句一就绪就送去合成，不必等全文。
- 可自定义人设：`conf/character_prompt.txt` 决定语气、身份与行为铁律。

**语音合成**

- 双后端：自建 **vva 音色（GPT-SoVITS）** 优先，失败自动回退 **Edge-TTS**。
- 合成线程与播放线程分离，边合成边播。
- 首句软切分：攒够 `tts_first_soft_chars` 个字符就在逗号处断开，压低首音延迟。

**插话打断（barge-in）**

- 播放语音期间麦克风不关；你一开口立刻停播，并把这句话直接当作下一轮输入。
- 三重条件抗误触：VAD 概率、峰值、以及峰值高于实测回声基线 `barge_in_peak_ratio` 倍，且需连续多帧达标。

**情绪与表现**

- 模型在回复开头输出一行 `[MOOD:xxx]` 标记语气，程序解析后触发对应桌宠动作。
- 情绪到行为的映射在 `conf/mood_config.properties`，改配置即可换表情，无需改代码。

**语音桥自检**

- `doctor.py` 一条命令检查依赖、音频设备、模型缓存、网络连通性与麦克风电平，并给出**具体调参建议**。

---

## 环境要求

| 组件 | 要求 |
|---|---|
| 操作系统 | Windows 10 / 11（音频与批处理脚本面向 Windows） |
| Java | JRE 8 或以上（`javaw` 需在 PATH 中） |
| Python | 3.10 – 3.13（实测 3.13 通过） |
| 网络 | 仅调用大模型时需要；STT 完全离线 |
| API Key | DeepSeek 平台密钥（见[配置说明](#配置说明)） |

可选：

- **vva 音色**：需要本机部署 [GPT-SoVITS](https://github.com/RVC-Boss/GPT-SoVITS) 服务。不部署也能跑，会自动回退 Edge-TTS（需联网）。

---

## 快速开始

### 1. 安装 Python 依赖

```bat
python -m pip install -r voice_bridge\requirements.txt
```

首次运行 `faster-whisper` 会下载 `small` 模型（约 460 MB，来自 Hugging Face）。网络受限时请先手动下载并放入本地缓存。

### 2. 配置 API Key

复制模板并填入你自己的密钥：

```bat
copy conf\ai_config.example.properties conf\ai_config.properties
```

```properties
# conf/ai_config.properties
api_key=<在这里填入你的 DeepSeek API Key>
model=deepseek-chat
max_tokens=500
temperature=0.8
```

> 该文件已被 `.gitignore` 忽略，**不要提交**。

### 3. 启动

```bat
start_voice.bat      :: 启动桌宠 + 语音桥
doctor_voice.bat     :: 语音链路自检（出问题先跑这个）
```

**只想单独调试语音**：

```bat
cd voice_bridge
python doctor.py --net --mic 4     :: 依赖 / 设备 / 模型 / 联网 / 麦克风电平
python doctor.py --echo            :: 外放回声标定（建议先跑，用于调 barge-in）
python voice_assistant.py --text   :: 纯文本模式，验证 LLM + TTS（不占麦克风）
python voice_assistant.py --once   :: 只处理一轮，方便看日志
python selftest.py                 :: 离线回归测试（无需麦克风/网络）
```

**只想启动桌宠**（不用语音）：

```bat
javaw -jar Shimeji-ee.jar
```

---

## 目录结构

```
Shimeji-ee/
├── Shimeji-ee.jar              桌宠主程序（Java，含窗口/动画/AI 气泡桥接）
├── start_voice.bat             一键启动：桌宠 + 语音桥
├── doctor_voice.bat            一键自检
├── licence.txt                 原始许可证（Shimeji-ee 上游）
│
├── conf/                       配置目录
│   ├── actions.xml             动作定义（帧序列）
│   ├── behaviors.xml           行为定义（何时播放哪个动作）
│   ├── settings.properties     桌宠基础设置
│   ├── theme.properties        菜单主题配色
│   ├── mood_config.properties  情绪 → 行为 映射（阶段2）
│   ├── character_prompt.txt    人设提示词（本地私密，仅模板入库）
│   ├── ai_config.properties    API Key（本地私密，仅模板入库）
│   ├── voice_config.properties 语音参数
│   └── language_*.properties   多语言界面文本（20+ 语言）
│
├── voice_bridge/               语音桥（Python）
│   ├── voice_assistant.py      主编排：会话循环、流式生成、气泡桥接、记忆回写
│   ├── vad_recorder.py         常开麦克风流 + silero VAD + 端点检测 + 打断门限
│   ├── stt_engine.py           faster-whisper 封装（静音过滤、幻觉黑名单、热词）
│   ├── tts_engine.py           分句器 + 合成/播放双线程流水线 + 打断
│   ├── voice_config.py         全部可调参数与默认值、耗时埋点
│   ├── memory_db.py            长期记忆（SQLite：用户画像 + 对话历史）
│   ├── doctor.py               自检与阈值标定
│   ├── selftest.py             离线回归测试
│   └── requirements.txt        依赖清单
│
├── img/                        精灵素材
│   ├── icon.png
│   └── Shimeji/                shime1.png … shime448.png（含各动作帧序列）
│
└── analysis/                   素材处理脚本（锚点对齐、帧重生成）
```

---

## 配置说明

配置读取优先级（从低到高）：

1. `voice_config.py` 里的 `DEFAULTS`
2. `conf/voice_config.properties`（可选，缺失即全用默认）
3. 环境变量 `VOICE_<KEY>`（大写），例如 `set VOICE_BARGE_IN=0`

改完 properties 需要**重启语音桥**生效。所有参数说明都写在
`conf/voice_config.properties` 的注释里，常用的几个：

| 想改善 | 调什么 |
|---|---|
| 反应慢，说完要等半天 | 调小 `endpoint_silence_ms`（650 → 400） |
| 话没说完就被抢话 | 调大 `endpoint_silence_ms`（650 → 900） |
| 首句出声慢 | 调小 `tts_max_sentence_chars` / `tts_first_soft_chars`；或改用 vva 后端 |
| 识别错字多 | 把错词加进 `stt_hotwords`；或换 `stt_model=medium`（更慢） |
| 静音时识别出幻觉 | 调大 `min_rms`、`peak_floor`（用 `doctor.py --mic` 标定） |
| 环境噪声误触发 | 看 `doctor.py --mic 4` 的实测底噪，把 `peak_floor` 提到 2~3 倍 |
| 输入延迟偏高 | 把 `input_device` / `output_device` 设为 `WASAPI` |

### 自定义音色（vva）

`voice_bridge/voice_config.py` 中的 `vva_python` 与 `vva_ui` 需要指向你本机的
GPT-SoVITS 环境，通过环境变量提供，无需改代码：

```bat
set VOICE_VVA_PYTHON=E:\GPT-SoVITS\sovits_env\Scripts\python.exe
set VOICE_VVA_UI=E:\你的路径\tts_ui.py
```

未设置或服务未就绪时，自动回退 Edge-TTS。

---

## 工作原理

```
麦克风（常开单路采集）
   │  silero VAD 端点检测（迟滞判定 + 前垫 / 后垫）
   ▼
STT  faster-whisper（本地离线，反幻觉）
   ▼
LLM  DeepSeek（流式）── 边收边按标点切句
   ▼
TTS  vva 音色（GPT-SoVITS）──失败回退──► Edge-TTS
   │  合成线程 / 播放线程分离，边合成边播
   ▼
扬声器  +  127.0.0.1:8800 桥接 → Java 桌宠：ChatBubble + 情绪动作
   ▲
   └── 播放期间持续监听：用户一开口立刻停播，并直接接住这句话
```

每轮对话都会打印分阶段耗时，便于定位瓶颈：

```
[Lat] 说完 0.11s | STT 0.42s | 回复就绪 1.05s | 播放结束 4.20s | 端到端首音 1.35s
```

---

## 实测性能基线

测试环境：本机 CPU 推理，`whisper-small` / `int8`。

| 环节 | 实测 | 说明 |
|---|---|---|
| STT 模型加载 | **3.5s** | 改为只认本地缓存后。此前每次启动去 huggingface.co 校验更新，国内网络下要 **39s** |
| STT 识别 | ~0.02s / 静音，< 1s / 正常短句 | 静音被 `vad_filter` 全部过滤，所以极快 |
| LLM 首句就绪 | **~1.4s** | `deepseek-chat` 流式，真实网络 |
| TTS 合成（Edge-TTS） | **1.5 ~ 3.2s / 句**（抖动大） | 当前最大的单点延迟；改用 vva 后端会明显更快 |
| 端到端首音 | ≈ LLM 首句 + 该句合成 | 例：1.4 + 2.0 ≈ 3.4s |

> **结论**：想再快，优先让 **vva 音色服务常驻**（`python doctor.py --tts` 可对比两个后端的首音延迟）。
> Edge-TTS 是「零部署的兜底方案」，不是低延迟方案。

---

## 情绪动作（阶段2）

模型每次回复的开头会输出一行情绪标记，例如：

```
[MOOD:happy]
好，这件事我接下了。
```

程序解析后查 `conf/mood_config.properties`，触发对应行为：

```properties
mood.happy=VivianHappy
mood.thinking=VivianThinking
mood.surprised=VivianSurprised
mood.sad=VivianSad
mood.angry=            # 留空 = 该情绪不触发动作
mood.farewell=
mood.default=
```

可选情绪：`happy` / `thinking` / `surprised` / `sad` / `angry` / `farewell` / `default`。
留空或删行表示该情绪**不触发动作**（桌宠无反应）。

想换表情：改这个文件即可，**不需要改代码**。动作帧的定义在 `conf/actions.xml`
与 `conf/behaviors.xml`。

---

## 隐私与安全

这个项目处理麦克风音频和 API 密钥，请务必读完本节。

**音频**

- STT 全程**本地运行**，音频不出本机。
- 只有**识别出的文字**会发送给 DeepSeek。
- 麦克风在语音桥运行期间**常开**（打断检测需要）。关掉那个黑窗口即停止采集。

**密钥**

- API Key 只放在 `conf/ai_config.properties`，该文件已被 `.gitignore` 忽略。
- 仓库中只有 `conf/ai_config.example.properties` 模板。
- 版本库中**永远不应出现真实密钥**。

**个人数据**

以下内容同样被忽略，不会入库：

| 忽略项 | 原因 |
|---|---|
| `conf/ai_config.properties` | 含 API Key |
| `conf/character_prompt.txt` | 个人人设提示词 |
| `conf/memory.db*` | SQLite 长期记忆：用户画像 + 对话历史 |
| `*.log` | 运行日志，可能含对话内容与本地路径 |
| `img/shime_preview.png` | 本地预览产物 |

> 更详细的排查清单和泄露处置流程见 [SECURITY.md](SECURITY.md)。
>
> **注意**：本项目早期在本地提交过含真实密钥的历史（该仓库从未推送，密钥未通过 Git 外泄）。
> 若你要基于旧目录建仓，**必须先清理历史**，见 SECURITY.md。

---

## 常见问题

**1. 气泡不显示**

桌宠没启动，`127.0.0.1:8800` 未监听。语音功能仍正常，只是没有气泡文字。

**2. vva 服务未运行**

启动时会自动拉起（首次加载模型较慢，最多等 60s）；失败则自动回退 Edge-TTS。
可用 `python doctor.py --tts` 检查。

**3. 麦克风打不开**

设备被其它程序独占。用 `python voice_assistant.py --list-devices` 查看设备号，
再把 `input_device` 设成对应序号或名称片段。

**4. 桌宠说话时被自己打断**

外放且回声大。先跑 `python doctor.py --echo` 标定回声基线，或临时设 `barge_in=0`
（此时桌宠说完才能接话），或直接戴耳机。

**5. 首次运行卡在模型下载**

`faster-whisper` 在拉 Hugging Face 模型。网络受限时手动下载 `small` 模型放入本地缓存，
之后启动就只认缓存（见[性能基线](#实测性能基线)：39s → 3.5s）。

**6. 一处都跑不通**

直接跑 `doctor_voice.bat`，按输出里的 `[XX]` 逐条修。

---

## 许可与致谢

- 本项目基于 **Shimeji-ee**（原作者 Nyorotono / asdfman 等）二次开发，桌面宠物主体
  与精灵素材沿用其许可，原始许可证见 `licence.txt`。使用与再分发请遵守上游许可。
- 语音链路使用 [faster-whisper](https://github.com/SYSTRAN/faster-whisper)、
  [silero-vad](https://github.com/snakers4/silero-vad)、
  [Edge-TTS](https://github.com/rany2/edge-tts)、
  [GPT-SoVITS](https://github.com/RVC-Boss/GPT-SoVITS)。
- 大模型服务由 [DeepSeek](https://platform.deepseek.com/) 提供。

角色「薇薇安·班希」的形象与设定版权归其原权利方所有，本仓库仅作个人技术学习用途。
