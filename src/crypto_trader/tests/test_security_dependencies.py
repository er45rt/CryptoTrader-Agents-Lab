"""S-02 deterministic dependency policy; no network or trading."""
from importlib.metadata import version
from pathlib import Path
import tomllib

from packaging.version import Version


ROOT = Path(__file__).resolve().parents[3]


def test_s02_installed_http_and_protobuf_have_security_floor():
    assert Version(version("requests")) >= Version("2.33.0")
    assert Version(version("urllib3")) == Version("2.8.0")
    assert Version(version("protobuf")) == Version("5.29.6")


def test_s02_lock_records_approved_packages():
    data = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    locked = {package["name"]: package["version"] for package in data["package"]}
    assert Version(locked["requests"]) >= Version("2.33.0")
    assert locked["urllib3"] == "2.8.0"
    assert locked["protobuf"] == "5.29.6"
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    deps = project["project"]["dependencies"]
    assert any(dep.startswith("requests>=2.33.0") for dep in deps)
    assert "urllib3==2.8.0" in deps
    assert "protobuf==5.29.6" in deps
