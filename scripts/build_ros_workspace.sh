#!/usr/bin/env bash
# Run from the project root in a sourced Jazzy Linux environment.
set -euo pipefail
project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
workspace_root="${1:-$project_root/work/ros_build}"
mkdir -p "$workspace_root"
cd "$workspace_root"
source /opt/ros/jazzy/setup.bash
# Explicit paths avoid colcon stopping at the outer Python package and missing
# the nested interface source package. No source copies or destructive cleanup.
colcon build --merge-install --paths "$project_root" "$project_root/ros2_ws/src/voice_patrol_interfaces" \
  --packages-select voice_patrol_interfaces robot_voice_patrol --event-handlers console_direct+
printf 'Built workspace. Source: %s/install/setup.bash\n' "$workspace_root"
