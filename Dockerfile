FROM ros:jazzy-ros-base-noble
SHELL ["/bin/bash", "-c"]
ENV DEBIAN_FRONTEND=noninteractive PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential python3-colcon-common-extensions python3-pytest \
    ros-jazzy-nav2-msgs ros-jazzy-rosidl-default-generators \
    ros-jazzy-launch-testing ros-jazzy-launch-testing-ros && \
    rm -rf /var/lib/apt/lists/*
WORKDIR /ws
COPY . /ws/src/robot_voice_patrol
COPY ros2_ws/src/voice_patrol_interfaces /ws/src/voice_patrol_interfaces
RUN source /opt/ros/jazzy/setup.bash && \
    colcon build --merge-install --paths /ws/src/robot_voice_patrol /ws/src/voice_patrol_interfaces \
    --packages-select voice_patrol_interfaces robot_voice_patrol && \
    mkdir -p /ws/test-results /data
ENTRYPOINT ["bash", "/ws/src/robot_voice_patrol/scripts/ros_entrypoint.sh"]
CMD ["ros2", "launch", "robot_voice_patrol", "assistant.launch.py", "host:=0.0.0.0", "db_path:=/data/missions.sqlite3"]
