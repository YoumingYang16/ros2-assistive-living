cmake_minimum_required(VERSION 3.8)
project(voice_patrol_interfaces)
find_package(ament_cmake REQUIRED)
find_package(rosidl_default_generators REQUIRED)
find_package(builtin_interfaces REQUIRED)
find_package(action_msgs REQUIRED)
rosidl_generate_interfaces(${PROJECT_NAME}
  "msg/Observation.msg"
  "action/Inspect.action"
  "action/ExecuteSkill.action"
  "srv/GetCapabilities.srv"
  DEPENDENCIES builtin_interfaces action_msgs
)
ament_export_dependencies(rosidl_default_runtime)
ament_package()
