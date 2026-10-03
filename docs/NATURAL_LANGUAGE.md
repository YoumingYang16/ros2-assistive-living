> V5 新增生活辅助服务与四项家居技能。本文保留原有任务平台的说明；新增接口与边界见 [HOME_INTERFACES.md](HOME_INTERFACES.md) 和 [ASSISTIVE_API.md](ASSISTIVE_API.md)。

# V3 中文任务与模型接口

本模块把语言编译为经过验证的任务计划。`DialoguePlanner.interpret(text, context)` 不执行动作；任务执行、持久化会话和请求去重由 `MissionEngine` 负责。默认没有模型请求，也不需要 API 密钥。

## 默认可用的语言能力

| 需求 | 示例 |
|---|---|
| 导航与同义表达 | `带我去会议室`、`导航至前台`、`到仓库去` |
| 连续任务 | `先去前台，接着去仓库，最后回基地` |
| 观察与找物 | `去会议室看看有没有水杯`、`查找水杯在会议室` |
| 巡逻 | `开始巡逻两圈`、`在会议室和走廊巡逻两圈` |
| 等待 | `等候三分钟`、`等待1.5秒` |
| 即时控制 | `立即停止`、`暂停一下`、`继续执行`、`进度怎么样` |
| 地点澄清 | `找水杯` → 系统询问地点 → `会议室` |
| 物体澄清 | `去仓库找一下` → 系统询问物体 → `箱子` |
| 上下文指代 | 先提出 `去仓库找箱子`，随后 `去那里找它` |
| 草稿修改 | 预览 `去会议室找水杯`，再说 `改去仓库` 或 `把会议室改成仓库` |
| 条件搜索 | `先去会议室找水杯，没找到就去仓库，找到后告诉我并返回起点` |

条件搜索的含义可从计划预览中核对：

1. 导航至会议室，检查水杯。
2. 仅在该检查**明确返回 `not_found`** 时，导航至仓库再次检查。
3. 任一检查明确返回 `found` 时，执行相应的返回起点分支。
4. `inconclusive`、超时、失败、跳过都不等于 `not_found`。此时不凭空执行备用地点分支。

若希望无论找到与否都返回，应说：`去会议室找水杯，没找到就去仓库，最后返回起点`。报告来自实际步骤结果；“告诉我”记录报告意图，不虚构一个额外硬件动作。这里的“找到后返回”指整个“告诉我并返回”后续动作只在找到后发生，摘要会明确这一点。

V3 新增以下完整指令：`依次去前台、仓库、会议室找水杯，找到后返回起点`、`重复三次：去前台然后返回起点`、`去会议室，如果失败或超时就去前台`、`去会议室找水杯然后去仓库找水杯，如果两处都没找到就返回起点`。顺序搜索最多十个不同的已配置地点；默认只有明确未找到才继续，可显式添加“即使结果不确定也继续”。

默认理解仍有明确边界：仅支持配置中的地点与物体、最多 100 个展开后的步骤、巡逻/重复 1–10 次、单次等待最长 3,600 秒。支持 `播报：请保持通道畅通`、`生成报告`、`等待定位有效`；`拍照`、`对接充电桩`、`左转90度`、`跟随小王持续五秒` 生成需要适配器声明能力的请求，未接入时不会伪造成功。任意聊天、抓取和开门不属于实现范围。未知尾句会使整条任务被拒绝，已解析的前半句不会提前执行。通用的嵌套条件、分支和模板见 [工作流文档](WORKFLOWS.md)。

调度由引擎在普通语言规划前处理，解析器 `parse_schedule_intent(text, now=None, timezone="Asia/Hong_Kong")` 仅提取明确时间，不执行任务。支持 `十分钟后去会议室`、`每天上午9点巡逻`、`每隔十分钟去前台`、`当前任务完成后去仓库`。返回带时区的 `run_at`、可选 `repeat` 与内层任务 `text`；拒绝过去的“今天”时间、嵌套调度、过短循环及安排停止等即时控制。重启后的待执行队列保持暂停，需操作员恢复。

地点记忆记录的是**上一条计划中的地点**，不代表已到达的位置；实时位置来自适配器。当前版本不依据聊天文字伪造观察记忆。

## 会话与草稿接口

```python
planner = DialoguePlanner(config, provider=False)  # 强制离线规则模式
first = planner.interpret("找水杯", {})
second = planner.interpret("会议室", first.context)
draft = planner.interpret("改去仓库", second.context)
confirmed = planner.interpret("确认执行", draft.context)
```

返回 `PlanningResult`，`kind` 是 `task / stop / pause / resume / status / clarify / answer`。`clarify` 和 `answer` 没有计划。每次返回新的 `context`，调用者负责按会话保存，不能跨会话混用。

