# 语音输入、本地流式识别与 WAV 识别

V3 提供四条语音路径（浏览器识别、本地流式识别、WAV 转写、离线命令行）。网页默认先显示识别文字，由用户确认发送；可选连续语音模式需要每次手动开启。WAV 文件由当前运行服务的机器使用本地 Vosk 模型识别，不调用云端识别 API。离线命令行客户端可以识别麦克风或已有 WAV。

## 网页语音

点击“点击说话”开始一次识别，途中显示临时文本，完成后可在输入框修改。识别结束不会自动启动新任务。勾选“连续语音 · 自动发送”是明确启用自动发送的操作；完整结果会提交，页面持续显示麦克风状态。取消勾选、点击结束、离开任务控制台视图、切换到浏览器后台或关闭页面都会结束本次聆听，不会在页面重新可见时自行恢复。

连续模式下，“停止”“停止任务”“取消任务”等完整独立停止指令直接走控制接口。像“不要停止”“到了以后停止”这样的句子不会匹配优先停止规则。普通请求尚未返回时可以继续识别停止指令；普通新任务会被当前请求的互斥状态或服务端任务状态拒绝。

语音播报开始前会中止当前识别并丢弃它的后续回调；播报结束后保留 650 ms 间隔，再恢复已开启的连续聆听。麦克风权限拒绝、无输入设备等错误会停止会话。连续三轮未获得有效语音或识别错误后也会停止，避免无限重试；必须再次点击开启。

浏览器 Web Speech API 的可用性因浏览器而异，有些浏览器会使用远程识别服务。该路径不保证离线。需要明确离线处理时，使用下方的 WAV 或命令行路径。网页本身不保存音频文件，不会启动后台常驻录音，也不提供经过现场验证的声学唤醒或回声消除。

## 配置离线模型

V3 页面提供“语音输入 → 本地 Vosk 流式识别”。模型配置完成后，点击“开始本地收音”才请求麦克风权限。AudioWorklet 把实际设备采样率转换成单声道 16 kHz PCM16LE，每 200 ms 上传一个带递增序号的音频块。浏览器不会回放这些音频，也不会将转写直接提交为任务。点击“结束并转写”后，文本填入任务框供检查；每个最终片段可以单独纠正，服务器保留纠正前后的版本。

流式会话最长 60 秒，最多同时 4 个；重复/乱序块被拒绝，慢连接积压超过 8 块时客户端取消会话。取消、离开任务页面、切到浏览器后台、播报开始都会停止采集并取消会话，不自动恢复。浏览器请求的降噪/回声消除只是设备选项，本次验证未使用真实麦克风，因此不宣称现场声学效果。

接口为 `GET /api/voice/capabilities`、`POST /api/voice/sessions`、`POST /api/voice/sessions/{id}/chunk`（原始 PCM，`X-Audio-Sequence` 从 0 开始）、`POST .../{id}/finish`、`POST .../{id}/correct {segment_id,text}`、`DELETE .../{id}`。识别结果带 `provider/local/simulated` 来源信息和 `requires_confirmation:true`。`ASRProvider`、`StreamingRecognizer` 与 `TTSProvider` 协议可注入可信进程内实现；浏览器 TTS 提供播放指令，`audio_generated:false` 表示没有在服务端生成音频，不能当作已经听到。

