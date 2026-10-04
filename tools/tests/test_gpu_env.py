# SPDX-License-Identifier: MIT
"""The session's GPU environment (desktop/gpu-env, /etc/plasma/gpu-env), sourced by the session, KWin
and the workspaces: hardware Mesa on KGSL, Qt Quick and GTK on GL (not Vulkan: whole grey frames,
docs/56), and no stale software overrides on Adreno. Non-Qualcomm phones such as Pixel 8 Pro
(husky/Mali) keep KWin on QPainter but draw Qt Quick with GL on proven virgl or Mesa's llvmpipe, without
inherited KGSL overrides. What the GPU then draws,
and whether a frame flashes grey, is the phone's to show (docs/51, docs/56)."""
import shlex
import importlib.util
import json
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
GPU_ENV = ROOT / 'desktop/gpu-env'


def session(env, tmp_path, kgsl=True, flatpak=True, probe=None):
    """The environment after the file is sourced, as desktop/session does."""
    # Substitute paths, not the conditional: exercise the shell's real -c test
    # without needing an Adreno device (or root to mknod) on the development host.
    gpu = '/dev/null' if kgsl else str(tmp_path / 'missing-kgsl')
    extension = tmp_path / 'flatpak-gl'
    if flatpak:
        extension.mkdir(exist_ok=True)
    script = tmp_path / 'gpu-env'
    script.write_text(GPU_ENV.read_text().replace('/dev/kgsl-3d0', gpu).replace(
        '/var/lib/flatpak/extension/org.freedesktop.Platform.GL.rungic', str(extension)).replace(
        '/usr/libexec/rungic-virgl-probe', str(probe or tmp_path / 'no-probe')))
    out = subprocess.run(['sh', '-c', f'. {shlex.quote(str(script))} && env -0'],
                         env=env, capture_output=True, check=True).stdout
    return dict(line.split('=', 1) for line in out.decode().split('\0') if '=' in line)


# covers: apps.gpu/E2
def test_qt_quick_and_gtk_draw_with_gl_on_kgsl(tmp_path):
    env = session({'PATH': '/usr/bin:/bin', 'LIBGL_ALWAYS_SOFTWARE': '1', 'QT_QUICK_BACKEND': 'software',
                   'LD_LIBRARY_PATH': '/opt/old-mesa/lib'}, tmp_path)
    assert env['QSG_RHI_BACKEND'] == 'opengl' and env['GSK_RENDERER'] == 'gl'
    assert env['MESA_LOADER_DRIVER_OVERRIDE'] == 'kgsl' and env['FD_KGSL_ENABLE_DMABUF'] == '1'
    assert env['KWIN_COMPOSE'] == 'O2ES'
    assert env['VK_DRIVER_FILES'] == '/usr/share/vulkan/icd.d/freedreno_icd.aarch64.json'
    assert env['FLATPAK_GL_DRIVERS'] == 'rungic'
    # Left over from a software-rendering session: gone, or Qt Quick and Mesa would render on the CPU.
    assert 'LIBGL_ALWAYS_SOFTWARE' not in env and 'LD_LIBRARY_PATH' not in env and env['QT_QUICK_BACKEND'] == ''


# covers: apps.gpu/E2
def test_husky_keeps_qpainter_kwin_and_draws_qt_quick_with_llvmpipe(tmp_path):
    # Pixel 8 Pro's Mali has no KGSL. A stale override must not force Mesa to open an
    # unavailable Adreno driver. KWin stays on QPainter (its shm buffers reach the app), but
    # Qt Quick must use GL: its software scene graph left the dock and app drawer blank on
    # husky (docs/73), and Mesa's llvmpipe draws them.
    defaults = (ROOT / 'desktop/session').read_text().split('export RUNGIC_ANDROID_DISPLAY=')[0]
    script = tmp_path / 'defaults'
    script.write_text(defaults[defaults.index('export QT_QUICK_BACKEND='):])
    env = subprocess.run(['sh', '-c', f'. {shlex.quote(str(script))}; env -0'],
                         env={'PATH': '/usr/bin:/bin'}, capture_output=True, check=True).stdout
    defaults = dict(line.split('=', 1) for line in env.decode().split('\0') if '=' in line)
    stale = {'MESA_LOADER_DRIVER_OVERRIDE': 'kgsl', 'FD_KGSL_ENABLE_DMABUF': '1',
             'FD_KGSL_DMABUF_UBWC': '1', 'VK_DRIVER_FILES': 'freedreno.json',
             'GSK_RENDERER': 'gl', 'FLATPAK_GL_DRIVERS': 'rungic'}
    for inherited in ({}, stale):
        env = session({**defaults, **inherited}, tmp_path, kgsl=False)
        assert env['KWIN_COMPOSE'] == 'Q'
        assert env['QT_QUICK_BACKEND'] == '' and env['QSG_RHI_BACKEND'] == 'opengl'
        assert 'LIBGL_ALWAYS_SOFTWARE' not in env
        for key in stale:
            assert key not in env, f'{key} must not select the Adreno path on husky'


