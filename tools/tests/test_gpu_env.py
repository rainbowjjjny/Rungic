# SPDX-License-Identifier: MIT
"""The session's GPU environment (desktop/gpu-env, /etc/plasma/gpu-env), sourced by the session, KWin
and the workspaces: hardware Mesa on KGSL, Qt Quick and GTK on GL (not Vulkan: whole grey frames,
docs/56), and no stale software overrides on Adreno. Non-Qualcomm phones such as Pixel 8 Pro
(husky/Mali) keep the software defaults without inherited KGSL overrides. What the GPU then draws,
and whether a frame flashes grey, is the phone's to show (docs/51, docs/56)."""
import shlex
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GPU_ENV = ROOT / 'desktop/gpu-env'


def session(env, tmp_path, kgsl=True, flatpak=True):
    """The environment after the file is sourced, as desktop/session does."""
    # Substitute paths, not the conditional: exercise the shell's real -c test
    # without needing an Adreno device (or root to mknod) on the development host.
    gpu = '/dev/null' if kgsl else str(tmp_path / 'missing-kgsl')
    extension = tmp_path / 'flatpak-gl'
    if flatpak:
        extension.mkdir(exist_ok=True)
    script = tmp_path / 'gpu-env'
    script.write_text(GPU_ENV.read_text().replace('/dev/kgsl-3d0', gpu).replace(
        '/var/lib/flatpak/extension/org.freedesktop.Platform.GL.rungic', str(extension)))
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
def test_husky_keeps_session_software_defaults_and_mesa_can_choose_llvmpipe(tmp_path):
    # Pixel 8 Pro's Mali has no KGSL. A stale override must not force Mesa to
    # open an unavailable Adreno driver, or replace the working QPainter defaults.
    defaults = (ROOT / 'desktop/session').read_text().split('export RUNGIC_ANDROID_DISPLAY=')[0]
    script = tmp_path / 'defaults'
    script.write_text(defaults[defaults.index('export QT_QUICK_BACKEND='):])
    env = subprocess.run(['sh', '-c', f'. {shlex.quote(str(script))}; env -0'],
                         env={'PATH': '/usr/bin:/bin'}, capture_output=True, check=True).stdout
    defaults = dict(line.split('=', 1) for line in env.decode().split('\0') if '=' in line)
    stale = {'MESA_LOADER_DRIVER_OVERRIDE': 'kgsl', 'FD_KGSL_ENABLE_DMABUF': '1',
             'FD_KGSL_DMABUF_UBWC': '1', 'VK_DRIVER_FILES': 'freedreno.json',
             'QSG_RHI_BACKEND': 'opengl', 'GSK_RENDERER': 'gl', 'FLATPAK_GL_DRIVERS': 'rungic'}
    for inherited in ({}, stale):
        env = session({**defaults, **inherited}, tmp_path, kgsl=False)
        assert env['KWIN_COMPOSE'] == 'Q' and env['QT_QUICK_BACKEND'] == 'software'
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
        if kgsl:
            assert unsets == []
        else:
            assert len(unsets) == 1
            assert {'MESA_LOADER_DRIVER_OVERRIDE', 'FD_KGSL_ENABLE_DMABUF', 'FD_KGSL_DMABUF_UBWC',
                    'VK_DRIVER_FILES', 'QSG_RHI_BACKEND', 'GSK_RENDERER', 'FLATPAK_GL_DRIVERS'} <= unsets[0]
