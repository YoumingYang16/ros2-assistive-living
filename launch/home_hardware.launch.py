"""Home mission bridge plus hardware skill server; no simulated provider."""
from pathlib import Path
from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue


def generate_launch_description():
    share = Path(get_package_share_directory("robot_voice_patrol"))
    return LaunchDescription([
        DeclareLaunchArgument("config", default_value=str(share / "config" / "home.json")),
        DeclareLaunchArgument("provider", default_value=""),
        DeclareLaunchArgument("hardware_journal", default_value=".runtime/hardware-receipts.sqlite3"),
        DeclareLaunchArgument("db_path", default_value=".runtime/home-missions.sqlite3"),
        DeclareLaunchArgument("dashboard", default_value="true"),
        DeclareLaunchArgument("port", default_value="8773"),
        IncludeLaunchDescription(PythonLaunchDescriptionSource(str(share / "launch" / "assistant.launch.py")),
            launch_arguments={name: LaunchConfiguration(name) for name in ("config", "db_path", "dashboard", "port")}.items()),
        Node(package="robot_voice_patrol", executable="voice_patrol_hardware", output="screen", parameters=[{
            "config": ParameterValue(LaunchConfiguration("config"), value_type=str),
            "provider": ParameterValue(LaunchConfiguration("provider"), value_type=str),
            "journal_path": ParameterValue(LaunchConfiguration("hardware_journal"), value_type=str),
        }]),
    ])