# covers: apps.gpu/E2
def test_kgsl_without_flatpak_extension_does_not_force_it(tmp_path):
    env = session({'PATH': '/usr/bin:/bin'}, tmp_path, flatpak=False)
    assert env['MESA_LOADER_DRIVER_OVERRIDE'] == 'kgsl'
    assert 'FLATPAK_GL_DRIVERS' not in env


# covers: apps.gpu/E2
def test_kwin_ubwc_only_on_qualcomm(tmp_path):
    # UBWC is an Adreno modifier; software output on husky uses shared memory.
    # Execute the launcher with a stand-in wrapper so we inspect its actual env.
    wrapper = tmp_path / 'wrapper'
    wrapper.write_text('#!/bin/sh\nenv -0\n')
    wrapper.chmod(0o755)
    for kgsl in (True, False):
        script = tmp_path / 'kwin'
        script.write_text((ROOT / 'desktop/kwin').read_text().replace(
            '/dev/kgsl-3d0', '/dev/null' if kgsl else str(tmp_path / 'missing')).replace(
            '/etc/plasma/gpu-env', str(tmp_path / 'no-gpu-env')).replace(
            '/mnt/android-wayland/android-display.ini', str(tmp_path / 'no-display')).replace(
            '/usr/bin/kwin_wayland_wrapper', shlex.quote(str(wrapper))))
        out = subprocess.run(['sh', str(script)], env={'PATH': '/usr/bin:/bin', 'FD_KGSL_DMABUF_UBWC': '1'},
                             capture_output=True, check=True).stdout
        env = dict(line.split('=', 1) for line in out.decode().split('\0') if '=' in line)
        if kgsl:
            assert env['FD_KGSL_DMABUF_UBWC'] == '1'
        else:
            assert 'FD_KGSL_DMABUF_UBWC' not in env


# covers: apps.gpu/E2 desktop.session/E4
def test_session_removes_stale_adreno_settings_from_user_manager_on_husky(tmp_path):
    # import-environment ignores unset shell variables: an old user manager can
    # otherwise keep forcing KGSL on husky despite gpu-env clearing its own env.
    source = (ROOT / 'desktop/session').read_text()
    source = source[source.index('# The GPU settings into the user manager'):source.index('# Qt picks the platform theme')]
    systemctl = tmp_path / 'systemctl'
    systemctl.write_text('#!/bin/sh\necho "$*"\n')
    systemctl.chmod(0o755)
    for kgsl in (True, False):
        script = tmp_path / 'import'
        script.write_text(source.replace('/dev/kgsl-3d0', '/dev/null' if kgsl else str(tmp_path / 'missing')))
        done = subprocess.run(['sh', '-eu', str(script)], capture_output=True, text=True, check=True,
                              env={'PATH': f'{tmp_path}:/usr/bin:/bin', 'RUNGIC_LOGIN_VARS': ''})
        commands = [line.split()[1:] for line in done.stdout.splitlines()]
        assert commands[-1][0] == 'import-environment'
        unsets = [set(c[1:]) for c in commands if c[0] == 'unset-environment']
        assert {'GALLIUM_DRIVER', 'VTEST_SOCKET_NAME'} in unsets
        unsets.remove({'GALLIUM_DRIVER', 'VTEST_SOCKET_NAME'})
        if kgsl:
            assert unsets == []
        else:
            assert len(unsets) == 1
            assert {'MESA_LOADER_DRIVER_OVERRIDE', 'FD_KGSL_ENABLE_DMABUF', 'FD_KGSL_DMABUF_UBWC',
                    'VK_DRIVER_FILES', 'QSG_RHI_BACKEND', 'GSK_RENDERER', 'FLATPAK_GL_DRIVERS'} <= unsets[0]


