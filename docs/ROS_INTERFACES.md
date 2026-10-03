> V5 新增生活辅助服务与四项家居技能。本文保留原有任务平台的说明；新增接口与边界见 [HOME_INTERFACES.md](HOME_INTERFACES.md) 和 [ASSISTIVE_API.md](ASSISTIVE_API.md)。

# ROS 2 V3 接口

目标环境为 ROS 2 Jazzy。任务软件、ROS 接口和软件测试服务可在没有机器人硬件的情况下开发和验收。默认使用自定义 `Inspect` Action；旧的 JSON 感知接口仍可显式选择。

## 运行边界

`voice_patrol_ros` 创建真实 LifecycleNode、6 线程执行器、任务工作线程及可选网页服务。导航调用外部 `NavigateToPose`，本项目不直接发布速度、不包含底盘驱动，也不把软件端点的坐标插值当作路径规划。

| 接口 | 类型 | 方向 | 用途 |
|---|---|---|---|
| `/voice_patrol/command` | `std_msgs/msg/String` | 订阅 | ASR 输出或文字指令 |
| `/voice_patrol/state` | `std_msgs/msg/String` | 发布 | 完整任务、健康与定位快照，约 2 Hz |
| `/voice_patrol/speech_text` | `std_msgs/msg/String` | 发布 | 去重的播报文本，接任意 TTS |
| `/navigate_to_pose` | `nav2_msgs/action/NavigateToPose` | 客户端 | 外部导航服务 |
| `/voice_patrol/inspect` | `voice_patrol_interfaces/action/Inspect` | 客户端 | 有进度、结果和取消的感知操作 |
| `/voice_patrol/execute_skill` | `voice_patrol_interfaces/action/ExecuteSkill` | 客户端 | 拍照、回充、有限跟随、转向的类型化接口 |
| `/voice_patrol/get_capabilities` | `voice_patrol_interfaces/srv/GetCapabilities` | 客户端 | 显式能力声明，协议版本 3 |
| 各 Action 的 `/_action/status` | `action_msgs/msg/GoalStatusArray` | 订阅 | 核对历史 Goal UUID 是否达到终态 |
| `/voice_patrol/change_state` 等 | 标准 lifecycle_msgs 服务 | 服务端 | ROS 管理节点状态 |
| `/amcl_pose` | `geometry_msgs/msg/PoseWithCovarianceStamped` | 订阅 | map 坐标定位，best-effort/volatile |
| `/voice_patrol/inspection/request` | `std_msgs/msg/String` | 发布 | 仅 JSON 兼容模式启用 |
| `/voice_patrol/inspection/result` | `std_msgs/msg/String` | 订阅 | 仅 JSON 兼容模式启用 |

文字/状态 topic 默认 reliable/volatile、depth=10；没有历史命令重放。节点应先完成发现再发指令。远程重复命令需要 HTTP 接口的 request_id 去重能力；传统 ROS String 命令每次发布都是一次提交。

## 编译与启动

项目根目录包含 Python ROS 包，接口包位于 `ros2_ws/src/voice_patrol_interfaces`。避免只对根目录递归发现而漏掉内层包：

```bash
source /opt/ros/jazzy/setup.bash
bash scripts/build_ros_workspace.sh
source work/ros_build/install/setup.bash
ros2 launch robot_voice_patrol assistant.launch.py \
  config:=/absolute/path/config/default.json db_path:=.runtime/missions.sqlite3
```

ROS launch、普通 Python 本地演示与容器默认网页统一为 `http://127.0.0.1:8768`。

无需网页时传入 `dashboard:=false`。支持 config、db_path、host、port、map_frame、use_sim_time、autostart 参数。map_frame 为空时使用 JSON 配置。接口名可以在 JSON ros 对象配置；使用 ROS remapping 时，Action 与其 `/_action/status` 名称应成对映射，确保恢复核对订阅同一端点。

```bash
ros2 run robot_voice_patrol voice_patrol_ros --ros-args \
  -r /navigate_to_pose:=/robot1/navigate_to_pose \
  -r /voice_patrol/inspect:=/robot1/inspect
ros2 topic pub --once /voice_patrol/command std_msgs/msg/String "{data: '去会议室检查有没有水杯，然后返回起点'}"
ros2 topic echo /voice_patrol/speech_text
```

use_sim_time 影响 ROS 目标时间戳。任务超时及状态发布使用墙上单调时钟，仿真时钟停止不会使取消检查无限等待。

## 真实 ROS 管理生命周期

