#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""tools/build_on_device.py and the phone's transfer command without the Mac mini or the phone (docs/71):
the default build host, the Mac's system proxy in every container command, the incremental source
sync, and what the phone's restricted key may do on the build host."""
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import build_on_device

ROOT = Path(__file__).resolve().parents[1]

SCUTIL = """<dictionary> {
  ExceptionsList : <array> {
    0 : 127.0.0.1
  }
  HTTPEnable : 1
  HTTPPort : 6152
  HTTPProxy : 127.0.0.1
  HTTPSEnable : 1
  HTTPSPort : 6152
  HTTPSProxy : 127.0.0.1
  SOCKSEnable : 1
  SOCKSPort : 6153
  SOCKSProxy : 127.0.0.1
}
"""


class FakeMac(build_on_device.MacMini):
    """The Mac mini's ssh as a list of commands; scutil answers with the given system proxy."""

    def __init__(self, scutil=SCUTIL, container=None):
        self.commands, self.scutil, self.container = [], scutil, container

    def ssh(self, command, timeout, check=True, data=None, stdout=subprocess.PIPE):
        self.commands.append(command)
        out = b''
        if command == 'scutil --proxy':
            out = self.scutil.encode()
        elif command.startswith(f'{self.DOCKER} inspect'):
            out = (self.container or '').encode()
        return subprocess.CompletedProcess(command, 0, out, b'')


class ProxyTests(unittest.TestCase):
    # covers: delivery.build-hosts/E6
    def test_every_container_command_carries_the_macs_system_proxy(self):
        mac = FakeMac(container=f'{build_on_device.MacMini.image()} true')
        want = {'http_proxy': 'http://host.docker.internal:6152', 'https_proxy': 'http://host.docker.internal:6152',
                'no_proxy': 'localhost,127.0.0.1'}
        self.assertEqual(mac.proxy(), want)
        mac.run('apt-get update')
        mac.out('true')
        command = mac.commands[-1]
        self.assertTrue(command.startswith(f'{mac.DOCKER} exec -i '), command)
        for key, value in want.items():
            self.assertIn(f'-e {key}={value} ', command)
        # scutil is asked once per session, not per command.
        self.assertEqual(mac.commands.count('scutil --proxy'), 1)

    # covers: delivery.build-hosts/E6
    def test_the_build_image_gets_the_proxy_too(self):
        mac = FakeMac(container='')
        mac.ensure()
        build = next(c for c in mac.commands if ' build -q ' in c)
        self.assertIn('--build-arg http_proxy=http://host.docker.internal:6152 ', build)
        self.assertIn('--build-arg https_proxy=http://host.docker.internal:6152 ', build)

    # covers: delivery.build-hosts/E6
    def test_no_system_proxy_no_flags(self):
        mac = FakeMac(scutil='<dictionary> {\n  HTTPEnable : 0\n  HTTPSEnable : 0\n}\n',
                      container=f'{build_on_device.MacMini.image()} true')
        self.assertEqual(mac.proxy(), {})
        self.assertEqual(mac.exec('true'), f'{mac.DOCKER} exec {mac.CONTAINER} true')


class DefaultHostTests(unittest.TestCase):
    def host_for(self, env):
        seen = []
        with patch.dict(os.environ, env, clear=False), patch.object(sys, 'argv', ['build_on_device.py', 'kwin', 'status']), \
                patch.object(build_on_device, 'status', lambda component: seen.append(build_on_device.host.name) or ''):
            if 'RUNGIC_BUILD_HOST' not in env:
                os.environ.pop('RUNGIC_BUILD_HOST', None)
            build_on_device.main()
        return seen[0]

    # covers: delivery.build-hosts/E1
    def test_the_mac_mini_unless_told_otherwise(self):
        saved = build_on_device.host
        self.addCleanup(setattr, build_on_device, 'host', saved)
        self.assertEqual(self.host_for({}), 'macmini')
        self.assertEqual(self.host_for({'RUNGIC_BUILD_HOST': 'phone'}), 'phone')

    # covers: delivery.build-hosts/E1
    def test_a_mac_mini_build_leaves_the_phone_alone(self):
        # install and divert change the phone's system: refused on the build host (tools/conftest.py
        # fails any test that reaches the phone).
        saved = build_on_device.host
        self.addCleanup(setattr, build_on_device, 'host', saved)
        build_on_device.host = FakeMac()
        for action in (lambda: build_on_device.install('kwin'), lambda: build_on_device.divert('kwin', ['a=/b'])):
            with self.assertRaisesRegex(SystemExit, 'changes the phone'):
                action()


