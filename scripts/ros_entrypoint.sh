#!/usr/bin/env bash
set -eo pipefail
# Enable nounset only after sourcing ROS's generated environment scripts.
source /opt/ros/jazzy/setup.bash
source /ws/install/setup.bash
set -u
exec "$@"
