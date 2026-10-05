# 安全说明

本仓库处理**麦克风音频**与**大模型 API 密钥**，请在使用和二次分发前读完本文。

> **⚠️ 本仓库的由来**
>
> 这是一个**全新初始化的仓库**，只有 1 个提交，历史中不含任何密钥。
>
> 但它的**上游开发目录**是一个已经提交过 26 次的本地 Git 仓库，其中
> `conf/ai_config.properties` 曾在 **2 个提交**里带着真实密钥被跟踪。
> 该本地仓库**从未配置 remote、从未推送**，所以密钥没有通过 Git 外泄；
> 不过只要你推那个仓库，密钥就会进入公开历史。
>
> **不要再把密钥写回任何被跟踪的文件。** 若确实提交过，按下文
> [「如果不小心提交了密钥」](#如果不小心提交了密钥)处理旧历史。

---

## 1. 密钥管理

### 存放位置

唯一的密钥存放点是：

```
conf/ai_config.properties
```

内容形如：

```properties
api_key=<你的 DeepSeek API Key>
model=deepseek-chat
max_tokens=500
temperature=0.8
```

### 为什么这个文件不在仓库里

`conf/ai_config.properties` 已被 `.gitignore` 忽略。仓库中提供的是模板：

```
conf/ai_config.example.properties
```

首次使用请自行复制并填写：

```bat
copy conf\ai_config.example.properties conf\ai_config.properties
```

### 绝对不要做的事

- **不要**把真实密钥写进 `voice_config.properties`、`.bat`、`.md` 或任何会被提交的文件。
- **不要**用 `git add -f` 强制添加被忽略的文件。
- **不要**在 Issue、日志、截图里粘贴密钥。

### 如果不小心提交了密钥

**删除文件是不够的** —— Git 历史会永久保留旧内容。按顺序处理：

1. **立刻吊销密钥**（这一步最重要，先做）
   前往 <https://platform.deepseek.com/api_keys>，删除泄露的密钥并新建一个。
   只要旧密钥还有效，任何看到它的人都能用你的额度。
2. **从工作区移除**
   ```bat
   git rm --cached conf/ai_config.properties
   ```
   确认 `.gitignore` 已包含该路径。
3. **清理历史**（密钥曾在历史提交中出现时才需要）
   使用 [git-filter-repo](https://github.com/newren/git-filter-repo)：
   ```bat
   git filter-repo --path conf/ai_config.properties --invert-paths
   ```
   然后强制推送：
   ```bat
   git push --force --all
   ```
4. **假定它已经泄露**
   清理历史只是减少扩散面，不能撤回已经发生的暴露。吊销是唯一可靠的补救。

---

## 2. 隐私数据

### 音频

| 环节 | 是否离开本机 |
|---|---|
| 麦克风采集 | 否 |
| VAD 端点检测 | 否 |
| 语音识别（faster-whisper） | **否，完全本地推理** |
| 大模型请求 | **是**，但只发送**识别出的文字** |
| 语音合成（Edge-TTS） | 是（文本发往微软服务） |

语音合成若使用本地部署的 vva（GPT-SoVITS）后端，则文本同样不出本机。

### 麦克风常开

语音桥运行期间麦克风**持续采集**，这是插话打断（barge-in）功能的前提。
关掉启动语音桥的那个命令行窗口即停止采集。

不想被持续监听时，可以设 `barge_in=0` 并在不需要时直接退出语音桥。

### 本地留存的数据

| 文件 | 内容 | 是否入库 |
|---|---|---|
| `conf/memory.db` | SQLite 长期记忆：用户画像 + 对话历史 | 否（已忽略） |
| `conf/memory.db-wal` / `-shm` | SQLite 预写日志 | 否（已忽略） |
| `conf/memory_summary.txt` | 记忆摘要 | 否（已忽略） |
| `ShimejieeLog*.log` | 运行日志，可能含对话内容与本地绝对路径 | 否（已忽略） |
| `conf/character_prompt.txt` | 个人人设提示词 | 否（已忽略） |
| `img/shime_preview.png` | 本地预览拼图产物 | 否（已忽略） |

要彻底清除个人数据，直接删除上述文件即可。

---

## 3. 本机暴露面

语音桥与桌宠之间通过**本机回环地址**通信，不对外监听：

| 服务 | 地址 | 用途 |
|---|---|---|
| 桌宠气泡桥接 | `127.0.0.1:8800` | Python → Java，推送气泡文字与情绪动作 |
| vva 语音服务 | `127.0.0.1:9876` | 本地 TTS 后端（可选） |

两者均绑定 `127.0.0.1`，局域网内其他设备无法访问。

---

## 4. 发布前自检清单

准备把项目推到公开仓库时，逐项确认：

- [ ] `conf/ai_config.properties` **不在**暂存区（`git status` 应看不到它）
- [ ] 全仓库搜索 `sk-` 无真实密钥命中
- [ ] `.gitignore` 覆盖：`ai_config.properties`、`memory.db*`、`*.log`、`character_prompt.txt`
- [ ] 运行日志（`ShimejieeLog*.log`）已删除
- [ ] 记忆数据库（`conf/memory.db*`）已删除
- [ ] 源码中的本机绝对路径已改为可配置
- [ ] `git log -p` 抽查首次提交，确认无敏感内容
- [ ] 若密钥曾提交过：**已吊销并重建**

### 一键辅助

本仓库附带 `publish_github.ps1`（在项目外层的发布工具目录中），它会：

1. 把项目复制到一个干净目录；
2. 剔除日志、记忆库、备份、编译中间产物；
3. 把密钥行改写为占位符、把绝对路径改写为相对路径；
4. 写入 README / 本文件 / 配置模板；
5. **扫描暂存树**，发现残留密钥立即中止，不提交任何内容。

```powershell
# 只生成干净副本，不推送
.\publish_github.ps1

# 生成并推送
.\publish_github.ps1 -RepoName Shimeji-ee-Vivian -Push -Visibility public
```

> 注意：脚本只清理**将要提交的副本**，不会改动你的原始工作目录，
> 也无法从**已有的 Git 历史**中移除任何东西。若原目录已初始化为仓库并提交过密钥，
> 请按[第 1 节](#如果不小心提交了密钥)处理历史。

---

## 5. 报告问题

发现安全问题请**不要**开公开 Issue，改用私下渠道联系维护者。
请附上：受影响文件、复现步骤、影响范围、以及你的建议处置方式。
