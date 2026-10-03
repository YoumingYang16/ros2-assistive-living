# 执行前检查与场景验证

V4 提供两种互补检查：执行前检查读取当前能力和计划结构；场景验证在隔离的软件适配器中执行相同的任务状态机。两者均不派发真实任务，不改变实时历史、队列、会话或配置。

## 控制台

打开“场景验证”，选择“三地搜索示例”，或从可视编排编辑器导入当前草稿。点击“检查当前执行条件”查看当前执行器占用、生命周期、能力、确认状态、配置指纹，以及导航能否保证后续观察的位置。

阻碍会令 ready=false。位置和条件方面的提示是保守的静态分析，不能取代实际状态检查。配置超时合计只是全部步骤乘以尝试次数的预算，不是耗时预测，不包含远端取消确认等待。预检通过也不保证稍后状态没有变化。

选择一个或多个场景后运行，可比较执行状态、目标结论、失败/跳过步骤、重试次数，展开每个场景查看证据。报告可导出 JSON；修改输入会清除旧结果，避免将旧结果用于新草稿。运行期间输入被锁定，结果始终对应已提交的草稿。

| 场景 ID | 处理 |
|---|---|
| configured | 使用配置中的预设物体 |
| empty | 所有地点的物体列表为空，明确返回未发现 |
| inconclusive | 带目标物体的观察返回无法判断，不能进入 not_found 分支 |
| sensor_failure | 观察抛出不可重试的 SENSOR_UNAVAILABLE |
| navigation_timeout | 导航直接返回已确认的 EXECUTION_TIMEOUT |
| transient_navigation | 每个导航步骤首次失败，后续尝试成功；无重试预算时任务失败 |

场景验证为每次运行创建新的内存数据库、独立配置和适配器，并在结束后关闭。不使用实时适配器，不调用语言模型，不输出扬声器声音。拍照/跟随等扩展技能采用明确的测试端点，不生成真实图片或检测人物。

一次最多选择六个不同场景，每个场景最多 100 个内置技能，单个场景墙钟上限 5 秒。等待和动作时间已加速，wait_state 的真实等待上限为 30 ms。导航超时是故障注入，并非测量。超出运行上限会停止该场景，并设置 bounded_stop=true；不能将这类部分结果当作验证完成。

同一 HTTP 服务一次只运行一个场景请求，第二个请求返回 409。可信第三方插件不在场景执行白名单内，以免插件访问真实设备。模型或历史导航计划在隔离副本中去掉确认门槛用于验证，原草稿仍保持待确认，模拟结果不构成真实执行授权。

## CLI

```bash
python -m robot_voice_patrol --workflow examples/workflows/three_location_search.json --scenario all
python -m robot_voice_patrol --workflow examples/workflows/timeout_fallback.json --scenario navigation_timeout
```

CLI 输出同样的 JSON 报告，不打开 --db 指定的运行数据库。场景中的预期失败是报告结果，不等同于 CLI 工具错误；输入无效或内部错误仍返回非零退出码。此选项不能和真实执行、队列提交、恢复等模式混用。

## API

- `GET /api/scenarios`：目录、限制和模拟声明。
- `POST /api/preflight {workflow,parameters?}` 或 `{plan}`：读取当前环境的检查报告。
- `POST /api/scenarios/run {workflow,parameters?,scenario_ids:[...]}` 或 `{plan,scenario_ids:[...]}`：隔离运行并比较。

报告包含执行器真实产生的 results、step_states 和 report；最外层 simulated/isolated/timing_accelerated 均为 true。preflight 不接收自然语言，避免修改对话草稿；自然语言先在控制台预览后使用结构化 plan。

以上能力验证软件流程和错误处理，不是物理仿真，不验证真实导航路径、识别准确率或机器人动力学。
