# SPDX-License-Identifier: MIT
"""One Mesa runtime must serve husky (Mali via virgl, CPU GL) and Qualcomm (KGSL).

These are packaging checks with a synthetic install tree, not renderer acceptance.
"""
import os
from pathlib import Path
import re
import runpy
import shlex
import subprocess
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


# covers: apps.gpu/E5
def test_husky_mali_via_vtest_keeps_qualcomm_and_cpu_fallbacks():
    # Mali stays in Android's init PID namespace: virpipe speaks vtest rather
    # than opening /dev/mali0 in LXC (kbase would panic). The same Mesa must still
    # serve Qualcomm KGSL and keep llvmpipe when the Android server is unavailable.
    opts = options()
    assert {'virgl', 'freedreno', 'zink', 'softpipe', 'llvmpipe'} <= set(
        opts['gallium-drivers'].split(','))
    assert opts['freedreno-kmds'] == 'kgsl'
    assert opts['vulkan-drivers'] == 'freedreno'
    assert opts['llvm'] == 'enabled'


# covers: apps.gpu/E5
def test_virpipe_safety_patches_are_in_the_build_queue():
    # Qt's threaded contexts share a socket: mixed requests crash fence waits.
    # A missing vtest server must fail screen creation before any protocol I/O;
    # this lets the caller handle failure instead of aborting plasmashell later.
    patches = ROOT / 'packages/mesa/debian/patches'
    series = [line.strip() for line in (patches / 'series').read_text().splitlines()
              if line.strip() and not line.startswith('#')]
    required = ['rungic/vtest-socket-transaction-lock.patch',
                'rungic/vtest-connect-failure.patch']
    for name in required:
        assert name in series
        assert (patches / name).is_file()
    assert series.index(required[0]) < series.index(required[1])


# covers: apps.gpu/E5
def test_virpipe_changes_get_a_new_package_revision():
    # +rungic5 did not contain virgl or the vtest fixes. Publishing at that same
    # version would let APT keep the old binary on husky instead of upgrading it.
    first = (ROOT / 'packages/mesa/debian/changelog').read_text().splitlines()[0]
    assert first == 'mesa (26.3.0~devel20260824+rungic6) resolute; urgency=medium'


def options():
    return dict(arg[2:].split('=', 1) for arg in shlex.split(
        (ROOT / 'desktop/mesa-meson-options').read_text()) if arg.startswith('-D'))


# covers: apps.gpu/E5
def test_husky_needs_cpu_gl_even_without_a_usable_mesa_gpu():
    # Qt Quick's software scene graph left the drawer blank (docs/73); CPU GL
    # must be included in the same rootfs, not supplied by a phone-specific fork.
    assert {'softpipe', 'llvmpipe'} <= set(options()['gallium-drivers'].split(','))
    assert options()['llvm'] == 'enabled'


# covers: apps.gpu/E1
def test_adding_husky_does_not_remove_qualcomm_adreno_backends():
    opts = options()
    assert {'freedreno', 'zink'} <= set(opts['gallium-drivers'].split(','))
    assert opts['freedreno-kmds'] == 'kgsl'
    assert opts['vulkan-drivers'] == 'freedreno'
    assert opts['glvnd'] == 'enabled'


# covers: apps.gpu/E5
def test_cpu_gl_change_has_a_new_package_version():
    # A repository must not replace the old KGSL-only binaries at the same version.
    first = (ROOT / 'packages/mesa/debian/changelog').read_text().splitlines()[0]
    revision = re.match(r'mesa \(26\.3\.0~devel20260824\+rungic(\d+)\)', first)
    assert revision and int(revision[1]) > 3


