# 日常生活辅助服务 API（V7 / 7.0.0）

生活辅助服务保存提醒、个人需求、人工协助请求、异常报告、辅助设备台账、主动平安确认和本人核对记录，并生成本机待办交接摘要。它独立于机器人运动任务队列：机器人暂停、正在执行导航或等待硬件时，提醒依然可以到时。默认通信模式为 `local_only`；不会拨号、发消息、购物付款或执行身体接触照护。

## 服务入口

```python
service = AssistiveService(store, start=True, clock=None)
service.snapshot()                  # 当前可见记录、汇总计数、最近事件与后台健康
service.catalog()                   # 12 领域、95 场景、14 套清单、边界与覆盖审查
service.preview(text)               # 无写入的规则预览；不认识则返回 None
service.command(text, request_id)    # 保存生活指令；不认识则返回 None
service.action(body)                # 严格结构化操作
service.tick()                      # 推进提醒、人工响应等待和主动平安确认的期限
service.close()                     # 应先于 MissionStore.close() 调用
```

Web 集成使用 `GET /api/assistive`、`GET /api/assistive/catalog` 和 `POST /api/assistive/action`，V7 本机默认地址为 `http://127.0.0.1:8773`。V7 新增只读 `GET /api/assistive/history`、`GET /api/assistive/history/meta` 和 `GET /api/assistive/history/{id}`；没有新增外部通信路由。Python/CLI 与 ROS 命令话题也可进入服务；网页只是一个客户端。所有写操作都应带唯一 `request_id`，同一次网络重试复用同一个 ID。服务允许最长 256 字符，便于附加会话前缀。

已有记录的多轮生活对话经过 `MissionEngine.submit(text, request_id, session_id)`，对应 `POST /api/command`；预览为 `POST /api/plan`。请在连续操作中使用同一个 `session_id`。直接 `service.command()` 保留单句创建兼容入口，多轮关联由 `AssistiveDialogue` 处理。三项 V7 使用流程和 HTTP 示例见 [API.md](API.md)。

保存记录的操作成功返回下列结构；`handover.build` 返回 `assistive.type=handover` 和 `assistive.report`，不创建一条照护完成记录：

```json
{
  "ok": true,
  "message": "面向用户的明确结果和边界说明",
  "assistive": {"type": "reminder", "record": {"id": "...", "kind": "reminder", "state": "pending"}}
}
```

重复请求返回原回执，并带 `deduplicated: true`。相同 ID 携带不同操作内容会拒绝。状态可能随后发生变化，因此查看当前状态应重新读取 `snapshot()`，不要把保存的操作回执当成实时状态。

## 操作字段

所有操作包含 `op`；下表中的 `?` 表示可选。未知字段、错误类型、非有限数字和越界值均会拒绝。

| op | 字段 | 含义 |
|---|---|---|
| `reminder.create` | `title`, `due_at` **或** `delay_seconds` **或** `calendar`, `category?`, `repeat_seconds?`, `end_at?`, `grace_seconds?`, `note?` | 首次时间三选一。类别取目录中的 `reminder_categories`。固定重复间隔至少 60 秒；日历与固定间隔互斥。|
| `reminder.update` | `id`, `title?`, `due_at?`, `delay_seconds?`, `repeat_seconds?`, `calendar?`, `end_at?`, `expected_revision?` | 至少提供一个可修改字段；只修改未结束提醒。规则与截止可用 `null` 清除，修订冲突拒绝覆盖。|
| `reminder.ack` | `id`, `expected_revision?` | 本人确认已经知晓。不能等同已服药、已饮水或已完成身体活动。|
| `reminder.snooze` | `id`, `seconds?`, `expected_revision?` | 稍后提醒，默认 600 秒，允许 30–86400 秒，不能超过截止。|
| `reminder.cancel` | `id`, `expected_revision?` | 取消当前提醒及其后续重复。|
| `assistance.create` | `category`, `detail?`, `urgency?`, `contact_id?`, `consent?`, `escalate_seconds?` | 类别取 `help_categories`。紧急程度为 `normal/urgent`。指定联系人必须明确提供 `consent: true`。|
| `assistance.report` | `id`, `status`, `note?` | `status` 只能为 `acknowledged/resolved/escalated`。这是本机使用者报告，具有 `local_user_report` 来源，不冒充外部人员回执。|
| `assistance.cancel` | `id` | 取消本地待处理请求。|
| `profile.update` | `changes` | 部分更新个人偏好，见下方。|
| `contact.save` | `name`, `id?`, `relationship?`, `contact_hint?` | 本地联系人，现有 ID 表示编辑。`contact_hint` 供人工查看，不会自动发送。|
| `contact.remove` | `id` | 从可选联系人中移除，保留历史请求的引用。|
| `checklist.start` | `routine` **或** `title` + `items` | 内置流程为下表所列 14 套；自定义项目 1–30 个。同一天未完成的内置清单会复用。|
| `checklist.check` | `id`, `item_id`, `checked` | 逐项核对；全部勾选才显示完成。|
| `checklist.reset` | `id` | 清空该清单的勾选状态。|
| `need.add` | `title`, `quantity?`, `category?`, `note?` | 数量是文本。类别为 `shopping/care/repair/other`。仅记录需求，不产生订单或付款。|
| `need.check` | `id`, `checked` | 本人更新需求处理记录。|
| `need.remove` | `id` | 从当前列表移除，保留数据库记录。|
| `checkin.create` | `feeling`, `note?`, `needs_help?` | 状态为 `good/okay/uncomfortable/need_help`。需要帮助时原子创建关联的本地协助请求；没有诊断字段内容或治疗建议。|