class ConfigurableHostTests(unittest.TestCase):
    """husky contributors need their own build host; the original Adreno owner keeps theirs."""

    # covers: delivery.build-hosts/E1
    def test_remote_address_is_configurable_and_owner_default_survives(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(build_on_device.MacMini().SSH[-1], 'choukevin@macmini.wire.net')
        with patch.dict(os.environ, {'RUNGIC_BUILD_SSH': 'builder@studio.example'}):
            self.assertEqual(build_on_device.MacMini().SSH[-1], 'builder@studio.example')
        # Explicit CLI selection wins over both environment selections.
        saved = build_on_device.host
        self.addCleanup(setattr, build_on_device, 'host', saved)
        with patch.dict(os.environ, {'RUNGIC_BUILD_HOST': 'phone', 'RUNGIC_BUILD_SSH': 'env@host'}), \
                patch.object(sys, 'argv', ['build_on_device.py', '--host', 'macmini',
                                          '--ssh-host', 'cli@host', 'mesa', 'status']), \
                patch.object(build_on_device, 'status', return_value=''):
            build_on_device.main()
        self.assertEqual(build_on_device.host.SSH[-1], 'cli@host')

    # covers: delivery.build-hosts/E1
    def test_local_docker_selected_from_environment_or_cli_without_phone_access(self):
        saved = build_on_device.host
        self.addCleanup(setattr, build_on_device, 'host', saved)
        for env, argv in [({'RUNGIC_BUILD_HOST': 'local-docker'}, []),
                          ({'RUNGIC_BUILD_HOST': 'phone'}, ['--host', 'local-docker'])]:
            with self.subTest(env=env), patch.dict(os.environ, env), \
                    patch.object(sys, 'argv', ['build_on_device.py', *argv, 'mesa', 'status']), \
                    patch.object(build_on_device, 'status', return_value=''):
                build_on_device.main()
                self.assertEqual(build_on_device.host.name, 'local-docker')
                self.assertIsInstance(build_on_device.host, build_on_device.LocalDocker)
                with self.assertRaisesRegex(SystemExit, 'changes the phone'):
                    build_on_device.install('mesa')

    # covers: delivery.build-hosts/E1
    def test_invalid_environment_target_has_a_cli_error(self):
        with patch.dict(os.environ, {'RUNGIC_BUILD_HOST': 'typo'}), \
                patch.object(sys, 'argv', ['build_on_device.py', 'mesa', 'status']):
            with self.assertRaises(SystemExit) as error:
                build_on_device.main()
        self.assertEqual(error.exception.code, 2)


class LocalDockerTests(unittest.TestCase):
    def local(self, arch='aarch64', cpus=20, memory_gib=8, existing=False):
        local = build_on_device.LocalDocker()
        calls = []

        def command(cmd, timeout, check=True, data=None, stdout=subprocess.PIPE):
            calls.append((cmd, data))
            out = b''
            if ' info ' in cmd:
                out = json.dumps({'Architecture': arch, 'NCPU': cpus,
                                  'MemTotal': memory_gib * 1024**3}).encode()
            elif cmd == 'scutil --proxy':
                out = SCUTIL.encode()
            elif ' inspect -f ' in cmd and existing:
                out = f'{local.image()} true'.encode()
            return subprocess.CompletedProcess(cmd, 0, out, b'')

        local.ssh = command
        return local, calls

    # covers: delivery.build-hosts/E1
    def test_local_transport_streams_binary_input_without_ssh(self):
        local = build_on_device.LocalDocker()
        with patch('subprocess.run', return_value=subprocess.CompletedProcess([], 0, b'ok', b'')) as run:
            result = local.ssh('docker exec -i rungic-build cat', 15, data=b'\x00payload')
        self.assertEqual(run.call_args.args[0], ['sh', '-c', 'docker exec -i rungic-build cat'])
        self.assertEqual(run.call_args.kwargs['input'], b'\x00payload')
        self.assertEqual(result.stdout, b'ok')

    # covers: delivery.build-hosts/E1, delivery.build-hosts/E6
    def test_local_image_volume_proxy_and_native_platform_reuse_the_existing_recipe(self):
        # CPU rendering for non-Qualcomm husky needs LLVM; do not create a second,
        # smaller Docker recipe that silently drops Mesa's build dependencies.
        local, calls = self.local()
        local.ensure()
        build, archive = next((c, data) for c, data in calls if ' build -q ' in c)
        self.assertIn('--platform linux/arm64', build)
        self.assertIn('--build-arg https_proxy=http://host.docker.internal:6152', build)
        with tarfile.open(fileobj=io.BytesIO(archive)) as context:
            self.assertEqual(context.extractfile('Dockerfile').read(),
                             (ROOT / 'tools/pq/arm64-host.Dockerfile').read_bytes())
        start = next(c for c, _ in calls if ' run -d ' in c)
        self.assertIn('--platform linux/arm64', start)
        self.assertIn('-v rungic-build:/root/rungic-build', start)
        self.assertIn('--name rungic-build', start)
        local.run('true')
        self.assertIn('-e https_proxy=http://host.docker.internal:6152', calls[-1][0])
        before = len(calls)
        local.ensure()
        self.assertEqual(len(calls), before)

    # covers: delivery.build-hosts/E1
    def test_running_local_container_is_reused_without_rebuilding(self):
        local, calls = self.local(existing=True)
        local.ensure()
        self.assertFalse(any(' build -q ' in c or ' run -d ' in c for c, _ in calls))

    # covers: delivery.build-hosts/E1
    def test_docker_vm_resources_bound_default_and_explicit_parallelism(self):
        # The Studio has 20 CPUs but Docker has only 8 GiB: compiling LLVM/Mesa
        # at -j20 can exhaust the VM even though the host has 128 GiB.
        # With 64 GiB the VM can feed every CPU, so no fixed cap may throttle the build.
        for cpus, memory, want in [(20, 8, 4), (20, 4, 2), (2, 8, 2), (20, 1, 1), (20, 64, 20)]:
            with self.subTest(cpus=cpus, memory=memory):
                local, _ = self.local(cpus=cpus, memory_gib=memory)
                self.assertEqual(local.jobs, want)
        local, _ = self.local()
        with patch.object(build_on_device, 'host', local), patch.object(local, 'background') as background:
            build_on_device.start('mesa', 'targets', 20)
            self.assertIn('ninja -C /root/rungic-build/mesa/build -j 4', background.call_args.args[1])
            build_on_device.start('mesa', 'targets', 2)
            self.assertIn('ninja -C /root/rungic-build/mesa/build -j 2', background.call_args.args[1])

    # covers: delivery.build-hosts/E1
    def test_local_target_refuses_emulation_and_invalid_resources_before_building(self):
        for args in [{'arch': 'x86_64'}, {'cpus': 0}, {'memory_gib': 0}]:
            with self.subTest(args=args):
                local, calls = self.local(**args)
                with self.assertRaises(build_on_device.rungic_device.DeviceError):
                    local.ensure()
                self.assertFalse(any(' build -q ' in c or ' run -d ' in c for c, _ in calls))

    # covers: delivery.build-hosts/E1
    def test_adreno_owners_remote_parallelism_and_invalid_job_requests(self):
        with patch.object(build_on_device, 'host', FakeMac()):
            self.assertEqual(build_on_device.build_jobs(), 10)
            self.assertEqual(build_on_device.build_jobs(12), 12)
            for jobs in (0, -1):
                with self.subTest(jobs=jobs), self.assertRaisesRegex(SystemExit, 'positive'):
                    build_on_device.build_jobs(jobs)

    # covers: delivery.build-hosts/E1, delivery.packaging/E5
    def test_project_and_sdk_packages_use_local_docker_and_bounded_jobs(self):
        # Task 3.2 builds project packages too; the SDK-based Flatpak GL package
        # must not force husky contributors back onto the author's Mac mini.
        import rungic_package
        local, _ = self.local()
        for image in (None, 'example/sdk:aarch64'):
            with self.subTest(image=image), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                pkg = {'name': 'rungic-test', 'dir': root / 'packaging/rungic-test',
                       'architecture': 'arm64', 'image': image}
                calls = []

                def run(script, timeout=120, check=True):
                    calls.append(script)
                    if 'build-run.sh' in script:
                        raise RuntimeError('captured build script')
                    return subprocess.CompletedProcess([], 0, '', '')

                def sdk(cmd, timeout, **kwargs):
                    calls.append(cmd)
                    return subprocess.CompletedProcess([], 0, b'exit=0\n', b'')

                with patch.object(build_on_device, 'host', local), \
                        patch.object(build_on_device, 'expire'), patch.object(local, 'run', run), \
                        patch.object(local, 'put_tar'), patch.object(local, 'put'), \
                        patch.object(local, 'ssh', sdk), \
                        patch.object(rungic_package, 'WORKSPACE', root), \
                        patch.object(rungic_package, 'stage_sources', return_value='archive'), \
                        patch.object(rungic_package, 'maintainer_scripts'), \
                        patch.object(rungic_package, 'unit_list', return_value=''), \
                        patch.object(rungic_package, 'git', return_value='1770000000'):
                    # Cache the VM limit before replacing the transport for the SDK.
                    local._jobs = 4
                    with self.assertRaisesRegex(RuntimeError, 'captured build script'):
                        rungic_package.build_device(pkg, None, jobs=20)
                script = next(c for c in calls if 'build-run.sh' in c)
                self.assertIn('JOBS=4 ', script)
                if image:
                    command = next(c for c in calls if 'docker run --rm' in c)
                    self.assertIn('--platform linux/arm64', command)
                    self.assertIn('-e JOBS=4 ', command)
                    self.assertIn('--volumes-from rungic-build', command)


class LocalHost:
    """A build host whose container is this machine: scripts run here with sh, BASE under a temp dir."""
    name, jobs = 'local', 2

    def __init__(self, bin_dir):
        self.env = {**os.environ, 'PATH': f'{bin_dir}:{os.environ["PATH"]}'}
        self.steps = None

    def run(self, script, timeout=120, check=True):
        result = subprocess.run(['bash', '-c', script], capture_output=True, text=True, env=self.env)
        if check and result.returncode:
            raise AssertionError(result.stderr)
        return result

    def out(self, script, timeout=120):
        return self.run(script, timeout).stdout

    def put_tar(self, archive, directory):
        Path(directory).mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive) as tar:
            tar.extractall(directory, filter='tar')

    def background(self, component, steps):
        self.steps = steps


class IncrementalSyncTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        bin_dir = self.root / 'bin'
        bin_dir.mkdir()
        (bin_dir / 'chown').write_text('#!/bin/sh\nexit 0\n')     # the container builds as root; here we are not
        (bin_dir / 'chown').chmod(0o755)
        self.base = self.root / 'rungic-build'
        self.staged = self.root / 'staged'
        for p in (patch.object(build_on_device, 'BASE', str(self.base)),
                  patch.object(build_on_device, 'host', LocalHost(bin_dir)),
                  patch.object(build_on_device, 'stage', self.stage)):
            p.start()
            self.addCleanup(p.stop)

    def stage(self, component):
        """As pq.py source extracts it: every file with a fresh mtime (quilt)."""
        now = time.time()
        for path in self.staged.rglob('*'):
            os.utime(path, (now, now))
        archive = self.root / 'stage.tar'
        with tarfile.open(archive, 'w') as tar:
            tar.add(self.staged / 'src', arcname='src')
            tar.add(self.staged / 'cmake-shims', arcname='cmake-shims')
        return archive

    def write(self, name, text):
        path = self.staged / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    # covers: delivery.build-hosts/E2, apps.gpu/E5
    def test_cached_mesa_build_picks_up_cpu_rendering_options(self):
        # Contributors may already have a KGSL-only build tree. Adding llvmpipe
        # to the source options must reconfigure that tree, or husky still gets no GL.
        work = self.base / 'mesa'
        (work / 'build').mkdir(parents=True)
        (work / 'configure.args').write_text('-Dllvm=disabled\n')
        (work / 'meson-options').write_text((ROOT / 'desktop/mesa-meson-options').read_text())
        meson = self.root / 'bin/meson'
        meson.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > configure.args\n')
        meson.chmod(0o755)
        ninja = self.root / 'bin/ninja'
        ninja.write_text('#!/bin/sh\ngrep -qx -- -Dllvm=enabled configure.args\n')
        ninja.chmod(0o755)
        for cached in (False, True):
            with self.subTest(cached=cached):
                if cached:
                    (work / 'build/build.ninja').write_text('cached KGSL build')
                (work / 'configure.args').write_text('-Dllvm=disabled\n')
                build_on_device.start('mesa', 'targets', 2)
                build_on_device.host.run(build_on_device.host.steps)
                args = (work / 'configure.args').read_text()
                self.assertEqual('--reconfigure' in args, cached)

    # covers: delivery.build-hosts/E2
    def test_unchanged_files_keep_their_time_and_the_obj_tree_stays(self):
        self.write('src/kept.cpp', 'same\n')
        self.write('src/changed.cpp', 'before\n')
        self.write('src/removed.cpp', 'gone soon\n')
        self.write('src/debian/rules', 'rules\n')
        self.write('cmake-shims/x.cmake', '\n')
        build_on_device.sync('demo')
        src = self.base / 'demo/src'
        obj = src / 'obj-aarch64-linux-gnu/kept.o'
        obj.parent.mkdir()
        obj.write_text('object')
        (src / 'debian/.debhelper').mkdir()
        old = time.time() - 3600
        for path in (src / 'kept.cpp', src / 'changed.cpp'):
            os.utime(path, (old, old))

        time.sleep(0.01)
        self.write('src/changed.cpp', 'after\n')
        (self.staged / 'src/removed.cpp').unlink()
        build_on_device.sync('demo')

        self.assertEqual((src / 'kept.cpp').stat().st_mtime, old, 'an unchanged file keeps its time: make skips it')
        self.assertEqual((src / 'changed.cpp').read_text(), 'after\n')
        self.assertFalse((src / 'removed.cpp').exists())
        self.assertEqual(obj.read_text(), 'object', 'the last build output stays')
        self.assertTrue((src / 'debian/.debhelper').is_dir())
        self.assertFalse((self.base / 'demo/incoming').exists())

        build_on_device.start('demo', 'incremental', 2)
        steps = build_on_device.host.steps
        self.assertIn(f'test -d {src}/obj-aarch64-linux-gnu', steps)
        self.assertIn('debian/rules binary', steps)
        self.assertNotIn('dpkg-buildpackage', steps)

    # covers: delivery.build-hosts/E2
    def test_incremental_build_compiles_make_and_ninja_trees_before_packaging(self):
        for generator in ('make', 'ninja'):
            with self.subTest(generator=generator):
                src = self.base / generator / 'src'
                obj = src / 'obj-aarch64-linux-gnu'
                obj.mkdir(parents=True)
                (src / 'input').write_text('updated source\n')
                (src / 'debian').mkdir()
                rules = src / 'debian/rules'
                rules.write_text('#!/bin/sh\nset -eu\n'
                                 'test "$1" = binary\n'
                                 'cmp input obj-aarch64-linux-gnu/output\n'
                                 'cp obj-aarch64-linux-gnu/output packaged\n')
                rules.chmod(0o755)
                if generator == 'make':
                    (obj / 'Makefile').write_text('output: ../input\n\tcp ../input output\n')
                else:
                    (obj / 'build.ninja').write_text('rule copy\n  command = cp $in $out\n'
                                                   'build output: copy ../input\n')
                build_on_device.start(generator, 'incremental', 2)
                build_on_device.host.run(build_on_device.host.steps)
                self.assertEqual((src / 'packaged').read_text(), 'updated source\n')
                # Reusing the same tree must compile a subsequent source change too.
                # macOS make 3.81 compares whole seconds; give the fixture an
                # older output rather than relying on a 10 ms scheduling delay.
                old = time.time() - 2
                os.utime(obj / 'output', (old, old))
                (src / 'input').write_text('second revision\n')
                build_on_device.host.run(build_on_device.host.steps)
                self.assertEqual((src / 'packaged').read_text(), 'second revision\n')


