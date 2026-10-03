# 提醒修改与本地日历（V7）

生活提醒仍由 `AssistiveService` 管理，独立于机器人动作和运动队列。V7 增加提醒修改、每周日历、结束日期及可选修订号检查；兼容旧的 `repeat_seconds` 和已保存记录。这些操作只修改本机记录，不产生机械任务、诊断或对外通信。

## 结构化字段

使用 `POST /api/assistive/action` 或 `service.action(body)`。每次新操作使用新 `request_id`，网络重试保留相同 ID；同 ID、不同内容会被拒绝。修改、审计与去重回执共用同一事务，失败时一起回滚。

| 操作 | 必填 | 可选 |
|---|---|---|
| `reminder.create` | `title`，以及 `due_at` / `delay_seconds` / `calendar` 三选一 | `category`, `repeat_seconds`, `end_at`, `grace_seconds`, `note`, `request_id` |
| `reminder.update` | `id`，以及至少一个可修改字段 | `title`, `due_at`, `delay_seconds`, `repeat_seconds`, `calendar`, `end_at`, `expected_revision`, `request_id` |
| `reminder.ack` | `id` | `expected_revision`, `request_id` |
| `reminder.snooze` | `id` | `seconds`, `expected_revision`, `request_id` |
| `reminder.cancel` | `id` | `expected_revision`, `request_id` |

所有字段严格校验，未知字段被拒绝。`update` 不接受直接写 `state`、`revision` 或伪造已知晓。标题最多 160 字符；单次操作沿用服务的 24,000 字节上限。

`due_at` 和 `end_at` 是带时区的 ISO 时间，存储时转为 UTC。相对 `delay_seconds` 允许 0 至 366 天；创建或重新排期的首次时间须处于过去一天至未来 366 天内。`repeat_seconds` 仍允许 60 秒至 366 天的固定间隔。`end_at` 为包含该时刻的截止边界，不能早于当前待确认提醒的时间。

日历对象必须且只能包含以下三项：

```json
{
  "weekdays": [1, 3, 5],
  "local_time": "08:00",
  "timezone": "Asia/Hong_Kong"
}
```

`weekdays` 是不重复的 ISO 星期整数，1 为周一、7 为周日，至少选一天；保存时排序。`local_time` 严格使用 `HH:MM`。`timezone` 必须是运行环境可解析的 IANA 时区。每天日历可使用 `[1,2,3,4,5,6,7]`。

创建日历提醒不同时提供 `due_at`、`delay_seconds` 或非空 `repeat_seconds`。系统从当前时刻计算严格在未来的下一次；截止前没有可用时刻则拒绝创建。

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

## 修改语义

`reminder.update` 适用于尚未结束的 `pending/due/missed` 记录。已经 `acknowledged/cancelled/completed` 的记录不能通过修改复活，应新建提醒。

- 仅改标题或结束日期，保留当前待确认状态和通知；不会自动表示已知晓。
- 提供新的 `due_at` 或 `delay_seconds` 会重新排首次时间、清除旧通知并回到 `pending`。两者不能同时提供。
- 提供发生实际变化的非空 `calendar` 会替换周历、清除固定间隔，并从现在重新计算下一次。规范化后与原规则相同的日历不会重新排期，整张表单仅修改标题或截止时也不会跳过当前待确认项。
- 提供非空 `repeat_seconds` 会切换为固定间隔、清除周历，保留当前首次时间；可同时提供新的首次时间。间隔实际改变时以当前这次时间为新基准，包括此前已经延后的时间；未改变的间隔不重设基准。
- `calendar:null` 清除日历，`repeat_seconds:null` 清除固定间隔，`end_at:null` 清除截止；清除某项不会隐式清除其他项。
- 当前仍为日历提醒时，不接受另给一次性的 `due_at/delay_seconds`。改为单次须显式清除日历；若想确定变为单次，可同时清除两类重复规则。
- 将截止日期改到当前待确认时间之前会被拒绝。若希望立即结束待确认提醒，应取消；不能利用截止日期静默抹掉待办。

```json
{
  "op": "reminder.update",
  "request_id": "edit-water-2",
  "id": "从实际记录取得的提醒ID",
  "expected_revision": 3,
  "title": "新的提醒标题",
  "calendar": null,
  "repeat_seconds": null,
  "delay_seconds": 1200,
  "end_at": null
}
```

每条提醒返回 `revision`，新记录为 1，旧记录缺此字段时按 1 处理。编辑、知晓、延后、取消以及计时线程将其从待处理改为到时/逾期，都会递增修订号。可选的 `expected_revision` 必须是正整数；不匹配时拒绝并要求重新读取记录。它保护的是整个提醒状态，不能只在标题变化时更新客户端保存的版本。