`calendar` 对象包含 `weekdays`（1 为周一、7 为周日）、`local_time`（HH:MM）和 `timezone`（IANA 时区）；`end_at` 为包含边界的带时区时间。`expected_revision` 是正整数，提醒返回的 `revision` 在每次修改或计时状态变化时更新；旧数据默认按 1 处理。完整创建、修改、知晓、延后、夏令时和截止语义见 [REMINDER_CALENDAR.md](REMINDER_CALENDAR.md)。

## V7 历史与多轮生活操作

历史列表支持 `kind/state/query/since/until/limit/offset`，所有筛选组合会严格校验。日期依据 `updated_at`，带时区且含边界；`limit` 1–100，偏移 0–1,000,000，HTTP 分页字段只接受十进制数字。包含已移除记录，返回 `records/total/limit/offset/has_more/meta`。

元数据路由不接受查询参数，返回 `kinds/states/kind_states` 标签和规则。详情路由只接受 `event_limit/event_offset`，返回 `record/events/event_total/event_limit/event_offset/events_has_more/meta`，审计按记录独立分页。完整字段、隐私过滤和只读保证见 [ASSISTIVE_HISTORY.md](ASSISTIVE_HISTORY.md)。

生活对话支持知晓、取消和延后已有提醒、修改标题或日期与周历、主动确认或取消平安等待、记录协助响应与处理结果、核对清单项目和用品准备状态。目标不明确时返回 `kind=clarify`、`needs_clarification=true` 和候选 `options`，需要在同一会话选择；旧候选在记录变化后失效。语音只是文字指令的输入方式，既有机器人计划仍使用原确认流程。

完整会话契约及句式见 [ASSISTIVE_DIALOGUE.md](ASSISTIVE_DIALOGUE.md)。相同日历规则的编辑回传不重新排期，避免完整表单覆盖尚未确认的提醒；只改标题或截止时也保留当前通知。

## V6 新增操作

以下 `op` 由 `living_operations.py` 处理，沿用严格字段校验、事务、审计和 `request_id` 去重。

| op | 字段 | 实际结果与边界 |
|---|---|---|
| `incident.create` | `category`, `detail?` | 保存本人异常报告，同时原子创建 `support_interruption` 人工协助。相同类别已有 `open/acknowledged` 记录时复用，不覆盖原详情、不重复建协助。|
| `incident.ack` | `id`, `note?` | 记为本人知晓 `acknowledged`，尚未解决。|
| `incident.resolve` | `id`, `note?` | 记为本人报告已解决 `resolved`；不独立确认物理恢复，不自动结束关联协助。|
| `equipment.add` | `title`, `category`, `service_due_at?`, `note?` | 登记台账；给出带时区维护日期时，同时创建关联提醒。类别为 `mobility_aid/communication/home_device/other`。|
| `equipment.service` | `id`, `next_due_at?`, `note?` | 保存本人维护记录，取消旧的未结束维护提醒，并按新日期建提醒；不填新日期则不再安排。设备必须为 `active`。|
| `equipment.retire` | `id` | 台账进入 `completed`，取消未结束维护提醒；不控制真实设备开关。|
| `wellbeing.start` | `seconds?`, `title?` | 本人主动开启一次确认等待；默认 1800 秒，范围 30–86400 秒；默认标题“本人平安确认”。|
| `wellbeing.confirm` | `id` | 对 `waiting/overdue` 记录保存本人确认并进入 `completed`；不自动解决已关联的协助。|
| `wellbeing.cancel` | `id` | 结束 `waiting/overdue` 等待并进入 `cancelled`；此前生成的协助仍需单独处理。|
| `handover.build` | `note?` | 生成当前本机摘要；返回 `assistive.report`。不修改生活记录、不发送消息。若携带 `request_id`，仍会保存幂等回执。|

