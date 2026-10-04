#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""The offline test run itself (docs/95): tools/run-tests.sh runs every part and says which failed,
skips the QML tests without PySide6, and no test reaches the phone (tools/conftest.py)."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import rungic_device

ROOT = Path(__file__).resolve().parents[1]

FAKE_PYTHON = '''#!/bin/sh
echo "python $*" >> "$LOG"
case "$*" in
  "-c import PySide6") exit "$PYSIDE_RC" ;;
  "-m pytest"*) exit "$PYTEST_RC" ;;
esac
exit 0
'''
FAKE_TOOL = '#!/bin/sh\necho "$(basename "$0") $*" >> "$LOG"\nexit 0\n'


class NoDeviceTests(unittest.TestCase):
    """Whatever path a tool takes to adb, a test stops before a process starts."""

    def setUp(self):
        self.started = []

        def spy(argv, *args, **kwargs):
            self.started.append(argv)
            raise AssertionError(f'a process was started: {argv[:3]}')
        p = patch.object(rungic_device.subprocess, 'run', spy)
        p.start()
        self.addCleanup(p.stop)
        rungic_device.transport.cache_clear()
        self.addCleanup(rungic_device.transport.cache_clear)

    # covers: delivery.offline-tests/E2
    def test_every_way_to_the_phone_fails_the_test(self):
        for name, call in (('run', lambda: rungic_device.run('id', 'root')),
                           ('out', lambda: rungic_device.out('id', 'container')),
                           ('push', lambda: rungic_device.push(__file__)),
                           ('adb', lambda: rungic_device.adb('pull', '/x', '/tmp/x')),
                           ('transport', lambda: rungic_device.transport()),
                           ('from_container', lambda: rungic_device.from_container('/etc/hostname', '/tmp/x'))):
            with self.subTest(name), patch.dict(os.environ, {'RUNGIC_TRANSPORT': ''}):
                with self.assertRaisesRegex(AssertionError, 'an offline test reached the device'):
                    call()
        self.assertEqual(self.started, [], 'no adb process may start, not even `adb devices`')


@unittest.skipUnless(shutil.which('git'), 'needs git')
class RunTestsScriptTests(unittest.TestCase):
    """tools/run-tests.sh in a small repository, its programs stood in for (they log what they were asked)."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / 'repo'
        files = {'tools/run-tests.sh': (ROOT / 'tools/run-tests.sh').read_text(),
                 'tools/tests/test_cards.py': 'from PySide6.QtQml import QQmlEngine\n',
                 'tools/test_plain.py': 'def test(): pass\n',
                 'tools/ci/test_ci.py': 'def test(): pass\n',
                 'tools/pq.py': '',
                 'system/good.sh': '#!/bin/sh\necho fine\n',
                 'android/hook.sh': '#!/system/bin/sh\nif true; then echo ok; fi\n'}
        for name, text in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        venv = self.root / '.work/venv/bin'
        venv.mkdir(parents=True)
        (venv / 'python').write_text(FAKE_PYTHON)
        (venv / 'python').chmod(0o755)
        self.bin = Path(temp.name) / 'bin'
        self.bin.mkdir()
        for tool in ('javac', 'java'):
            (self.bin / tool).write_text(FAKE_TOOL)
            (self.bin / tool).chmod(0o755)
        self.log = Path(temp.name) / 'log'
        self.git('init', '-q')

    def git(self, *args):
        subprocess.run(['git', *args], cwd=self.root, check=True, capture_output=True)

    def run_tests(self, pyside=True, pytest_ok=True):
        self.git('add', '-A')
        self.log.write_text('')
        env = {**os.environ, 'PATH': f'{self.bin}:{os.environ["PATH"]}', 'LOG': str(self.log),
               'PYSIDE_RC': '0' if pyside else '1', 'PYTEST_RC': '0' if pytest_ok else '1'}
        result = subprocess.run(['sh', str(self.root / 'tools/run-tests.sh')], capture_output=True, text=True, env=env)
        return result, self.log.read_text().splitlines()

    # covers: delivery.offline-tests/E1
    def test_one_command_runs_every_part(self):
        result, log = self.run_tests()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip().splitlines()[-1], 'All offline tests passed')
        pytest = [l for l in log if l.startswith('python -m pytest')]
        self.assertEqual(pytest, ['python -m pytest -q -p no:cacheprovider tools/ci tools/test_plain.py tools/tests system/account'])
        self.assertIn('python tools/pq.py lint', log)
        self.assertTrue(any(l.startswith('javac ') and 'android/app/tests/' in l for l in log), log)
        self.assertIn('java -cp', ' '.join(log))
        self.assertTrue(any('com.rungic.plasma.FirstBootStateTest' in l for l in log))
        self.assertTrue(any('com.rungic.plasma.ControlExceptionTest' in l for l in log))
        self.assertTrue(any('com.rungic.plasma.DisplayGeometryTest' in l for l in log),
                        'husky and Adreno display policy must run with the existing Java checks')

    # covers: delivery.offline-tests/E1
    def test_failures_are_named_at_the_end(self):
        (self.root / 'system/broken.sh').write_text('#!/bin/sh\nif then fi (\n')
        result, _ = self.run_tests(pytest_ok=False)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout.strip().splitlines()[-1], 'FAILED: python sh:system/broken.sh')

    # covers: delivery.offline-tests/E3
    def test_without_pyside6_the_qml_tests_are_skipped_with_a_hint(self):
        result, log = self.run_tests(pyside=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('skipped tools/tests/test_cards.py: no PySide6 (sh tools/dev-setup.sh)', result.stdout)
        pytest = [l for l in log if l.startswith('python -m pytest')]
        self.assertEqual(pytest, ['python -m pytest -q -p no:cacheprovider --ignore=tools/tests/test_cards.py '
                                  'tools/ci tools/test_plain.py tools/tests system/account'])


if __name__ == '__main__':
    unittest.main()
