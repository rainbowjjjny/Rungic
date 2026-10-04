# covers: install.gki-kernel/E1
"""Version-format and baseline regressions for OEM module auditing."""

import hashlib
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import module_abi


def write_module(path, versions):
    """Build an ELF64 relocatable module with real legacy __versions records."""
    names = b"\0.shstrtab\0__versions\0"
    payload = b"".join(struct.pack("<Q56s", crc, name.encode())
                       for name, crc in versions.items())
    data = bytearray(64 + 3 * 64)
    struct.pack_into("<16sHHIQQQIHHHHHH", data, 0,
                     b"\x7fELF\x02\x01\x01" + bytes(9), 1, 183, 1,
                     0, 0, 64, 0, 64, 0, 0, 64, 3, 1)
    struct.pack_into("<IIQQQQIIQQ", data, 128,
                     1, 3, 0, 0, len(data), len(names), 0, 0, 1, 0)
    struct.pack_into("<IIQQQQIIQQ", data, 192,
                     11, 1, 0, 0, len(data) + len(names), len(payload), 0, 0, 8, 64)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data + names + payload)


class ModuleBaselineTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.modules = self.root / "modules"
        self.module = self.modules / "vendor_dlkm/driver.ko"
        self.versions = {"module_layout": 0x12345678, "vendor_symbol": 0x90abcdef}
        write_module(self.module, self.versions)
        self.symvers = self.root / "Module.symvers"
        self.write_symvers({"module_layout": self.versions["module_layout"]})
        self.baseline = self.root / "baseline.json"
        result, _ = self.run_check(output=self.baseline)
        self.assertEqual(result.returncode, 0, result.stderr)

    def write_symvers(self, versions):
        self.symvers.write_text("".join(f"0x{crc:08x}\t{name}\tvmlinux\tEXPORT_SYMBOL\n"
                                        for name, crc in versions.items()))

    def run_check(self, *options, output=None):
        output = output or self.root / "candidate.json"
        result = subprocess.run([sys.executable, str(Path(module_abi.__file__)),
                                 str(self.symvers), str(self.modules), "--output", str(output),
                                 *map(str, options)], capture_output=True, text=True)
        report = json.loads(output.read_text()) if output.exists() else None
        return result, report

    def test_missing_export_vs_baseline_fails(self):
        self.write_symvers({})
        result, report = self.run_check("--baseline", self.baseline)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("new unresolved", result.stderr)
        self.assertIn("vendor_dlkm/driver.ko", result.stderr)
        self.assertIn("module_layout", result.stderr)
        self.assertEqual(report["mismatch_count"], 0)
        self.assertEqual(report["modules"][0]["unresolved_by_gki"],
                         ["module_layout", "vendor_symbol"])

    def assert_inventory_failure(self, *modules):
        result, _ = self.run_check("--baseline", self.baseline)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("inventory", result.stderr)
        for module in modules:
            self.assertIn(module, result.stderr)
        return result

    def test_added_module_fails(self):
        write_module(self.modules / "system_dlkm/other.ko", self.versions)
        self.assert_inventory_failure("system_dlkm/other.ko")

    def test_removed_module_fails(self):
        # Keep a second baseline module so the candidate is not empty.
        write_module(self.modules / "system_dlkm/other.ko", self.versions)
        result, _ = self.run_check(output=self.baseline)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.module.unlink()
        self.assert_inventory_failure("vendor_dlkm/driver.ko")

    def test_all_modules_removed_fails_and_names_missing_module(self):
        self.module.unlink()
        self.assert_inventory_failure("vendor_dlkm/driver.ko")

    def test_renamed_module_fails(self):
        # Identical bytes under a different relative path are a different inventory.
        self.module.rename(self.modules / "vendor_dlkm/renamed.ko")
        self.assert_inventory_failure("vendor_dlkm/driver.ko", "vendor_dlkm/renamed.ko")

    def test_changed_module_sha256_fails(self):
        # A signature change leaves all parsed version references unchanged.
        self.module.write_bytes(self.module.read_bytes() + b"changed module signature")
        result = self.assert_inventory_failure("vendor_dlkm/driver.ko")
        self.assertIn("sha256", result.stderr)

    def test_identical_baseline_passes(self):
        result, report = self.run_check("--baseline", self.baseline)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(report["modules"], json.loads(self.baseline.read_text())["modules"])
        self.assertEqual(report["unresolved_count"], 1)

    def test_fewer_unresolved_symbols_passes(self):
        self.write_symvers(self.versions)
        result, report = self.run_check("--baseline", self.baseline)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(report["unresolved_count"], 0)

    def test_unresolved_allowance_is_per_module(self):
        write_module(self.modules / "system_dlkm/other.ko", {"vendor_symbol": 0x90abcdef})
        result, baseline = self.run_check(output=self.baseline)
        self.assertEqual(result.returncode, 0, result.stderr)
        # Resolving vendor symbols reduces the total, but driver still loses an export.
        self.write_symvers({"vendor_symbol": self.versions["vendor_symbol"]})
        result, report = self.run_check("--baseline", self.baseline)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("vendor_dlkm/driver.ko", result.stderr)
        self.assertIn("module_layout", result.stderr)
        self.assertLess(report["unresolved_count"], baseline["unresolved_count"])

    def test_crc_mismatch_fails_with_and_without_baseline(self):
        self.write_symvers({"module_layout": 1})
        for options in ((), ("--baseline", self.baseline)):
            with self.subTest(options=options):
                result, report = self.run_check(*options)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("CRC mismatch", result.stderr)
                self.assertIn("vendor_dlkm/driver.ko", result.stderr)
                self.assertIn("module_layout", result.stderr)
                self.assertEqual(report["mismatch_count"], 1)

    def test_no_baseline_keeps_old_unresolved_behavior_and_report(self):
        self.write_symvers({})
        result, report = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(set(report), {"schema_version", "symvers_sha256", "module_count",
                                      "references", "matched", "mismatch_count",
                                      "unresolved_count", "modules"})
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["unresolved_count"], 2)
        self.assertEqual(report["mismatch_count"], 0)
        self.assertEqual(report["symvers_sha256"], hashlib.sha256(b"").hexdigest())
        self.assertEqual(report["modules"][0]["sha256"],
                         hashlib.sha256(self.module.read_bytes()).hexdigest())

    def test_optional_artifact_hashes(self):
        config = self.root / ".config"
        image = self.root / "Image"
        config.write_bytes(b"CONFIG_MODVERSIONS=y\n")
        image.write_bytes(b"synthetic kernel image\0")
        for options in (("--config", config), ("--image", image),
                        ("--baseline", self.baseline, "--config", config, "--image", image)):
            with self.subTest(options=options):
                result, report = self.run_check(*options)
                self.assertEqual(result.returncode, 0, result.stderr)
                for flag, path, key in (("--config", config, "config_sha256"),
                                        ("--image", image, "image_sha256")):
                    if flag in options:
                        self.assertEqual(report[key], hashlib.sha256(path.read_bytes()).hexdigest())
                    else:
                        self.assertNotIn(key, report)
                self.assertEqual(report["symvers_sha256"],
                                 hashlib.sha256(self.symvers.read_bytes()).hexdigest())

    def test_invalid_baseline_fails_clearly(self):
        original = json.loads(self.baseline.read_text())
        for baseline in ([], {}, {"modules": []}, {"modules": [{}]},
                         {"modules": [original["modules"][0]] * 2}):
            with self.subTest(baseline=baseline):
                self.baseline.write_text(json.dumps(baseline))
                result, _ = self.run_check("--baseline", self.baseline)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("baseline", result.stderr)
                self.assertNotIn("Traceback", result.stderr)


