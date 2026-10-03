# 无硬件 ROS 测试

测试分三层，报告中不能互相代替：

1. tests/test_ros_adapter.py、test_ros_skills.py 使用 Python fake futures，验证迟到接收、取消确认、未知终态阻塞、证据校验、能力新鲜度和 Goal UUID 核对。test_managed_node.py 验证配置/激活/清理回调和资源释放；替身不证明 native ROS 状态机或 DDS。
2. tests_ros/test_dds_launch.py 在真实 rclpy/DDS 中启动助手进程和软件 ActionServer，验证消息与进程集成。
3. 实际 Nav2 规划器、控制器或视觉算法验证，不由上述软件端点覆盖。本交付没有声称完成这一层，也不要求机器人硬件。

## 已有 Linux ROS 2 环境

需要 Ubuntu 24.04、ROS 2 Jazzy、nav2_msgs、rosidl_default_generators、colcon、pytest 和 launch_testing。执行：

```bash
source /opt/ros/jazzy/setup.bash
bash scripts/build_ros_workspace.sh
source work/ros_build/install/setup.bash
python3 -m unittest discover -s tests -v
python3 -m pytest -c tests_ros/pytest.ini tests_ros/test_dds_launch.py -v \
  --junitxml=work/ros-dds-results.xml
```

集成测试使用单独 ROS_DOMAIN_ID 和 localhost discovery，启动两个独立进程。覆盖导航与 Typed Inspect 完整任务、inconclusive 结果、感知取消终态、迟到目标接收后的取消、传感器错误、能力声明与 ExecuteSkill、真实管理生命周期停用/激活和服务器进程被强制结束。最后一项只终止测试启动的 fixture 进程，不会操作其他节点。

该测试套件依赖实际 ROS 环境。缺少 rclpy 或接口包时应直接失败，不把 skip 输出包装成 DDS 测试成功。

## 容器

需要可运行 Linux 容器的 Docker。当前项目未自动安装 Docker 或启用系统虚拟化。

```bash
docker build -t voice-patrol-v3 .
docker compose --profile test run --rm tests
```

JUnit 报告位于 work/ros-test-results/ros-dds.xml。镜像包含 Jazzy 运行时、编译工具、自定义接口与项目源码；第一次构建会下载 ROS 基础镜像和依赖，大小取决于缓存。

只演示任务软件和真实 DDS 通信：

```bash
docker compose --profile software-demo up --build
```

浏览器访问 http://127.0.0.1:8768。助手和软件端点通过容器网络通信；网页只映射到宿主机回环地址。容器内部绑定 0.0.0.0 需要显式 VOICE_PATROL_CONTAINER_BIND=1，允许的浏览器 Host 值由 compose 的 VOICE_PATROL_ALLOWED_HOSTS 限定。

停止演示：`docker compose --profile software-demo down`。默认保留 mission_data 卷中的任务记录；不自动删除历史。

## CI

.github/workflows/ros-integration.yml 在 Ubuntu runner 中构建同一镜像，运行 Python 测试及 launch_testing，然后保存 JUnit 报告。此工作流需要把项目作为仓库根目录提交后运行；提供配置文件不等于已执行 CI。

## 软件端点故障参数

examples/ros_mock_endpoints.py 支持 ROS 参数，供测试重复制造场景：

| 参数 | 默认值 | 作用 |
|---|---|---|
| navigation_delay_seconds | -1 | 使用配置耗时；非负值覆盖 |
| acknowledgement_delay_seconds | 0 | 延迟导航目标接收 |
| inspection_delay_seconds | -1 | 覆盖观察耗时 |
| inspection_outcome | 空 | 按预设物体推导；可设 inconclusive |
| sensor_available | true | false 时感知失败 |
| accept_cancellation | true | false 时拒绝取消请求 |
| skill_delay_seconds | 0.2 | 拍照、回充、转向测试端点延迟 |
| available_skills | capture/dock/follow/turn | 能力服务的显式声明列表 |

这些参数只属于明确标识的软件测试端点，不能用于掩盖外部真实服务的错误。