异常类别固定为：`power_failure`（停电）、`network_failure`（网络中断）、`device_failure`（辅助设备故障）、`blocked_route`（通道或出口受阻）、`caregiver_absent`（照护人员未到）、`extreme_weather`（恶劣天气影响）、`lost_communication`（呼叫设备不可用）。通道受阻和呼叫设备不可用创建 `urgent` 协助，其余默认 `normal`；全部默认未发送。

设备名称和平安确认标题最长 100 字符；备注和异常详情最长 500 字符。维护日期继承提醒创建的时间限制：过去一天至未来 366 天。日期或字段校验失败时，关联提醒与台账修改一起回滚。

## 内置清单与个人偏好

| routine ID | 清单 |
|---|---|
| `morning` | 晨间清单 |
| `night` | 睡前清单 |
| `outdoor` | 出门清单 |
| `return_home` | 回家清单 |
| `meal` | 用餐清单 |
| `home_safety` | 居家检查清单 |
| `medical_visit` | 就医准备清单 |
| `travel` | 旅行准备清单 |
| `work_study` | 工作学习准备清单 |
| `social` | 社交活动准备清单 |
| `outage_prepare` | 停电备用准备清单 |
| `evacuation_prepare` | 应急撤离准备清单 |
| `therapy_prepare` | 个人活动准备清单 |
| `visitor_prepare` | 访客准备清单 |

个人偏好字段包括 `display_name`、`language` (`zh-CN/en`)、`text_scale` (1–2)、`high_contrast`、`reduced_motion`、`speech_enabled`、`speech_rate` (0.5–1.5)、`simple_mode`、`switch_scan_seconds` (1–10)、`timezone` (`Asia/Hong_Kong/UTC`)。`confirmation_required` 必须保持 `true`；这个偏好不会取消机器人自身的执行确认和能力检查。偏好存储并不意味着所有外部客户端自动支持这些显示选项。

## 持久化与状态含义

服务在共享数据库中创建 `assistive_records/assistive_receipts/assistive_events/assistive_meta/assistive_delivery_receipts`，单独记录领域版本 `1`，不改变现有任务数据库的 `user_version=3`。现有整库备份包含全部生活辅助数据。每项写入、领域审计和幂等回执在同一个 SQLite 事务内完成。

提醒状态为 `pending → due → missed`，本人确认后进入 `acknowledged`；重复提醒确认后安排下一个未来时点，跳过已经错过的周期，不连续补发。到时通知的 `channel` 为 `local_dashboard`，`audible_confirmed` 保持 `false`：显示提醒不证明听到或执行了提醒内容。服务重启会恢复到时/逾期状态，不重放机器人动作。

人工协助默认进入 `created`；超出等待时间进入 `escalated`，仅表示本地界面应突出提示，没有联系第二个人或公共急救服务。使用者可记录响应、解决或取消。只有显式集成下述可信宿主接口并验证回执后，才允许记录 `delivered`。外部发送成功和照护完成是不同状态。

V6 新增记录沿用上述表和领域版本；`snapshot()` 增加 `incidents`、`equipment`、`wellbeing` 列表与对应状态计数。

- **异常**：`open → acknowledged → resolved`，也可从 `open` 直接报告解决。来源为 `local_user_report`，`sensor_confirmed=false`、`physical_recovery_confirmed=false`。结束异常不等于关联人员请求已处理，后者需单独报告状态。
- **设备**：在用台账为 `active`，停用后为 `completed`；`hardware_verified=false`。`service_reports` 保留最近 100 条本人记录，每条 `independently_verified=false`；维护提醒 ID 保存在 `reminder_id`。
- **主动确认**：`waiting` 到期进入 `overdue`，在同一事务中只创建一次紧急程度为 `urgent` 的本地协助。若协助台账已满，先保留 `overdue` 和 `escalation_error`，`assistance_id` 仍为空；腾出容量后自动重试创建，其他提醒照常推进。页面明确显示协助记录尚未建立，不伪造成功。`source=user_opted_in`、`health_inference=null`，没有自动传感器推断。本人确认或取消后结束本次等待；此前产生的协助不自动结束。
- **重启**：恢复已有记录并推进已经到期的确认等待；不会重放设备动作。持续计时需要服务存活，单次 CLI 命令退出后不在后台继续运行。

### 本机交接摘要

