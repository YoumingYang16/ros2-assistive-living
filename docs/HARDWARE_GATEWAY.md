# ROS 2 与真实设备的控制链

本项目包含实际 ROS 2 节点和 Action 客户端代码，网页是其中一个输入和状态显示端。V5 已实现客户端；V6 增加可接入设备供应商驱动的通用技能服务端。不能把这两点理解为已经实现了任意型号机器人的底层驱动。

## 输入到执行

```text
麦克风 → 语音识别 → 文字命令，或直接文字/网页输入
  → MissionEngine / DialoguePlanner（澄清、确认、条件、调度、持久化）
  → Ros2Adapter
      → Nav2 NavigateToPose ActionClient → 外部 Nav2 导航栈 → 底盘驱动
      → Inspect ActionClient → 外部物品感知服务
      → ExecuteSkill ActionClient（协议 5、技能码 1–8）
          → HardwareGatewayNode ActionServer
          → HardwareGateway（校验、去重、单操作租约、执行回执）
          → 显式配置的 HardwareDriver → 设备驱动/控制器 → 传感器回读
  ← ROS Action 反馈和结果 ← 驱动终态证据
  → 任务记录 / 确认或失败处理 / 网页或 speech_text 播报通道
```

也可以向 `/voice_patrol/command` 发布 `std_msgs/msg/String`。关闭 dashboard 后任务节点和硬件服务端仍可运行。`speech_text` 表示提交播报文本，不等于扬声器已经播放或用户已经听到。

## 已提供与待对接

| 部分 | 本项目状态 |
|---|---|
| ROS 2 生命周期任务节点、Action 客户端、取消与状态查询 | 已有代码 |
| 自定义 ExecuteSkill / Inspect / GetCapabilities 接口定义 | 已有代码，需 colcon 构建 |
| ExecuteSkill ActionServer 与 GetCapabilities 服务 | V6 新增 |
| 灯/窗帘/风扇/电视控制回读、取物/放置/确认交接接口 | 有参数校验和执行流程；实际动作由驱动实现 |
| Nav2 导航、定位、地图、底盘串口/CAN/电机驱动 | 部署方提供并配置 |
| 摄像头识别、物体姿态、抓取规划、机械臂运动学、力/碰撞控制 | 部署方提供；没有用模拟代码冒充这些能力 |
| 移乘、洗浴、喂食等身体接触照护 | 人工协助流程；不作为该接口的自主机械动作 |
| ROS 原生 DDS 联调、真实硬件动作 | 本轮未运行、未验证 |

## 启动

先按 `ROS_INTERFACES.md` 在 ROS 2 环境构建接口包和本包，并准备好设备对应的 Nav2、定位与感知服务。使用实际家庭地图坐标更新 home 配置；示例坐标不是实测地图。

```bash
ros2 launch robot_voice_patrol home_hardware.launch.py \
  config:=/absolute/path/home.json \
  provider:=my_robot_driver:create_driver \
  hardware_journal:=/absolute/path/hardware-receipts.sqlite3
```

`my_robot_driver:create_driver` 是部署方编写并安装的可信 Python 工厂，示例名称不是附送的厂商驱动。工厂接收经验证的配置，返回下面的驱动对象。只有本机启动参数可以指定模块；没有通过 HTTP 导入或运行任意代码的入口。不填 provider 时服务端仍可启动，但不声明任何硬件能力，也不会返回模拟成功。

无 ROS 环境也可查看默认接入状态；此命令不会派发动作：

```bash
python -m robot_voice_patrol.hardware_gateway
```

如显式传 `--provider my_robot_driver:create_driver`，会导入并构造这个可信驱动、读取能力，然后关闭驱动。工厂初始化自身的设备连接行为由驱动负责。诊断报告不会宣称已测试原生 ROS 或真实动作。

仅启动硬件技能节点：

```bash
ros2 run robot_voice_patrol voice_patrol_hardware --ros-args \
  -p config:=/absolute/path/home.json \
  -p provider:=my_robot_driver:create_driver \
  -p journal_path:=/absolute/path/hardware-receipts.sqlite3
```

任务端可以禁用网页：`home_hardware.launch.py dashboard:=false`。在设备部署中应使用独立且稳定的硬件回执数据库路径，不与任务数据库共用。

## 驱动契约

`HardwareDriver` 定义在 `robot_voice_patrol/hardware_gateway.py`：

