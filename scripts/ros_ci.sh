#!/usr/bin/env bash
set -euo pipefail
source /opt/ros/jazzy/setup.bash
source /ws/install/setup.bash
cd /ws/src/robot_voice_patrol
python3 -m unittest discover -s tests -v
python3 -m pytest -c tests_ros/pytest.ini tests_ros/test_dds_launch.py -v --junitxml=/ws/test-results/ros-dds.xml