class ModuleVersionsTest(unittest.TestCase):
    def test_nobits_does_not_require_file_payload(self):
        # A large .bss is legal even when sh_offset + sh_size is beyond EOF.
        data = bytearray(64 + 3 * 64 + 32)
        data[:6] = b"\x7fELF\x02\x01"
        struct.pack_into("<Q", data, 0x28, 64)
        struct.pack_into("<HHH", data, 0x3a, 64, 3, 1)
        names = b"\0.shstrtab\0.bss\0"
        data[256:256 + len(names)] = names
        struct.pack_into("<IIQQQQIIQQ", data, 128, 1, 3, 0, 0, 256, len(names), 0, 0, 1, 0)
        struct.pack_into("<IIQQQQIIQQ", data, 192, 11, 8, 0, 0, 288, 8192, 0, 0, 8, 0)
        self.assertEqual(dict(module_abi.sections(bytes(data)))[".bss"], b"")

    def parse(self, entries):
        with patch.object(Path, "read_bytes", return_value=b"fixture"), \
                patch.object(module_abi, "sections", return_value=entries.items()):
            return module_abi.module_versions(Path("fixture.ko"))

    def test_legacy_android15(self):
        data = struct.pack("<Q56s", 0x12345678, b"module_layout")
        self.assertEqual(self.parse({"__versions": data}), {"module_layout": 0x12345678})

    def test_extended_long_rust_name_and_c_terminator(self):
        name = "_RNv" + "long_rust_symbol" * 8
        self.assertEqual(self.parse({
            "__version_ext_crcs": struct.pack("<II", 0x12345678, 0x90abcdef),
            "__version_ext_names": name.encode() + b"\0module_layout\0\0",
        }), {name: 0x12345678, "module_layout": 0x90abcdef})

    def test_extended_takes_precedence_like_kernel(self):
        self.assertEqual(self.parse({
            "__versions": struct.pack("<Q56s", 1, b"symbol"),
            "__version_ext_crcs": struct.pack("<I", 2),
            "__version_ext_names": b"symbol\0",
        }), {"symbol": 2})

    def test_reject_malformed_extended(self):
        cases = [
            {"__version_ext_crcs": b"1234"},
            {"__version_ext_names": b"symbol\0"},
            {"__version_ext_crcs": b"123", "__version_ext_names": b"symbol\0"},
            {"__version_ext_crcs": b"1234", "__version_ext_names": b"symbol"},
            {"__version_ext_crcs": b"12345678", "__version_ext_names": b"symbol\0\0"},
            {"__version_ext_crcs": b"1234", "__version_ext_names": b"one\0two\0"},
            {"__version_ext_crcs": b"12345678", "__version_ext_names": b"one\0one\0"},
        ]
        for entries in cases:
            with self.subTest(entries=entries), self.assertRaises(ValueError):
                self.parse(entries)


if __name__ == "__main__":
    unittest.main()
