# ROS 运行时验证记录

记录日期：2026-10-03（Asia/Hong_Kong）。本次开发机为 Windows，没有机器人硬件测试。下述下载与原生运行时探针发生在 V2 开发期间；V3 没有重复下载，也未尝试绕过同一系统限制。

## 已执行

- 检查 docker、podman、conda、mamba、micromamba、MSVC cl、cmake、ninja：开始时均不可用。wsl.exe 存在，但调用提示 Windows Subsystem for Linux 尚未安装。
- 检查 ROS Python 模块和包：原开发 Python 环境不含 rclpy。
- 下载独立 micromamba 2.9.0 可执行文件（约 10.9 MB），仅放入工作区 work/ros2_runtime。
- 实际完成 RoboStack Jazzy 依赖解析：205 个包，预计下载约 220.4 MiB，包含 rclpy 7.1.11、nav2_msgs 1.3.12 和 Fast DDS。
- 实际尝试安装。长路径策略 LongPathsEnabled=0 导致部分 ROS 包缓存文件超过 Windows 路径限制。改用指向工作区的临时 R: 路径和本地压缩包显式安装，完成 204 包的局部运行环境；nav2_msgs 追加安装仍遇到长路径问题。
- 实际执行 rclpy 导入和节点创建探针，程序在导入 rclpy 的二进制扩展时被 Windows 应用程序控制策略阻止，未到达节点创建。
- 临时 R: 映射已移除。没有启用 WSL、安装系统功能、修改长路径注册表、关闭应用程序控制或重启电脑。
- 使用官方 rosidl_adapter 的纯 Python 解析器成功解析 Observation.msg 与 Inspect.action，并成功生成两份 IDL。此步骤不加载被阻止的 rclpy 扩展，也不产生可运行的原生 typesupport。

关键探针等价命令：

```powershell
micromamba run -p R:\e python -c "import rclpy; rclpy.init(); n=rclpy.create_node('runtime_probe'); n.destroy_node(); rclpy.shutdown()"
```

实际异常：

```text
ImportError: DLL load failed while importing _rclpy_pybind11:
应用程序控制策略已阻止此文件。
```

这属于操作系统应用程序控制边界。开发过程中没有尝试绕过该策略。

## 软件检查

V2 ROS 适配器协议单元测试已在普通 Python 环境执行，覆盖三态 Typed Inspect、取消竞态、故障阻塞与健康状态。相关源码完成 Python AST/语法检查，package.xml 完成 XML 解析检查。最终全项目测试数量与结果以主交付报告为准。

V3 新增注册表、软件技能、类型化 ExecuteSkill、能力发现与过期策略、Goal UUID 核对和管理生命周期资源回调测试。普通 Python 中使用受控 futures / 节点替身执行，不声称验证 native DDS。实际 Mock 超时已验证触发工作流 timed_out 补救分支。

V3 复用已下载的官方 rosidl_adapter，成功将 Observation.msg、Inspect.action、ExecuteSkill.action、GetCapabilities.srv 四个接口转为 IDL。机器可读记录见 [ROS_INTERFACE_VALIDATION.json](ROS_INTERFACE_VALIDATION.json)。这一检查不需要导入 rclpy 原生扩展，也不能替代 typesupport 编译。

## 本次未验证

V7 继续使用同一环境边界，没有重复下载或绕过原生库限制。本版的四个接口再次通过官方 rosidl_adapter 的解析与 IDL 生成；硬件网关、ROS 节点回调和客户端互锁通过普通 Python 替身测试，新增生活对话也检查了 ROS 文本回调。真实 ActionServer 代码尚未在原生 DDS 中运行，不能把这些软件测试写成真实 ROS 通信或硬件验证。

- 真正的 rclpy 节点、DDS 发现或 topic/action 往返。
- 自定义 voice_patrol_interfaces 的原生 typesupport 编译/链接；纯 Python 解析与 IDL 转换已完成。
- Linux 容器构建、colcon 构建、launch_testing 或 GitHub Actions 执行。
- 真实 Nav2 规划与控制、真实视觉识别、麦克风音频、机器人硬件。

已提供可以在 Linux ROS 2 或 Linux 容器环境执行的构建脚本和集成测试。**这些文件已经编写和静态检查，但未运行的集成测试不能称为通过。**

运行时路线参考：[Micromamba 官方手动安装](https://mamba.readthedocs.io/en/latest/installation/micromamba-installation.html)、[RoboStack Conda 安装](https://robostack.github.io/conda.html)。本地探针日志保存在开发工作区 work/ros2_runtime，不包含在项目安装包中。
