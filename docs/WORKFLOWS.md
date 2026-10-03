# V3 工作流和模板

`compile_workflow(data, config, parameters=None)` 把有限串行 DSL 编译为普通 `Plan`，随后经过公共技能与计划验证器。编译不执行动作，不解析代码或表达式。工作流 DSL 使用 `version: 1`，导出计划使用 `version: 3`，两者版本号互相独立。在线编辑器和 `GET /api/workflow/schema` 使用与 `schemas/workflow-v1.schema.json` 相同的结构。

```json
{
  "version": 1,
  "name": "三地搜索水杯",
  "steps": [{
    "type": "search", "id": "find",
    "locations": ["reception", "storage", "meeting_room"],
    "object_name": "水杯", "return_home": "found",
    "continue_on": ["not_found"]
  }]
}
```

节点支持四种类型：

| 类型 | 字段和行为 |
|---|---|
| `step` | `kind` 和技能字段 `target/seconds/object_name/timeout/max_retries/params/on_failure`；可选 `condition` |
| `repeat` | `count` 为 1–10 整数，`body` 为节点数组；各次迭代使用独立步骤 ID |
| `if` | `condition`、`then` 和可选 `else`；未知条件的两边都不执行 |
| `search` | 1–10 个不同配置地点、一个配置物体；`return_home=always/found/never`；`continue_on` 显式选择 `not_found` 或 `inconclusive` |

节点可带 `id`（1–24 字符，英文字母起始、字母数字下划线短横线）。条件使用同一作用域中已完成声明的步骤 ID；循环中的局部引用在每次展开时重写。分支与循环内部的 ID 不向外泄漏。直接使用展开后 ID 时也必须引用此前步骤。展开后最多 100 步，节点最多嵌套 5 层；超限整体拒绝，不能部分运行。

条件叶节点为 `{"step_id":"look","outcome":"found"}`，可使用 `{"all":[...]}`、`{"any":[...]}`、`{"not":...}`，最多 8 层、64 节点，每个 all/any 包含 1–16 项。状态结果支持 `succeeded/failed/timed_out/skipped`；物体观察结果支持 `found/not_found/inconclusive`。缺少结果、跳过/失败的观察以及不确定的 found/not_found 比较都得到“未知”。**not(未知) 仍是未知**。可用显式 `inconclusive` 分支处理不确定证据。

失败默认中止；只有 `on_failure:"continue"` 的步骤才允许执行失败备用分支。超时与普通失败分别匹配 `timed_out` 和 `failed`。以下工作流在导航超时或失败后生成阶段报告：

```json
{
  "version": 1,
  "name": "导航失败备用报告",
  "steps": [
    {"type":"step", "id":"go", "kind":"navigate", "target":"meeting_room", "on_failure":"continue"},
    {"type":"if", "condition":{"any":[{"step_id":"go","outcome":"failed"},{"step_id":"go","outcome":"timed_out"}]},
     "then":[{"type":"step","kind":"report","params":{"title":"导航未完成"}}]}
  ]
}
```

模板使用类型化参数。顶层 `parameters` 定义 `location/object/text/integer/number`；数值可配置 `min/max`，每项可带 `default` 和 `label`。`${name}` 必须是完整值，不能嵌入文本或表达式。例：`{"type":"step","kind":"navigate","target":"${place}"}` 配合 `"parameters":{"place":{"type":"location","default":"meeting_room"}}`。`values` 提供保存值，`compile_workflow(..., parameters={"place":"storage"})` 可覆盖；输入模板不会被修改。未知参数、布尔冒充数字、未配置地点和越界值均拒绝。最多 20 个参数；保存模板时必须已有可用值或默认值，模板运行 API 可传 `parameters`。

`POST /api/workflow/preview {"workflow":...}` 返回编译计划；`POST /api/workflow/submit` 提交；`POST /api/templates` 保存；`POST /api/templates/{id}/run` 加入队列。队列执行时保留同一技能能力校验和机器人互斥约束。

查找工作流自动推导单一物体目标，也可顶层声明 `goal:{"kind":"find_object","object_name":"水杯"}`。报告的 `goal_outcome` 与执行状态独立：有明确 found 为 `achieved`；全部相应观察明确 not_found 为 `not_achieved`；遗漏、失败、不确定为 `unknown`；没有声明目标为 `not_applicable`。来源和模拟标记留在步骤证据中。
