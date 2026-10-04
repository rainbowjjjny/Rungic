#!/bin/sh
# Android app-domain server for husky Mali; no GPU devices enter LXC (docs/108).
# Needs Python 3.12+ with PyYAML, Meson, Ninja, pkg-config, patch and Android NDK (r28.2 tested).
# RUNGIC_VIRGL_OFFLINE=1 uses only the hash-checked .work/sources cache.
set -eu
task_root=$(cd "$(dirname "$0")/.." && pwd)
export PYTHONPYCACHEPREFIX=${PYTHONPYCACHEPREFIX:-$task_root/.work/cache/pycache}
task_python=$(command -v "${RUNGIC_VIRGL_PYTHON:-python3}")
# Meson runs build-machine Python generators that need PyYAML. Preserve the
# selected development interpreter for them as well as the source preparer.
export PATH="$(dirname "$task_python"):$PATH"
"$task_python" -c 'import yaml'
task_build=$task_root/.work/build/virgl-server
task_out=${RUNGIC_NATIVE_OUT:-${RUNGIC_JNI_LIBS_DIR:-${RUNGIC_NATIVE_LIBS:-$task_root/.work/refs/plasma-mobile-20260923/native-libs}/lib/arm64-v8a}}
mkdir -p "$task_build" "$task_out"
# The NDK's darwin-x86_64 prebuilt also supports Apple Silicon. Do not choose a
# Linux prebuilt on Darwin just because both are present in a shared SDK.
task_prebuilt=$("$task_python" - <<'PY'
import os, platform, re
from pathlib import Path
host = {'Darwin': 'darwin-x86_64', 'Linux': 'linux-x86_64'}.get(platform.system())
if not host:
    raise SystemExit('Unsupported NDK build host')
ndk = os.environ.get('RUNGIC_ANDROID_NDK') or os.environ.get('ANDROID_NDK_HOME') or os.environ.get('ANDROID_NDK_ROOT')
if ndk:
    choices = [Path(ndk)]
elif os.environ.get('RUNGIC_ANDROID_SYSROOT'):
    prebuilt = Path(os.environ['RUNGIC_ANDROID_SYSROOT']).parent
    if not (prebuilt / 'bin/clang').is_file():
        raise SystemExit('Invalid RUNGIC_ANDROID_SYSROOT')
    print(prebuilt)
    raise SystemExit(0)
else:
    sdk = os.environ.get('ANDROID_HOME') or os.environ.get('ANDROID_SDK_ROOT')
    sdks = [Path(sdk)] if sdk else [Path.home() / 'android-sdk', Path.home() / 'Library/Android/sdk']
    choices = [p for sdk in sdks for p in (sdk / 'ndk').glob('*')]
    choices.sort(key=lambda p: tuple(int(v) for v in re.findall(r'\d+', p.name)), reverse=True)
for choice in choices:
    prebuilt = choice / 'toolchains/llvm/prebuilt' / host
    if (prebuilt / 'bin/aarch64-linux-android30-clang').is_file():
        print(prebuilt)
        break
else:
    raise SystemExit('Install an Android NDK or set RUNGIC_ANDROID_NDK')
PY
)
task_offline=
[ "${RUNGIC_VIRGL_OFFLINE:-0}" != 1 ] || task_offline=--offline
# shellcheck disable=SC2086
"$task_python" "$task_root/tools/prepare_virgl_server.py" --output "$task_build/source" $task_offline >/dev/null
# Use absolute paths and quoted Meson strings, including SDKs under directories with spaces.
"$task_python" - "$task_prebuilt" "$task_build/cross.ini" <<'PY'
import sys
from pathlib import Path
prebuilt, out = Path(sys.argv[1]), Path(sys.argv[2])
def quote(value):
    return "'" + str(value).replace('\\', '\\\\').replace("'", "\\'") + "'"
binaries = {name: prebuilt / 'bin' / binary for name, binary in {
    'c': 'aarch64-linux-android30-clang', 'cpp': 'aarch64-linux-android30-clang++',
    'ar': 'llvm-ar', 'strip': 'llvm-strip'}.items()}
out.write_text('[binaries]\n' + ''.join(f'{key} = {quote(value)}\n' for key, value in binaries.items()) +
    "pkg-config = 'pkg-config'\n[host_machine]\nsystem = 'android'\ncpu_family = 'aarch64'\ncpu = 'aarch64'\nendian = 'little'\n")
PY
# Avoid host libdrm/GBM/epoxy discovery, and link both pinned components statically.
unset PKG_CONFIG_PATH PKG_CONFIG_SYSROOT_DIR
export PKG_CONFIG_LIBDIR="$task_build/install/lib/pkgconfig"
rm -rf "$task_build/epoxy-build" "$task_build/virgl-build" "$task_build/install"
meson setup "$task_build/epoxy-build" "$task_build/source/libepoxy" \
    --cross-file "$task_build/cross.ini" --prefix "$task_build/install" --libdir lib \
    --buildtype release --default-library static -Degl=yes -Dglx=no -Dx11=false -Dtests=false
ninja -C "$task_build/epoxy-build" install
meson setup "$task_build/virgl-build" "$task_build/source" \
    --cross-file "$task_build/cross.ini" --prefix "$task_build/install" --libdir lib \
    --buildtype release --default-library static -Dplatforms=egl -Dvenus=false -Dvideo=false -Dtests=false
ninja -C "$task_build/virgl-build" vtest/virgl_test_server
# PIE executable named lib*.so: Android extracts it into the executable nativeLibraryDir.
cp "$task_build/virgl-build/vtest/virgl_test_server" "$task_out/libvirgl_test_server.so"
"$task_prebuilt/bin/llvm-strip" --strip-unneeded "$task_out/libvirgl_test_server.so"
chmod 755 "$task_out/libvirgl_test_server.so"
# APK build copies the notices next to the native library into assets/virgl.
mkdir -p "$task_out/../../licenses/virgl"
cp "$task_build/source/COPYING" "$task_out/../../licenses/virgl/COPYING-virglrenderer"
cp "$task_build/source/libepoxy/COPYING" "$task_out/../../licenses/virgl/COPYING-libepoxy"
echo "$task_out/libvirgl_test_server.so"
