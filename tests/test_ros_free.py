"""Guard: the wheel must import with no ROS/rclpy on the path.

fm-tools is consumed outside a ROS environment (shell scripts, other repos), so
importing the package must never pull in rclpy.
"""

import sys


def test_package_imports_without_rclpy():
    import fm_tools.tui  # noqa: F401

    assert "rclpy" not in sys.modules
