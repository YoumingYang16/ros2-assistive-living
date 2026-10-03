# V7 生活操作、提醒日历与历史接口

默认服务地址为 `http://127.0.0.1:8773`，由项目目录运行 `start_home.ps1` 或 `python -m robot_voice_patrol --home --mode mock --fixture-skills --port 8773` 启动。请求与响应使用 UTF-8 JSON。网页只是一种客户端；任务引擎、CLI 和 ROS 2 命令话题共用相应的软件处理链。

这份说明集中列出 V7 补齐的三项功能。原生活操作契约见 [ASSISTIVE_API.md](ASSISTIVE_API.md)，ROS 2 设备控制见 [HARDWARE_GATEWAY.md](HARDWARE_GATEWAY.md)。所有验证结论限于软件与接口，不能据此声称实机已通过验证，或所有人的日常生活需求都已穷尽。

## 1. 用文字或语音继续操作生活记录

| 方法与路径 | 请求 | 结果 |
|---|---|---|
| `POST /api/plan` | `text`, `session_id?` | 无生活记录写入的预览；需要选项时返回候选 |
| `POST /api/command` | `text`, `request_id?`, `session_id?` | 按指令保存生活操作或更新会话选择；机器人计划继续走原确认流程 |
| `POST /api/assistive/action` | `op` 和对应操作字段，`request_id?` | 明确记录 ID 的结构化生活操作 |

连续对话应使用同一个 `session_id`。每次新提交使用唯一 `request_id`，同一请求的网络重试复用该 ID。不能给新内容复用旧 ID。语音识别结果也提交到相同文字链路，不绕过候选选择、参数或状态校验。

```json
{"text":"确认提醒喝水","session_id":"home-user","request_id":"water-ack-1"}
```

常用完整句式包括：

- “确认提醒喝水”“知道了”“提醒喝水稍后十分钟”“取消提醒喝水”。
- “把喝水提醒改名为按自己的安排饮水”“把喝水提醒改到明天上午八点”。
- “把喝水提醒改成每周一和周三上午八点”。
- “我在”“确认平安”“取消平安确认”。
- “有人回应了”“协助已解决”“取消协助请求”。
- “完成晨间清单第一个项目”“取消勾选晨间清单第一个项目”。
- “纸巾已备好”。

实际名称取自用户已保存记录；例如清单模板的显示名或本人自定义标题。仍使用受约束的完整句式，不保证任意口语、否定或复合句都能识别。未知输入不会仅截取一个正向片段当作已授权操作。

如果有多条候选，响应包含 `kind: "clarify"`、`needs_clarification: true`、`options` 和说明。用户可在同一会话说“第一个”“第二个”或选按钮，超过五条时需补充准确名称。候选和当前记录关联，记录已经更新、结束或进入下一周期时，旧序号或旧按钮不会改动下一条记录。会话选择时效为五分钟，可说“取消生活选择”结束当前选择而不修改生活记录。

预览中的 `execution_command` 或候选 `options` 可能包含关联校验标记，客户端应原样提交所选命令，不自行构造或删除标记。通用“确认执行”仍属于机器人计划确认，不用来跳过生活记录的具体选择。

“知晓提醒”只记录本人知晓，“协助已解决”只记录本人报告；它们不证明已经喝水、服药、身体护理完成或外部人员确实到场。

完整对话句式、会话恢复和选项校验契约见 [ASSISTIVE_DIALOGUE.md](ASSISTIVE_DIALOGUE.md)。

## 2. 创建、修改和结束周期提醒

在生活首页创建提醒时，选择单次、固定间隔、每天固定时间或每周指定几天；可另设截止时间。当前列表和历史详情内的“修改提醒”可以编辑未结束的安排。编辑中的记录发生变化时，先重新读取当前记录再保存。

```json
{
  "op": "reminder.create",
  "request_id": "weekly-water-1",
  "title": "按自己的安排喝水",
  "calendar": {
    "weekdays": [1, 3, 5],
    "local_time": "08:00",
    "timezone": "Asia/Hong_Kong"
  },
  "end_at": "2026-12-31T23:59:59+08:00"
}
```

创建时 `due_at`、`delay_seconds`、`calendar` 三选一。日历与非空 `repeat_seconds` 互斥；`weekdays` 使用 1 至 7 表示周一至周日。日期示例需改为实际使用时的未来安排。

