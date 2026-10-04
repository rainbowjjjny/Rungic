package com.rungic.plasma;

/** Phone display policy on a host JVM, without an Android device. */
public final class DisplayGeometryTest {
    static void equal(int expected, int actual, String why) {
        if (expected != actual) throw new AssertionError(why + ": expected " + expected + ", got " + actual);
    }

    // covers: desktop.display-size/E7 desktop.display-size/E8
    public static void main(String[] args) {
        // Pixel 8 Pro (husky) has Mali, not KGSL: native resolution would overload CPU rendering.
        equal(720, DisplayGeometry.defaultRenderShortEdge(false, 1008, 2244), "husky software default");
        equal(720, DisplayGeometry.defaultRenderShortEdge(false, 2244, 1008), "rotation keeps software default");
        // Existing Qualcomm/Adreno phones must keep their native-resolution default in both orientations.
        equal(1080, DisplayGeometry.defaultRenderShortEdge(true, 1080, 2400), "Adreno default unchanged");
        equal(1080, DisplayGeometry.defaultRenderShortEdge(true, 2400, 1080), "Adreno landscape unchanged");
        equal(480, DisplayGeometry.defaultRenderShortEdge(false, 480, 800), "do not upscale small screens");
        equal(480, DisplayGeometry.defaultRenderShortEdge(true, 480, 800), "small Adreno default unchanged");

        // DPI and full-display pixels describe the panel, independently of the 720p render buffer.
        int[] portrait = DisplayGeometry.physicalSizeMm(1440, 3120, 480f, 520f);
        equal(76, portrait[0], "panel width uses xdpi, not Android logical density");
        equal(152, portrait[1], "panel height uses ydpi, not another phone's fixed size");
        // Android 16 retains natural-axis DPI during rotation. Use the mode's natural
        // pixel dimensions, then rotate the millimetres for the host output.
        int[] landscape = DisplayGeometry.physicalSizeMm(1440, 3120, 480f, 520f, true);
        equal(portrait[1], landscape[0], "landscape width uses natural panel height and ydpi");
        equal(portrait[0], landscape[1], "landscape height uses natural panel width and xdpi");
        int[] unrotated = DisplayGeometry.physicalSizeMm(1440, 3120, 480f, 520f, false);
        equal(portrait[0], unrotated[0], "0/180 degrees keep the natural width");
        equal(portrait[1], unrotated[1], "0/180 degrees keep the natural height");
        int[] adreno = DisplayGeometry.physicalSizeMm(1080, 2400, 403.4f, 403.4f);
        equal(68, adreno[0], "existing Adreno panel still reports its measured width");
        equal(151, adreno[1], "existing Adreno panel still reports its measured height");
        for (float dpi : new float[]{0f, -1f, Float.NaN, Float.POSITIVE_INFINITY}) {
            int[] unknown = DisplayGeometry.physicalSizeMm(1440, 3120, dpi, 520f);
            equal(0, unknown[0], "invalid panel DPI means unknown, not an enormous physical size");
            equal(152, unknown[1], "invalid xdpi does not discard valid ydpi");
        }
        equal(0, DisplayGeometry.physicalSizeMm(0, 3120, 480f, 520f)[0], "missing pixels mean unknown");
        System.out.println("PASS husky software and Adreno defaults, measured panel size, rotation and invalid DPI");
    }
}
