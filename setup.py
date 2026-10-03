from glob import glob
from setuptools import find_packages, setup


setup(
    name="robot_voice_patrol",
    version="7.0.0",
    packages=find_packages(exclude=["tests"]),
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/robot_voice_patrol"]),
        ("share/robot_voice_patrol", ["package.xml"]),
        ("share/robot_voice_patrol/launch", glob("launch/*.launch.py")),
        ("share/robot_voice_patrol/config", glob("config/*.json")),
    ],
    package_data={"robot_voice_patrol": ["web/*.html", "web/*.css", "web/*.js", "config/*.json"]},
    install_requires=["setuptools"],
    extras_require={"voice": ["vosk>=0.3.45,<0.4", "sounddevice>=0.4.6,<0.6"]},
    zip_safe=False,
    maintainer="ROS Voice Patrol contributors",
    maintainer_email="maintainers@example.com",
    description="Chinese voice mission orchestration with mock and ROS 2 Nav2 adapters",
    license="Apache-2.0",
    python_requires=">=3.10",
    entry_points={"console_scripts": [
        "voice-patrol=robot_voice_patrol.__main__:main",
        "voice_patrol_ros=robot_voice_patrol.ros_node:main",
        "voice_patrol_hardware=robot_voice_patrol.hardware_node:main",
        "voice-patrol-mic=robot_voice_patrol.voice_client:main",
    ]},
)