默认 autostart=true 完成 configure → activate。使用 autostart=false 可通过标准 ROS 工具管理：

```bash
ros2 launch robot_voice_patrol assistant.launch.py autostart:=false
ros2 lifecycle get /voice_patrol
ros2 lifecycle set /voice_patrol configure
ros2 lifecycle set /voice_patrol activate
ros2 lifecycle set /voice_patrol deactivate
ros2 lifecycle set /voice_patrol cleanup
```

configure 创建适配器、任务存储、命令入口与网页；activate 允许任务执行和播报；deactivate 只在没有活动任务及未决外部目标时成功；cleanup 释放这些资源。需要先停止并取得终态，再请求停用。inactive 状态不接受新任务。

网页生命周期操作在 ROS 模式调用真实 trigger_* 状态转换。cleanup 会关闭网页端口，随后需通过 ROS lifecycle configure 重新建立服务。HTTP 关闭被短暂延后，让清理回执发出。错误处理尝试释放资源并回到 unconfigured；外部动作仍未决时不能声称完成清理，节点可能进入 finalized，需核对旧目标后重启。

Mock 的 LifecycleController 实现相同任务准入规则，无需 rclpy。其 cleanup 是软件逻辑停用，ROS cleanup 还会释放节点管理的接口资源。

## Typed ExecuteSkill 与能力发现

新增 action/ExecuteSkill.action 和 srv/GetCapabilities.srv。GetCapabilities 返回 protocol_version="3"、skills、simulated、provider。消费者忽略未识别能力名；声明最长有效 5 秒，正常状态读取会异步刷新。Action 未发现、能力声明缺失/过期或执行器异常时，不接受外部技能。

ExecuteSkill Goal 使用固定枚举 CAPTURE=1、DOCK=2、FOLLOW=3、TURN=4。字段包括请求 ID、地点、相机/图像格式、跟随对象、时长、距离、角度和步骤时间预算。没有 Python、shell 或自由工具调用字段。

Result 必须匹配 request_id 与 skill，同时具备真实成功终态、success=true、error_code=0、有效时间戳与非空 evidence_json。拍照额外要求 media_uri；引用仅保存，不自动下载或打开。服务端时间不能早于请求，未来偏差最多 5 秒。反馈为 phase/progress/message，取消与导航共用同一操作槽位。外部技能执行失败默认不自动重试，避免复制外部副作用。

软件 ROS 端点提供全部四项测试能力，结果标记 simulated:true；拍照返回 mock:// 测试引用并注明 image_generated=false，跟随只验证时间、取消和协议。集成真实硬件时，应由驱动/业务节点实现这些接口并提供真实证据。

## Typed Inspect Action

定义文件：`ros2_ws/src/voice_patrol_interfaces/action/Inspect.action` 和 `msg/Observation.msg`。

Goal 含 request_id、地点 ID target、object_name、时间预算 timeout_seconds。Feedback 含阶段 phase、0..1 的 progress 及说明。Result 含对应请求信息、UTC observed_at、错误码、观测列表和 simulated 标志。

| outcome | 含义 | 转换为任务结果 |
|---|---|---|
| `FOUND=1` | 本次观察发现目标 | outcome:found, found:true |
| `NOT_FOUND=2` | 本次观察没有发现目标 | outcome:not_found, found:false |
| `INCONCLUSIVE=3` | 遮挡、模糊或证据不足 | outcome:inconclusive, found:null |
| `OBSERVED=4` | 一般场景巡检完成 | outcome:observed, found:null |

INCONCLUSIVE 是有效但无法确定的观察结果，不能作为“没有找到”的证据。找物任务允许前三种，一般巡检允许后两种。任务中的条件分支匹配具体 outcome，避免把缺失证据变成否定结论。

错误码：NONE=0、INVALID_REQUEST=1、SENSOR_UNAVAILABLE=2、OBSERVATION_TIMEOUT=3、INTERNAL_ERROR=4。感知节点必须使用 Action 的真实成功/终止/取消状态；非零错误码不能记成成功。

每条 Observation 包含 label、0..1 confidence、sensor、media_uri、bbox_xywh 和有界 JSON 对象 details_json。二维框的宽高不能为负，数值必须有限。图像路径只作为元数据保存，服务不会自动读取或下载。

成功观察必须有非空证据。时间必须含有效 UTC 值，不能早于请求超过 5 秒，也不能比接收端当前时间超前超过 5 秒。这里使用 UTC，不使用 ROS 仿真时间；不同主机应保持时间同步。

## JSON 兼容模式

