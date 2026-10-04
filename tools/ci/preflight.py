#!/usr/bin/env python3
"""Read-only preflight for one device spec and an audited OEM extraction."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import time


def sha256(path):
    value = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def avb_public_key_sha1(data):
    """Hash the serialized key blob of a standalone AVB v1 vbmeta image.

    Layout: AOSP libavb/avb_vbmeta_image.h (MIT), blob
    f9cbac447d0941429d813ca0aadef4401e7ec8d7. All header integers are big endian;
    the key offset is relative to the auxiliary block, after authentication.
    This extracts a digest, not a cryptographic verification of the signature.
    """
    require(len(data) >= 256 and data[:4] == b"AVB0", "invalid AVB vbmeta header")
    require(struct.unpack_from(">I", data, 4)[0] == 1, "unsupported AVB header version")
    auth_size, aux_size = struct.unpack_from(">QQ", data, 12)
    key_offset, key_size = struct.unpack_from(">QQ", data, 64)
    require(auth_size % 64 == 0 and aux_size % 64 == 0, "invalid AVB block alignment")
    aux_start = 256 + auth_size
    require(aux_start + aux_size <= len(data), "truncated AVB blocks")
    require(key_size > 0 and key_offset + key_size <= aux_size, "invalid AVB public key range")
    start = aux_start + key_offset
    return hashlib.sha1(data[start:start + key_size]).hexdigest()


def verify_stock(spec, stock):
    """Select the extraction contract from the spec; old specs keep the Motorola path.

    pixel-factory consumes prepare_pixel_stock.py's verification.json directly.
    Each <partition>_sha256 in stock pins both its report entry and local .img.
    The build is the build ID in the independently captured fingerprint.
    """
    identity = spec["identity"]
    pinned = spec["stock"]
    stock_format = pinned.get("format", "motorola")
    require(stock_format in ("motorola", "pixel-factory"), "unsupported stock format")
    verification_path = stock / "verification.json"
    verification = json.loads(verification_path.read_text())
    evidence = {"stock_verification_sha256": sha256(verification_path)}
    if stock_format == "pixel-factory":
        require(verification.get("schema_version") == 1, "unsupported Pixel verification schema")
        require(verification.get("archive_name") == pinned["archive_name"], "OEM archive name mismatch")
        require(verification.get("archive_sha256") == pinned["archive_sha256"], "OEM archive mismatch")
        require(verification.get("device") == identity["device"], "OEM device mismatch")
        build = re.fullmatch(r"[^/]+/[^/]+/[^:]+:[^/]+/([^/]+)/[^/]+:[^/]+/[^/]+", identity["fingerprint"])
        require(build is not None, "invalid device spec fingerprint")
        require(verification.get("build") == build[1], "OEM build mismatch")
        require(verification.get("flashed") is False and verification.get("device_tested") is False,
                "stock verification report has unexpected device state")
        partitions = {name.removesuffix("_sha256"): digest for name, digest in pinned.items()
                      if name.endswith("_sha256") and name != "archive_sha256"}
        require("boot" in partitions and "vbmeta" in partitions, "stock spec lacks boot or vbmeta hash")
        for partition, digest in partitions.items():
            require(re.fullmatch(r"[a-z][a-z0-9_]*", partition) is not None, "invalid stock partition name")
            name = f"{partition}.img"
            entry = verification.get("files", {}).get(name, {})
            require(entry.get("sha256") == digest, f"OEM {partition} verification mismatch")
            path = stock / name
            require(path.stat().st_size == entry.get("bytes"), f"OEM {partition} size mismatch")
            require(sha256(path) == digest, f"OEM {partition} changed")
        if pinned.get("avb_public_key_sha1") is not None:
            require(avb_public_key_sha1((stock / "vbmeta.img").read_bytes()) == pinned["avb_public_key_sha1"],
                    "OEM AVB key mismatch")
    else:
        manifest_path = stock / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        require(verification["stock_manifest_sha256"] == sha256(manifest_path), "stock manifest changed")
        require(manifest["archive_sha256"] == pinned["archive_sha256"], "OEM archive mismatch")
        require(manifest["fingerprint"] == identity["fingerprint"], "OEM fingerprint mismatch")
        require(manifest["device"] == identity["device"], "OEM device mismatch")
        require(manifest["files"]["boot.img"]["sha256"] == pinned["boot_sha256"], "OEM boot manifest mismatch")
        require(sha256(stock / "boot.img") == pinned["boot_sha256"], "OEM boot changed")
        require(verification["super_sha256"] == pinned["super_sha256"], "OEM super verification mismatch")
        require(verification["avb_public_key_sha1"] == pinned["avb_public_key_sha1"], "OEM AVB key mismatch")
        require(not verification["flashed"] and not verification["device_tested"],
                "stock verification report has unexpected device state")
        evidence["stock_manifest_sha256"] = sha256(manifest_path)
    return evidence


def adb(port, serial, *args):
    command = ["adb", "-P", str(port), "-s", serial, *args]
    return subprocess.run(command, check=True, capture_output=True, text=True, timeout=30).stdout.strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec", type=Path)
    parser.add_argument("stock", type=Path)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--adb-port", type=int, default=5037)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    spec = json.loads(args.spec.read_text())
    require(spec.get("schema_version") == 1, "unsupported device spec schema")
    identity = spec["identity"]
    stock = args.stock.resolve(strict=True)
    stock_evidence = verify_stock(spec, stock)

    devices = subprocess.run(["adb", "-P", str(args.adb_port), "devices"], check=True,
                             capture_output=True, text=True, timeout=30).stdout
    states = dict(re.findall(r"^([^\s]+)\s+(device|offline|unauthorized)$", devices, re.MULTILINE))
    require(states.get(args.serial) == "device", f"target {args.serial} is not authorized on ADB port {args.adb_port}")
    props = {
        name: adb(args.adb_port, args.serial, "shell", "getprop", name)
        for name in ("ro.product.device", "ro.boot.hardware.sku", "ro.build.fingerprint",
                     "ro.bootloader", "ro.build.version.sdk", "ro.boot.flash.locked",
                     "ro.boot.verifiedbootstate", "ro.boot.slot_suffix")
    }
    expected = {
        "ro.product.device": identity["product"],
        "ro.boot.hardware.sku": identity["sku"],
        "ro.build.fingerprint": identity["fingerprint"],
        "ro.bootloader": identity["bootloader"],
        "ro.build.version.sdk": str(spec["release_requirements"]["android_api"]),
        "ro.boot.flash.locked": "0",
        "ro.boot.verifiedbootstate": "orange",
    }
    for name, wanted in expected.items():
        require(props[name] == wanted, f"{name}: got {props[name]!r}; expected {wanted!r}")
    # The spec's partitions are A/B: a phone that reports no active slot is not the one it describes.
    require(props["ro.boot.slot_suffix"] in ("_a", "_b"),
            f"ro.boot.slot_suffix: got {props['ro.boot.slot_suffix']!r}; expected _a or _b")
    release = adb(args.adb_port, args.serial, "shell", "uname", "-r")
    require(release == spec["kernel"]["stock_release"], "running kernel differs from OEM baseline")
    selinux = adb(args.adb_port, args.serial, "shell", "getenforce")
    require(selinux == "Enforcing", "SELinux is not Enforcing")
    battery = adb(args.adb_port, args.serial, "shell", "dumpsys", "battery")
    match = re.search(r"^\s*level:\s*(\d+)$", battery, re.MULTILINE)
    require(match is not None, "battery level unavailable")
    battery_percent = int(match[1])
    require(battery_percent >= spec["release_requirements"]["minimum_battery_percent"],
            "battery below device spec minimum")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    host_free = shutil.disk_usage(args.output.parent.resolve()).free
    minimum = spec["release_requirements"]["minimum_host_free_gib"] * 1024**3
    require(host_free >= minimum, "host free space below build minimum")

    report = {
        "schema_version": 1,
        "result": "source-and-device-preflight-passed",
        "timestamp_unix": int(time.time()),
        "device_spec_id": spec["id"],
        "device_spec_sha256": sha256(args.spec),
        "serial": args.serial,
        "adb_port": args.adb_port,
        "observed": {"properties": props, "kernel_release": release,
                     "selinux": selinux, "battery_percent": battery_percent,
                     "host_free_bytes": host_free},
        **stock_evidence,
        "kernel_common_commit": spec["kernel"]["common_commit"],
        "note": "Read-only preflight; no kernel, rootfs, firmware bundle, flash, or acceptance has run."
    }
    temporary = args.output.with_name(args.output.name + ".partial")
    temporary.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    temporary.replace(args.output)
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except (KeyError, ValueError, OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        print(f"preflight failed: {error}", file=sys.stderr)
        raise SystemExit(1)