@pytest.fixture
def virgl(tmp_path):
    """Real timeout/state, fake EGL. Restricted macOS cannot bind a Unix socket:
    substitute a directory predicate there; missing/regular nodes still fail."""
    with tempfile.TemporaryDirectory(prefix='b3-') as folder, socket.socket(socket.AF_UNIX) as sock:
        shared = Path(folder)
        socket_predicate = '-S "$socket"'
        try:
            sock.bind(str(shared / 'vtest.sock'))
        except PermissionError:
            (shared / 'vtest.sock').mkdir()
            socket_predicate = '-d "$socket"'
        state = shared / 'vtest.state'
        state.write_text('running\n')
        eglinfo = tmp_path / 'eglinfo'
        eglinfo.write_text('#!/bin/sh\n'
                           'test "$EGL_PLATFORM" = surfaceless && test "$GALLIUM_DRIVER" = virpipe || exit 9\n'
                           'test "$VTEST_SOCKET_NAME" = "' + str(shared / 'vtest.sock') + '" || exit 9\n'
                           # Real eglinfo -B also tries GBM/Wayland/X11 and exits 3 when they fail,
                           # as on husky outside a session, even after printing the virgl renderer.
                           'test "$*" = "-B -p surfaceless" || { echo "OpenGL ES profile renderer: virgl (Mali-G715)"; exit 3; }\n'
                           'test -z "${LIBGL_ALWAYS_SOFTWARE:-}${MESA_LOADER_DRIVER_OVERRIDE:-}" || exit 9\n'
                           'echo "OpenGL ES profile renderer: virgl (Mali-G715)"\n')
        eglinfo.chmod(0o755)
        probe = tmp_path / 'probe'
        probe.write_text((ROOT / 'desktop/virgl-probe').read_text().replace(
            '/mnt/android-wayland', str(shared)).replace('-S "$socket"', socket_predicate))
        probe.chmod(0o755)
        env = {'PATH': f'{tmp_path}:{Path(shutil.which("timeout")).parent}:/usr/bin:/bin'}
        yield probe, state, shared / 'vtest.sock', eglinfo, env


# covers: apps.gpu/E8
@pytest.mark.parametrize('failure', ['none', 'no-newline', 'failed', 'missing-state', 'missing-socket',
                                     'regular-socket', 'wrong-renderer', 'exit-failure', 'timeout',
                                     'extra-newline', 'extra-line', 'whitespace', 'empty', 'unreadable', 'nul'])