请求去重先于版本检查：同一请求重试返回原回执，即使提醒后来已经变化；读取当前状态须再调用查询接口。更新审计记录包含修改前后的标题、时间、规则、截止、状态和修订号。

## 到时、知晓、延后和结束日期

到时仍为 `pending → due → missed`。服务不因错过几个周期而创建一串历史提醒：当前未确认项保留为一条记录，重复规则在本人确认知晓后计算下一次严格未来的时间，跳过已经错过的周期。

`reminder.ack` 表示知晓，不表示已经喝水、服药或完成活动。尚未到时的 `pending` 提醒不能提前确认。到最后一次时点或已经超过截止后，知晓使记录保持 `acknowledged`，不再排下一次；截止本身不会清除已到时但仍未确认的记录。

`reminder.snooze` 延后当前一次，默认 600 秒，允许 30–86400 秒。新的提醒时间超过 `end_at` 时拒绝，原记录保持不变。`scheduled_due_at` 保留原周期基准：延后不会把固定间隔永久漂移，也不会在提前延后并确认后再次安排同一个原日历时点。此字段是服务管理字段，不接受客户端直接设置。

重启时恢复记录和期限，已到时记录进入到时/逾期状态，不补刷旧周期、不重放机械动作。计时需要服务运行；单次 CLI 命令退出后不会在后台继续执行提醒。时区数据暂时缺失时不会擅自改成其他时区，相关排期操作会明确失败并保留原记录。

## 夏令时与时区策略

每周日历枚举当地日期，转换后保存 UTC 时刻，不通过固定 86400 秒计算下一天。

| 情况 | 行为 |
|---|---|
| 春季跳时导致本地时刻不存在 | 跳过该当地日期，使用下一个符合星期规则的有效日期；不平移到其他钟点 |
| 秋季回拨使同一本地时刻出现两次 | 只选第一次 UTC 时刻；第一次已过后不会选择第二次 |
| 当前时间恰好等于日历时刻 | 新创建/重新排期选择严格未来的一次；已有该时刻的提醒仍正常到时 |
| 候选时刻等于截止 | 允许；晚于截止则不再安排 |
| 环境没有对应 IANA 时区数据 | 除下述固定回退外明确失败，不悄悄换时区 |

`UTC/Etc/UTC` 可直接解析；`Asia/Hong_Kong` 在缺失时区数据时使用现代香港 UTC+08:00 固定回退。其他时区需环境提供数据，不能将部署机的时区当作替代。历史香港时区规则不属于该固定回退所支持的用途；提醒排期受当前时间附近的范围限制。

原“每天9点提醒我…”文字句式仍保留固定 86400 秒格式，兼容已有调用；现有个人时区只支持 Hong Kong/UTC。需要其他 IANA 时区或夏令时日历语义时，使用 `calendar` 的全周规则。

## 文字入口与公开帮助函数

服务新增创建入口：

```text
每周一三五8点提醒我喝水
每周一和周三上午八点提醒我喝水
每星期一、星期三下午两点三十分提醒我按自己的安排活动
开始20分钟平安确认
```

这些是受约束句式，不把否定、条件或组合命令的正向片段单独执行。提醒修改及已有记录选择由生活对话层组合结构化 API。

`robot_voice_patrol.reminder_schedule` 提供无副作用的公开函数：

- `validate_calendar(value)`：严格校验并规范化星期顺序。
- `calendar_timezone(name)`：解析时区或透明失败。
- `next_calendar_occurrence(calendar, after, *, end_at=None)`：参数时间为带时区 `datetime`，返回下一 UTC 时刻；截止前没有候选时返回 `None`。
- `parse_clock_text(text)`：将 `上午八点`、`14:30` 等转成 `HH:MM`；未匹配时返回 `None`，越界时拒绝。
- `parse_weekly_reminder(text, timezone_name="Asia/Hong_Kong")`：完整句转成 `reminder.create` 提案，不写入记录；未匹配返回 `None`。

## 定向验证

`tests/test_reminder_calendar.py` 验证周历跨时区、春秋夏令时、严格截止、漏周期、延后保留基准、修订冲突、并发修改、幂等重试、无效更新回滚、旧记录兼容和重启。并运行原生活辅助及生活支持测试检查兼容性：

```sh
python -m unittest tests.test_reminder_calendar tests.test_assistive_service tests.test_living_operations
```

测试使用本机内存或临时数据库及可控时间，不连接机器人、不打开麦克风、不发送消息。
