# rungic-plasma-session
install -Dm755 "$SRC/system/init" "$DESTDIR/usr/sbin/rungic-plasma-init"
install -Dm755 "$SRC/system/android-shm-mount" "$DESTDIR/usr/libexec/rungic-android-shm-mount"
install -Dm755 "$SRC/desktop/session" "$DESTDIR/usr/libexec/rungic-plasma-session"
install -Dm755 "$SRC/desktop/login-environment.py" "$DESTDIR/usr/libexec/rungic-login-environment"
install -Dm755 "$SRC/system/user-dirs" "$DESTDIR/usr/libexec/rungic-user-dirs"
install -Dm755 "$SRC/system/shared-storage" "$DESTDIR/usr/libexec/rungic-plasma-shared"
install -Dm755 "$SRC/desktop/kwin" "$DESTDIR/usr/libexec/rungic-plasma-kwin"
install -Dm755 "$SRC/desktop/desktop-action.py" "$DESTDIR/usr/libexec/rungic-plasma-desktop-action"
install -Dm755 "$SRC/desktop/display.py" "$DESTDIR/usr/libexec/rungic-plasma-display"
install -Dm755 "$SRC/desktop/brightness.py" "$DESTDIR/usr/libexec/rungic-plasma-brightness"
install -Dm644 "$SRC/desktop/power-policy.py" "$DESTDIR/usr/libexec/rungic-power-policy.py"
install -d "$DESTDIR/usr/share/locale/zh_CN/LC_MESSAGES"
msgfmt -c --check-format -o "$DESTDIR/usr/share/locale/zh_CN/LC_MESSAGES/rungic-power-policy.mo" "$SRC/desktop/po/zh_CN/rungic-power-policy.po"
install -Dm644 "$SRC/system/account/protocol" "$DESTDIR/usr/share/rungic/account-protocol"
install -Dm755 "$SRC/system/account/setup.py" "$DESTDIR/usr/libexec/rungic-plasma-account-setup"
install -Dm644 "$SRC/system/account/rungic-account.desktop" "$DESTDIR/usr/share/applications/rungic-account.desktop"
install -Dm755 "$SRC/system/user-exec" "$DESTDIR/usr/bin/rungic-plasma-user-exec"
install -Dm755 "$SRC/desktop/rebrand-user.py" "$DESTDIR/usr/libexec/rungic-rebrand-user"
install -Dm755 "$SRC/system/rebrand-system.sh" "$DESTDIR/usr/libexec/rungic-rebrand-system"
mkdir -p "$DESTDIR/usr/bin"
g++ -O2 -g1 -std=c++20 -fPIC -o "$DESTDIR/usr/bin/rungic-plasma-screen-metrics" "$SRC/desktop/screen-metrics.cpp" \
    $(pkg-config --cflags --libs Qt6Gui)
for unit in rungic-plasma-session rungic-plasma-shared; do
    install -Dm644 "$SRC/desktop/$unit.service" "$DESTDIR/usr/lib/systemd/system/$unit.service"
done
for unit in rungic-plasma-display rungic-plasma-brightness; do
    install -Dm644 "$SRC/desktop/$unit.service" "$DESTDIR/usr/lib/systemd/user/$unit.service"
done
install -Dm644 "$SRC/desktop/kwin-override.conf" "$DESTDIR/usr/lib/systemd/user/plasma-kwin_wayland.service.d/rungic.conf"
install -Dm644 "$SRC/system/pulse-override.conf" "$DESTDIR/usr/lib/systemd/user/pulseaudio.service.d/rungic.conf"
