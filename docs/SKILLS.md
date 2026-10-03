> V5 新增生活辅助服务与四项家居技能。本文保留原有任务平台的说明；新增接口与边界见 [HOME_INTERFACES.md](HOME_INTERFACES.md) 和 [ASSISTIVE_API.md](ASSISTIVE_API.md)。

# V3 技能与扩展

技能注册表统一提供参数定义、地点策略、能力状态、结果检查和执行入口。HTTP `GET /api/skills` 返回目录；可视化流程编辑器据此生成参数表单。任意模型输出或工作流仍要通过统一计划验证。

| 技能 | 参数 | 实现与边界 |
|---|---|---|
| navigate | Step.target | 外部 Nav2 或明确标识的软件坐标插值 |
| inspect | Step.target / object_name | 三态观察证据，感知 Action 或软件预设数据 |
| wait | Step.seconds | 可取消的有界等待 |
| speak | text | 将文本送入播报事件/ROS speech_text；不假定扬声器已播放 |
| report | title、include_observations | 当前任务阶段报告，保留证据来源和模拟标志 |
| wait_state | field、operator、value、poll_seconds | 轮询适配器公开状态字段，支持 eq/ne/gt/gte/lt/lte 与取消超时 |
| capture | camera、format=jpeg/png；可选 target | 外部拍照接口，必须返回媒体引用及证据 |
| dock | 可选 target，默认为 home | 外部回充接口，成功以服务端终态为准 |
| follow | subject、duration_seconds、distance_meters | 有限时长的外部跟随接口 |
| turn | angle_degrees | -360..360 度的外部转向接口 |

`wait_state` 的 field 是 JSON 状态的点路径，例如 `location`、`health.navigation_ready`。只支持公开字段和标量比较，不执行表达式。缺失字段保持未知，包括 ne 比较也不提前成功。gt/gte/lt/lte 只接受数值。到期产生 STATE_TIMEOUT，可被工作流的超时分支处理。

外部四项能力默认不可用。ROS 适配器同时要求 ExecuteSkill Action 已发现、GetCapabilities 显式声明该技能，并且能力声明未过期；未连接时返回 CAPABILITY_UNAVAILABLE。不会因为“模型知道这个动作”就声称硬件可以执行。

## 无硬件开发

正常 `--mode mock` 只有基础适配器和本地技能。`--fixture-skills` 显式启用四项外部技能的软件测试实现：

```bash
python -m robot_voice_patrol --mode mock --fixture-skills
```

软件转向更新模拟 yaw，回充更新模拟位置/charging，跟随运行可取消的时间序列，拍照返回 `mock://frames/...` 测试引用。**拍照 fixture 不生成图像，跟随 fixture 不检测或追踪真实人物。** 所有结果明确包含 simulated:true，并可用于验证工作流、证据传递和异常处理。真实 ROS 软件端点见 `examples/ros_mock_endpoints.py`，同样不声称实现视觉、运动控制或硬件。

## 插件开发

开发者可以在可信启动代码中注册 SkillSpec；不支持从 HTTP 导入模块、上传 Python 或执行 shell。

```python
from robot_voice_patrol.skills import SkillSpec, get_registry

def read_temperature(step, adapter, cancel, feedback, context):
    if cancel.is_set():
        from robot_voice_patrol.contracts import ExecutionCancelled
        raise ExecutionCancelled("已取消")
    reading = your_sensor_interface.read(timeout=step.timeout)
    return {"kind": "temperature", "status": "succeeded", "source": reading.source,
            "evidence": {"celsius": reading.celsius, "sample_id": reading.sample_id}}

get_registry().register(SkillSpec(
    name="temperature", label="温度读取", description="接入已有温度服务",
    schema={"type": "object", "properties": {}, "additionalProperties": False},
    executor=read_temperature, external=True,
))
```

上述例子中的 your_sensor_interface 由集成方实现，且 adapter.capabilities() 必须明确返回 temperature 可用。这是扩展示例，不是内置温度能力。

注册表拒绝重名技能，复制 schema 后保存，目录返回的对象不会反向修改注册信息。可选 validator 对标准参数校验之后的 Step 做语义校验。参数 schema 支持 object/string/number/integer/boolean/null/array/anyOf、枚举、长度、数值边界与字符串模式；未声明参数默认拒绝。

执行器必须自行遵守 timeout 与 cancel，返回可 JSON 序列化的真实终态，不能把已发送请求当作已完成。插件是可信代码，注册表不提供强制终止任意 Python 线程的沙箱。

## 验证

tests/test_skills.py 覆盖播报缺失、报告证据、状态更新/超时/取消、未连接拒绝、显式模拟、参数越界、注册冲突、结果类型不匹配。ROS 端点的真实 DDS 运行状态见 ROS_RUNTIME_VERIFICATION.md。
