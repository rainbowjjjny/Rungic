#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Build this project's own Debian packages (rungic-*) from packaging/<name>/ (docs/61).

Each package directory holds:
  package.json  name, architecture (all|arm64), build (host|device), paths (repository paths
                the package is built from: a change there means a rebuild), upstream (patch-queue
                components, packages/<name>, whose patched source build.sh finds in
                $SRC/upstream/<name>; docs/71), depends,
                build_depends (installed on the phone before a device build), image (a device
                package whose build.sh runs in this container image instead, next to the Mac
                mini's build container: the Flatpak GL extension in the Freedesktop SDK),
                shlibdeps (false: no Ubuntu library dependencies, for files that link against
                another runtime's libraries), description,
                formerly (the package's name before the Rungic rename: Conflicts, Replaces and the
                enable state of its renamed units carry over, docs/70),
                obsolete (files of the old manual installs that postinst removes once the
                package's own copies are in place), units {system: [...], user: [...]} to enable,
                conffiles are every file under /etc
  build.sh      POSIX sh, run with DESTDIR (the package root) and SRC (the repository tree);
                installs the files into $DESTDIR
  preinst, postinst, prerm, postrm   optional maintainer script bodies; the generated
                postinst removes 'obsolete', reloads systemd and enables 'units' first

Host packages are built here (reproducibly: SOURCE_DATE_EPOCH is the commit time); device
packages are built on Ubuntu 26.04 ARM64 where tools/build_on_device.py builds (--host phone: the
phone's container, in a transient unit; --host macmini: the Mac mini build host; default
$RUNGIC_BUILD_HOST, else macmini), with dpkg-shlibdeps adding library dependencies. Versions are 0.<commit count>, with +bN when a rebuild of the same
commit differs. Built packages go to the release pool (.work/apt/repo) and are recorded in
.work/apt/project-builds.json with the git tree hash of their paths, so an unchanged
package is not rebuilt.

  rungic_package.py list                    packages and whether they are current
  rungic_package.py build NAME... | --all   build what changed (--force: even if current)
"""
import argparse
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

import build_on_device
from rungic_device import WORKSPACE
import rungic_release

PACKAGING = WORKSPACE / 'packaging'
BUILDS = rungic_release.APT / 'project-builds.json'
DEVICE_BASE = build_on_device.PACKAGE_BASE
MAINTAINER = 'range-dev <noreply@localhost>'


def definitions():
    result = {}
    for spec in sorted(PACKAGING.glob('*/package.json')):
        data = json.loads(spec.read_text())
        data['dir'] = spec.parent
        result[data['name']] = data
    return result


def git(*args):
    return subprocess.run(['git', *args], cwd=WORKSPACE, capture_output=True, text=True, check=True).stdout.strip()


def identity_paths(pkg):
    """What a package is built from: its paths, its directory, and for each upstream component its
    recipe and patches (packages/<name>) and the shared files its overlay places."""
    import pq
    paths = set(pkg['paths']) | {str(pkg['dir'].relative_to(WORKSPACE))}
    for name in pkg.get('upstream', []):
        paths.add(f'packages/{name}')
        paths.update(entry['from'] for entry in pq.overlay(name).values())
    return sorted(paths)


def tree_hash(pkg):
    """Content identity of a package: the git trees/blobs of identity_paths()."""
    paths = identity_paths(pkg)
    dirty = git('status', '--porcelain', '--', *paths)
    if dirty:
        raise SystemExit(f"{pkg['name']}: uncommitted changes in its paths:\n{dirty}")
    ids = [f"{p}={git('rev-parse', f'HEAD:{p}')}" for p in paths]
    return hashlib.sha256('\n'.join(ids).encode()).hexdigest()[:16]


def builds():
    return json.loads(BUILDS.read_text()) if BUILDS.exists() else {}


def current(pkg):
    record = builds().get(pkg['name'])
    return bool(record) and record['tree'] == tree_hash(pkg) and \
        (rungic_release.POOL / record['file']).exists()


MIRROR = {'system': '/var/lib/systemd/deb-systemd-helper-enabled',
          'user': '/var/lib/systemd/deb-systemd-user-helper-enabled'}


def former_unit(pkg, unit):
    """The unit's name under the package's former name ('formerly', docs/70), if it had one."""
    former = pkg.get('formerly')
    if not former or not unit.startswith('rungic-'):
        return None
    return 'moto-' + unit[len('rungic-'):]


def maintainer_scripts(pkg, root):
    """DEBIAN/{preinst,postinst,prerm,postrm} and conffiles."""
    debian = root / 'DEBIAN'
    units = pkg.get('units', {})
    obsolete = pkg.get('obsolete', [])
    post = ['#!/bin/sh', 'set -e', '']
    renamed = [(scope, flag, unit, former_unit(pkg, unit)) for scope, flag in (('system', ''), ('user', ' --user'))
               for unit in units.get(scope, []) if former_unit(pkg, unit)]
    if renamed:
        # Renamed units (docs/70): whether the old unit was enabled, read before the obsolete links
        # below go. Its enable links, as deb-systemd-helper recorded them (.dsh-also), are dangling
        # once apt removed the old package, and still there unless the administrator disabled it;
        # deb-systemd-helper's own mirror of them does not see an administrator's systemctl disable.
        post += ['rungic_was_enabled() {   # unit state-directory',
                 '  [ -f "$2/$1.dsh-also" ] || return 0',
                 '  while read -r link; do',
                 '    [ -L "$link" ] || return 1',
                 '  done < "$2/$1.dsh-also"',
                 '  return 0',
                 '}']
        for index, (scope, flag, unit, former) in enumerate(renamed):
            post += [f"rungic_former_{index}=disabled",
                     f"if rungic_was_enabled '{former}' {MIRROR[scope]}; then rungic_former_{index}=enabled; fi"]
        post += ['']
    if obsolete:
        items = ' '.join(p if '*' in p else shlex.quote(p) for p in obsolete)   # patterns with * expand
        post += ['# Files of the manual installation this package replaces (docs/61). Symlinks go only when',
                 '# dangling: an old enable link has the same path as the one systemctl enable makes now.',
                 'if [ "$1" = configure ]; then',
                 f'  for f in {items}; do',
                 '    if [ -L "$f" ]; then continue; elif [ -f "$f" ]; then rm -f "$f"; elif [ -d "$f" ]; then rm -rf "$f"; fi',
                 '  done',
                 f'  for f in {items}; do',
                 '    if [ -L "$f" ] && [ ! -e "$f" ]; then rm -f "$f"; fi',
                 '  done',
                 'fi', '']
    if units.get('system') or units.get('user') or pkg.get('user_systemd'):
        post += ['if [ -d /run/systemd/system ]; then systemctl daemon-reload || true; fi']
    for scope, flag in (('system', ''), ('user', ' --user')):
        for unit in units.get(scope, []):
            # debhelper 14's postinst-systemd{,-user}-enable: was-enabled is true without a record, so
            # a first installation enables; an administrator's disable survives upgrades, and removal
            # followed by reinstallation (prerm no longer disables, docs/70).
            post += ['if [ "$1" = configure ] || [ "$1" = abort-upgrade ] || [ "$1" = abort-deconfigure ] || '
                     '[ "$1" = abort-remove ]; then']
            former = former_unit(pkg, unit)
            if former:
                # Renamed (docs/70): the old unit's state (read above) carries over, then its record
                # goes (a rollback to the old package enables it again, as debhelper does without a
                # record). was-enabled cannot tell: the old unit file is gone once apt removed the
                # package this one replaces.
                index = [u for _, _, u, _ in renamed].index(unit)
                post += [f"  if deb-systemd-helper{flag} debian-installed '{former}'; then",
                         f"    if [ \"$rungic_former_{index}\" = enabled ]; then deb-systemd-helper{flag} enable '{unit}' >/dev/null || true",
                         f"    else deb-systemd-helper{flag} disable '{unit}' >/dev/null || true; fi",
                         f"    deb-systemd-helper{flag} purge '{former}' >/dev/null || true",
                         f"  elif deb-systemd-helper --quiet{flag} was-enabled '{unit}'; then"]
            else:
                post += [f"  if deb-systemd-helper --quiet{flag} was-enabled '{unit}'; then"]
            post += [
                     f"    deb-systemd-helper{flag} enable '{unit}' >/dev/null || true",
                     f"  else deb-systemd-helper{flag} update-state '{unit}' >/dev/null || true; fi",
                     'fi']
    if units.get('user') or pkg.get('user_systemd'):
        # Running user managers keep the unit files and drop-ins they loaded; a session restart
        # would otherwise start the old (possibly deleted) command lines.
        post += ['for dir in /run/user/*; do',
                 '  uid=${dir##*/}; [ -S "$dir/systemd/private" ] || continue',
                 '  name=$(getent passwd "$uid" | cut -d: -f1); [ -n "$name" ] || continue',
                 '  runuser -u "$name" -- env XDG_RUNTIME_DIR="$dir" systemctl --user daemon-reload || true',
                 'done']
    custom = pkg['dir'] / 'postinst'
    if custom.exists():
        post += ['', custom.read_text().replace('#!/bin/sh\n', '')]
    scripts = {'postinst': '\n'.join(post) + '\n'}
    prerm = ['#!/bin/sh', 'set -e']
    if units.get('system'):
        # debhelper's prerm-systemd-restart: stop on removal, keep the enable state (docs/70).
        names = ' '.join(f"'{u}'" for u in units['system'])
        prerm += ['if [ -z "$DPKG_ROOT" ] && [ "$1" = remove ] && [ -d /run/systemd/system ]; then',
                  f'  deb-systemd-invoke stop {names} >/dev/null || true', 'fi']
    if (pkg['dir'] / 'prerm').exists():
        prerm += [(pkg['dir'] / 'prerm').read_text().replace('#!/bin/sh\n', '')]
    scripts['prerm'] = '\n'.join(prerm) + '\n'
    if (pkg['dir'] / 'preinst').exists():
        scripts['preinst'] = (pkg['dir'] / 'preinst').read_text()
    postrm = ['#!/bin/sh', 'set -e']
    for scope, flag in (('system', ''), ('user', ' --user')):
        if units.get(scope):
            # debhelper's postrm-systemd{,-user}: the enable records go only on purge.
            names = ' '.join(f"'{u}'" for u in units[scope])
            postrm += [f'if [ "$1" = purge ]; then deb-systemd-helper{flag} purge {names} >/dev/null || true; fi']
    if units.get('system'):
        postrm += ['if [ "$1" = remove ] && [ -d /run/systemd/system ]; then systemctl --system daemon-reload >/dev/null || true; fi']
    if (pkg['dir'] / 'postrm').exists():
        postrm += [(pkg['dir'] / 'postrm').read_text().replace('#!/bin/sh\n', '')]
    if len(postrm) > 2:
        scripts['postrm'] = '\n'.join(postrm) + '\n'
    for name, text in scripts.items():
        path = debian / name
        path.write_text(text)
        path.chmod(0o755)
    etc = root / 'etc'
    conffiles = sorted('/' + str(p.relative_to(root)) for p in etc.rglob('*')
                       if (p.is_file() or p.is_symlink()) and not p.is_symlink()) if etc.exists() else []
    if conffiles:
        (debian / 'conffiles').write_text('\n'.join(conffiles) + '\n')


def unit_list(pkg):
    """/usr/share/rungic/units/NAME.list: the units this package enables, for rungic-integrity."""
    units = pkg.get('units', {})
    lines = [f'system {u}' for u in units.get('system', [])] + [f'user {u}' for u in units.get('user', [])]
    return '\n'.join(lines) + '\n' if lines else None


def control(pkg, version, root, extra_depends=''):
    size = sum(p.stat().st_size for p in root.rglob('*') if p.is_file() and not p.is_symlink()) // 1024 + 1
    depends = ', '.join(d for d in (pkg.get('depends', ''), extra_depends) if d)
    fields = {'Package': pkg['name'], 'Version': version, 'Architecture': pkg['architecture'],
              'Maintainer': MAINTAINER, 'Installed-Size': str(size), 'Section': 'misc', 'Priority': 'optional'}
    # A renamed package (docs/70) conflicts with and replaces its former name, without a version:
    # installing it removes the old package, and going back to a release with the old name removes it.
    former = [pkg['formerly']] if pkg.get('formerly') else []
    extra = {'conflicts': former, 'replaces': former}
    for key, name in (('depends', 'Depends'), ('recommends', 'Recommends'), ('conflicts', 'Conflicts'),
                      ('replaces', 'Replaces'), ('breaks', 'Breaks'), ('provides', 'Provides')):
        value = depends if key == 'depends' else ', '.join(v for v in [pkg.get(key)] + extra.get(key, []) if v)
        if value:
            fields[name] = value
    summary, _, body = pkg['description'].partition('\n')
    text = ''.join(f'{k}: {v}\n' for k, v in fields.items())
    text += f'Description: {summary}\n'
    for line in (body.strip() or summary).splitlines():
        text += f' {line.strip() or "."}\n'
    (root / 'DEBIAN/control').write_text(text)


def next_version(name, file_bytes=None):
    base = f"0.{git('rev-list', '--count', 'HEAD')}"
    have = rungic_release.pool_debs().get(name, {})
    if base not in have:
        return base
    if file_bytes is not None and have[base].read_bytes() == file_bytes:
        return base
    n = 1
    while f'{base}+b{n}' in have:
        n += 1
    return f'{base}+b{n}'


def record(pkg, version, deb, tree):
    data = builds()
    data[pkg['name']] = {'version': version, 'file': deb.name, 'tree': tree, 'commit': git('rev-parse', 'HEAD'),
                         'built': time.strftime('%Y-%m-%dT%H:%M:%S')}
    BUILDS.parent.mkdir(parents=True, exist_ok=True)
    BUILDS.write_text(json.dumps(data, indent=1, sort_keys=True) + '\n')


def build_host(pkg, tree, dev=None):
    """dev: {'version': ..., 'dest': Path} for a development build (tools/rungic_dev.py): the working
    tree as it is, that version, the .deb in dest; nothing goes to the release pool or its records.
    A device build's dev may have 'take': take(path on the build host, dest / name) instead of
    copying the .deb here."""
    epoch = git('log', '-1', '--format=%ct')
    work = Path(tempfile.mkdtemp(dir=WORKSPACE / '.work/cache', prefix=f"{pkg['name']}-"))
    try:
        root = work / 'root'
        (root / 'DEBIAN').mkdir(parents=True)
        env = dict(os.environ, DESTDIR=str(root), SRC=str(WORKSPACE), SOURCE_DATE_EPOCH=epoch, LC_ALL='C.UTF-8')
        if pkg.get('upstream'):
            # Use the same staged, patched inputs as device builds. Host packages must not
            # silently read an old installed/vendor tree in place of their pinned upstream.
            source = work / 'src'
            source.mkdir()
            with tarfile.open(stage_sources(pkg, worktree=bool(dev))) as archive:
                archive.extractall(source, filter='tar')
            env['SRC'] = str(source)
        subprocess.run(['sh', '-eu', str(pkg['dir'] / 'build.sh')], cwd=WORKSPACE, env=env, check=True)
        maintainer_scripts(pkg, root)
        if unit_list(pkg):
            (root / 'usr/share/rungic/units').mkdir(parents=True, exist_ok=True)
            (root / f"usr/share/rungic/units/{pkg['name']}.list").write_text(unit_list(pkg))
        for path in [root, *root.rglob('*')]:   # normalized modes and times: reproducible output
            if not path.is_symlink():
                mode = path.stat().st_mode
                path.chmod(0o755 if path.is_dir() or mode & 0o111 else 0o644)
            os.utime(path, (int(epoch), int(epoch)), follow_symlinks=False)
        tmp = work / 'out.deb'

        def pack(version):
            control(pkg, version, root)
            os.utime(root / 'DEBIAN/control', (int(epoch), int(epoch)))
            subprocess.run(['dpkg-deb', '--root-owner-group', '-Zxz', '--build', str(root), str(tmp)],
                           check=True, capture_output=True, env=env)
            return tmp.read_bytes()

        if dev:
            pack(dev['version'])
            target = dev['dest'] / f"{pkg['name']}_{dev['version']}_{pkg['architecture']}.deb"
            shutil.copy2(tmp, target)
            return target
        # 0.<commit count>, unless the pool has that version with other contents: then +bN.
        base = f"0.{git('rev-list', '--count', 'HEAD')}"
        existing = rungic_release.pool_debs().get(pkg['name'], {}).get(base)
        version = base
        if existing is not None and pack(base) != existing.read_bytes():
            version = next_version(pkg['name'])
        pack(version)
        target = rungic_release.POOL / f"{pkg['name']}_{version}_{pkg['architecture']}.deb"
        if not target.exists():
            shutil.copy2(tmp, target)
        record(pkg, version, target, tree)
        return target
    finally:
        shutil.rmtree(work)


def stage_sources(pkg, worktree=False):
    """The tracked files of the package's paths (symlinks resolved, so shared files come along) as a
    tar for the phone. Only tracked files: local build trees never reach a package build. worktree
    (development builds) adds the new files git does not ignore, so work not yet added builds too."""
    archive = WORKSPACE / f".work/cache/{pkg['name']}-src.tar"
    paths = sorted(set(pkg['paths']) | {str(pkg['dir'].relative_to(WORKSPACE))})
    extra = ['--others', '--exclude-standard'] if worktree else []
    files = subprocess.run(['git', 'ls-files', '-z', '--cached', *extra, '--', *paths], cwd=WORKSPACE,
                           capture_output=True, check=True).stdout.decode().split('\0')
    with tarfile.open(archive, 'w', dereference=True) as tar:
        for name in filter(None, files):
            if (WORKSPACE / name).exists() or (WORKSPACE / name).is_symlink():   # a deleted, unstaged file is gone
                tar.add(WORKSPACE / name, arcname=name, recursive=False)
        for name in pkg.get('upstream', []):
            import pq
            tree = pq.source(name, WORKSPACE / f".work/cache/{pkg['name']}-upstream/{name}")
            tar.add(tree, arcname=f'upstream/{name}', filter=lambda info: None if '/.pc' in info.name
                    or info.name.endswith('/.pc') else info)
    return archive


def sync_script(base, clean=False):
    """Shell that puts the tree unpacked in {base}/incoming into {base}/src for an incremental build.
    The build trees build.sh makes inside src (cmake's build/, FFmpeg's objects next to its sources)
    stay: a file whose content is the same keeps its time, so make and ninja rebuild only what
    changed; a changed file gets the time of now. A source file no longer in the tree goes (the last
    tree's list is src.manifest); what the build made is never in that list. A first build, a tree
    from before this (no manifest) or clean: src is the new tree as it is."""
    return f'''set -e
cd {base}
(cd incoming && find . ! -type d | LC_ALL=C sort) > incoming.manifest
if {'true' if clean else 'false'} || [ ! -d src ] || [ ! -f src.manifest ]; then
  rm -rf src && mv incoming src
else
  LC_ALL=C comm -23 src.manifest incoming.manifest | (cd src && tr '\\n' '\\0' | xargs -0 -r rm -f --)
  rsync -rlpD --checksum incoming/ src/
  rm -rf incoming
fi
mv incoming.manifest src.manifest
touch {build_on_device.MARKER}
'''


def build_device(pkg, tree, jobs=4, dev=None, clean=False):
    """dev: as for build_host. clean: build from nothing, not on the kept tree (sync_script)."""
    host = build_on_device.host
    jobs = build_on_device.build_jobs(jobs)
    run = lambda script, level='container', timeout=120, check=True: host.run(script, timeout, check)
    name = pkg['name']
    base = f'{DEVICE_BASE}/{name}'
    if pkg.get('image'):
        # build.sh runs in another container image (a Flatpak SDK) next to the build container,
        # sharing its build volume: both remote and local Docker can run it.
        if host.name not in ('macmini', 'local-docker'):
            raise SystemExit(f'{name}: built in {pkg["image"]}, which needs --host macmini or local-docker')
        base = f'{build_on_device.BASE}/packages/{name}'
    if pkg.get('build_depends'):
        missing = run('dpkg-query -W -f \'${db:Status-Abbrev} ${Package}\\n\' '
                      + ' '.join(pkg['build_depends']) + ' 2>&1 | grep -v "^ii" || true', 'container').stdout
        if missing.strip():
            print(f'installing build dependencies of {name} on {host.name}: {" ".join(pkg["build_depends"])}', flush=True)
            run(('apt-get update -qq; ' if host.name != 'phone' else '') +
                'DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends '
                + ' '.join(pkg['build_depends']), 'container', timeout=3600)
    build_on_device.expire()
    run(f'rm -rf {base}/incoming {base}/root', 'container', timeout=600)
    host.put_tar(stage_sources(pkg, worktree=bool(dev)), f'{base}/incoming')
    run(sync_script(base, clean), 'container', timeout=600)
    work = WORKSPACE / f'.work/cache/{name}-device'
    shutil.rmtree(work, ignore_errors=True)
    (work / 'DEBIAN').mkdir(parents=True)
    maintainer_scripts(pkg, work)          # conffiles are listed after the build, on the phone
    for script in (work / 'DEBIAN').iterdir():
        if script.name != 'conffiles':
            host.put(script, f'{base}/debian-scripts/{script.name}', '755')
    shutil.rmtree(work)
    run(f'rm -f {base}/unit.list', 'container')
    if unit_list(pkg):
        listing = WORKSPACE / f'.work/cache/{name}-unit.list'
        listing.write_text(unit_list(pkg))
        host.put(listing, f'{base}/unit.list', '644')
        listing.unlink()
    epoch = git('log', '-1', '--format=%ct')
    unit = f'rungic-package-{name}'
    build_step = f'''rm -rf "$DESTDIR"; mkdir -p "$DESTDIR/DEBIAN"
sh -eu "$SRC/{pkg['dir'].relative_to(WORKSPACE)}/build.sh"'''
    if pkg.get('image'):
        run(f'rm -rf {base}/root && mkdir -p {base}/root/DEBIAN', 'container')
        env = {'DESTDIR': f'{base}/root', 'SRC': f'{base}/src', 'SOURCE_DATE_EPOCH': epoch, 'JOBS': str(jobs),
               'HOME': '/root', 'LC_ALL': 'C.UTF-8', **host.proxy()}
        command = (f'{host.DOCKER} run --rm {host.platform_flags()}-u 0 --volumes-from {host.CONTAINER} '
                   + ''.join(f'-e {k}={shlex.quote(v)} ' for k, v in env.items())
                   + f'{pkg["image"]} nice -n 10 sh -eu {base}/src/{pkg["dir"].relative_to(WORKSPACE)}/build.sh')
        print(f'{name}: building in {pkg["image"]}', flush=True)
        result = host.ssh(f'{command} > /tmp/{name}-build.log 2>&1; echo "exit=$?"; tail -30 /tmp/{name}-build.log',
                          14500, check=False)
        output = result.stdout.decode(errors='replace')
        if 'exit=0' not in output:
            raise SystemExit(f'{name}: build in {pkg["image"]} failed\n{output[-4000:]}')
        build_step = f'# the files are in $DESTDIR, built in {pkg["image"]}'
    script = f'''set -e
cd {base}
export DESTDIR={base}/root SRC={base}/src SOURCE_DATE_EPOCH={epoch} JOBS={jobs} LC_ALL=C.UTF-8
{build_step}
cp debian-scripts/* "$DESTDIR/DEBIAN/"
if [ -f unit.list ]; then install -Dm644 unit.list "$DESTDIR/usr/share/rungic/units/{name}.list"; fi
# Debug information to {base}/dbgsym by build-id (for NAME-dbgsym), then strip (docs/61).
rm -rf {base}/dbgsym
find "$DESTDIR" -type f ! -path "$DESTDIR/DEBIAN/*" | while read -r f; do
  head -c4 "$f" | grep -q ELF || continue
  case "$(readelf -h "$f" 2>/dev/null | sed -n 's/^ *Type: *\\([A-Z]*\\).*/\\1/p')" in EXEC|DYN) ;; *) continue ;; esac
  id=$(readelf -n "$f" 2>/dev/null | sed -n 's/.*Build ID: \\([0-9a-f]*\\).*/\\1/p' | head -n1)
  if [ -n "$id" ] && readelf -S "$f" 2>/dev/null | grep -q '\\.debug_info'; then
    d={base}/dbgsym/usr/lib/debug/.build-id/$(echo "$id" | cut -c1-2)
    mkdir -p "$d"
    objcopy --only-keep-debug --compress-debug-sections "$f" "$d/$(echo "$id" | cut -c3-).debug"
  fi
  strip --strip-unneeded "$f"
done
if [ -d "$DESTDIR/etc" ]; then (cd "$DESTDIR" && find etc -type f | sort | sed 's|^|/|') > "$DESTDIR/DEBIAN/conffiles"; fi
[ -s "$DESTDIR/DEBIAN/conffiles" ] || rm -f "$DESTDIR/DEBIAN/conffiles"
# Library dependencies of the ELF files, as dpkg-shlibdeps computes them.
elves=$(find "$DESTDIR" -type f ! -path "$DESTDIR/DEBIAN/*" -exec sh -c 'head -c4 "$1" | grep -q ELF && echo "$1"' _ {{}} \\;)
rm -rf shlibs; mkdir -p shlibs/debian; printf 'Source: x\\n\\nPackage: {name}\\nArchitecture: any\\n' > shlibs/debian/control
if [ -n "$elves" ] && {'true' if pkg.get('shlibdeps', True) else 'false'}; then
  # Libraries the package ships itself (private FFmpeg, plugins) resolve inside DESTDIR.
  libdirs=$(find "$DESTDIR" -name '*.so*' ! -path "$DESTDIR/DEBIAN/*" -printf '-l%h\n' | sort -u)
  (cd shlibs && dpkg-shlibdeps -O --ignore-missing-info $libdirs $elves 2>shlibs.err | sed -n 's/^shlibs:Depends=//p') > shlibs.txt || true
  [ ! -s shlibs/shlibs.err ] || sed 's/^/shlibdeps: /' shlibs/shlibs.err | head -5
else
  : > shlibs.txt
fi
find "$DESTDIR" -newermt "@$SOURCE_DATE_EPOCH" -exec touch -h -d "@$SOURCE_DATE_EPOCH" {{}} + 2>/dev/null || true
du -sk --exclude=DEBIAN "$DESTDIR" | cut -f1 > size.txt
'''
    run(f'mkdir -p {base} && cat > {base}/build-run.sh <<\'RUNGIC_EOF\'\n{script}RUNGIC_EOF', 'container')
    if host.name == 'phone':
        command = (f'systemctl reset-failed {unit} 2>/dev/null || true\n'
                   f'systemd-run --unit={unit} --wait --pipe --collect --quiet -p TimeoutStartSec=14400 --nice=10 '
                   f'-p IOSchedulingClass=idle --setenv=HOME=/root sh -eu {base}/build-run.sh')
    else:
        command = f'HOME=/root nice -n 10 sh -eu {base}/build-run.sh'
    result = run(f'{command} > {base}/build.log 2>&1; echo "exit=$?"; tail -30 {base}/build.log',
                 'container', timeout=14500, check=False)
    if 'exit=0' not in result.stdout:
        raise SystemExit(f'{name}: build on {host.name} failed\n{result.stdout[-4000:]}{result.stderr[-1000:]}')
    shlibs = run(f'cat {base}/shlibs.txt', 'container').stdout.strip()
    # Control file with the version, built here, then the .deb on the phone.
    version = dev['version'] if dev else next_version(name)
    ctl = WORKSPACE / f'.work/cache/{name}-control'
    fake = WORKSPACE / f'.work/cache/{name}-ctlroot'
    shutil.rmtree(fake, ignore_errors=True)
    (fake / 'DEBIAN').mkdir(parents=True)
    control(pkg, version, fake, shlibs)
    size = run(f'cat {base}/size.txt', 'container').stdout.strip()
    text = (fake / 'DEBIAN/control').read_text()
    text = '\n'.join(f'Installed-Size: {size}' if l.startswith('Installed-Size:') else l
                     for l in text.splitlines()) + '\n'
    ctl.write_text(text)
    shutil.rmtree(fake)
    deb_name = f'{name}_{version}_{pkg["architecture"]}.deb'
    host.put(ctl, f'{base}/root/DEBIAN/control', '644')
    run(f'cd {base} && dpkg-deb --root-owner-group -Zxz --build root {deb_name} >/dev/null', 'container',
        timeout=1800)
    ctl.unlink()
    debs = [deb_name]
    # Debug symbols, when there are any: NAME-dbgsym at the same version, in the release repository
    # for rungic-crash-symbols; never a dependency of the release.
    dbg_name = f'{name}-dbgsym_{version}_{pkg["architecture"]}.deb'
    dbg_control = (f'Package: {name}-dbgsym\nVersion: {version}\nArchitecture: {pkg["architecture"]}\n'
                   f'Maintainer: {MAINTAINER}\nSection: debug\nPriority: optional\n'
                   f'Depends: {name} (= {version})\nDescription: debug symbols for {name}\n')
    made = run(f'''cd {base}
[ -d dbgsym/usr/lib/debug ] || exit 3
mkdir -p dbgsym/DEBIAN && printf '%s' {shlex.quote(dbg_control)} > dbgsym/DEBIAN/control
dpkg-deb --root-owner-group -Zxz --build dbgsym {dbg_name} >/dev/null''', 'container', timeout=1800, check=False)
    if made.returncode == 0:
        debs.append(dbg_name)
    dest = dev['dest'] if dev else rungic_release.POOL
    for deb in debs:
        if dev and dev.get('take'):
            dev['take'](f'{base}/{deb}', dest / deb)   # kept where it is, for the phone (rungic_dev.py)
            continue
        local = WORKSPACE / f'.work/cache/{deb}'
        host.get(f'{base}/{deb}', local)
        shutil.move(local, dest / deb)
    target = dest / deb_name
    if dev:
        return target
    record(pkg, version, target, tree)
    return target


def build(names, force=False, jobs=4, clean=False):
    defs = definitions()
    rungic_release.POOL.mkdir(parents=True, exist_ok=True)
    done = []
    for name in names:
        pkg = defs.get(name) or sys.exit(f'no package {name} in {PACKAGING}')
        tree = tree_hash(pkg)
        if not force and current(pkg):
            done.append({'package': name, 'version': builds()[name]['version'], 'built': False})
            continue
        print(f'building {name} ({pkg["build"]})', flush=True)
        deb = build_host(pkg, tree) if pkg['build'] == 'host' else build_device(pkg, tree, jobs, clean=clean)
        done.append({'package': name, 'version': builds()[name]['version'], 'built': True, 'file': deb.name})
    rungic_release.index()
    return done


def listing():
    rows = []
    recorded = builds()
    for name, pkg in definitions().items():
        try:
            state = 'current' if current(pkg) else 'stale' if name in recorded else 'never built'
        except SystemExit as error:
            state = f'uncommitted: {error}'.splitlines()[0]
        rows.append({'package': name, 'build': pkg['build'], 'architecture': pkg['architecture'],
                     'version': recorded.get(name, {}).get('version'), 'state': state})
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='cmd', required=True)
    sub.add_parser('list')
    p = sub.add_parser('build'); p.add_argument('names', nargs='*'); p.add_argument('--all', action='store_true')
    p.add_argument('--force', action='store_true'); p.add_argument('--jobs', type=int)
    p.add_argument('--clean', action='store_true', help='build from nothing, not incrementally on the kept tree')
    p.add_argument('--host', choices=sorted(build_on_device.HOSTS), default=os.environ.get('RUNGIC_BUILD_HOST', 'macmini'),
                   help='where device packages build (default $RUNGIC_BUILD_HOST, else macmini)')
    a = parser.parse_args()
    if a.cmd == 'list':
        result = listing()
    else:
        names = list(definitions()) if a.all else a.names
        if not names:
            parser.error('name packages or pass --all')
        build_on_device.use(a.host)
        result = build(names, a.force, build_on_device.build_jobs(a.jobs), a.clean)
    print(json.dumps(result, indent=1, ensure_ascii=False))


if __name__ == '__main__':
    main()