设置 ros.perception_backend 为 json，可在尚未构建自定义接口的环境中接旧感知节点。该模式只取消本地等待并忽略迟到结果，**不能确认远端检测任务已停止**；需要远端取消语义时使用 Action。

请求：

```json
{"request_id":"generated-uuid","target":"meeting_room","object_name":"水杯","requested_at":"2026-10-03T08:00:00+00:00","timeout_seconds":15}
```

结果：

```json
{
  "request_id":"generated-uuid", "target":"meeting_room", "object_name":"水杯",
  "success":true, "outcome":"inconclusive", "found":null,
  "observed_at":"2026-10-03T08:00:02+00:00", "summary":"画面被遮挡，无法确定",
  "evidence":{"sensor":"front_camera","reason":"occluded"}, "simulated":false
}
```

示例时间和 ID 必须替换为真实当前请求/观测信息。兼容旧版只有 found:true/false 的结果，但 V2 推荐显式 outcome。不匹配、重复、超期结果忽略；矛盾 outcome/found、NaN、无证据和陈旧结果拒绝。单条 JSON 上限 256 KiB。

## 取消与故障恢复

导航、Typed Inspect 和 ExecuteSkill 共享一个操作槽位。接收目标前发生取消或超时，仍保留该请求；迟到的 accepted 响应会触发取消。只有收到终态结果才释放槽位。取消服务回复 accepted 本身不是终态证明。

暂停/停止会额外等待最多 ros.cancel_timeout 秒确认终态。未确认时报告 STOP_UNCONFIRMED，不会标成已暂停或已停止，也不会开始新操作。链路恢复后终态返回会释放槽位；目标状态无法确认时需要人工核对远端目标后重启，禁止自动重放运动。

RosExecutionError 附带 code、retryable、stage 和 details。典型错误：SERVER_UNAVAILABLE、GOAL_ACK_TIMEOUT、EXECUTION_TIMEOUT、ACTION_ABORTED、INVALID_OBSERVATION、UNKNOWN_ACTION_STATE、ROS_EXECUTOR_FAILED、STOP_UNCONFIRMED。只有允许重试的错误才适用任务的有限重试预算。

派发前记录请求关联，接收后通过 feedback.execution_ref 记录 Goal UUID、Action 名和 step_id。恢复时订阅的 Action 状态必须证明所记录目标已成功/取消/失败；未收到历史状态、缺失 UUID 或未完成步骤没有关联目标均阻止恢复。新进程本地 action_pending=false 不证明旧机器人目标已终止。ROS 状态历史的保留由服务端决定，旧目标不再发布时可能无法自动核对；本项目不作分布式 exactly-once 保证。

## 健康快照

robot.health 提供导航/感知发现状态、执行器存活、定位新鲜度、阻塞状态、最近结构化错误及错误计数。robot.action 提供技能类型、phase、goal_id、取消服务回复及已用时间。执行器故障会锁定为错误，新操作被拒绝。

定位初始值为 null，只有 frame 和四元数有效的定位消息才更新。pose_valid 根据最近接收时间以及 ros.pose_stale_seconds 判断；该标志表示定位数据新鲜，不保证定位精度。本软件不擅自修改 Nav2 的规划/控制参数。

## 软件端点与验证范围

`examples/ros_mock_endpoints.py` 提供真实 ROS ActionServer/topic，但数据来自坐标插值和预设物体。它能验证接口和任务编排，不能验证 Nav2 算法、视觉模型或硬件。运行中结果明确标记 simulated:true。

本次实际执行和未执行的检查见 [ROS_RUNTIME_VERIFICATION.md](ROS_RUNTIME_VERIFICATION.md)。Linux/Docker 集成测试步骤见 [ROS_TESTING.md](ROS_TESTING.md)。

官方接口依据：[rclpy Jazzy ActionClient](https://github.com/ros2/rclpy/blob/jazzy/rclpy/rclpy/action/client.py)、[Nav2 Jazzy NavigateToPose](https://github.com/ros-navigation/navigation2/blob/jazzy/nav2_msgs/action/NavigateToPose.action)、[launch_testing 示例](https://github.com/ros2/launch/blob/jazzy/launch_testing/test/launch_testing/examples/hello_world_launch_test.py)。

生命周期依据：[rclpy LifecycleNode](https://github.com/ros2/rclpy/blob/jazzy/rclpy/rclpy/lifecycle/node.py)。V3 还通过本机已下载的 rclpy 7.1.11 纯 Python 源码核对回调和 trigger_* API；未加载受系统策略阻止的原生 DLL。
