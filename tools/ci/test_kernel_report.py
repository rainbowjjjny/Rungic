"""Non-Qualcomm phones such as husky need reproducible CI3 kernel reports too.

Synthetic v4 boot images keep these checks independent of firmware downloads,
devices and build machines. A banner in the cmdline/footer must never identify
the running kernel: standalone preflight compares its release and whole boot.
"""
# covers: install.standalone-install/E1 install.gki-kernel/E4
import gzip
import hashlib
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys

import pytest

from kernel_report import make_report


BANNER = b'Linux version 6.1.145-android14-11 (builder@host) (clang version 17) #1 SMP PREEMPT\n'
IMAGE = b'\x00' * 64 + BANNER + b'\x00kernel data'


def legacy(data):
    """One literal-only LZ4 block; no compressor needed to create the fixture."""
    length = len(data) - 15
    extension = b'\xff' * (length // 255) + bytes([length % 255])
    block = b'\xf0' + extension + data
    return b'\x02\x21\x4c\x18' + struct.pack('<I', len(block)) + block


def boot(kernel, ramdisk=b'', signature=b'', footer=b''):
    def page(data):
        return data + b'\x00' * (-len(data) % 4096)
    header = bytearray(1584)
    struct.pack_into('<8s9I', header, 0, b'ANDROID!', len(kernel), len(ramdisk), 0, 1584, 0, 0, 0, 0, 4)
    struct.pack_into('<I', header, 1580, len(signature))
    # A plausible but wrong version outside the kernel must be ignored.
    header[44:44 + len(BANNER)] = BANNER.replace(b'6.1.145', b'9.9.999')
    return page(header) + page(kernel) + page(ramdisk) + page(signature) + footer


@pytest.mark.parametrize('compression', ['gzip', 'lz4-legacy', 'none'])
def test_report_comes_from_boot_kernel_and_hashes_entire_file(tmp_path, compression):
    if compression == 'lz4-legacy' and not shutil.which('lz4'):
        pytest.skip('legacy decoding requires the upstream lz4 CLI')
    kernel = {'gzip': gzip.compress, 'lz4-legacy': legacy, 'none': lambda data: data}[compression](IMAGE)
    data = boot(kernel, ramdisk=b'ramdisk', signature=b'signature', footer=b'AVB footer')
    source = tmp_path / 'boot.img'
    source.write_bytes(data)
    report = make_report(source)
    assert report['kernel_banner'] == BANNER.decode().strip()
    # This is exactly how standalone.py pack derives the preflight uname value.
    assert report['kernel_banner'].split('Linux version ', 1)[1].split(' ', 1)[0] == '6.1.145-android14-11'
    assert report['boot_sha256'] == hashlib.sha256(data).hexdigest()
    assert report['boot_bytes'] == len(data)
    assert report['kernel_compression'] == compression
    assert report['image_sha256'] == hashlib.sha256(IMAGE).hexdigest()
    assert report['image_bytes'] == len(IMAGE)
    assert report['boot_header_version'] == 4


def test_optional_image_must_be_the_kernel_in_boot(tmp_path):
    source, image = tmp_path / 'boot.img', tmp_path / 'Image'
    source.write_bytes(boot(gzip.compress(IMAGE)))
    image.write_bytes(IMAGE)
    assert make_report(source, image)['image_sha256'] == hashlib.sha256(IMAGE).hexdigest()
    # An unrelated build output cannot silently supply the report for a different boot.
    image.write_bytes(IMAGE.replace(b'6.1.145', b'6.1.146'))
    with pytest.raises(ValueError, match='Image.*match'):
        make_report(source, image)


@pytest.mark.parametrize('offset,value,message', [
    (40, 3, 'version'), (20, 1580, 'header size'),
    (8, 0, 'kernel'), (8, 100000, 'kernel'),
    (12, 100000, 'ramdisk'), (1580, 100000, 'signature'),
])
def test_malformed_header_cannot_generate_a_preflight_report(tmp_path, offset, value, message):
    data = bytearray(boot(gzip.compress(IMAGE)))
    struct.pack_into('<I', data, offset, value)
    source = tmp_path / 'boot.img'
    source.write_bytes(data)
    with pytest.raises(ValueError, match=message):
        make_report(source)


@pytest.mark.parametrize('data', [b'', b'ANDROID!', b'not a boot image' * 300])
def test_truncated_or_wrong_magic_rejected(tmp_path, data):
    source = tmp_path / 'boot.img'
    source.write_bytes(data)
    with pytest.raises(ValueError, match='header|magic'):
        make_report(source)


@pytest.mark.parametrize('kernel,message', [
    (gzip.compress(b'no banner'), 'banner'),
    (gzip.compress(IMAGE)[:-3], 'gzip'),
    (gzip.compress(IMAGE + b'\x00Linux version 6.6.0 (other) #2 SMP\n'), 'banner'),
    (b'\xfd7zXZ\x00not supported', 'compression'),
])
def test_no_guessing_from_header_footer_or_corrupt_kernel(tmp_path, kernel, message):
    source = tmp_path / 'boot.img'
    source.write_bytes(boot(kernel, footer=BANNER))
    with pytest.raises(ValueError, match=message):
        make_report(source)


def test_missing_lz4_has_actionable_error(tmp_path, monkeypatch):
    source = tmp_path / 'boot.img'
    source.write_bytes(boot(legacy(IMAGE)))
    monkeypatch.setenv('PATH', '')
    with pytest.raises(ValueError, match='install.*lz4'):
        make_report(source)


@pytest.mark.skipif(not shutil.which('lz4'), reason='needs upstream lz4 CLI')
def test_corrupt_legacy_block_is_rejected(tmp_path):
    source = tmp_path / 'boot.img'
    source.write_bytes(boot(legacy(IMAGE)[:-4]))
    with pytest.raises(ValueError, match='LZ4'):
        make_report(source)


def test_cli_writes_pack_input_and_leaves_no_report_on_failure(tmp_path):
    source, output = tmp_path / 'boot.img', tmp_path / 'kernel-report.json'
    source.write_bytes(boot(gzip.compress(IMAGE)))
    command = [sys.executable, str(Path(__file__).with_name('kernel_report.py')), str(source), '--output', str(output)]
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(output.read_text()) == make_report(source)
    output.unlink()
    source.write_bytes(b'broken')
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode != 0
    assert 'FAILED:' in result.stderr
    assert not output.exists()
