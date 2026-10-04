#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Derive standalone.py pack's kernel report from an Android v4 boot image.

Usage: kernel_report.py boot.img --output .work/husky/kernel-report.json
Optional --image Image checks an uncompressed build output against boot's kernel.
LZ4 legacy decoding uses the upstream lz4 CLI; gzip uses the Python stdlib.
This reads host files only and proves neither ABI compatibility nor device boot.
"""
import argparse
import gzip
import hashlib
import json
from pathlib import Path
import re
import struct
import subprocess
import sys
import zlib


PAGE_SIZE = 4096
HEADER_SIZE = 1584
LZ4_LEGACY = b'\x02\x21\x4c\x18'


def align(size):
    return (size + PAGE_SIZE - 1) // PAGE_SIZE * PAGE_SIZE


def unpack_kernel(boot):
    # AOSP boot_img_hdr_v4: v3's 1580 bytes plus signature_size. kernel begins
    # at the fixed 4096-byte page, irrespective of header_size or AVB padding.
    if len(boot) < HEADER_SIZE:
        raise ValueError('Truncated boot header')
    if boot[:8] != b'ANDROID!':
        raise ValueError('Invalid Android boot magic')
    version = struct.unpack_from('<I', boot, 40)[0]
    if version != 4:
        raise ValueError(f'Unsupported boot header version {version}; expected 4')
    if struct.unpack_from('<I', boot, 20)[0] != HEADER_SIZE:
        raise ValueError('Invalid v4 boot header size')
    kernel_size, ramdisk_size = struct.unpack_from('<II', boot, 8)
    signature_size = struct.unpack_from('<I', boot, 1580)[0]
    if not kernel_size:
        raise ValueError('Boot has no kernel (init_boot is not a kernel image)')
    offset = PAGE_SIZE
    for name, size in [('kernel', kernel_size), ('ramdisk', ramdisk_size), ('signature', signature_size)]:
        if size and offset + size > len(boot):
            raise ValueError(f'Truncated boot {name}')
        offset += align(size)
    return boot[PAGE_SIZE:PAGE_SIZE + kernel_size]


def decompress_kernel(kernel):
    if kernel.startswith(b'\x1f\x8b'):
        try:
            return gzip.decompress(kernel), 'gzip'
        except (EOFError, OSError, zlib.error) as exc:
            raise ValueError(f'Invalid gzip kernel: {exc}') from exc
    if kernel.startswith(LZ4_LEGACY):
        try:
            result = subprocess.run(['lz4', '-d', '-c'], input=kernel, capture_output=True, check=False)
        except FileNotFoundError as exc:
            raise ValueError('LZ4 legacy kernel: install the upstream lz4 CLI and put it on PATH') from exc
        if result.returncode:
            raise ValueError(f'Invalid LZ4 legacy kernel: {result.stderr.decode(errors="replace").strip()}')
        return result.stdout, 'lz4-legacy'
    # Known compressed formats cannot be treated as a raw Image just because
    # they might contain a literal banner. Raw arm64 Image itself has no wrapper.
    if kernel.startswith((b'\xfd7zXZ\x00', b'BZh', b'\x04\x22\x4d\x18', b'\x28\xb5\x2f\xfd', b'\x89LZO')):
        raise ValueError('Unsupported kernel compression; expected gzip, LZ4 legacy or raw Image')
    return kernel, 'none'


def kernel_banner(image):
    # linux_banner is a printable, newline/NUL-terminated string. Ignore the
    # kernel's format strings ("Linux version %s") and reject ambiguous versions.
    banners = {m.group().decode('ascii') for m in re.finditer(
        rb'Linux version [0-9][!-~]* [\x20-\x7e]+(?=[\n\x00])', image)}
    if len(banners) != 1:
        raise ValueError(f'Expected one Linux version banner in decompressed kernel; found {len(banners)}')
    return banners.pop()


def make_report(boot_path, image_path=None):
    boot = Path(boot_path).read_bytes()
    image, compression = decompress_kernel(unpack_kernel(boot))
    if image_path is not None and Path(image_path).read_bytes() != image:
        raise ValueError('Supplied Image does not match the decompressed boot kernel')
    return {
        'schema_version': 1,
        'kernel_banner': kernel_banner(image),
        'boot_sha256': hashlib.sha256(boot).hexdigest(),
        'boot_bytes': len(boot),
        'boot_header_version': 4,
        'kernel_compression': compression,
        'image_sha256': hashlib.sha256(image).hexdigest(),
        'image_bytes': len(image),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('boot', type=Path, help='Final boot image, including any AVB footer/padding')
    parser.add_argument('--image', type=Path, help='Optional uncompressed Image to verify against boot')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.resolve() in {p.resolve() for p in (args.boot, args.image) if p is not None}:
        raise ValueError('Report output must not overwrite an input image')
    report = make_report(args.boot, args.image)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError) as exc:
        print(f'FAILED: {exc}', file=sys.stderr)
        raise SystemExit(1)