```python
def create_driver(config):
    return YourDeviceDriver(config)

class YourDeviceDriver:
    def capabilities(self):
        # 只声明本驱动已真实接入并可执行的能力。
        return {"home_control": {"available": True, "simulated": False}}

    def execute(self, command, cancel, feedback):
        # command: DriverCommand(request_id, step, requested_at)
        # 必须先验证位置/设备/目标/载荷等真实前置条件。
        # 在循环中检查 cancel；向设备发停止请求后等待实际终态。
        # 将 command.request_id 传到底层可去重的设备命令中。
        # 使用真实设备命令与传感器回读构造下面规定的回执。
        raise NotImplementedError("Implement the actual device operation")

    def stop(self):
        # 非阻塞：请求停止。此返回本身不代表硬件已停止。
        raise NotImplementedError("Implement the device stop request")

    def snapshot(self):
        return {"connected": False}  # 替换为真实连接/载荷/设备状态。

    def close(self):
        pass  # 释放自己创建的设备连接。
```

驱动返回的 JSON 对象必须包含 `request_id`、`kind`、`target`、`status`（succeeded/failed/cancelled）、`terminal_confirmed: true`、`simulated`、`observed_at`（有时区的 ISO 时间）和非空 `evidence`。时间必须晚于请求开始且是新鲜回读。`simulated` 必须匹配该技能的能力声明。无确认终态就不应返回这类回执；若串口断线等原因无法确认，抛出异常后网关会持久记录 unknown 并阻止新动作。

成功回执还需要按技能提供证据：

| 技能 | 成功证据 |
|---|---|
| capture | `media_uri` 非空，evidence.media_created=true |
| dock | target 匹配、docked=true、charging_confirmed=true |
| follow | subject 匹配、tracking_confirmed=true、motion_complete=true |
| turn | measured angle_degrees 与请求差值不超过 2°、motion_complete=true |
| home_control | device/target 匹配、reported_state 匹配、readback_confirmed=true |
| pick_object | item/target 匹配、payload_confirmed=true |
| place_object | item/target/surface 匹配、released=true、surface_confirmed=true |
| handover_object | item/target/recipient 匹配、released=true、recipient_acknowledged=true、receipt_id 非空 |

家庭物品操作禁止自动重试。驱动仍需自行核实当前位置、物品新鲜检测、抓取可行性、载荷和接受者状态，不能仅因为目标名字合法就开动机械。上层任务会验证抓取前的观察证据，但它不替代设备端感知和物理互锁。

## 取消、超时和异常恢复

- 网关同时只允许一个通用技能驱动物理操作。取消和超时仅设置 cancel 并调用 stop；在驱动实际返回前保留租约。即使驱动卡住，新操作也会被阻止。
- 取消/超时后驱动迟到的成功回执保留 `driver_status` 和原始证据，整个请求仍记为 cancelled/timed_out，避免隐藏已经发生的动作。
- 回执先写 SQLite，重复相同 request_id 返回原回执，不再执行；相同 ID、不同参数会被拒绝。崩溃留下的 running 记录启动后转为 unknown，不自动重放。
- 驱动抛异常、证据不完整、关联错误、过期时间戳都进入 unknown 并停止声明能力。unknown 不是“已经停好”。操作人员核实硬件后，可以通过可信本机集成调用 `gateway.reconcile(request_id, fresh_receipt, note)`，提交匹配的终态证据和核实说明。没有网页一键解除互锁入口，也不要删除回执数据库来绕过未知状态。
- 网关 ROS 请求可以报告失败，但回执的 `gateway_terminal_confirmed=false` 会使任务端保留全局执行租约并报告 `UNKNOWN_HARDWARE_STATE`，导航也被阻止。任务端不会把 ROS 的 ABORTED 当作实际机械停止，也不会仅凭 ROS 状态主题解除这个未知状态。
- 此租约只约束本技能网关。Nav2、外部遥控器和其他控制源的互斥与急停仍应由底盘/机械臂控制器及部署层共同实现。
- 这是协作式 Python 驱动接口，不是实时控制器或安全认证系统。ROS 网络访问控制和实际硬件急停由部署系统负责。

## 本轮验证范围

`tests/test_hardware_gateway.py` 使用明确标记 simulated 的内存驱动验证：8 个 wire 技能码到驱动到回执的路径、严格参数验证、能力缺失、单操作互斥、取消/超时等待真实返回、迟到证据、去重、冲突、异常锁止、数据库重启及人工核实恢复。通过注入 ROS 类型桩调用真实节点回调，验证 typed Goal→Gateway→Provider→typed Result、能力服务及取消终态时序；`tests/test_ros_skills.py` 另外验证未知物理终态阻止后续导航。

上述测试不加载 rclpy 原生库、不建立 DDS 网络、不连接实体设备，不能替代目标 ROS 2 主机上的集成验证与设备调试。

