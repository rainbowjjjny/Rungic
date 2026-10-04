"""The container's /dev/shm must carry the APK's SELinux label, or the app cannot map
KWin's software (QPainter) buffers and the desktop never shows (found on husky).

Two causes, both covered here: Ubuntu's libmount drops SELinux mount options when the
container cannot see selinuxfs, and the Pixel policy denies the filesystem association.
"""
import runpy
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / 'system/android-shm-mount'
CONTEXT = 'u:object_r:appdomain_tmpfs:s0:c53,c257,c512,c768'


class Libc:
    def __init__(self, result=0):
        self.calls, self.result = [], result

    def mount(self, source, target, fstype, flags, data):
        self.calls.append((source, target, fstype, flags, data))
        return self.result


def run(libc, context_file, mountinfo):
    with mock.patch('ctypes.CDLL', return_value=libc), \
            mock.patch('sys.argv', ['android-shm-mount', str(context_file), str(mountinfo)]):
        runpy.run_path(str(HELPER), run_name='__main__')


# covers: install.container-base/E1
def test_mounts_with_the_apk_context_through_the_kernel_not_libmount(tmp_path):
    context_file = tmp_path / 'android-shm-context'
    context_file.write_text(CONTEXT + '\n')
    mountinfo = tmp_path / 'mountinfo'
    mountinfo.write_text(f'1 2 0:9 / /dev/shm rw - tmpfs tmpfs rw,context="{CONTEXT}"\n')
    libc = Libc()
    run(libc, context_file, mountinfo)
    (source, target, fstype, flags, data), = libc.calls
    assert (source, target, fstype) == (b'tmpfs', b'/dev/shm', b'tmpfs')
    assert flags == 2 | 4  # MS_NOSUID | MS_NODEV
    # The categories contain commas, so the context is quoted inside the option string.
    assert f'context="{CONTEXT}"'.encode() in data
    assert b'mode=1777' in data


# covers: install.container-base/E1
def test_refuses_a_mount_whose_label_did_not_take(tmp_path):
    # libmount reported success while silently dropping context=: never continue unlabelled.
    context_file = tmp_path / 'android-shm-context'
    context_file.write_text(CONTEXT + '\n')
    mountinfo = tmp_path / 'mountinfo'
    mountinfo.write_text('1 2 0:9 / /dev/shm rw - tmpfs tmpfs rw,seclabel\n')
    with pytest.raises(SystemExit) as stop:
        run(Libc(), context_file, mountinfo)
    assert 'context' in str(stop.value)


# covers: install.container-base/E1
def test_reports_a_kernel_refusal(tmp_path):
    context_file = tmp_path / 'android-shm-context'
    context_file.write_text(CONTEXT + '\n')
    with mock.patch('ctypes.get_errno', return_value=13), pytest.raises(SystemExit) as stop:
        run(Libc(result=-1), context_file, tmp_path / 'mountinfo')
    assert 'Permission denied' in str(stop.value)


# covers: install.container-base/E1
def test_init_uses_the_helper_and_policy_allows_the_association():
    init = (ROOT / 'system/init').read_text()
    assert 'android-shm-mount' in init
    assert not any(line.lstrip().startswith('mount ') and '/dev/shm' in line and 'context=' in line
                   for line in init.splitlines()), 'util-linux mount drops context= without selinuxfs'
    rule = (ROOT / 'system/rootfs.sepolicy.rule').read_text()
    assert 'allow appdomain_tmpfs appdomain_tmpfs filesystem associate' in rule
    build = (ROOT / 'packaging/rungic-plasma-session/build.sh').read_text()
    assert 'system/android-shm-mount' in build