上下文包括 `last_target`、`previous_target`、`last_object`、`clarification`、`pending_plan`；引擎注入 `active_mission`。仅草稿允许修订，运行中修改会被拒绝。修订和模型计划都有 `metadata.requires_confirmation=true`；引擎必须保留草稿，明确确认后才能执行。确认入口清除此标记。任务实际提交后清除草稿；`停止` 等控制指令不经过模型。

草稿保存配置指纹；地点或任务配置变化后，旧草稿不能直接确认或修订，需重新预览完整任务。`取消计划` 清除当前会话的待确认草稿、关联的 `pending_schedule` 和澄清问题。单一物体的查找计划声明 `metadata.goal={"kind":"find_object","object_name":"水杯"}`，执行完成和目标找到分别报告；未执行或不确定观察不会被当成目标失败。

## 可选模型：显式启用

使用环境变量配置，不在网页、配置 JSON 或日志中保存密钥。**模型名称没有默认值**，需选择自己已有访问权限的模型。

OpenAI Responses（PowerShell 示例；密钥只在本机环境设置）：

```powershell
$env:VOICE_PATROL_MODEL_PROVIDER = 'openai'
$env:VOICE_PATROL_MODEL = '<你选择的模型名称>'
$env:OPENAI_API_KEY = '<你的 API 密钥>'
$env:VOICE_PATROL_MODEL_TIMEOUT = '15'
python -m robot_voice_patrol --mode mock
```

本地 Ollama：

```powershell
$env:VOICE_PATROL_MODEL_PROVIDER = 'ollama'
$env:VOICE_PATROL_MODEL = '<已安装的本地模型名称>'
$env:VOICE_PATROL_OLLAMA_URL = 'http://127.0.0.1:11434/api/chat'
python -m robot_voice_patrol --mode mock
```

恢复默认模式：将 `VOICE_PATROL_MODEL_PROVIDER` 设为 `none`。只有规则解析无法处理且没有命中拒绝条件的指令才提交给模型。模型可以提供计划、澄清或说明；模型提出的任务先成为待确认草稿。规则能够处理的任务和停止控制不依赖模型服务。

OpenAI 使用官方 HTTPS `/v1/responses`、`text.format.type=json_schema`、`strict=true`、`store=false`；不支持自定义 OpenAI 转发地址，不读取任何已有账号凭据文件。Ollama 使用本机 HTTP `/api/chat`、`format` JSON schema 和 `stream=false`。不自动下载模型，不自动重试外部请求，不跟随 HTTP 重定向。

两条路径都检查完整响应、拒绝/截断、JSON 重复字段、字段类型、未知字段、地点白名单、技能白名单、步骤限制和条件引用，再通过公共 `validate_plan`。模型观察步骤必须先在同一分支明确导航到该地点。超时、无法连接、非法 JSON 或不完整输出均不生成可执行计划。错误信息不回显服务返回的敏感正文。

模型调用只发送当前指令、地点名称/别名、物体词表、执行限制及最少量的语义上下文；不会发送任务数据库和事件日志。OpenAI 开启后，这些请求内容会发送到对应云端 API，计费以你的账户为准。本项目未进行真实付费调用，协议验证使用本地假服务器，不能据此宣称某个在线模型的语言准确率。

官方接口依据：[OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs)、[Ollama Structured Outputs](https://docs.ollama.com/capabilities/structured-outputs)、[Ollama Chat API](https://docs.ollama.com/api/chat)。

## 软件评估

```bash
python -m unittest discover -s tests -v
python evals/run.py --output evals/latest-report.json
```

`evals/cases.json` 含 179 条人工选定的语义回归用例，覆盖原有语言能力、多地点搜索、复合条件、有界重复、失败/超时备用任务、技能请求及整条拒绝。用例检查目标顺序、观察物体、等待时间、条件结果、目标元数据、失败策略和是否需要确认，而不只是判断是否抛异常。评估器强制关闭模型，报告类别通过数、错误接受任务数及延迟 p50/p95。调度、模板参数、三值逻辑、版本兼容和模型结构的边界由 `tests/test_workflow_v3.py` 另外验证。

`evals/baseline-report.json` 是本次软件运行的记录。它证明这些已列明样例的行为，**不代表真实语音识别准确率、通用自然语言准确率或机器人实机成功率**。加入自己的口语、地点和误识别样本后应重新运行；不要通过删除失败用例来提高报告数字。

`schemas/model-output.schema.json` 给出 V3 模型完整响应契约，包含十个内置技能、封闭参数结构、复合条件和失败策略；请求使用 V3 schema，校验器仍接受完整的旧 V2 模型响应。`schemas/plan-v3.schema.json` 给出 V3 导出计划结构；V1/V2/V3 计划均可导入。`schemas/plan-v2.schema.json` 保留旧格式说明。JSON schema 之外，条件只能引用此前的步骤，找到/未找到/不确定必须引用指定物体的观察，这些约束由 Python 语义验证器检查。