def test_husky_virgl_requires_live_egl_and_falls_back_without_stale_overrides(tmp_path, virgl, failure):
    # Mali GPU access stays in Android's init PID namespace. Mere socket/state presence
    # cannot prove virgl works; a dead server must leave the llvmpipe Dock/drawer usable.
    probe, state, sock, eglinfo, base_env = virgl
    if failure == 'failed':
        state.write_text('failed\n')
    elif failure in ('no-newline', 'extra-newline', 'extra-line', 'whitespace', 'empty', 'nul'):
        state.write_text({'no-newline': 'running', 'extra-newline': 'running\n\n',
                          'extra-line': 'running\nfailed\n', 'whitespace': 'running \n', 'empty': '',
                          'nul': 'running\0'}[failure])
    elif failure == 'unreadable':
        # Simulate a failed read even when this test runs as root. Merely seeing
        # a regular state file must not turn an access error into a running server.
        cmp = tmp_path / 'cmp'
        cmp.write_text('#!/bin/sh\nexit 2\n')
        cmp.chmod(0o755)
    elif failure == 'missing-state':
        state.unlink()
    elif failure in ('missing-socket', 'regular-socket'):
        sock.rmdir() if sock.is_dir() else sock.unlink()
        if failure == 'regular-socket':
            sock.touch()
    elif failure == 'wrong-renderer':
        eglinfo.write_text('#!/bin/sh\necho "EGL driver name: virgl"\necho "OpenGL renderer: llvmpipe"\n')
    elif failure == 'exit-failure':
        eglinfo.write_text('#!/bin/sh\necho "OpenGL renderer: virgl (Mali-G715)"\nexit 1\n')
    elif failure == 'timeout':
        eglinfo.write_text('#!/bin/sh\ntrap "" TERM\nsleep 10\n')
    inherited = {**base_env, 'GALLIUM_DRIVER': 'virpipe', 'VTEST_SOCKET_NAME': '/old/dead.sock',
                 'KWIN_COMPOSE': 'O2ES', 'MESA_LOADER_DRIVER_OVERRIDE': 'kgsl',
                 'LIBGL_ALWAYS_SOFTWARE': '1'}
    start = time.monotonic()
    done = subprocess.run([str(probe)], env=inherited, timeout=5)
    env = session(inherited, tmp_path, kgsl=False, probe=probe)
    assert time.monotonic() - start < 8, 'Two probes must finish within their 2s + 1s kill bounds'
    assert env['KWIN_COMPOSE'] == 'Q' and env['QSG_RHI_BACKEND'] == 'opengl'
    if failure in ('none', 'no-newline'):
        assert done.returncode == 0
        assert env['GALLIUM_DRIVER'] == 'virpipe'
        assert env['VTEST_SOCKET_NAME'] == '/mnt/android-wayland/vtest.sock'
    else:
        assert done.returncode != 0
        assert 'GALLIUM_DRIVER' not in env and 'VTEST_SOCKET_NAME' not in env


# covers: apps.gpu/E2 apps.gpu/E8
def test_qualcomm_never_probes_or_inherits_virpipe(tmp_path):
    # The new husky path must not intercept Qualcomm's working freedreno/KGSL path.
    probe = tmp_path / 'probe'
    attempted = tmp_path / 'probe-attempted'
    probe.write_text(f'#!/bin/sh\ntouch {shlex.quote(str(attempted))}\nexit 99\n')
    probe.chmod(0o755)
    env = session({'PATH': '/usr/bin:/bin', 'GALLIUM_DRIVER': 'virpipe',
                   'VTEST_SOCKET_NAME': '/dead.sock'}, tmp_path, probe=probe)
    assert env['MESA_LOADER_DRIVER_OVERRIDE'] == 'kgsl' and env['KWIN_COMPOSE'] == 'O2ES'
    assert 'GALLIUM_DRIVER' not in env and 'VTEST_SOCKET_NAME' not in env
    assert not attempted.exists(), 'Qualcomm must not depend on the husky vtest server'