# covers: apps.gpu/E5
def test_packaged_cpu_gl_keeps_drivers_glvnd_and_llvm_runtime(tmp_path):
    stage, output, source = (tmp_path / n for n in ('stage', 'debs', 'source'))
    lib = stage / 'usr/lib/aarch64-linux-gnu'
    files = ['libgallium-26.3.0.so', 'libEGL_mesa.so.0', 'libGLX_mesa.so.0',
             'libgbm.so.1', 'pkgconfig/gbm.pc', 'libvulkan_freedreno.so']
    for name in files:
        path = lib / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('synthetic runtime')
    (lib / 'libgbm.so').symlink_to('libgbm.so.1')
    (lib / 'dri').mkdir()
    for driver in ('swrast', 'kgsl', 'zink', 'virtio_gpu'):
        (lib / 'dri' / f'{driver}_dri.so').symlink_to('../libgallium-26.3.0.so')
    (source / 'docs').mkdir(parents=True)
    (source / 'docs/license.rst').write_text('synthetic MIT license')
    env = {'RUNGIC_MESA_STAGE': str(stage), 'RUNGIC_MESA_PACKAGES': str(output),
           'RUNGIC_MESA_SOURCE': str(source), 'RUNGIC_MESA_VERSION': 'test-version'}
    with patch.dict(os.environ, env), patch('subprocess.run') as commands:
        runpy.run_path(str(ROOT / 'desktop/package-mesa.py'), run_name='__main__')
    control = (output / 'mesa-libgallium/DEBIAN/control').read_text()
    # Ubuntu resolute mesa-libgallium uses this runtime, not llvm-21-dev.
    assert 'libllvm21 (>= 1:21.1.0)' in control
    assert 'llvm-21-dev' not in control
    for driver in ('swrast', 'kgsl', 'zink', 'virtio_gpu'):
        path = output / 'libgl1-mesa-dri/usr/lib/aarch64-linux-gnu/dri' / f'{driver}_dri.so'
        assert path.is_symlink()
        assert path.readlink() == Path('../libgallium-26.3.0.so')
    dri = (output / 'libgl1-mesa-dri/DEBIAN/control').read_text()
    assert 'mesa-libgallium (= test-version)' in dri
    assert 'libgbm1 (= test-version)' in dri
    # GLVND's dispatchers remain Ubuntu-owned; only Mesa vendor libraries ship.
    assert not list(output.rglob('libEGL.so*'))
    assert not list(output.rglob('libGLX.so*'))
    assert len(commands.call_args_list) == 7
    subprocess.run(['sh', '-n', str(output / 'mesa-libgallium/DEBIAN/preinst')], check=True)


# covers: apps.gpu/E5
def test_flatpak_gl_extension_builds_without_llvm():
    # The Flatpak GL extension builds Mesa in the Freedesktop SDK image, which has no
    # llvm-config: inheriting the system Mesa's -Dllvm=enabled made meson fail there.
    # It only serves KGSL phones (gpu-env); others keep the runtime's own GL.default.
    script = (ROOT / 'packaging/rungic-flatpak-gl/build.sh').read_text()
    line = next(l for l in script.splitlines() if l.startswith('options='))
    out = subprocess.run(['sh', '-c', f'{line}\nprintf %s "$options"'], env={'SRC': str(ROOT), 'PATH': os.environ['PATH']},
                         capture_output=True, text=True, check=True).stdout
    opts = dict(a[2:].split('=', 1) for a in shlex.split(out) if a.startswith('-D'))
    assert opts['llvm'] == 'disabled'
    assert 'llvmpipe' not in opts['gallium-drivers'].split(',')
    # Adding host virgl must not leak into the SDK extension's own override.
    assert 'virgl' not in opts['gallium-drivers'].split(',')
    assert {'freedreno', 'zink', 'softpipe'} <= set(opts['gallium-drivers'].split(','))


# covers: apps.gpu/E5
def test_gbm_backend_ships_in_libgbm1_like_ubuntu(tmp_path):
    # Ubuntu resolute's libgbm1 (26.0.8-1ubuntu0.3) owns gbm/dri_gbm.so. If our
    # mesa-libgallium carried it, dpkg refuses the upgrade ("trying to overwrite
    # ... which is also in package libgbm1") and the rootfs cannot install Mesa.
    stage, output, source = (tmp_path / n for n in ('stage', 'debs', 'source'))
    lib = stage / 'usr/lib/aarch64-linux-gnu'
    for name in ('libgallium-26.3.0.so', 'libEGL_mesa.so.0', 'libGLX_mesa.so.0', 'libgbm.so.1',
                 'pkgconfig/gbm.pc', 'libvulkan_freedreno.so', 'dri/swrast_dri.so', 'gbm/dri_gbm.so'):
        path = lib / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('synthetic runtime')
    (source / 'docs').mkdir(parents=True)
    (source / 'docs/license.rst').write_text('synthetic MIT license')
    env = {'RUNGIC_MESA_STAGE': str(stage), 'RUNGIC_MESA_PACKAGES': str(output),
           'RUNGIC_MESA_SOURCE': str(source), 'RUNGIC_MESA_VERSION': 'test-version'}
    with patch.dict(os.environ, env), patch('subprocess.run'):
        runpy.run_path(str(ROOT / 'desktop/package-mesa.py'), run_name='__main__')
    assert (output / 'libgbm1/usr/lib/aarch64-linux-gnu/gbm/dri_gbm.so').is_file()
    assert not list((output / 'mesa-libgallium').rglob('dri_gbm.so'))
