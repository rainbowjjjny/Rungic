# covers: install.device-spec/E1
"""Reject unpinned and incomplete Pixel factory archives before publication."""

import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest


DEVICE = "husky"
BUILD = "CP1A.260405.005"
PARTITIONS = (
    "boot", "init_boot", "vendor_boot", "vendor_kernel_boot", "dtbo",
    "vbmeta", "vbmeta_system", "vbmeta_vendor", "vendor_dlkm", "system_dlkm",
)
REQUIRED_FILES = tuple(f"{name}.img" for name in PARTITIONS) + ("android-info.txt",)
TOOL = Path(__file__).with_name("prepare_pixel_stock.py")


def fixture(tmp_path, missing=None):
    files = {name: f"fake {name}\n".encode() for name in REQUIRED_FILES if name != missing}
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as zipped:
        for name, data in files.items():
            zipped.writestr(name, data)
    archive = tmp_path / "husky-cp1a.260405.005-factory-test.zip"
    prefix = f"{DEVICE}-{BUILD.lower()}"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr(f"{prefix}/image-{prefix}.zip", inner.getvalue())
        zipped.writestr(f"{prefix}/bootloader-husky-test.img", b"bootloader")
        zipped.writestr(f"{prefix}/radio-husky-test.img", b"radio")
    return archive, files


def run_tool(archive, output, expected):
    return subprocess.run(
        [sys.executable, str(TOOL), str(archive), "--sha256", expected,
         "--device", DEVICE, "--build", BUILD, "--output", str(output)],
        capture_output=True, text=True,
    )


def test_wrong_sha256_refuses_before_writing(tmp_path):
    archive, _files = fixture(tmp_path)
    output = tmp_path / "out"
    result = run_tool(archive, output, "0" * 64)
    assert result.returncode != 0
    assert "SHA-256 mismatch" in result.stderr
    assert set(tmp_path.iterdir()) == {archive}


@pytest.mark.parametrize("missing", REQUIRED_FILES)
def test_missing_partition_refuses(tmp_path, missing):
    archive, _files = fixture(tmp_path, missing=missing)
    output = tmp_path / "out"
    result = run_tool(archive, output, hashlib.sha256(archive.read_bytes()).hexdigest())
    assert result.returncode != 0
    assert "Missing required" in result.stderr
    assert missing in result.stderr
    assert set(tmp_path.iterdir()) == {archive}


def test_happy_path_writes_verified_partitions(tmp_path):
    archive, files = fixture(tmp_path)
    output = tmp_path / "out"
    expected = hashlib.sha256(archive.read_bytes()).hexdigest()
    result = run_tool(archive, output, expected)
    assert result.returncode == 0, result.stderr
    assert {path.name for path in output.iterdir()} == set(files) | {"verification.json"}
    report = json.loads((output / "verification.json").read_text())
    assert report["schema_version"] == 1
    assert report["device"] == DEVICE
    assert report["build"] == BUILD
    assert report["archive_name"] == archive.name
    assert report["archive_sha256"] == expected
    assert not report["flashed"] and not report["device_tested"]
    assert "super_sha256" not in report
    assert set(report["files"]) == set(files)
    for name, data in files.items():
        extracted = output / name
        assert extracted.read_bytes() == data
        assert report["files"][name]["sha256"] == hashlib.sha256(extracted.read_bytes()).hexdigest()
        assert report["files"][name]["bytes"] == extracted.stat().st_size
    assert not output.with_name(output.name + ".partial").exists()
