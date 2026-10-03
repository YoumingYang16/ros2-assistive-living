"""Start the mission bridge; robot drivers, localization and Nav2 are external."""
from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue
from pathlib import Path


def generate_launch_description():
    config = str(Path(get_package_share_directory("robot_voice_patrol")) / "config" / "default.json")
    return LaunchDescription([
        DeclareLaunchArgument("config", default_value=config),
        DeclareLaunchArgument("dashboard", default_value="true"),
        DeclareLaunchArgument("host", default_value="127.0.0.1"),
        DeclareLaunchArgument("port", default_value="8773"),
        DeclareLaunchArgument("autostart", default_value="true"),
        DeclareLaunchArgument("db_path", default_value=".runtime/missions.sqlite3"),
        DeclareLaunchArgument("use_sim_time", default_value="false"),
        DeclareLaunchArgument("map_frame", default_value=""),
        Node(
            package="robot_voice_patrol", executable="voice_patrol_ros", output="screen",
            parameters=[{
                "config": LaunchConfiguration("config"),
                "dashboard": ParameterValue(LaunchConfiguration("dashboard"), value_type=bool),
                "autostart": ParameterValue(LaunchConfiguration("autostart"), value_type=bool),
                "host": LaunchConfiguration("host"),
                "port": ParameterValue(LaunchConfiguration("port"), value_type=int),
                "db_path": ParameterValue(LaunchConfiguration("db_path"), value_type=str),
                "use_sim_time": ParameterValue(LaunchConfiguration("use_sim_time"), value_type=bool),
                "map_frame": ParameterValue(LaunchConfiguration("map_frame"), value_type=str),
            }],
        ),
    ])
