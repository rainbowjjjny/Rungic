#!/usr/bin/env python3
"""tools/ci/preflight.py against stand-in phones: a real Motorola spec (G100, portov_cn),
a synthetic Pixel-format spec and matching audited extractions. A mismatch stops before the
report is written; only reading commands are sent to the phone. AVB key extraction is offline."""
import hashlib
import json
import struct
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import preflight

ROOT = Path(__file__).resolve().parents[2]
SPEC = ROOT / 'profiles/devices/motorola/portov_cn/W1VT36H.1-51-8.json'
SERIAL = 'ZY32TEST'
READING = {'getprop', 'uname', 'getenforce', 'dumpsys'}


class Preflight(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        spec = json.loads(SPEC.read_text())
        stock = self.root / 'stock'
        stock.mkdir()
        (stock / 'boot.img').write_bytes(b'stock boot')
        spec['stock']['boot_sha256'] = hashlib.sha256(b'stock boot').hexdigest()
        spec['release_requirements']['minimum_host_free_gib'] = 0
        self.spec_data = spec
        identity = spec['identity']
        (stock / 'manifest.json').write_text(json.dumps({
            'archive_sha256': spec['stock']['archive_sha256'], 'fingerprint': identity['fingerprint'],
            'device': identity['device'], 'files': {'boot.img': {'sha256': spec['stock']['boot_sha256']}}}))
        manifest_sha = hashlib.sha256((stock / 'manifest.json').read_bytes()).hexdigest()
        (stock / 'verification.json').write_text(json.dumps({
            'stock_manifest_sha256': manifest_sha, 'super_sha256': spec['stock']['super_sha256'],
            'avb_public_key_sha1': spec['stock']['avb_public_key_sha1'], 'flashed': False, 'device_tested': False}))
        self.stock = stock
        self.output = self.root / 'out/preflight.json'
        self.phone = {
            'state': 'device',
            'ro.product.device': identity['product'], 'ro.boot.hardware.sku': identity['sku'],
            'ro.build.fingerprint': identity['fingerprint'], 'ro.bootloader': identity['bootloader'],
            'ro.build.version.sdk': str(spec['release_requirements']['android_api']),
            'ro.boot.flash.locked': '0', 'ro.boot.verifiedbootstate': 'orange', 'ro.boot.slot_suffix': '_b',
            'uname': spec['kernel']['stock_release'], 'getenforce': 'Enforcing', 'battery': '80'}
        self.sent = []

    def adb(self, argv, check=True, capture_output=True, text=True, timeout=None):
        self.assertEqual(argv[:3], ['adb', '-P', '5037'])
        if argv[3] == 'devices':
            out = f"List of devices attached\n{SERIAL}\t{self.phone['state']}\n"
        else:
            self.assertEqual(argv[3:6], ['-s', SERIAL, 'shell'])
            command = argv[6:]
            self.sent.append(command)
            out = {'getprop': lambda: self.phone[command[1]], 'uname': lambda: self.phone['uname'],
                   'getenforce': lambda: self.phone['getenforce'],
                   'dumpsys': lambda: f"Current Battery Service state:\n  level: {self.phone['battery']}\n"}[command[0]]()
        return subprocess.CompletedProcess(argv, 0, out + '\n', '')

    def preflight(self):
        spec = self.root / 'spec.json'
        spec.write_text(json.dumps(self.spec_data))
        argv = ['preflight.py', str(spec), str(self.stock), '--serial', SERIAL, '--output', str(self.output)]
        with mock.patch.object(sys, 'argv', argv), mock.patch.object(preflight.subprocess, 'run', self.adb), \
                mock.patch('builtins.print'):
            preflight.main()
        return json.loads(self.output.read_text())

    # covers: install.device-spec/E2
    def test_matching_phone_passes_with_only_reading_commands(self):
        report = self.preflight()
        self.assertEqual(report['result'], 'source-and-device-preflight-passed')
        self.assertEqual(report['observed']['properties']['ro.boot.slot_suffix'], '_b')
        self.assertEqual(report['observed']['battery_percent'], 80)
        self.assertTrue(self.sent)
        self.assertLessEqual({c[0] for c in self.sent}, READING, 'nothing but reading is sent to the phone')

    # covers: install.device-spec/E2
    def test_any_difference_stops_without_a_report(self):
        cases = {'state': 'unauthorized', 'ro.product.device': 'mumba', 'ro.boot.hardware.sku': 'XT2537-4',
                 'ro.build.fingerprint': 'motorola/portov_cn/portov:16/OTHER/x:user/release-keys',
                 'ro.bootloader': 'MBM-other', 'ro.build.version.sdk': '35', 'ro.boot.flash.locked': '1',
                 'ro.boot.verifiedbootstate': 'green', 'ro.boot.slot_suffix': '', 'uname': '6.6.0-other',
                 'getenforce': 'Permissive', 'battery': '29'}
        good = dict(self.phone)
        for field, bad in cases.items():
            with self.subTest(field=field):
                self.phone = dict(good, **{field: bad})
                self.sent = []
                self.output.unlink(missing_ok=True)
                with self.assertRaises(ValueError):
                    self.preflight()
                self.assertFalse(self.output.exists())
                self.assertLessEqual({c[0] for c in self.sent}, READING)

    # covers: install.device-spec/E2
    def test_too_little_host_space_stops(self):
        self.spec_data['release_requirements']['minimum_host_free_gib'] = 10 ** 9
        with self.assertRaisesRegex(ValueError, 'host free space'):
            self.preflight()
        self.assertFalse(self.output.exists())


def vbmeta_image(key=b'AVB public key blob', auth_size=64, key_offset=32):
    """A header, authentication padding and auxiliary block with a nonzero key offset."""
    header = bytearray(256)
    header[:4] = b'AVB0'
    struct.pack_into('>IIQQI', header, 4, 1, 0, auth_size, 128, 1)
    struct.pack_into('>QQ', header, 64, key_offset, len(key))
    auxiliary = bytearray(128)
    auxiliary[key_offset:key_offset + len(key)] = key
    return bytes(header) + b'A' * auth_size + bytes(auxiliary) + b'trailing partition padding'


class PixelPreflight(Preflight):
    def setUp(self):
        super().setUp()
        # A different codename proves the new path is selected by the spec's format.
        self.spec_data['identity'].update(product='testpixel', device='testpixel', sku='TEST',
                                          fingerprint='google/testpixel/testpixel:16/TEST.001/123:user/release-keys',
                                          bootloader='test-loader')
        identity = self.spec_data['identity']
        self.phone.update({'ro.product.device': identity['product'], 'ro.boot.hardware.sku': identity['sku'],
                           'ro.build.fingerprint': identity['fingerprint'], 'ro.bootloader': identity['bootloader']})
        self.spec_data['stock'] = {'format': 'pixel-factory', 'archive_name': 'testpixel-factory.zip',
                                   'archive_sha256': 'a' * 64, 'avb_public_key_sha1': None}
        self.verification = {'schema_version': 1, 'device': 'testpixel', 'build': 'TEST.001',
                             'archive_name': 'testpixel-factory.zip', 'archive_sha256': 'a' * 64,
                             'files': {}, 'flashed': False, 'device_tested': False}
        for partition in ('boot', 'init_boot', 'vendor_boot', 'vendor_kernel_boot', 'dtbo', 'vbmeta',
                          'vbmeta_system', 'vbmeta_vendor', 'vendor_dlkm', 'system_dlkm'):
            data = vbmeta_image() if partition == 'vbmeta' else f'stock {partition}'.encode()
            (self.stock / f'{partition}.img').write_bytes(data)
            digest = hashlib.sha256(data).hexdigest()
            self.spec_data['stock'][f'{partition}_sha256'] = digest
            self.verification['files'][f'{partition}.img'] = {'sha256': digest, 'bytes': len(data)}
        self.verification['files']['android-info.txt'] = {'sha256': 'b' * 64, 'bytes': 100}
        (self.stock / 'manifest.json').unlink()
        self.write_verification()

    def write_verification(self):
        (self.stock / 'verification.json').write_text(json.dumps(self.verification))

    # covers: install.device-spec/E2
    def test_pixel_report_needs_no_motorola_manifest_or_super(self):
        report = self.preflight()
        self.assertNotIn('stock_manifest_sha256', report)
        self.assertEqual(report['stock_verification_sha256'], preflight.sha256(self.stock / 'verification.json'))

    # covers: install.device-spec/E2
    def test_pixel_metadata_mismatches_stop_before_adb(self):
        good = dict(self.verification)
        for field, bad in {'schema_version': 2, 'device': 'other', 'build': 'OTHER.001',
                           'archive_name': 'other.zip', 'archive_sha256': '0' * 64,
                           'flashed': True, 'device_tested': True}.items():
            with self.subTest(field=field):
                self.verification = dict(good, **{field: bad})
                self.write_verification()
                with mock.patch.object(preflight.subprocess, 'run') as adb_run:
                    with self.assertRaises(ValueError):
                        self.preflight_without_adb_mock()
                    adb_run.assert_not_called()
                self.assertFalse(self.output.exists())

    def preflight_without_adb_mock(self):
        spec = self.root / 'spec.json'
        spec.write_text(json.dumps(self.spec_data))
        with mock.patch.object(sys, 'argv', ['preflight.py', str(spec), str(self.stock),
                                             '--serial', SERIAL, '--output', str(self.output)]):
            preflight.main()

    # covers: install.device-spec/E2
    def test_every_declared_partition_is_verified_against_spec_and_disk(self):
        for name, entry in list(self.verification['files'].items()):
            if not name.endswith('.img'):
                continue
            path = self.stock / name
            data = path.read_bytes()
            for mutation in ('report hash', 'report size', 'missing entry', 'disk hash', 'missing file'):
                with self.subTest(partition=name, mutation=mutation):
                    self.verification['files'][name] = dict(entry)
                    if mutation == 'report hash':
                        self.verification['files'][name]['sha256'] = '0' * 64
                    elif mutation == 'report size':
                        self.verification['files'][name]['bytes'] += 1
                    elif mutation == 'missing entry':
                        del self.verification['files'][name]
                    elif mutation == 'disk hash':
                        path.write_bytes(b'X' * len(data))
                    else:
                        path.unlink()
                    self.write_verification()
                    with self.assertRaises((ValueError, OSError)):
                        self.preflight()
                    self.assertFalse(self.output.exists())
                    self.assertEqual(self.sent, [])
                    path.write_bytes(data)
                    self.verification['files'][name] = entry
        self.write_verification()

    # covers: install.device-spec/E2
    def test_partitions_are_selected_from_spec(self):
        data = b'another declared partition'
        digest = hashlib.sha256(data).hexdigest()
        self.spec_data['stock']['extra_partition_sha256'] = digest
        self.verification['files']['extra_partition.img'] = {'sha256': digest, 'bytes': len(data)}
        self.write_verification()
        with self.assertRaises(OSError):
            self.preflight()
        (self.stock / 'extra_partition.img').write_bytes(data)
        self.preflight()

    # covers: install.device-spec/E2
    def test_avb_key_is_checked_when_spec_pins_it(self):
        self.spec_data['stock']['avb_public_key_sha1'] = hashlib.sha1(b'AVB public key blob').hexdigest()
        self.preflight()
        self.output.unlink()
        self.spec_data['stock']['avb_public_key_sha1'] = '0' * 40
        with self.assertRaisesRegex(ValueError, 'AVB key'):
            self.preflight()
        self.assertFalse(self.output.exists())

    # covers: install.device-spec/E2
    def test_unknown_stock_format_is_rejected(self):
        self.spec_data['stock']['format'] = 'unknown'
        with self.assertRaisesRegex(ValueError, 'stock format'):
            self.preflight()


class AvbPublicKey(unittest.TestCase):
    # covers: install.device-spec/E2
    def test_key_hash_uses_auxiliary_offset_and_size_only(self):
        key = b'only the AVB key, not authentication or padding'
        for auth_size in (0, 64, 128):
            with self.subTest(auth_size=auth_size):
                self.assertEqual(preflight.avb_public_key_sha1(vbmeta_image(key, auth_size)),
                                 hashlib.sha1(key).hexdigest())

    # covers: install.device-spec/E2
    def test_malformed_vbmeta_is_rejected(self):
        good = vbmeta_image()
        cases = [b'', good[:255], b'NOPE' + good[4:], good[:256], good[:-60]]
        for offset, fmt, value in ((4, '>I', 2), (12, '>Q', 65), (20, '>Q', 129),
                                   (64, '>Q', 128), (72, '>Q', 0), (72, '>Q', 129)):
            bad = bytearray(good)
            struct.pack_into(fmt, bad, offset, value)
            cases.append(bytes(bad))
        for data in cases:
            with self.subTest(data=data[:80]):
                with self.assertRaises(ValueError):
                    preflight.avb_public_key_sha1(data)


if __name__ == '__main__':
    unittest.main()
