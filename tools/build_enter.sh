#!/bin/sh
# Build a root "enter" program (tools/rungic_*_enter.c) as a static Android executable with the NDK:
#   tools/build_enter.sh plasma|lxc    -> .work/build/android/rungic-NAME-enter
# RUNGIC_ENTER_OUT redirects the newly compiled binary into a build record.
# The program pivots into a Linux runtime before it execs, so its own C library does not matter;
# RUNGIC_ANDROID_NDK overrides the newest NDK under $ANDROID_HOME.
set -eu
task_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
name=${1:?usage: build_enter.sh plasma|lxc}
task_clang=${RUNGIC_ANDROID_CLANG:-}
task_sysroot=${RUNGIC_ANDROID_SYSROOT:-}
if [ -z "$task_clang" ] || [ -z "$task_sysroot" ]; then
    task_ndk=${RUNGIC_ANDROID_NDK:-$(ls -d "${ANDROID_HOME:-$HOME/android-sdk}"/ndk/* 2>/dev/null | sort -V | tail -1)}
    if [ -x "$task_ndk/toolchains/llvm/prebuilt/linux-x86_64/bin/clang" ]; then
        task_clang=$task_ndk/toolchains/llvm/prebuilt/linux-x86_64/bin/clang
        task_sysroot=$task_ndk/toolchains/llvm/prebuilt/linux-x86_64/sysroot
    else
        # ACK checkouts may carry the compiler and Android r26 sysroot separately.
        task_ack=$(CDPATH= cd -- "$task_root/../android-kernel" 2>/dev/null && pwd) || task_ack=
        task_clang=$task_ack/prebuilts/clang/host/linux-x86/clang-r510928/bin/clang
        task_sysroot=$task_ack/prebuilts/ndk-r26/toolchains/llvm/prebuilt/linux-x86_64/sysroot
    fi
fi
[ -x "$task_clang" ] && [ -d "$task_sysroot" ] || {
    echo 'No Android clang/sysroot: set RUNGIC_ANDROID_CLANG and RUNGIC_ANDROID_SYSROOT' >&2; exit 1;
}
task_out=${RUNGIC_ENTER_OUT:-$task_root/.work/build/android}
mkdir -p "$task_out"
"$task_clang" --target=aarch64-linux-android31 --sysroot="$task_sysroot" -static -s -O2 -Wall -Wextra -Werror \
    "$task_root/tools/rungic_${name}_enter.c" -o "$task_out/rungic-$name-enter"
echo "$task_out/rungic-$name-enter"
