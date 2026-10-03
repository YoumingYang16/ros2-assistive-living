# V5 家居与取送接口

V5 新增 `home_control`、`pick_object`、`place_object`、`handover_object` 四项外部技能。与原有十项技能一起使用。软件模拟只在显式 `--fixture-skills` 时开放；所有结果携带 `simulated: true`。

## 参数与结果

|技能|params|必要成功证据|
|---|---|---|
|home_control|device: light/curtain/fan/television；state: on/off|device、target 对应请求；reported_state 对应期望；readback_confirmed=true|
|pick_object|item：允许物品|item、target 对应请求；payload_confirmed=true|
|place_object|item、surface|item、target、surface 对应请求；released=true；surface_confirmed=true|
|handover_object|item、recipient|item、target、recipient 对应请求；released=true；recipient_acknowledged=true；非空 receipt_id|

target 必须是已配置地点。三种取放技能在当前位置尚未确认时不得发送。取物还要求本任务最近 10 秒内的明确 found 观察，其后不能发生导航、跟随、转向或取放动作。实际驱动仍负责操作前的新鲜局部感知、碰撞防护、载荷限制和交接检测；此软件不提供机械臂控制算法。

允许物品：空水杯、密封饮用水、手机、遥控器、纸巾、毛巾、眼镜、书、钥匙。物品也必须登记在 object_names。水杯与装有热液体的杯子不能被自动当成空水杯。药物、热液体、刀具、人体搬运等不在取放接口白名单。

## 交接与中断

`把手机从客厅送到卧室` 生成五步：导航→观察→取物→导航→交接，需要显式确认执行。任务目标是确认交接，找到物品不等于已送达。接收人不在、拒绝交接或证据缺失时失败；软件预设端点会保留持有载荷，并同步移动后的物品位置。

四种新技能必须 max_retries=0、on_failure=abort。动作中途取消/暂停后不自动重放，也不从历史检查点恢复它们。取消终态不证明动作没有物理副作用，需先核实载荷/设备状态再创建新任务。成功终态与停止发生竞态时，已经返回的完成证据仍保存。

真实 ROS 驱动应公开自身载荷状态并维护重启后的载荷核对。桌面软件不把缺少驱动状态理解成“手里一定没有东西”。本版 UI 显示“无已确认载荷”而非现实世界空载保证。

## ROS 2 接口

重建 `ros2_ws/src/voice_patrol_interfaces`。ExecuteSkill.action 增加 HOME_CONTROL=5、PICK_OBJECT=6、PLACE_OBJECT=7、HANDOVER_OBJECT=8，以及 `string parameters_json`；其他关联结果与时间戳字段保留。

GetCapabilities 响应必须声明 protocol_version="5" 并列出实际支持的新技能。旧协议 "3" 只允许原来的四种扩展技能。ROS Action 类型变化需要客户端与服务端同时重新构建，不能与旧生成类型直接混用。

适配器检查本次 request_id、skill、成功终态、错误码、有效时间戳、JSON 证据和上述业务证据。没有设备、能力声明过期、证据不匹配不会返回假成功。测试使用可控 Action Future 和软件预设，另执行官方 rosidl_adapter 的语法/IDL 生成检查；原生 DDS、Nav2、硬件均未运行。

## 家居配置

`config/home.json` 内置八个居家地点，坐标仅用于软件演示。真实地图/导航坐标和驱动设备映射必须由集成人员替换。现有 `config/default.json` 保留传统控制台地点，便于回归验证。灯/窗帘/风扇/电视以 `(target, device)` 映射到硬件；禁止驱动将未知目标悄悄映射为任意设备。

## 人工帮助与通信

人工照护不通过取放技能实现。由 AssistiveService 保存请求，再通过受信宿主的 AssistanceTransport 接口接通信平台。默认没有发送器；只有联系同意和指定联系人齐全时可生成待发送信封。签名回执表示提供方报告了送达或响应，不代表身体照护已完成。详见 ASSISTIVE_API.md。
