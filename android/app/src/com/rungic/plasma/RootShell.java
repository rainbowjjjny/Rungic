package com.rungic.plasma;

import java.io.File;
import java.util.function.Predicate;

/** Magisk's su for the app's root actions: the product image's copy first, else where Magisk mounted it. */
final class RootShell {
    private RootShell() {}

    // /product/bin: Motorola images with Magisk prebuilt. The rest: Magisk on a stock ROM
    // (Pixel 8 Pro: /system_ext/bin), its other bin targets and its tmpfs.
    private static final String[] CANDIDATES = {
        "/product/bin/su", "/system_ext/bin/su", "/system/bin/su", "/system/xbin/su", "/debug_ramdisk/su"};

    static final String SU = find(path -> new File(path).canExecute());

    static String find(Predicate<String> executable) {
        for (String path : CANDIDATES) if (executable.test(path)) return path;
        return CANDIDATES[0];
    }

    /** su's directory first, as the product path used to be. */
    static String path() {
        return new File(SU).getParent() + ":/product/bin:/system/bin:/system/xbin:/vendor/bin";
    }
}
