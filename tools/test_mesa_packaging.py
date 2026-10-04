# SPDX-License-Identifier: MIT
"""One Mesa runtime must serve husky (Mali, CPU GL) and Qualcomm (KGSL).

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
    for driver in ('swrast', 'kgsl', 'zink'):
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
    for driver in ('swrast', 'kgsl', 'zink'):
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
