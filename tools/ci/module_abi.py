#!/usr/bin/env python3
"""Compare a GKI Module.symvers with every OEM module's version references."""

import argparse
import hashlib
import json
from pathlib import Path
import struct
import sys


def sections(data):
    if data[:6] != b"\x7fELF\x02\x01":
        raise ValueError("expected little-endian ELF64 module")
    offset = struct.unpack_from("<Q", data, 0x28)[0]
    size, count, strings = struct.unpack_from("<HHH", data, 0x3A)
    if size < 64 or offset + size * count > len(data) or strings >= count:
        raise ValueError("invalid ELF section table")
    headers = [struct.unpack_from("<IIQQQQIIQQ", data, offset + size * i)
               for i in range(count)]
    names = data[headers[strings][4]:headers[strings][4] + headers[strings][5]]
    for header in headers:
        name_end = names.find(b"\0", header[0])
        if name_end < 0:
            raise ValueError("invalid ELF section name")
        name = names[header[0]:name_end].decode("ascii")
        start, length = header[4:6]
        if header[1] == 8:  # SHT_NOBITS (.bss) has no file payload.
            yield name, b""
            continue
        if start + length > len(data):
            raise ValueError(f"invalid {name} section bounds")
        yield name, data[start:start + length]


def module_versions(path):
    entries = dict(sections(path.read_bytes()))
    # ACK android16-6.12 kernel/module/version.c uses paired u32 CRCs and
    # NUL-terminated names for CONFIG_EXTENDED_MODVERSIONS (including Rust).
    if "__version_ext_crcs" in entries or "__version_ext_names" in entries:
        crcs = entries.get("__version_ext_crcs")
        names = entries.get("__version_ext_names")
        if crcs is None or names is None or not crcs or len(crcs) % 4 or not names.endswith(b"\0"):
            raise ValueError(f"{path}: invalid extended symbol versions")
        count = len(crcs) // 4
        # modpost emits concatenated "symbol\\0" string literals, so C adds
        # one final implicit terminator after the last explicit terminator.
        fields = names.split(b"\0", count)
        symbols = fields[:count]
        if len(fields) != count + 1 or fields[-1] not in (b"", b"\0") or any(not name for name in symbols):
            raise ValueError(f"{path}: extended CRC/name count mismatch")
        decoded = [name.decode("ascii") for name in symbols]
        if len(set(decoded)) != len(decoded):
            raise ValueError(f"{path}: duplicate extended symbol version")
        return dict(zip(decoded, struct.unpack(f"<{len(decoded)}I", crcs)))
    version_data = entries.get("__versions")
    if version_data is None:
        raise ValueError(f"{path}: missing __versions")
    if len(version_data) % 64:
        raise ValueError(f"{path}: invalid __versions length")
    result = {}
    for start in range(0, len(version_data), 64):
        entry = version_data[start:start + 64]
        crc = struct.unpack_from("<I", entry)[0]
        symbol = entry[8:].split(b"\0", 1)[0].decode("ascii")
        if symbol in result:
            raise ValueError(f"{path}: duplicate version for {symbol}")
        result[symbol] = crc
    return result


def baseline_failures(reports, path):
    baseline = json.loads(path.read_text())
    if (not isinstance(baseline, dict) or not isinstance(baseline.get("modules"), list)
            or not baseline["modules"]):
        raise ValueError(f"{path}: invalid baseline module inventory")
    stock = {}
    for item in baseline["modules"]:
        if (not isinstance(item, dict) or not isinstance(item.get("module"), str)
                or not isinstance(item.get("sha256"), str)
                or not isinstance(item.get("unresolved_by_gki"), list)
                or any(not isinstance(name, str) for name in item["unresolved_by_gki"])):
            raise ValueError(f"{path}: invalid baseline module record: {item}")
        name = item["module"]
        if name in stock:
            raise ValueError(f"{path}: duplicate baseline module: {name}")
        stock[name] = item
    candidate = {item["module"]: item for item in reports}
    failures = []
    for name in sorted(stock.keys() - candidate.keys()):
        failures.append(f"module inventory: missing module {name}")
    for name in sorted(candidate.keys() - stock.keys()):
        failures.append(f"module inventory: added module {name}")
    for name in sorted(stock.keys() & candidate.keys()):
        if candidate[name]["sha256"] != stock[name]["sha256"]:
            failures.append(f"module inventory: {name}: sha256 differs from baseline")
        unresolved = (set(candidate[name]["unresolved_by_gki"])
                      - set(stock[name]["unresolved_by_gki"]))
        if unresolved:
            failures.append(f"{name}: new unresolved symbols: {', '.join(sorted(unresolved))}")
    return failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("symvers", type=Path)
    parser.add_argument("modules", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline", type=Path,
                        help="stock-equivalent report; require identical modules and no new unresolved symbols")
    parser.add_argument("--config", type=Path, help="kernel .config to bind by SHA-256")
    parser.add_argument("--image", type=Path, help="kernel Image to bind by SHA-256")
    args = parser.parse_args()
    expected = {}
    for line in args.symvers.read_text().splitlines():
        fields = line.split()
        if len(fields) < 3:
            raise ValueError(f"invalid Module.symvers line: {line}")
        expected[fields[1]] = int(fields[0], 16)
    modules = sorted(args.modules.rglob("*.ko"))
    if not modules and not args.baseline:
        raise ValueError("no OEM modules found")
    reports = []
    for module in modules:
        versions = module_versions(module)
        mismatch = {name: {"oem_crc": f"0x{crc:08x}",
                           "built_crc": f"0x{expected[name]:08x}"}
                    for name, crc in versions.items()
                    if name in expected and crc != expected[name]}
        reports.append({"module": str(module.relative_to(args.modules)),
                        "sha256": hashlib.sha256(module.read_bytes()).hexdigest(),
                        "references": len(versions),
                        "matched": sum(name in expected and crc == expected[name]
                                       for name, crc in versions.items()),
                        "unresolved_by_gki": sorted(set(versions) - set(expected)),
                        "mismatch": mismatch})
    report = {"schema_version": 1,
              "symvers_sha256": hashlib.sha256(args.symvers.read_bytes()).hexdigest(),
              "module_count": len(reports),
              "references": sum(item["references"] for item in reports),
              "matched": sum(item["matched"] for item in reports),
              "mismatch_count": sum(len(item["mismatch"]) for item in reports),
              "unresolved_count": sum(len(item["unresolved_by_gki"]) for item in reports),
              "modules": reports}
    for name in ("config", "image"):
        path = getattr(args, name)
        if path is not None:
            report[f"{name}_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    failures = baseline_failures(reports, args.baseline) if args.baseline else []
    for item in reports:
        for name, crcs in item["mismatch"].items():
            failures.append(f"{item['module']}: CRC mismatch for {name}: "
                            f"OEM {crcs['oem_crc']}, candidate {crcs['built_crc']}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({key: value for key, value in report.items() if key != "modules"}, indent=2))
    if failures:
        print("module ABI check failed:\n" + "\n".join(f"  {failure}" for failure in failures),
              file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, struct.error) as error:
        print(f"module ABI check failed: {error}", file=sys.stderr)
        sys.exit(2)