# covers: apps.gpu/E8 install.login-environment/E2
def test_login_profile_cannot_restore_dead_virpipe_after_capability_selection(tmp_path):
    # session reads the login profile after gpu-env; old user exports must not
    # undo a failed probe and send the shell back into M5's crash loop.
    spec = importlib.util.spec_from_file_location('gpu_login', ROOT / 'desktop/login-environment.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    (tmp_path / '.profile').write_text('export GALLIUM_DRIVER=virpipe\nexport VTEST_SOCKET_NAME=/dead.sock\n')
    env = module.login_environment({'HOME': str(tmp_path), 'PATH': '/usr/bin:/bin'}, '/bin/sh')
    assert 'GALLIUM_DRIVER' not in env and 'VTEST_SOCKET_NAME' not in env


# covers: apps.gpu/E8
def test_missing_probe_keeps_llvmpipe_and_clears_inherited_virpipe(tmp_path):
    # Older/partial installations must still offer the CPU GL desktop on husky.
    env = session({'PATH': '/usr/bin:/bin', 'GALLIUM_DRIVER': 'virpipe',
                   'VTEST_SOCKET_NAME': '/dead.sock'}, tmp_path, kgsl=False)
    assert env['KWIN_COMPOSE'] == 'Q' and env['QSG_RHI_BACKEND'] == 'opengl'
    assert 'GALLIUM_DRIVER' not in env and 'VTEST_SOCKET_NAME' not in env


# covers: apps.gpu/E8 desktop.session/E4
def test_session_imports_virgl_and_clears_manager_before_failed_probe(tmp_path):
    source = (ROOT / 'desktop/session').read_text()
    source = source[source.index('# The GPU settings into the user manager'):source.index('# Qt picks the platform theme')]
    systemctl = tmp_path / 'systemctl'
    systemctl.write_text('#!/bin/sh\necho "$*"\n')
    systemctl.chmod(0o755)
    script = tmp_path / 'import'
    script.write_text(source.replace('/dev/kgsl-3d0', str(tmp_path / 'missing')))
    for working in (True, False):
        done = subprocess.run(['sh', '-eu', str(script)], capture_output=True, text=True, check=True,
                              env={'PATH': f'{tmp_path}:/usr/bin:/bin', 'RUNGIC_LOGIN_VARS': '',
                                   **({'GALLIUM_DRIVER': 'virpipe', 'VTEST_SOCKET_NAME': '/live.sock'} if working else {})})
        commands = [line.split()[1:] for line in done.stdout.splitlines()]
        assert {'GALLIUM_DRIVER', 'VTEST_SOCKET_NAME'} <= set(commands[-1][1:])
        assert any(c[0] == 'unset-environment' and {'GALLIUM_DRIVER', 'VTEST_SOCKET_NAME'} <= set(c[1:])
                   for c in commands[:-1])


# covers: apps.gpu/E8
def test_kwin_never_uses_virpipe_even_with_working_probe(tmp_path, virgl):
    # KWin has no virgl buffer integration: husky uses QPainter/SHM, Adreno keeps KGSL.
    probe, _, _, _, base_env = virgl
    wrapper = tmp_path / 'wrapper'
    wrapper.write_text('#!/bin/sh\nenv -0\n')
    wrapper.chmod(0o755)
    for kgsl in (False, True):
        session(base_env, tmp_path, kgsl=kgsl, probe=probe)
        script = tmp_path / 'kwin'
        script.write_text((ROOT / 'desktop/kwin').read_text().replace(
            '/dev/kgsl-3d0', '/dev/null' if kgsl else str(tmp_path / 'missing')).replace(
            '/etc/plasma/gpu-env', str(tmp_path / 'gpu-env')).replace(
            '/mnt/android-wayland/android-display.ini', str(tmp_path / 'no-display')).replace(
            '/usr/bin/kwin_wayland_wrapper', shlex.quote(str(wrapper))))
        done = subprocess.run(['sh', str(script)], env={**base_env, 'GALLIUM_DRIVER': 'virpipe',
                              'VTEST_SOCKET_NAME': '/stale.sock'}, capture_output=True, check=True)
        env = dict(line.split('=', 1) for line in done.stdout.decode().split('\0') if '=' in line)
        assert 'GALLIUM_DRIVER' not in env and 'VTEST_SOCKET_NAME' not in env
        assert env['KWIN_COMPOSE'] == ('O2ES' if kgsl else 'Q')


# covers: apps.gpu/E8 desktop.session/E6
def test_plasmashell_reprobes_each_restart_and_rewrites_manager(tmp_path, virgl):
    # M5's dead-server prototype lost the shell after 3 crashes in 60 s. Each retry
    # needs a fresh capability decision so husky recovers on llvmpipe and can use virgl again.
    probe, state, _, _, base_env = virgl
    session(base_env, tmp_path, kgsl=False, probe=probe)
    manager = tmp_path / 'manager.json'
    systemctl = tmp_path / 'systemctl'
    systemctl.write_text('#!' + shutil.which('python3') + '\n' +
                        'import json, os, sys\nfrom pathlib import Path\n' +
                        f'p = Path({str(manager)!r})\n' +
                        'env = json.loads(p.read_text())\ncmd, *args = sys.argv[2:]\n'
                        'if cmd == "unset-environment":\n'
                        '    for key in args: env.pop(key, None)\n'
                        'elif cmd == "import-environment":\n'
                        '    for key in args:\n'
                        '        if key in os.environ: env[key] = os.environ[key]\n'
                        'else: sys.exit(2)\np.write_text(json.dumps(env))\n')
    systemctl.chmod(0o755)
    refresh = tmp_path / 'refresh'
    refresh.write_text((ROOT / 'desktop/plasmashell-gpu-refresh').read_text().replace(
        '/etc/plasma/gpu-env', str(tmp_path / 'gpu-env')))
    manager.write_text(json.dumps({**base_env, 'GALLIUM_DRIVER': 'virpipe', 'VTEST_SOCKET_NAME': '/stale.sock',
                                  'PLASMA_DEFAULT_SHELL': 'org.kde.plasma.mobileshell'}))
    for status in ('running', 'failed', 'running', 'failed'):
        state.write_text(status + '\n')
        subprocess.run(['sh', '-eu', str(refresh)], env=json.loads(manager.read_text()), check=True)
        env = json.loads(manager.read_text())
        assert env['PLASMA_DEFAULT_SHELL'] == 'org.kde.plasma.mobileshell'
        assert env['KWIN_COMPOSE'] == 'Q' and env['QSG_RHI_BACKEND'] == 'opengl'
        if status == 'running':
            assert env['GALLIUM_DRIVER'] == 'virpipe' and env['VTEST_SOCKET_NAME'].endswith('/vtest.sock')
        else:
            assert 'GALLIUM_DRIVER' not in env and 'VTEST_SOCKET_NAME' not in env


# covers: desktop.session/E6 apps.gpu/E8
def test_probe_and_recovery_are_shipped_and_retries_are_paced():
    # Keep upstream's Type=dbus, bus name, --no-respawn and on-failure lifecycle.
    # Disable the 3/60s latch, but pace retries to avoid a busy crash loop.
    dropin = ROOT / 'desktop/plasmashell-gpu.conf'
    source = dropin.read_text()
    assert 'StartLimitIntervalSec=0' in source and 'RestartSec=5s' in source
    assert 'ExecStartPre=/usr/libexec/rungic-plasmashell-gpu-refresh' in source
    assert 'ExecStart=' not in source
    package = json.loads((ROOT / 'packaging/rungic-plasma-config/package.json').read_text())
    build = (ROOT / 'packaging/rungic-plasma-config/build.sh').read_text()
    for path in ('desktop/virgl-probe', 'desktop/plasmashell-gpu-refresh', 'desktop/plasmashell-gpu.conf'):
        assert path in package['paths'] and path in build
    assert 'mesa-utils' in package['depends'] and 'coreutils' in package['depends']
    assert 'diffutils' in package['depends']
    assert '/usr/lib/systemd/user/plasma-plasmashell.service.d/' in build


# covers: apps.gpu/E8
def test_probe_rejects_regular_file_with_real_socket_predicate(tmp_path):
    # Even in a sandbox without bind permission, exercise the production -S gate:
    # a leftover regular file named vtest.sock is not an Android vtest service.
    (tmp_path / 'vtest.state').write_text('running\n')
    (tmp_path / 'vtest.sock').touch()
    probe = tmp_path / 'probe'
    probe.write_text((ROOT / 'desktop/virgl-probe').read_text().replace('/mnt/android-wayland', str(tmp_path)))
    done = subprocess.run(['sh', str(probe)], env={'PATH': '/usr/bin:/bin'}, timeout=2)
    assert done.returncode != 0


# covers: apps.gpu/E8 desktop.session/E6
def test_config_package_installs_executable_probe_and_recovery(tmp_path):
    # A source-only probe would silently leave every Mali session on llvmpipe.
    # Run the actual package recipe with GNU install, also on macOS.
    install = shutil.which('ginstall') or shutil.which('install')
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    (bin_dir / 'install').symlink_to(install)
    dest = tmp_path / 'dest'
    dest.mkdir()
    subprocess.run(['sh', str(ROOT / 'packaging/rungic-plasma-config/build.sh')], check=True,
                   env={'SRC': str(ROOT), 'DESTDIR': str(dest), 'PATH': f'{bin_dir}:/usr/bin:/bin'})
    for source, target, mode in (
        ('desktop/virgl-probe', 'usr/libexec/rungic-virgl-probe', 0o755),
        ('desktop/plasmashell-gpu-refresh', 'usr/libexec/rungic-plasmashell-gpu-refresh', 0o755),
        ('desktop/plasmashell-gpu.conf', 'usr/lib/systemd/user/plasma-plasmashell.service.d/rungic-gpu.conf', 0o644),
    ):
        assert (dest / target).read_bytes() == (ROOT / source).read_bytes()
        assert (dest / target).stat().st_mode & 0o777 == mode
