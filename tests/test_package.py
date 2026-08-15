"""正式项目骨架的冒烟测试。"""

import tikiagent


def test_package_version() -> None:
    assert tikiagent.__version__ == "0.1.0"
