#!/usr/bin/env python3
"""Verify and unpack a pinned Pixel factory archive into host-side files.

The outer SHA-256 is checked before opening the ZIP or writing any output.
Only the required images and android-info.txt from
<device>-<build lowercase>/image-<device>-<build lowercase>.zip are published.
Kernel Image and .ko unpacking is a separate step. This never contacts a phone.

verification.json schema version 1:
  schema_version: 1
  device, build: the pinned CLI identity (build preserves its supplied case)
  archive_name, archive_sha256: outer archive basename and verified SHA-256
  files: {"boot.img": {"sha256": "...", "bytes": 123}, ...}
    All ten required partition images and android-info.txt have file entries.
  flashed, device_tested: false (host extraction is not device acceptance)

The identity and files fields follow the Motorola manifest naming convention.
tools/ci/preflight.py consumes this report when the spec's stock.format is
pixel-factory, comparing archive/device/build and rehashing declared partitions.
This extraction report claims no super_sha256 or AVB key verification.
"""

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
import tempfile
import zipfile


PARTITIONS = (
    "boot", "init_boot", "vendor_boot", "vendor_kernel_boot", "dtbo",
    "vbmeta", "vbmeta_system", "vbmeta_vendor", "vendor_dlkm", "system_dlkm",
)
REQUIRED_FILES = tuple(f"{name}.img" for name in PARTITIONS) + ("android-info.txt",)
BLOCK = 8 * 1024 * 1024


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(BLOCK), b""):
            value.update(chunk)
    return value.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--sha256", required=True, help="Pinned outer factory ZIP SHA-256")
    parser.add_argument("--device", required=True)
    parser.add_argument("--build", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    if not re.fullmatch(r"[0-9a-fA-F]{64}", args.sha256):
        raise ValueError("Expected SHA-256 must contain 64 hexadecimal digits")
    if not re.fullmatch(r"[a-z0-9_-]+", args.device):
        raise ValueError("Invalid pinned device name")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", args.build):
        raise ValueError("Invalid pinned build name")
    archive = args.archive.resolve(strict=True)
    archive_sha256 = digest(archive)
    if archive_sha256 != args.sha256.lower():
        raise ValueError(f"Factory archive SHA-256 mismatch: got {archive_sha256}; expected {args.sha256.lower()}")

    output = args.output.resolve()
    staging = output.with_name(output.name + ".partial")
    if output.exists() or staging.exists():
        raise SystemExit(f"Refusing to overwrite {output} or {staging}")
    prefix = f"{args.device}-{args.build.lower()}"
    inner_name = f"{prefix}/image-{prefix}.zip"
    with zipfile.ZipFile(archive) as factory, tempfile.TemporaryFile() as inner_file:
        names = factory.namelist()
        if len(names) != len(set(names)):
            raise ValueError("Factory archive contains duplicate entry names")
        if inner_name not in names:
            raise ValueError(f"Missing required image archive: {inner_name}")
        with factory.open(inner_name) as source:
            shutil.copyfileobj(source, inner_file, BLOCK)
        inner_file.seek(0)
        with zipfile.ZipFile(inner_file) as images:
            names = images.namelist()
            if len(names) != len(set(names)):
                raise ValueError("Image archive contains duplicate entry names")
            missing = set(REQUIRED_FILES) - set(names)
            if missing:
                raise ValueError(f"Missing required image archive files: {', '.join(sorted(missing))}")

            staging.mkdir(parents=True)
            report = {
                "schema_version": 1,
                "device": args.device,
                "build": args.build,
                "archive_name": archive.name,
                "archive_sha256": archive_sha256,
                "files": {},
                "flashed": False,
                "device_tested": False,
            }
            for position, name in enumerate(REQUIRED_FILES, 1):
                sha = hashlib.sha256()
                size = 0
                with images.open(name) as source, (staging / name).open("wb") as target:
                    for chunk in iter(lambda: source.read(BLOCK), b""):
                        target.write(chunk)
                        sha.update(chunk)
                        size += len(chunk)
                if size != images.getinfo(name).file_size:
                    raise ValueError(f"ZIP size mismatch: {name}")
                report["files"][name] = {"bytes": size, "sha256": sha.hexdigest()}
                print(f"[{position}/{len(REQUIRED_FILES)}] verified {name}", flush=True)

    (staging / "verification.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    staging.rename(output)
    print(f"Verified stock image set: {output}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1)
