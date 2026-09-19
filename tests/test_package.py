"""正式项目骨架的冒烟测试。"""

from pathlib import Path
import tomllib

import tikiagent


def test_package_version() -> None:
    assert tikiagent.__version__ == "1.0.0"


def test_runtime_and_package_metadata_versions_match() -> None:
    """发布版本只能有一个事实，避免 wheel 与运行时显示不同版本。"""

    project_root = Path(__file__).resolve().parents[1]
    with (project_root / "pyproject.toml").open("rb") as stream:
        project = tomllib.load(stream)

    assert project["project"]["version"] == tikiagent.__version__
    assert project["project"]["license"] == "MIT"
    assert project["project"]["license-files"] == ["LICENSE"]
    assert set(project["project"]["scripts"]) == {
        "tikiagent",
        "tikiagent-demo",
        "tikiagent-tui",
    }
