#!/bin/bash
set -euo pipefail
task_root=$(cd "$(dirname "$0")/.." && pwd)
task_tools="$task_root/tools/toolchains"
export PYTHONPYCACHEPREFIX=${PYTHONPYCACHEPREFIX:-$task_root/.work/cache/pycache}
task_native=$(python3 "$task_root/tools/prepare_android_host.py")
export PATH="$HOME/.cargo/bin:$PATH"
# The project proxy (AGENTS.md); RUNGIC_PROXY= builds without it on machines that cannot reach it.
task_proxy=${RUNGIC_PROXY-http://192.168.5.45:6152}
[ -z "$task_proxy" ] || export CARGO_HTTP_PROXY="$task_proxy"
# Toolchain: the kernel-tree clang where present, else the newest SDK NDK.
if [ -z "${RUNGIC_ANDROID_CLANG:-}" ] && [ ! -x /home/kevinzhow/android-kernel/prebuilts/clang/host/linux-x86/clang-r510928/bin/clang ]; then
  # linux-x86_64 on Linux build hosts, darwin-x86_64 (universal) in a macOS SDK.
  task_ndk=$(ls -d "${ANDROID_HOME:-$HOME/android-sdk}"/ndk/*/toolchains/llvm/prebuilt/linux-x86_64 "${ANDROID_HOME:-$HOME/android-sdk}"/ndk/*/toolchains/llvm/prebuilt/darwin-x86_64 2>/dev/null | sort -V | tail -1 || true)
  [ -n "$task_ndk" ] || { echo 'No Android clang: set RUNGIC_ANDROID_CLANG/RUNGIC_ANDROID_SYSROOT or install an NDK' >&2; exit 1; }
  export RUNGIC_ANDROID_CLANG="$task_ndk/bin/clang" RUNGIC_ANDROID_CLANGXX="$task_ndk/bin/clang++" RUNGIC_ANDROID_SYSROOT="$task_ndk/sysroot"
  task_ar="$task_ndk/bin/llvm-ar"
fi
export CARGO_TARGET_AARCH64_LINUX_ANDROID_LINKER="$task_tools/android-clang"
export CC_aarch64_linux_android="$task_tools/android-clang"
export CXX_aarch64_linux_android="$task_tools/android-clang++"
# llvm-ar sits next to the chosen clang; RUNGIC_ANDROID_AR overrides it.
if [ -n "${RUNGIC_ANDROID_AR:-}" ]; then task_ar=$RUNGIC_ANDROID_AR
elif [ -z "${task_ar:-}" ] && [ -n "${RUNGIC_ANDROID_CLANG:-}" ]; then task_ar=$(dirname "$RUNGIC_ANDROID_CLANG")/llvm-ar
fi
export AR_aarch64_linux_android=${task_ar:-/home/kevinzhow/android-kernel/prebuilts/clang/host/linux-x86/clang-r510928/bin/llvm-ar}
export CARGO_TARGET_DIR="${CARGO_TARGET_DIR:-$task_root/.work/build/native-target}"
export RUNGIC_JNI_LIBS_DIR="${RUNGIC_JNI_LIBS_DIR:-$task_root/.work/refs/plasma-mobile-20260923/native-libs/lib/arm64-v8a}"
task_native_out=${RUNGIC_NATIVE_OUT:-$task_root/.work/refs/plasma-mobile-20260923/native-libs/lib/arm64-v8a}
# Local Android build of libxkbcommon, else the copy shipped in the installed APK
# (tools/pull_installed_native_libs.py).
export RUNGIC_XKBCOMMON_LIB=${RUNGIC_XKBCOMMON_LIB:-$task_root/.work/deps/libxkbcommon/build-android/libxkbcommon.so}
[ -s "$RUNGIC_XKBCOMMON_LIB" ] || RUNGIC_XKBCOMMON_LIB="$RUNGIC_JNI_LIBS_DIR/libxkbcommon.so"
cargo build --manifest-path "$task_native/Cargo.toml" --locked --lib --release --target aarch64-linux-android --features smithay_android -j8
mkdir -p "$task_native_out"
[ "$RUNGIC_XKBCOMMON_LIB" -ef "$task_native_out/libxkbcommon.so" ] || cp "$RUNGIC_XKBCOMMON_LIB" "$task_native_out/libxkbcommon.so"
cp "$CARGO_TARGET_DIR/aarch64-linux-android/release/libuniffi_winland_core.so" "$task_native_out/"
# libuniffi_winland_core.so needs libc++_shared.so, which Android does not provide (docs/79):
# ship the one from the NDK it was linked against; refuse an output without it.
task_cxx=${RUNGIC_ANDROID_SYSROOT:+$RUNGIC_ANDROID_SYSROOT/usr/lib/aarch64-linux-android/libc++_shared.so}
if [ -n "$task_cxx" ] && [ -f "$task_cxx" ]; then cp "$task_cxx" "$task_native_out/"; fi
[ -s "$task_native_out/libc++_shared.so" ] || { echo "libc++_shared.so missing from $task_native_out" >&2; exit 1; }
