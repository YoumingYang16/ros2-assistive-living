# 生活记录历史查询

V7 的历史模块直接查询本机 SQLite 中的生活辅助记录，支持分类、状态、文字、更新时间范围和分页。它与首页每类最多显示 250 条的摘要分开，不会因此漏掉较早记录；已经移除、取消或结束的记录仍可检索。查询不调用 `tick()`，不改变任何记录，也不建立新的审计或操作回执。

历史中的“已知晓”“本人记录已处理”等状态保留原始来源，不能解释为系统已经核实外部联系、实际照护或机器人动作完成。

## Python 接口

```python
from robot_voice_patrol.assistive_history import (
    history_metadata, query_history, record_detail,
)

page = query_history(
    service,
    kind="reminder", state="acknowledged", query="喝水",
    since="2026-10-01T00:00:00+08:00",
    until="2026-10-03T23:59:59.999999+08:00",
    limit=25, offset=0,
)
detail = record_detail(service, record_id, event_limit=50, event_offset=0)
```

`query_history()` 返回 `records`、`total`、`limit`、`offset`、`has_more`、`meta`。默认不限定类型、状态和日期；默认每页 25 条。`limit` 必须是 1 至 100 的整数，`offset` 必须是 0 至 1,000,000 的整数，不接受布尔值、浮点数或字符串。

`kind` 支持 `contact`、`reminder`、`assistance`、`checklist`、`need`、`checkin`、`incident`、`equipment`、`wellbeing`。缺省用 `None`，不要传空字符串或 `all`。`history_metadata()` 返回可用于下拉框的 `kinds`、`states` 中文映射，以及各类型允许的 `kind_states`。不支持的类型、状态及不相容组合会报 `CommandError`。

`query` 为最多 200 字符的普通子串，只查标题、详细内容、备注、联系人姓名及异常处理备注：`title`、`detail`、`note`、`name`、`report_note`。英文 ASCII 大小写不敏感，中文按原文匹配。百分号、下划线、引号等按字面文本处理；不匹配记录 ID、联系人联系方式、JSON 字段名称或嵌套传输信息。嵌套历史报告的备注不属于全文搜索范围，可从记录详情查看。

日期按 **最后更新时间 `updated_at`** 筛选，不按创建时间或提醒到期时间。`since`、`until` 均包含边界，必须是带时区的 ISO 时间，按 UTC 规范化后比较，保留微秒精度。浏览器按用户选定时区将本地日历日转换为相应范围。缺省可传 `None`；日期字符串、无时区时间、倒置范围都被拒绝。排序固定为 `updated_at DESC, id ASC`，同一更新时间以 ID 稳定排序。

分页是当前数据库快照的偏移分页。单次查询的数量和记录在同一服务锁内读取；用户翻页之间如有其他操作改变排序，刷新可回到第一页重新查询，不应将跨多次请求的页面当作不可变导出快照。

## 详情与审计

`record_detail(service, record_id, event_limit=50, event_offset=0)` 返回：

- `record`：该记录的完整业务字段，移除结构化联系方式和传输凭据后返回。
- `events`：只属于该记录的审计事件，按审计 ID 由新到旧排序。
- `event_total`、`event_limit`、`event_offset`、`events_has_more`：明确分页边界，不静默截断审计。
- `meta`：本机来源、筛选规则、类别与状态说明。

事件分页同样限制每页 1 至 100 条、偏移最多 1,000,000；找不到记录或 ID 无效时报 `CommandError`。不会读取或返回 `assistive_delivery_receipts` 中的原始签名回执。

旧版提醒缺少 `revision` 时，列表与详情以只读方式返回 `revision: 1`，与当前面板和提醒编辑的兼容规则一致；不会为一次查询改写旧数据。

联系人 `contact_hint`、地址类字段，以及嵌套 `api_key`、`token`、`secret`、`password`、`signature`、原始回执和凭据等结构化字段会被过滤，原数据库保持不变。标题、详情和备注仍是用户保存在本机的内容；历史模块不推测或自动改写自由文本中的私人信息。页面仍应维持应用已有的本机访问限制，分享内容需要用户另行选择。

所有用户筛选值通过 SQL 参数绑定；枚举和固定查询字段不会由输入拼接。数据库读取失败时返回统一错误，不把 SQL 文本或损坏的数据内容放入错误消息。

## 验证

`tests/test_assistive_history.py` 验证 263 条记录完整翻页、相同更新时间排序、各生活记录类别、移除历史、只按可读字段搜索、SQL 注入字符串、时区与微秒边界、更新时间语义、无状态变更、61 条审计分页、嵌套凭据过滤以及严格输入错误处理。

```powershell
python -m unittest tests.test_assistive_history -v
```