建议在虚拟环境安装可选依赖，不影响不需要语音的模拟后端：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-voice.txt
```

从 [Vosk 官方模型列表](https://alphacephei.com/vosk/models) 手动下载并解压中文模型。例如 `vosk-model-small-cn-0.22`，官方页面列出的压缩包约 42 MB、Apache 2.0 许可。项目不会自动下载模型。

```powershell
$env:VOICE_PATROL_VOSK_MODEL = 'D:\models\vosk-model-small-cn-0.22'
python -m robot_voice_patrol --mode mock
```

Linux/macOS 使用同名环境变量，例如：

```bash
export VOICE_PATROL_VOSK_MODEL=/path/to/vosk-model-small-cn-0.22
python -m robot_voice_patrol --mode mock
```

在网页点击“上传 WAV 离线识别”，选择文件。文本回到输入框后仍须确认发送。未配置模型或 Vosk 不可用时，服务返回 `503` 并给出明确提示；不会用模拟文本冒充识别结果。只做文件识别时并不需要可用的麦克风或 `sounddevice` 音频设备。

文件限制：

- RIFF PCM WAV，整数 8/16/24/32 位，单声道或双声道。
- 采样率 8–96 kHz，最多 60 秒，文件最多 10 MiB。
- 拒绝压缩 WAV、MP3、WebM、截断文件、空音频和不支持的声道配置。
- 输入转为单声道、16 kHz、16 位 PCM；标准库使用轻量线性重采样。优先提供原生 16 kHz 单声道录音，避免额外转换。

模型在进程内按路径缓存，最多保留两个模型；每次请求创建独立识别器，同一时刻最多两个转写请求。模型不可用、处理繁忙、文件无效和识别为空分别保留真实结果，均不会自动生成任务。

## 命令行：文件与麦克风

只识别文件，不发送任务，也不打开麦克风：

```powershell
python -m robot_voice_patrol.voice_client --model D:\models\vosk-model-small-cn-0.22 --wav command.wav --transcribe-only
```

省略 `--transcribe-only` 时，显示完整识别结果，然后等待 `y` 确认。只有显式加 `--auto-send` 才会自动发送文件识别结果。

列出音频设备（不录音）：

```powershell
python -m robot_voice_patrol.voice_client --list-devices
```

打开选定麦克风，每条普通任务默认确认后发送：

```powershell
python -m robot_voice_patrol.voice_client --model D:\models\vosk-model-small-cn-0.22 --device 1 --url http://127.0.0.1:8768
```

可以添加 `--wake-word 行知`：普通任务须以“行知”开头。这里做的是**转写文字的前缀过滤**，不是专门训练的声学唤醒模型。添加 `--stop-only` 可只接收完整停止指令。麦克风路径的完整独立停止指令会跳过普通任务确认，并直接发送控制请求；它也不要求唤醒前缀。

录音采用每句独立的流，确认和 HTTP 请求期间关闭麦克风。音频队列有容量限制；溢出、设备丢帧或超过一秒的排队音频会使整句作废，而不是裁掉前半句继续执行。超时后的不完整尾句不会发送。Ctrl+C 关闭录音流。HTTP 失败不自动重试，以免重复任务；普通任务包含唯一 `request_id`，服务端去重。

`sounddevice` 使用 PortAudio。若操作系统缺少对应音频后端，客户端会报告依赖或设备不可用。参见 [sounddevice RawInputStream 文档](https://python-sounddevice.readthedocs.io/en/latest/api/raw-streams.html) 和 [Vosk 官方麦克风示例](https://github.com/alphacep/vosk-api/blob/master/python/example/test_microphone.py)。

## 已执行的验证

2026-10-03，V3 在 Windows 上使用既有中文合成 WAV 和真实本地 Vosk 识别器验证流式路径：

| 项目 | 结果 |
|---|---|
| 输入来源 | 既有 Windows SAPI Microsoft Huihui 中文合成录音文件，本次没有录音或播放 |
| 原始格式 | 单声道，22,050 Hz，16 位 PCM，4.052 秒 |
| 流式输入 | 重采样为 16 kHz PCM16LE，按 200 ms 分成 21 个有序块 |
| 预期/最终文字 | 去会议室然后返回起点（精确匹配） |
| 片段纠正 | 改为“去仓库然后返回起点”，保留原文与修订记录 |
| 取消验证 | 清除文本，后续 finish 被拒绝 |
| 本次耗时 | 1.344 秒，包含本次模型加载与处理 |
| WAV SHA-256 | `31f91acf42961439bac88f6acf35c584cd773255587f91d28192af19a939805e` |

详细来源、临时文字变化和最终结果见 [VOICE_VALIDATION.json](VOICE_VALIDATION.json)。独立的 [STREAMING_UI_VALIDATION.json](STREAMING_UI_VALIDATION.json) 记录浏览器界面测试：使用假的媒体设备和 ASR HTTP 返回值验证明确开启、转写填入、片段纠正、离开页面和播报中断，不访问麦克风或扬声器。

这是单条合成音频的真实识别与软件控制链路验证，不代表真实麦克风、噪声、回声消除、口音准确率或硬件停止延迟通过验证。模型与临时依赖未打包。

软件测试不需要麦克风或模型：

```bash
python -m unittest tests.test_audio tests.test_voice_client tests.test_voice_sessions -v
node --test tests/test_voice_ui.cjs tests/test_streaming_voice.cjs
```

Python 测试覆盖 PCM 转换、格式与边界拒绝、模型缓存并发、工作数限制、录音关闭时机、音频过期、请求不重发与停止优先。Node 测试用假的 SpeechRecognition 验证没有自动启动、临时文字不提交、连续识别、播报暂停和冷却、错误重试上限、旧回调丢弃和独立停止匹配。

网页端到端验证使用 `tests/ui_smoke.cjs`。先显式启动 mock 服务，再在准备好 Playwright 的测试环境运行：

```powershell
$env:UI_URL = 'http://127.0.0.1:8768'
node tests/ui_smoke.cjs
```

测试会创建模拟任务并临时修改后恢复配置，仅允许 mock 后端。它覆盖条件分支与跳过、档案与导出、配置校验保存、健康指标、语音手动确认、WAV 错误提示、请求编号重试一致性，以及四个页面在 390 px 宽度下的布局。浏览器识别器在页面加载前由测试替身替换，因此不会打开真实麦克风。可用 `BROWSER_EXECUTABLE` 指定已安装浏览器，`UI_ARTIFACTS` 指定截图目录。

V3 编排、队列、模板参数、记忆、生命周期和数据页面使用 `node tests/ui_v3_smoke.cjs` 验证；十个页面均检查 390 px 布局。`node tests/ui_streaming_smoke.cjs` 验证流式语音页面的假媒体输入与纠正/取消链路。控制器和 AudioWorklet 的 Node 测试共 16 项，另有 Python 流式会话边界及 HTTP 协议测试。
