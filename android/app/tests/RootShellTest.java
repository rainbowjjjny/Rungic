package com.rungic.plasma;

import java.util.Set;

/** Where the app finds Magisk's su, on a host JVM. */
public final class RootShellTest {
    static void equal(String expected, String actual, String why) {
        if (!java.util.Objects.equals(expected, actual))
            throw new AssertionError(why + ": expected " + expected + ", got " + actual);
    }

    // covers: install.desktop-entry/E1
    public static void main(String[] args) {
        // Motorola product images ship su in /product/bin: it stays first, so those phones do not change.
        equal("/product/bin/su", RootShell.find(Set.of("/product/bin/su", "/system_ext/bin/su")::contains),
              "bundled product su keeps priority");
        // A Pixel 8 Pro (husky) rooted with Magisk 31 has no /product/bin/su; Magisk mounts it in /system_ext/bin.
        // With the fixed path the app could not start any control action ("Cannot run program").
        equal("/system_ext/bin/su", RootShell.find(Set.of("/system_ext/bin/su", "/debug_ramdisk/su")::contains),
              "Magisk su on husky");
        equal("/system/bin/su", RootShell.find(Set.of("/system/bin/su")::contains), "Magisk su in /system/bin");
        equal("/debug_ramdisk/su", RootShell.find(Set.of("/debug_ramdisk/su")::contains), "Magisk tmpfs su");
        // No su at all: keep the old path so the existing "cannot run" error still explains it.
        equal("/product/bin/su", RootShell.find(path -> false), "no su found");
        System.out.println("PASS bundled, Magisk system_ext/system/debug_ramdisk su and missing su");
    }
}