class TransferTests(unittest.TestCase):
    """tools/pq/rungic-transfer: the phone's key runs only this on the build host."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.base = self.root / 'rungic-build'
        self.base.mkdir()
        (self.root / 'secret').write_text('not for the phone\n')
        # The real script, its fixed base moved under the temp directory.
        text = (ROOT / 'tools/pq/rungic-transfer').read_text()
        self.assertIn('\nBASE=/root/rungic-build\n', text)
        self.script = self.root / 'rungic-transfer'
        self.script.write_text(text.replace('\nBASE=/root/rungic-build\n', f'\nBASE={self.base}\n'))

    # covers: delivery.build-hosts/E4
    def test_pruning_preserves_exact_kept_names_with_shell_metacharacters(self):
        pool = self.base / build_on_device.MacMini.DEV_POOL
        pool.mkdir()
        keep = {'libegl-mesa0_26.3~devel_arm64.deb', 'with space.deb', 'literal$(touch CANARY).deb'}
        for name in {*keep, 'obsolete.deb', 'libegl-mesa0_26.2~devel_arm64.deb'}:
            (pool / name).write_text('package')
        mac = FakeMac()
        def run(script, timeout):
            return subprocess.run(['sh', '-eu', '-c', script], check=True,
                                  capture_output=True, text=True, cwd=pool)
        with patch.object(build_on_device, 'BASE', str(self.base)), patch.object(mac, 'run', run):
            mac.prune_kept(keep)
            self.assertEqual({p.name for p in pool.iterdir()}, keep)
            mac.prune_kept(set())
            self.assertEqual(list(pool.iterdir()), [])

    def transfer(self, command, data=b''):
        env = {'PATH': os.environ['PATH'], 'SSH_ORIGINAL_COMMAND': command}
        return subprocess.run(['sh', str(self.script)], input=data, capture_output=True, env=env, cwd=self.root)

    # covers: delivery.build-hosts/E4
    def test_put_and_get_relative_paths_under_the_build_directory(self):
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode='w') as tar:
            info = tarfile.TarInfo('pkg_1_arm64.deb')
            info.size = 4
            tar.addfile(info, io.BytesIO(b'data'))
        put = self.transfer('put dev-pool/in', archive.getvalue())
        self.assertEqual(put.returncode, 0, put.stderr)
        self.assertEqual((self.base / 'dev-pool/in/pkg_1_arm64.deb').read_bytes(), b'data')
        got = self.transfer('get dev-pool/in/pkg_1_arm64.deb')
        self.assertEqual((got.returncode, got.stdout), (0, b'data'))

    # covers: delivery.build-hosts/E4
    def test_everything_else_is_refused(self):
        (self.base / 'f').write_text('x')
        for command in ('get /etc/passwd', f'get {self.root}/secret', 'get ../secret', 'get a/../../secret',
                        'get ..', 'put ../escape', 'put /tmp/x', 'rm -rf f', 'sh -c id', 'get', 'get f extra',
                        '', 'scp -t f', 'get *'):
            with self.subTest(command=command):
                result = self.transfer(command)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn(b'not for the phone', result.stdout)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['rungic-build', 'rungic-transfer', 'secret'])
        self.assertEqual((self.base / 'f').read_text(), 'x')


if __name__ == '__main__':
    unittest.main()
