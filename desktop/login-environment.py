#!/usr/bin/python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""The desktop's login environment, as a display manager gives it (docs/100). Prints shell `export`
lines for the session script (desktop/session) to eval.

On other Plasma systems SDDM or GDM starts the session through the user's login shell, which reads
/etc/profile and ~/.profile: the whole desktop, and every terminal opened from it, inherits that
PATH. This desktop is started by a systemd unit instead, so it never read them, and ~/.local/bin
was missing from its PATH: a tool an installer put there (Codex's official script) could not be
found until a new terminal read the line the script appended to ~/.bashrc.

What it takes over, measured in child shells (a profile that fails or hangs cannot stop the
session: 10 s, then without it):
  PATH           from the user's login shell (`$SHELL -l`), with ~/.local/bin and ~/bin always on
                 it, existing yet or not (as Fedora's ~/.bashrc and systemd's file-hierarchy(7) have
                 them): a tool installed there runs from an open terminal's next command
  the user's own exports in their login files (what the login shell has beyond /etc/profile),
                 except the variables the session itself decides (PROTECTED)
System-wide profile.d settings other than PATH stay out: /etc/profile.d/maliit-framework.sh would
switch every Qt and GTK program to the Maliit input method.
The last line lists the variables for `systemctl --user import-environment`.
"""
import os
import pwd
import shlex
import subprocess
import sys

TIMEOUT_S = 10
MARK = '\0rungic-login-environment\0'
# The session's own decisions (desktop/session, /etc/plasma/gpu-env, the proxy) and a shell's own state.
PROTECTED = {
    'HOME', 'USER', 'LOGNAME', 'SHELL', 'PWD', 'OLDPWD', 'SHLVL', '_', 'PS1', 'PS2', 'TERM', 'MAIL',
    'XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS', 'XDG_SESSION_TYPE', 'XDG_CURRENT_DESKTOP',
    'XDG_SESSION_DESKTOP', 'XDG_MENU_PREFIX', 'XDG_DATA_DIRS', 'XDG_CONFIG_DIRS', 'WAYLAND_DISPLAY',
    'DISPLAY', 'QT_QPA_PLATFORM', 'GDK_BACKEND', 'MOZ_ENABLE_WAYLAND', 'QT_QUICK_BACKEND', 'KWIN_COMPOSE',
    'RUNGIC_ANDROID_DISPLAY', 'PULSE_SERVER', 'PULSE_COOKIE', 'MESA_LOADER_DRIVER_OVERRIDE',
    'FD_KGSL_ENABLE_DMABUF', 'VK_DRIVER_FILES', 'FLATPAK_GL_DRIVERS', 'QSG_RHI_BACKEND', 'GSK_RENDERER',
    'GALLIUM_DRIVER', 'VTEST_SOCKET_NAME',
    'QT_IM_MODULE', 'GTK_IM_MODULE', 'XMODIFIERS', 'http_proxy', 'https_proxy', 'HTTP_PROXY',
    'HTTPS_PROXY', 'ALL_PROXY', 'no_proxy', 'NO_PROXY', 'PLASMA_DEFAULT_SHELL',
}


def capture(argv, env):
    """The exported environment at the end of `argv` (a shell), or None when it fails or hangs."""
    try:
        done = subprocess.run(argv, env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return None
    out = done.stdout.decode('utf-8', 'replace')
    if MARK not in out:          # a profile that exits early or prints over the marker
        return None
    result = {}
    for entry in out.split(MARK, 1)[1].split('\0'):
        key, sep, value = entry.partition('=')
        if sep and key:
            result[key] = value
    return result


def user_bins(path, home):
    """`path` with ~/.local/bin and ~/bin at its front when missing, each directory once."""
    parts = [p for p in path.split(':') if p]
    local, own = os.path.join(home, '.local/bin'), os.path.join(home, 'bin')
    # Ubuntu's ~/.profile order: ~/.local/bin, then ~/bin, then the system's.
    if local not in parts:
        parts.insert(0, local)
    if own not in parts:
        parts.insert(parts.index(local) + 1, own)
    seen, unique = set(), []
    for p in parts:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return ':'.join(unique)


def login_environment(env, shell):
    """{name: value} to export: the login PATH and the user's own login exports."""
    dump = f"printf '%b' '{MARK.replace(chr(0), chr(92) + '0')}'; exec /usr/bin/env -0"
    system = capture(['sh', '-c', f'. /etc/profile >/dev/null 2>&1; {dump}'], env)
    login = capture([shell, '-l', '-c', dump], env) if shell else None
    result = {}
    if login and system:
        for key, value in login.items():
            if key not in PROTECTED and key != 'PATH' and not key.startswith('BASH_FUNC_') \
                    and system.get(key) != value and env.get(key) != value:
                result[key] = value
    path = (login or system or {}).get('PATH') or env.get('PATH', '/usr/local/bin:/usr/bin:/bin')
    result['PATH'] = user_bins(path, env.get('HOME', ''))
    return result


def main():
    env = dict(os.environ)
    try:
        shell = pwd.getpwuid(os.getuid()).pw_shell or env.get('SHELL') or '/bin/sh'
    except KeyError:
        shell = env.get('SHELL') or '/bin/sh'
    if not os.access(shell, os.X_OK) or shell.endswith(('/nologin', '/false')):
        shell = '/bin/sh'
    values = login_environment(env, shell)
    for key in sorted(values):
        print(f'export {key}={shlex.quote(values[key])}')
    print(f"RUNGIC_LOGIN_VARS={shlex.quote(' '.join(sorted(values)))}")


if __name__ == '__main__':
    sys.exit(main())
