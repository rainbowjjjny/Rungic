#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Build the CI2 arm64 root TREE from scratch, inside a privileged native Linux container.

Inputs (no binary baseline, QEMU, Android runtime or personal home is consumed):
* --repo: materialized, flat local APT pool from build_on_device.py collect and
  rungic_release.py index/build: Packages, Release (Origin/Label rungic), .debs
  for every rebuilt/project package, rungic-release and Mozilla Firefox. Remote
  placeholder records are insufficient. Index SHA256s are checked before use.
* --release: the JSON emitted by rungic_release.py build, including exact versions;
  it must match the metapackage's installed /usr/share/rungic/release.json.
* release/packages.json: selects rebuilt + project + coupled names. Changelog
  and project-build versions have already been resolved by rungic_release.py;
  bootstrap uses the immutable release JSON, not guessed versions or a phone.
* system/ubuntu-packages.txt: additional runtime selection (NOT build-packages or
  the build host's manual selection); ubuntu-excluded-packages.txt: omitted apps.
* --mirror: Ubuntu resolute/{updates,security}, all four components; default TUNA
  ports mirror. Ubuntu signatures use the container's ubuntu archive keyring.
* --firefox-version: exact non-snap Mozilla package, present in the local pool.
* --mozilla-keyring: defaults to the tracked system/config/etc/apt/keyrings/
  packages.mozilla.org.asc (docs/40, fingerprint
  35BAA0B33E9EB396F59CA838C0BA5CE6DC6315A3). No Mozilla network access during
  bootstrap; rungic-plasma-config ships the runtime source, pin AND public key.
  This input verifies the installed key, rather than replacing package files.
* --source-date-epoch: fixed nonnegative build epoch; --output: nonexistent Linux
  filesystem directory. Root privileges, native aarch64, mmdebstrap, apt,
  dpkg-deb, chroot and mount namespaces are required; bootstrap_rootfs.Dockerfile
  provides them. Wrapper supplies read-only inputs and Linux named-volume output.
* Optional --package-lock: previous packages.lock.json (all installed versions,
  architectures and .deb SHA256s). Replay pins the entire closure at 1001 and
  rejects changed bytes, added/missing packages or unavailable versions.
* Tracked system/config/etc/dpkg/.../zz-rungic-apps, system/policy-rc.d and this
  checkout are setup-hook inputs. All other tracked configuration is installed
  through release packages (packaging/rungic-*/); it is not overlaid by bootstrap.

Baseline responsibilities inferred from code: create rungic UID/GID 1000,
/home/rungic and Ubuntu's /etc/skel, locked root/template passwords; generate
zh_CN.UTF-8 for desktop/session; prepare var/log/plasma before system/init;
clear build machine/SSH identity and Android DNS placeholder, as the image
builder does. Shared home directories, audio cookie, SELinux label and account
markers belong to CI3/account preparation and must NOT be fabricated here.
SSH socket/service defaults and session units come from package maintainer
scripts; socket enablement is checked, never disabled. No passwordless sudo,
linger, user settings, arbitrary kernel or display-manager policy is added.

Outputs: root/ accepted by build_rootfs_image.py and build_host_seed.py;
packages.lock.{json,tsv}, retained debs/ for replay, bootstrap-report.json, and
the same lock/report under root/usr/share/rungic/build. The report binds input
hashes and tool versions. A first resolve against a rolling archive records the
closure; for repeatability retain its .debs or an immutable archive and replay
the lock. Fixed epoch + locked package bytes/configuration do not establish
byte-identical maintainer-script output or ext4 images (docs/94).

Open questions requiring real container/CI3 acceptance: the absent historical
baseline may have contained undocumented local defaults or extra manual apps;
none can be recovered from Git. Does the selected release's clean dependency
closure pass rungic-clicker's pip check (docs/80), first account preparation,
Plasma/llvmpipe and all existing hardware paths? The community kit invocation
is corroborating context only: its raw scripts could not be fetched in this
session, so its account/configuration work is not treated as verified source.
No Docker build, package install, image or device acceptance is claimed here.

Research: mmdebstrap 1.5.7 (MIT), Debian trixie manual, setup/customize hooks,
file-mirror-automount and SOURCE_DATE_EPOCH:
https://manpages.debian.org/trixie/mmdebstrap/mmdebstrap.1.en.html
Community reference (not copied): yayoinoyume/Rungic dev/munch-cgroup-fix,
munch-build-kit/rootfs-scripts/{bootstrap.sh,populate.sh}. Ubuntu package licenses
remain in usr/share/doc/*/copyright; this tool does not infer binary provenance.
"""

import argparse
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

from build_rootfs_image import (check_fresh_account, check_home_layout,
                                check_preinstalled_apps, packages, sha256,
                                write_release_preferences)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MIRROR = 'http://mirrors.tuna.tsinghua.edu.cn/ubuntu-ports'
MOZILLA_KEYRING = ROOT / 'system/config/etc/apt/keyrings/packages.mozilla.org.asc'
NAME = re.compile(r'[a-z0-9][a-z0-9+.-]+')
VERSION = re.compile(r'[0-9][A-Za-z0-9.+:~\-]*')


def checked_name(value):
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise ValueError('invalid package name')
    return value


def checked_version(value):
    if not isinstance(value, str) or not VERSION.fullmatch(value):
        raise ValueError('invalid package version')
    return value


def package_list(path):
    return sorted({checked_name(line.split('#', 1)[0].strip())
                   for line in path.read_text().splitlines() if line.split('#', 1)[0].strip()})


def release_names(spec):
    names = list(spec.get('project', {})) + list(spec.get('coupled', []))
    for component in spec.get('rebuilt', {}).values():
        names += component['packages']
    return {checked_name(name) for name in names}


def control_stanzas(text):
    for stanza in text.strip().split('\n\n'):
        fields = {}
        key = None
        for line in stanza.splitlines():
            if line[:1].isspace() and key:
                fields[key] += '\n' + line[1:]
            elif ': ' in line:
                key, value = line.split(': ', 1)
                fields[key] = value
        if fields:
            yield fields


def validate_repository(repo):
    release = dict(line.split(': ', 1) for line in (repo / 'Release').read_text().splitlines()
                   if ': ' in line and not line.startswith(' '))
    if release.get('Origin') != 'rungic' or release.get('Label') != 'rungic':
        raise ValueError('repository Release must have Origin and Label rungic')
    entries, hashes = {}, {}
    for entry in control_stanzas((repo / 'Packages').read_text()):
        name, version = checked_name(entry['Package']), checked_version(entry['Version'])
        arch = entry['Architecture']
        # Ignore foreign builds, but never use one for the requested release.
        if arch not in ('arm64', 'all'):
            continue
        filename = Path(entry['Filename'])
        if filename.is_absolute() or '..' in filename.parts or filename.suffix != '.deb':
            raise ValueError('repository filename escapes pool or is not a deb')
        path = repo / filename
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(repo):
            raise ValueError('repository deb is missing, linked or remote-only')
        digest = sha256(path)
        if digest != entry.get('SHA256'):
            raise ValueError('repository SHA256 mismatch: ' + str(filename))
        identity = (name, version, arch)
        if identity in entries:
            raise ValueError('duplicate package identity in repository')
        entries[identity] = str(path)
        hashes[str(filename)] = digest
    for name in ('Packages', 'Release'):
        hashes[name] = sha256(repo / name)
    return entries, hashes


def validate_inputs(repo, release, output, firefox_version, mozilla_keyring=MOZILLA_KEYRING,
                    spec=ROOT / 'release/packages.json', runtime=ROOT / 'system/ubuntu-packages.txt',
                    excluded=ROOT / 'system/ubuntu-excluded-packages.txt', mirror=DEFAULT_MIRROR,
                    package_lock=None):
    # lexists catches broken links too, before resolve follows one away from output.
    if os.path.lexists(output):
        raise ValueError('output exists; choose a new run directory')
    output = output.resolve()
    paths = {name: Path(value).resolve(strict=True) for name, value in
             dict(repo=repo, release=release, spec=spec, runtime=runtime,
                  excluded=excluded, mozilla_keyring=mozilla_keyring).items()}
    if not paths['repo'].is_dir() or output.is_relative_to(paths['repo']):
        raise ValueError('output must be outside the repository pool')
    for name, path in paths.items():
        if name != 'repo' and not path.is_file():
            raise ValueError('input must be a file: ' + name)
    uri = urlsplit(mirror)
    if (uri.scheme not in ('http', 'https') or not uri.netloc or uri.query or uri.fragment
            or uri.username or re.search(r'[\s\[\]"\'\\]', mirror)):
        raise ValueError('mirror must be a plain HTTP(S) archive URL')
    manifest = json.loads(paths['release'].read_text())
    checked_version(manifest['version'])
    pins = {checked_name(n): checked_version(v) for n, v in manifest['packages'].items()}
    selection = json.loads(paths['spec'].read_text())
    if set(pins) != release_names(selection):
        raise ValueError('release package selection differs from release/packages.json')
    pins.update({'rungic-release': manifest['version'], 'firefox': checked_version(firefox_version)})
    omitted = package_list(paths['excluded'])
    # ubuntu-keyring makes the archive source usable inside the completed tree.
    requested = sorted(set(package_list(paths['runtime'])) | {'ubuntu-keyring'} | set(pins))
    if set(requested).intersection(omitted):
        raise ValueError('requested release/runtime contains excluded packages')
    entries, hashes = validate_repository(paths['repo'])
    local = set(selection.get('project', {})) | {'rungic-release', 'firefox'}
    for component in selection.get('rebuilt', {}).values():
        local.update(component['packages'])
    for name in sorted(local):
        if not any((name, pins[name], arch) in entries for arch in ('arm64', 'all')):
            raise ValueError('local arm64/all package missing: ' + name + '=' + pins[name])
    locked = None
    if package_lock is not None:
        lock = json.loads(Path(package_lock).read_text())
        if lock.get('schema') != 1 or not isinstance(lock.get('packages'), dict) or not lock['packages']:
            raise ValueError('invalid package lock')
        locked = lock['packages']
        for name, record in locked.items():
            checked_name(name)
            checked_version(record['version'])
            if record['architecture'] not in ('all', 'arm64') or not re.fullmatch(r'[0-9a-f]{64}', record['sha256']):
                raise ValueError('invalid architecture/hash in lock')
        if (set(requested) - set(locked) or set(locked).intersection(omitted)
                or any(locked[n]['version'] != v for n, v in pins.items() if n in locked)):
            raise ValueError('package lock does not match release/runtime selection')
        pins.update({name: record['version'] for name, record in locked.items()})
        requested = sorted(locked)
    setup_inputs = [ROOT / 'system/policy-rc.d',
                    ROOT / 'system/config/etc/dpkg/dpkg.cfg.d/zz-rungic-apps',
                    Path(__file__).resolve()]
    input_hashes = {n: sha256(p) for n, p in paths.items() if n != 'repo'}
    input_hashes.update({str(p.relative_to(ROOT)): sha256(p) for p in setup_inputs})
    if package_lock is not None:
        input_hashes['package_lock'] = sha256(Path(package_lock))
    return dict(paths={n: str(p) for n, p in paths.items()}, output=str(output),
                manifest=manifest, pins=pins, excluded=omitted, locked=locked,
                include=[n + '=' + pins[n] if n in pins else n for n in requested],
                mirror=mirror.rstrip('/'), repository_sha256=hashes,
                inputs_sha256=input_hashes)


def preferences(pins, version):
    lines = ['# Rungic image release ' + version]
    for name, value in sorted(pins.items()):
        lines += ['', f'Package: {name}', f'Pin: version {value}', 'Pin-Priority: 1001']
    return '\n'.join(lines) + '\n'


def exclusion_preferences(excluded):
    return ''.join(f'Package: {name}\nPin: version *\nPin-Priority: -1\n\n' for name in excluded)


def sources(mirror, repo):
    archive = '\n'.join(f'deb [arch=arm64 signed-by=/usr/share/keyrings/ubuntu-archive-keyring.gpg] '
                        f'{mirror} {suite} main universe restricted multiverse'
                        for suite in ('resolute', 'resolute-updates', 'resolute-security'))
    return archive + f'\ndeb [trusted=yes] {repo.as_uri()} ./\n'


def build_environment(host, epoch):
    env = {k: v for k, v in host.items() if not k.startswith('PYTHON')}
    env.update(HOME='/root', PATH='/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
               DEBIAN_FRONTEND='noninteractive', LC_ALL='C.UTF-8', SOURCE_DATE_EPOCH=str(epoch))
    return env


def mmdebstrap_command(plan, plan_file, epoch):
    hook = shlex.join([sys.executable, str(Path(__file__).resolve()), '--hook-plan', str(plan_file)])
    return ['mmdebstrap', '--mode=root', '--architectures=arm64', '--variant=minbase',
            '--format=directory', '--aptopt=APT::Install-Recommends "false"',
            '--skip=essential/unlink',
            '--hook-dir=/usr/share/mmdebstrap/hooks/file-mirror-automount',
            '--setup-hook=' + hook + ' --hook setup "$1"',
            '--customize-hook=' + hook + ' --hook customize "$1"',
            '--include=' + ','.join(plan['include']), 'resolute',
            str(Path(plan['output']) / 'root'), str(plan_file.with_suffix('.list'))]


def write(root, relative, data, mode=0o644):
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    # /etc/resolv.conf and dbus machine-id may be absolute links in Ubuntu.
    if target.is_symlink():
        target.unlink()
    target.write_text(data)
    target.chmod(mode)


def setup(root, plan):
    write(root, 'etc/apt/preferences.d/rungic-release',
          preferences(plan['pins'], plan['manifest']['version']))
    # This is a build-time omission, not a permanent ban on user-installed apps.
    write(root, 'etc/apt/preferences.d/zz-bootstrap-excluded', exclusion_preferences(plan['excluded']))
    write(root, 'etc/dpkg/dpkg.cfg.d/zz-rungic-apps',
          (ROOT / 'system/config/etc/dpkg/dpkg.cfg.d/zz-rungic-apps').read_text())
    write(root, 'usr/sbin/policy-rc.d', (ROOT / 'system/policy-rc.d').read_text(), 0o755)


def check_installed(installed, pins, excluded, closure=None):
    if set(installed).intersection(excluded):
        raise ValueError('installed closure contains excluded packages')
    if any(installed.get(n, (None,))[0] != v for n, v in pins.items()):
        raise ValueError('installed packages differ from exact release/lock versions')
    if any(arch not in ('arm64', 'all') for _, arch in installed.values()):
        raise ValueError('installed closure is not arm64/all')
    if closure is not None and set(installed) != set(closure):
        raise ValueError('installed closure differs from package lock')


def chroot(root, *command):
    result = subprocess.run(['chroot', str(root), *command], text=True,
                            capture_output=True, env=build_environment(os.environ, os.environ['SOURCE_DATE_EPOCH']))
    if result.returncode:
        raise RuntimeError(f'chroot check failed: {shlex.join(command)}\n'
                           + result.stdout[-2000:] + result.stderr[-2000:])
    return result


def collect_artifacts(root, plan, installed):
    """Require an actual .deb for every installed identity, not just version strings."""
    candidates = list((root / 'var/cache/apt/archives').glob('*.deb'))
    candidates += list(Path(plan['paths']['repo']).glob('*.deb'))
    found = {}
    dest = Path(plan['output']) / 'debs'
    dest.mkdir()
    for deb in sorted(candidates):
        fields = subprocess.run(['dpkg-deb', '-f', str(deb), 'Package', 'Version', 'Architecture'],
                                check=True, capture_output=True, text=True).stdout
        data = next(control_stanzas(fields))
        name, version, arch = data['Package'], data['Version'], data['Architecture']
        if installed.get(name) != (version, arch):
            continue
        digest = sha256(deb)
        record = dict(version=version, architecture=arch, sha256=digest)
        if name in found and found[name] != record:
            raise ValueError('different deb bytes for installed identity: ' + name)
        if plan['locked'] is not None and plan['locked'].get(name) != record:
            raise ValueError('downloaded deb differs from package lock: ' + name)
        found[name] = record
        target = dest / f'{name}_{version.replace(":", "%3a")}_{arch}.deb'
        if not target.exists():
            shutil.copyfile(deb, target)
    if set(installed) != set(found):
        raise ValueError('deb artifacts missing for installed closure: ' + ', '.join(sorted(set(installed) - set(found))))
    # Retain a self-contained APT pool too, so archive GC cannot break lock replay.
    index = subprocess.run(['apt-ftparchive', 'packages', '.'], cwd=dest, check=True,
                           capture_output=True).stdout
    (dest / 'Packages').write_bytes(index)
    release = subprocess.run(['apt-ftparchive', '-o', 'APT::FTPArchive::Release::Origin=rungic',
                              '-o', 'APT::FTPArchive::Release::Label=rungic', 'release', '.'],
                             cwd=dest, check=True, capture_output=True).stdout
    (dest / 'Release').write_bytes(release)
    return {'schema': 1, 'packages': dict(sorted(found.items()))}


def customize(root, plan):
    installed = packages(root / 'var/lib/dpkg/status')
    check_installed(installed, plan['pins'], plan['excluded'], plan['locked'])
    check_preinstalled_apps(root, installed)
    embedded = json.loads((root / 'usr/share/rungic/release.json').read_text())
    if embedded != plan['manifest']:
        raise ValueError('metapackage release.json differs from supplied manifest')
    if (root / 'usr/share/rungic/account-protocol').read_text().strip() != '2':
        raise ValueError('release needs account protocol 2')
    # No previous template should be present in a from-scratch minbase tree.
    accounts = [line.split(':') for line in (root / 'etc/passwd').read_text().splitlines()]
    if any(1000 <= int(a[2]) < 65534 for a in accounts):
        raise ValueError('package created an unexpected regular account')
    chroot(root, '/usr/sbin/groupadd', '--gid', '1000', 'rungic')
    chroot(root, '/usr/sbin/useradd', '--uid', '1000', '--gid', '1000', '--create-home',
           '--home-dir', '/home/rungic', '--shell', '/bin/bash', 'rungic')
    for name in ('root', 'rungic'):
        chroot(root, '/usr/sbin/usermod', '--lock', name)
        chroot(root, '/usr/bin/chage', '--lastday', str(int(os.environ['SOURCE_DATE_EPOCH']) // 86400), name)
    write(root, 'etc/locale.gen', 'zh_CN.UTF-8 UTF-8\n')
    chroot(root, '/usr/sbin/locale-gen')
    chroot(root, '/usr/sbin/update-locale', 'LANG=zh_CN.UTF-8')
    chroot(root, '/usr/bin/apt-get', 'check')
    if chroot(root, '/usr/bin/dpkg', '--audit').stdout.strip():
        raise ValueError('dpkg --audit reported incomplete packages')
    # The project currently ships one venv (packaging/rungic-cua/build.sh).
    chroot(root, '/usr/lib/rungic-clicker/venv/bin/python', '-m', 'pip', 'check')
    chroot(root, '/usr/bin/systemctl', 'is-enabled', 'ssh.socket')
    lock = collect_artifacts(root, plan, installed)
    # Package scripts own final service policy; only builder identities are removed.
    for path in (root / 'etc/ssh').glob('ssh_host_*_key*'):
        path.unlink()
    for name in ('etc/machine-id', 'var/lib/dbus/machine-id'):
        write(root, name, '')
    write(root, 'etc/hostname', 'rungic\n')  # system/plasma.config lxc.uts.name
    write(root, 'etc/resolv.conf', '# Set from Android network on first boot\n')
    # The installed config's source points to the CI3 bind, never this build pool.
    (root / 'etc/apt/sources.list').unlink(missing_ok=True)
    write(root, 'etc/apt/sources.list.d/ubuntu.sources',
          'Types: deb\nURIs: ' + plan['mirror'] + '\nSuites: resolute resolute-updates resolute-security\n'
          'Components: main universe restricted multiverse\nArchitectures: arm64\n'
          'Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n')
    key = root / 'etc/apt/keyrings/packages.mozilla.org.asc'
    if not key.is_file() or sha256(key) != plan['inputs_sha256']['mozilla_keyring']:
        raise ValueError('installed Mozilla public key differs from configured input')
    (root / 'etc/apt/preferences.d/zz-bootstrap-excluded').unlink()
    # A replay's full-closure pins are build-only; runtime follows image/deploy policy.
    write_release_preferences(root, plan['manifest'])
    for name, mode in [('var/log/plasma', 0o755), ('var/lib/rungic-host', 0o755),
                       ('var/lib/rungic-apt', 0o755), ('tmp', 0o1777), ('var/tmp', 0o1777)]:
        path = root / name
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(mode)
    check_home_layout(root, '/home/rungic')
    check_fresh_account(root)
    home = root / 'home/rungic'
    if (home.stat().st_uid, home.stat().st_gid) != (1000, 1000):
        raise ValueError('template home owner must be 1000:1000')
    lock_text = json.dumps(lock, indent=2) + '\n'
    tsv = ''.join(f'{name}\t{version}\t{arch}\n' for name, (version, arch) in sorted(installed.items()))
    for name, data in [('packages.lock.json', lock_text), ('packages.lock.tsv', tsv)]:
        (Path(plan['output']) / name).write_text(data)
        write(root, 'usr/share/rungic/build/' + name, data)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hook-plan', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--hook', choices=('setup', 'customize'), help=argparse.SUPPRESS)
    parser.add_argument('hook_root', nargs='?', type=Path, help=argparse.SUPPRESS)
    for name in ('repo', 'release', 'output', 'package-lock'):
        parser.add_argument('--' + name, type=Path)
    parser.add_argument('--mozilla-keyring', type=Path, default=MOZILLA_KEYRING)
    parser.add_argument('--mirror', default=DEFAULT_MIRROR)
    parser.add_argument('--firefox-version')
    parser.add_argument('--source-date-epoch', type=int)
    args = parser.parse_args(argv)
    if args.hook:
        plan = json.loads(args.hook_plan.read_text())
        return (setup if args.hook == 'setup' else customize)(args.hook_root, plan)
    if any(getattr(args, n) is None for n in ('repo', 'release', 'output', 'mozilla_keyring',
                                             'firefox_version', 'source_date_epoch')):
        parser.error('repo, release, output, firefox-version and source-date-epoch are required')
    if args.source_date_epoch < 0:
        raise ValueError('source-date-epoch must be nonnegative')
    if platform.system() != 'Linux' or platform.machine() not in ('aarch64', 'arm64') or os.geteuid() != 0:
        raise ValueError('run inside a privileged native arm64 Linux container as root')
    os.umask(0o022)
    plan = validate_inputs(args.repo, args.release, args.output, args.firefox_version,
                           args.mozilla_keyring, mirror=args.mirror, package_lock=args.package_lock)
    keyinfo = subprocess.run(['gpg', '--batch', '--with-colons', '--show-keys',
                              plan['paths']['mozilla_keyring']], check=True,
                             capture_output=True, text=True).stdout
    fingerprints = [line.split(':')[9] for line in keyinfo.splitlines() if line.startswith('fpr:')]
    if not fingerprints or fingerprints[0] != '35BAA0B33E9EB396F59CA838C0BA5CE6DC6315A3':
        raise ValueError('Mozilla archive signing key fingerprint mismatch')
    output = Path(plan['output'])
    output.mkdir(parents=True, exist_ok=False)
    # Reserved output is intentionally retained on failure; never resume or overwrite it.
    env = build_environment(os.environ, args.source_date_epoch)
    with tempfile.TemporaryDirectory(prefix='rungic-bootstrap-') as work:
        plan_file = Path(work) / 'plan.json'
        plan_file.write_text(json.dumps(plan))
        plan_file.with_suffix('.list').write_text(sources(plan['mirror'], Path(plan['paths']['repo'])))
        command = mmdebstrap_command(plan, plan_file, args.source_date_epoch)
        print(shlex.join(command), flush=True)
        subprocess.run(command, check=True, env=env)
    # Check the pool did not change while apt was reading it.
    if validate_repository(Path(plan['paths']['repo']))[1] != plan['repository_sha256']:
        raise ValueError('repository changed during bootstrap')
    tool_versions = {}
    for name in ('mmdebstrap', 'dpkg-deb'):
        result = subprocess.run([name, '--version'], check=True, capture_output=True, text=True)
        tool_versions[name] = result.stdout + result.stderr
    report = dict(schema=1, arch='arm64', release=plan['manifest']['version'],
                  source_date_epoch=args.source_date_epoch, mirror=plan['mirror'],
                  inputs_sha256=plan['inputs_sha256'], repository_sha256=plan['repository_sha256'],
                  package_lock_sha256=sha256(output / 'packages.lock.json'),
                  replay=args.package_lock is not None, account_protocol=2,
                  checks=['apt-get check', 'dpkg --audit', 'pip check', 'ssh.socket enabled',
                          'fresh account', 'home ownership/layout', 'preinstalled exclusions'],
                  tool_versions=tool_versions,
                  scope='Locked package bytes and configuration; not byte-identical filesystem or device acceptance')
    data = json.dumps(report, indent=2) + '\n'
    (output / 'bootstrap-report.json').write_text(data)
    write(output / 'root', 'usr/share/rungic/build/bootstrap-report.json', data)
    print(data)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.CalledProcessError) as error:
        sys.exit(f'rootfs bootstrap failed: {error}')