`handover.build` 查询数据库中的未结束记录，包含 `reminders`、`assistance`、`incidents`、`equipment`、`needs`、`checklists` 和 `wellbeing`，并给出各列表 `counts`。提醒只保留截止生成时刻后 24 小时内的未结束项，包含已经逾期的提醒；知晓但未解决的协助和异常仍保留。设备保留在用台账。不受首页每类 250 条显示限制影响。

摘要包括 `generated_at`、可选输入的 `note`、`source=local_records_not_independently_verified` 和 `external_messages_sent=0`。采用字段白名单保留 ID、状态、标题、类别、期限、来源、关联 ID 及清单项目等，不默认复制联系人地址和记录中的私密备注；标题、清单项目和显式填写的摘要备注仍可能包含个人内容。首页可下载这份 JSON，生成或下载都不会将它发送给其他人。

摘要表示生成时刻的本机记录，不是实时订阅。重新获取当前摘要应使用新的 `request_id`；重用同一个 ID 会按幂等规则返回原来的摘要。

快照每类最多显示 250 条，优先未完成记录，并提供数据库完整状态计数；历史不是全部自动展开。每类未完成记录上限 1000，幂等请求回执和通信回执各上限 10000。接近上限时，应导出备份并更换数据文件保存后续记录；系统不会自动丢弃幂等回执。提醒文本最长 160 字符，备注最长 500 字符；完整单次操作上限 24 KB。

## 通信接口：默认不接通

`assistive_connectors.py` 提供 `AssistanceTransport` 协议和签名回执验证器。应用没有默认网络实现，也没有在后台调用该协议。真实部署的可信宿主需显式配置提供方身份、至少 32 字节的共享密钥，并实现自己的通信渠道。不要把密钥交给浏览器，也不要增加未认证的回执 HTTP 路由。

```python
from robot_voice_patrol.assistive_connectors import DeliveryReceiptVerifier

# 只有请求中已选择有效联系人且本人同意，才能准备信封。
envelope = service.delivery_envelope(request_id)  # 只读，不发送
# 宿主自行接入 transport.send(envelope)，不在默认应用中运行。
verifier = DeliveryReceiptVerifier("configured_provider", secret_key_bytes)
result = service.record_delivery_receipt(signed_receipt, verifier)
```

签名回执字段固定为 `version=1, receipt_id, request_id, contact_id, provider, status, occurred_at, delivery_id, signature`。`status` 只允许 `delivered/acknowledged/failed`。签名是排除 `signature` 后按键排序、无多余空格、UTF-8、保留 Unicode 的 JSON 文本的 HMAC-SHA256 小写十六进制摘要。提供方可用 `sign_receipt(body, key)` 生成。

默认校验时间窗口为过去 300 秒至未来 30 秒。服务还验证请求与联系人绑定、本人同意、请求未结束、回执 ID 不冲突。重复有效回执不会重复更新；失败不能覆盖已验证送达。送达后若仍未收到人员响应，也会按原等待时点进入本地升级状态。经签名验证的回执仅证明配置的提供方报告了该状态，不证明照护已经完成。

## 与机器人硬件控制的关系

上述 API 管理生活记录，不直接执行物理动作。取送、家居控制和导航经 `MissionEngine → Ros2Adapter` 进入 ROS 2 Action 客户端；V6 的 `HardwareGatewayNode` 提供 `ExecuteSkill` ActionServer 和能力服务，通过启动时显式加载的可信驱动工厂接入设备。

网关可校验设备读回与终态证据、保存去重回执、等待取消确认，并在物理状态未知时阻止新动作。部署方仍需提供厂商驱动、Nav2、定位与感知服务；网页可以关闭，ROS 任务与技能节点独立运行。完整驱动、接口和验证边界见 [HARDWARE_GATEWAY.md](HARDWARE_GATEWAY.md) 及 [HOME_INTERFACES.md](HOME_INTERFACES.md)。

## 验证范围

`tests/test_assistive_service.py` 覆盖提醒、重复与重启、事务和去重、联系人同意、本人报告来源、清单、需求、偏好、状态记录、独立计时、备份及签名回执约束。

`tests/test_living_operations.py` 覆盖 12 领域唯一映射、新入口无副作用预览、异常关联协助与重复报告、设备维护提醒原子更新、摘要字段与完整性、主动确认到期/确认/取消、记录重启恢复等。`scripts/verify_assistive.py` 按运行时目录验证 95 项示例入口；结果见 [ASSISTIVE_COVERAGE.json](ASSISTIVE_COVERAGE.json)，整体报告见 [VALIDATION.md](VALIDATION.md)。

上述是隔离软件验证。没有实际联系人发送、电话拨打、身体照护、原生 ROS/DDS 或真实硬件运行，不据此声称穷尽每个人的生活需求。