## 跨重启互锁与可信人工恢复

任务数据库另有 `settings.hardware_interlock`。ROS 模式每次派发 Nav2 导航或上述 8 种技能前先写 pending 意图，包括任务、步骤和后续收到的执行关联信息。成功结果先持久写入任务检查点，再解除意图。写检查点或解除记录失败都会保留互锁；进程重启后仍在等待的意图转换为 unknown。旧版本历史中尚未核实的 ROS 导航及外部机械步骤也会被隔离。

unknown 会阻止所有新的机械任务及队列派发，包括新导航。生活提醒、人工协助记录等独立软件服务仍可使用。网页确认历史记录、归档、停止/继续按钮以及生命周期切换均不会解除这个锁。`/api/state` 和 `/api/health` 返回 `hardware_interlock` 便于查看。

所有 8 项技能都禁止失败自动重试和暂停后重放；家庭 4 项保留错误码 `HOME_RECONCILIATION_REQUIRED`，原有 capture/dock/follow/turn 使用 `EXTERNAL_RECONCILIATION_REQUIRED`。历史恢复入口也不会重新执行这些步骤。包含这些技能的周期任务失败、取消或中断后取消自动周期，包括在调度最终记账前崩溃的情况；成功的用户既定周期仍可照常运行。已确认未派发、明确确认停止的取消或失败不会永久锁死硬件，新任务仍须由使用者明确创建。

绝对地点导航另有规则：在 Nav2 已明确确认取消且不存在未决目标后，先持久结清本次派发意图，才能暂停后重新导航到原目标；明确确认终态失败的导航可按既定次数重试，但每次都先结清旧意图再记录新意图。目标 ACK 丢失、停止未确认或进程中断时不能走这条重试路径，也不能因为重启就派发新目标。软件测试包含导航派发时真实子进程崩溃与重启，不包含真实 Nav2/DDS 测试。

可信恢复必须依次处理两个层面：

1. 部署方核实相关底盘、机械臂、设备、载荷与交接状态，并处理网关 `unknown` 回执。网关本机 API 为 `gateway.reconcile(request_id, fresh_driver_receipt, note)`；回执仍经过同样的关联、终态、时间与证据验证。
2. 如果任务端还保留旧 ROS 未决租约，核实后重新连接/启动任务节点。任务数据库的互锁仍会保留，重启自身不会放行。
3. 由可信本机集成调用 `engine.reconcile_hardware(verifier, note=...)`。此方法没有 HTTP 路由。verifier 接收互锁记录，必须实际核实全部相关资源，返回下面的回执结构。成功后记入任务历史和事件，不重放原动作。

```python
# 此段是部署集成示意；site_driver 是部署方实现的设备核实器，
# 并不是附送的测试驱动。不要把“点过确认”替换成设备核实结果。
def verify_from_devices(interlock):
    receipt = site_driver.verify_all_related_resources(interlock)
    # 必须返回真实核实结果；不满足条件时应抛出异常或返回否定结果。
    return {
        "interlock_id": interlock["id"],
        "terminal_confirmed": receipt["terminal_confirmed"],
        "all_resources_terminal": receipt["all_resources_terminal"],
        "observed_at": receipt["observed_at"],
        "verified_by": receipt["verified_by"],
        "evidence": receipt["evidence"],
    }

engine.reconcile_hardware(
    verify_from_devices,
    note="记录实际核实过的控制器、机械状态、载荷和操作人员信息",
)
```

回执需要匹配当前互锁 ID，两项确认值均为 true，包含非空证据与核实者，观测时间须有时区、晚于互锁建立且在近 30 秒内。若设备仍未确认或任务线程/接口仍有未决目标，拒绝解除。传入 `True`、网页 dismiss 或旧回执均不能替代 verifier。

离线恢复旧数据库也不会清锁：恢复代码先在临时 SQLite 中合并恢复前与备份中的全部 pending/unknown，再整体复制到目标数据库；不同未知操作保留在 `sources` 中。核实器必须处理合并后的所有来源，不能只核对第一条。备份恢复仍保留预恢复备份，队列仍保持暂停。请继续使用原任务和网关数据库，不能通过删除数据库来代替机械状态核实。

`tests/test_hardware_interlock.py` 覆盖派发前意图、未知状态重启后新导航被阻止、历史 dismiss/生命周期不能解锁、8 项技能暂停与失败不重放、成功/写盘失败的检查点顺序、可信核实、旧备份保锁及周期任务故障恢复。全部使用软件夹具，不代表真实硬件测试。