从接口获取真实 ID 和当前 `revision` 后修改：

```json
{
  "op": "reminder.update",
  "request_id": "weekly-water-edit-1",
  "id": "实际提醒ID",
  "expected_revision": 1,
  "title": "调整后的饮水提醒",
  "calendar": {"weekdays": [2, 4], "local_time": "09:00", "timezone": "Asia/Hong_Kong"}
}
```

`expected_revision` 可用于编辑、知晓、延后和取消操作；不匹配会拒绝覆盖。`calendar:null`、`repeat_seconds:null`、`end_at:null` 分别清除对应规则，已知晓或取消的终态记录不能通过编辑重新启用。

提交与当前规范化结果完全相同的日历规则，不会重新排期或清除正在等待确认的通知。修改标题或截止时，客户端可只提交实际修改字段；即使完整表单同时回传同一日历，也保留当前提醒状态。

提醒到时等待本人确认，错过周期不会自动生成连续催促或任务。本人知晓后安排下一次严格未来的时刻；最后一次知晓后结束。截止日期不会自动把尚未确认事项清除。固定间隔的延后保留原周期基准，日历按指定当地日期计算。完整修改字段、夏令时策略和回退边界见 [REMINDER_CALENDAR.md](REMINDER_CALENDAR.md)。

## 3. 分页查询完整生活历史

| 路由 | 参数 | 响应核心字段 |
|---|---|---|
| `GET /api/assistive/history/meta` | 不接受参数 | `kinds`, `states`, `kind_states`, 日期与分页规则 |
| `GET /api/assistive/history` | `kind?`, `state?`, `query?`, `since?`, `until?`, `limit?`, `offset?` | `records`, `total`, `limit`, `offset`, `has_more`, `meta` |
| `GET /api/assistive/history/{id}` | `event_limit?`, `event_offset?` | `record`, `events`, `event_total`, `event_limit`, `event_offset`, `events_has_more`, `meta` |

例如查询最近更新的提醒，每页 25 条：

```text
GET /api/assistive/history?kind=reminder&limit=25&offset=0
GET /api/assistive/history?kind=reminder&limit=25&offset=25
GET /api/assistive/history/实际记录ID?event_limit=20&event_offset=0
```

使用 HTTP 客户端的查询参数编码功能处理中文、`+08:00` 等值。不要直接拼接未编码的 `+`，因为它在 URL 查询中可能被解释为空格。

`limit/event_limit` 为 1–100，`offset/event_offset` 为 0–1,000,000；HTTP 值只接受十进制数字。未知、重复、不相容、超界和空参数会拒绝，`query=` 可表示不限制文字。详情 ID 从真实记录取得。未找到记录按当前服务错误契约返回 `400`；不存在的路由返回 `404`。

日期按 `updated_at` 筛选，两端包含边界，必须使用带时区 ISO 时间。首页日期选择按浏览器本机时区转换成相应日的开始和结束；每周提醒的日历时区独立选择。记录按更新时间由新到旧排列，同一时间按 ID 排序。历史包括已取消、已结束、已移除记录；逐页查询不受当前面板每类 250 条摘要限制。

文字搜索限于标题、详细内容、备注、联系人姓名和异常处理备注，不搜索 ID、联系地址或整个 JSON。结构化联系方式提示、凭据和原始签名回执不会通过历史接口返回；本人自由填写的内容保持原样。单条记录的审计同样分页，`events_has_more` 为真时应继续请求后页。列表与详情完全只读，不触发提醒到时或其他状态更新。详细契约见 [ASSISTIVE_HISTORY.md](ASSISTIVE_HISTORY.md)。

## 接入和验证边界

接口默认仅绑定本机，继续执行 Host/Origin 检查。这里没有新增多用户远程访问授权或消息发送服务。生活记录管理独立于机器人运动队列；实际运动通过 ROS 2 Action、能力服务、驱动接入和设备反馈完成。

对应单元与接口测试位于 `tests/test_assistive_history.py`、`tests/test_assistive_history_http.py`、`tests/test_reminder_calendar.py` 和生活对话测试；浏览器测试覆盖页面操作。当前累计验证数量以 [VALIDATION.md](VALIDATION.md) 和本轮生成报告为准，不把历史版本的计数当作本次重跑结果。
