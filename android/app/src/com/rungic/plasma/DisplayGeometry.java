package com.rungic.plasma;

/** Display calculations independent of Android, shared by the host and display settings. */
final class DisplayGeometry {
    private DisplayGeometry() {}

    static int defaultRenderShortEdge(boolean hasKgsl, int physicalWidth, int physicalHeight) {
        int nativeEdge = Math.min(physicalWidth, physicalHeight);
        return hasKgsl ? nativeEdge : Math.min(720, nativeEdge);
    }

    /** Mode pixels and DisplayMetrics DPI both use the panel's natural axes. */
    static int[] physicalSizeMm(int widthPixels, int heightPixels, float xdpi, float ydpi) {
        return new int[]{millimetres(widthPixels, xdpi), millimetres(heightPixels, ydpi)};
    }

    static int[] physicalSizeMm(int widthPixels, int heightPixels, float xdpi, float ydpi, boolean quarterTurn) {
        int[] natural = physicalSizeMm(widthPixels, heightPixels, xdpi, ydpi);
        return quarterTurn ? new int[]{natural[1], natural[0]} : natural;
    }

    private static int millimetres(int pixels, float dpi) {
        // Wayland uses zero for an unknown physical size. Never invent another phone's size.
        if (pixels <= 0 || !Float.isFinite(dpi) || dpi <= 0) return 0;
        double mm = pixels * 25.4 / dpi;
        return mm >= Integer.MAX_VALUE ? 0 : (int)Math.round(mm);
    }
}
