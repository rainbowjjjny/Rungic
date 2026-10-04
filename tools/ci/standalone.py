#!/usr/bin/env python3
"""Build, verify and install an independent Rungic payload on a prepared Android base.

Developer ADB entry point, not a firmware flasher. An existing runtime is refused;
upgrade/account migration and destructive device preparation are separate operations.
Installation can resume its own incomplete release. A caller supplies the trusted
manifest digest, exact serial and ADB port. No device discovery or default handset.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import cast_payload
import build_artifact
FILES = {'rootfs.img.gz', 'host-seed.tar.gz', 'rungic.apk', 'termux.apk',
         'termux-prefix.tar.gz', 'rungic-sparse-write', 'firstboot.sh', 'service.sh',
         'boot-dispatch.sh', 'seed.env', 'device-spec.json', 'rootfs-report.json', 'host-seed-report.json',
         'kernel-report.json', 'packages.lock.tsv', 'release.json'}
REMOTE = '/data/adb/rungic-install'
APP = 'com.rungic.plasma'


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    Path(path).write_text(json.dumps(value, indent=2) + '\n')


def valid_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}', value):
        raise ValueError('invalid release id')
    return value


def verify(folder, expected=None):
    folder = Path(folder)
    manifest_file = folder / 'manifest.json'
    if expected and digest(manifest_file) != expected:
        raise ValueError('manifest digest differs from trusted input')
    m = read(manifest_file)
    if m.get('schema') not in (1, 2) or m.get('kind') != 'rungic-standalone':
        raise ValueError('unsupported standalone manifest')
    valid_id(m['release'])
    if set(m['files']) != FILES | ({'build-manifest.json'} if m['schema'] == 2 else set()):
        raise ValueError('payload file inventory mismatch')
    for name, entry in m['files'].items():
        p = folder / name
        if p.is_symlink() or not p.is_file() or p.stat().st_size != entry['bytes'] or digest(p) != entry['sha256']:
            raise ValueError(f'payload verification failed: {name}')
    if m['schema'] == 2:
        validate_build_manifest(read(folder / 'build-manifest.json'), m['files'])
    return m


def validate_build_manifest(manifest, files):
    if manifest.get('schema') != 1 or not manifest.get('components'):
        raise ValueError('missing component build fingerprints')
    for record in manifest['components'].values():
        build_artifact.validate_record(record)
    fingerprints = {record['input_sha256']: record for record in manifest['components'].values()}
    for record in manifest['components'].values():
        for dependency in record['inputs']['dependencies'].values():
            if 'input_sha256' in dependency:
                parent = fingerprints.get(dependency['input_sha256'])
                if parent is None or not any(output['sha256'] == dependency.get('file_sha256') for output in parent['outputs'].values()):
                    raise ValueError('component dependency build is missing or mismatched')
    bindings = manifest.get('bindings', {})
    if set(bindings) != {'rootfs.img.gz', 'host-seed.tar.gz', 'rungic.apk', 'rungic-sparse-write'}:
        raise ValueError('missing primary artifact build bindings')
    for name, binding in bindings.items():
        record = manifest['components'][binding['component']]
        artifact = record['outputs'][binding['output']]
        if artifact != files[name]:
            raise ValueError(f'build record does not match payload: {name}')


def collect_build_manifest(plan_file, files):
    """Verify each expected recipe before packaging; archive portable records."""
    plan = read(plan_file)
    records = {}
    for name, entry in plan['components'].items():
        expected = build_artifact.inputs(read(entry['recipe']))
        record = build_artifact.verify(entry['directory'], expected)
        if record['component'] != name:
            raise ValueError('component name differs from build plan')
        records[name] = record
    result = {'schema': 1, 'components': records, 'bindings': plan['bindings'],
              'binary_inputs_policy': 'Pinned binary dependencies are recorded as inputs, not source cache hits.'}
    validate_build_manifest(result, files)
    return result


def validate_cast_host(host):
    build = host.get('cast_build')
    if not isinstance(build, dict) or build.get('schema') != 1 or build.get('inputs') != cast_payload.build_inputs():
        raise ValueError('host casting build is missing or stale; rebuild the host seed')
    if not build.get('jar_sha256') or build['jar_sha256'] != host.get('cast_jar_sha256'):
        raise ValueError('host casting jar does not match its build provenance')


def pack(args):
    out = args.output
    if out.exists():
        raise ValueError('output already exists; releases are immutable')
    valid_id(args.release_id)
    root = read(args.rootfs_report)
    host = read(args.host_report)
    kernel = read(args.kernel_report)
    spec = read(args.spec)
    release = read(args.package_release)
    if not all(root.get(k) is True for k in ('home_layout_checked', 'fresh_account_checked', 'preinstalled_apps_checked')):
        raise ValueError('rootfs lacks required build checks')
    if root['arch'] != 'arm64' or root['account_status_protocol'] != 2 or root['filesystem_check'] != 0:
        raise ValueError('rootfs architecture/protocol/filesystem check failed')
    if not host.get('home_layout_checked') or not host.get('fresh_account_checked') or host['arch'] != 'aarch64':
        raise ValueError('host seed is not a fresh ARM64 runtime')
    validate_cast_host(host)
    for file, expected in ((args.rootfs_gz, root['compressed_sha256']),
                           (args.host_seed, host['archive_sha256']),
                           (args.package_lock, root['package_lock_sha256']),
                           (args.package_release, root['release_sha256'])):
        if digest(file) != expected:
            raise ValueError(f'report does not match input: {file}')
    if root['release_version'] != release['version']:
        raise ValueError('package release mismatch')
    sources = {'rootfs.img.gz': args.rootfs_gz, 'host-seed.tar.gz': args.host_seed,
               'rungic.apk': args.apk, 'termux.apk': args.deps / 'termux.apk',
               'termux-prefix.tar.gz': args.deps / 'termux-prefix.tar.gz',
               'rungic-sparse-write': args.deps / 'rungic-sparse-write',
               'firstboot.sh': HERE / 'rungic-firstboot.sh',
               'service.sh': HERE / 'rungic-install-service.sh',
               'boot-dispatch.sh': HERE / 'rungic-install-boot-dispatch.sh',
               'device-spec.json': args.spec, 'rootfs-report.json': args.rootfs_report,
               'host-seed-report.json': args.host_report, 'kernel-report.json': args.kernel_report,
               'packages.lock.tsv': args.package_lock, 'release.json': args.package_release}
    build_manifest = collect_build_manifest(args.build_plan, {name: {'sha256': digest(path), 'bytes': path.stat().st_size} for name, path in sources.items()})
    out.mkdir(parents=True)
    write(out / 'build-manifest.json', build_manifest)
    for name, path in sources.items():
        subprocess.run(['cp', '--reflink=auto', str(path), str(out / name)], check=True)
    env = {'RELEASE_ID': args.release_id, 'HOST_SEED_SHA256': digest(out / 'host-seed.tar.gz'),
           'ROOTFS_GZ_SHA256': root['compressed_sha256'], 'ROOTFS_SHA256': root['rootfs_sha256'],
           'ROOTFS_BYTES': str(root['rootfs_bytes']), 'TERMUX_APK_SHA256': digest(out / 'termux.apk'),
           'TERMUX_PREFIX_SHA256': digest(out / 'termux-prefix.tar.gz'),
           'RUNGIC_APK_SHA256': digest(out / 'rungic.apk'),
           'SPARSE_WRITE_SHA256': digest(out / 'rungic-sparse-write'),
           'PHONE_HTTP_PROXY': spec['deployment']['phone_http_proxy']}
    (out / 'seed.env').write_text(''.join(f'{k}={shlex.quote(v)}\n' for k, v in env.items()))
    m = {'schema': 2, 'kind': 'rungic-standalone', 'release': args.release_id,
         'source_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
         'source_dirty': bool(subprocess.check_output(['git', 'diff', '--name-only'], text=True).strip()),
         'fingerprint': spec['identity']['fingerprint'], 'product': spec['identity']['product'],
         'kernel_release': kernel['kernel_banner'].split('Linux version ', 1)[1].split(' ', 1)[0],
         'boot_sha256': kernel['boot_sha256'], 'boot_bytes': kernel['boot_bytes'],
         'rootfs_bytes': root['rootfs_bytes'], 'minimum_battery_percent': spec['release_requirements']['minimum_battery_percent'],
         'package_release': root['release_version'],
         'files': {n: {'bytes': (out / n).stat().st_size, 'sha256': digest(out / n)} for n in sorted(FILES | {'build-manifest.json'})}}
    write(out / 'manifest.json', m)
    verify(out)
    print(json.dumps({'folder': str(out), 'manifest_sha256': digest(out / 'manifest.json')}))


class Device:
    def __init__(self, args):
        self.adb = [args.adb, '-P', str(args.adb_port), '-s', args.serial]

    def shell(self, script, root=False, timeout=120):
        return subprocess.check_output(self.adb + ['shell'] + (['su', '-c', 'sh'] if root else ['sh']),
                                       input=('set -eu\n' + script + '\n').encode(), timeout=timeout).decode().strip()

    def push(self, local, remote):
        subprocess.run(self.adb + ['push', str(local), remote], check=True, timeout=1800)


def preflight(device, m):
    actual = device.shell('id -u; getprop ro.build.fingerprint; getprop ro.product.device; uname -r; getenforce; getprop ro.boot.slot_suffix; cat /sys/class/power_supply/battery/capacity', root=True).splitlines()
    if len(actual) != 7 or actual[:5] != ['0', m['fingerprint'], m['product'], m['kernel_release'], 'Enforcing']:
        raise ValueError(f'prepared base does not match payload: {actual}')
    slot = actual[5]
    if slot not in ('_a', '_b') or int(actual[6]) < m['minimum_battery_percent']:
        raise ValueError('unsupported slot or insufficient battery')
    size = m['boot_bytes']
    if not isinstance(size, int) or not 0 < size <= 128 * 1024**2 or size % 4096:
        raise ValueError('invalid boot report size')
    boot = device.shell(f'dd if=/dev/block/by-name/boot{slot} bs=4096 count={size // 4096} 2>/dev/null | sha256sum', root=True).split()[0]
    if boot != m['boot_sha256']:
        raise ValueError('running base boot image differs from verified candidate')
    return {'fingerprint': actual[1], 'kernel': actual[3], 'selinux': actual[4], 'slot': slot, 'boot_sha256': boot}


def install(args):
    folder = args.payload.resolve()
    m = verify(folder, args.manifest_sha256)
    d = Device(args)
    evidence = preflight(d, m)
    rid = m['release']
    # Refuse replacement, even when it would fit: a full update needs data migration.
    d.shell(f'''if [ -f {REMOTE}/active.env ]; then
        grep -qxF {shlex.quote('RELEASE_ID=' + rid)} {REMOTE}/active.env
        test "$(cat {REMOTE}/manifest.sha256)" = {shlex.quote(args.manifest_sha256)}
    else
        test ! -e /data/adb/rungic-lxc
        test ! -e /data/adb/rungic-plasma
    fi
    test -x /data/adb/magisk/busybox
    df -k /data | tail -n 1 | awk -v need={m['rootfs_bytes'] // 1024 + 4 * 1024**2} '{{if ($4 < need) exit 1}}'
    ''', root=True)
    stage = '/data/local/tmp/rungic-' + rid
    d.shell(f'mkdir -p {stage}')
    # Every file the verified manifest lists (schema 2 adds build-manifest.json), as checked below.
    for name in sorted(set(m['files']) | {'manifest.json'}):
        d.push(folder / name, stage + '/' + name)
    checks = '\n'.join(f"echo {shlex.quote(v['sha256'] + '  ' + stage + '/' + n)} | sha256sum -c -" for n, v in m['files'].items())
    d.shell(checks, root=True, timeout=300)
    # Android package operations must run as shell, not Magisk's SELinux domain.
    if not d.shell('pm path com.termux || true'):
        print(d.shell(f'pm install -r {stage}/termux.apk', timeout=180), flush=True)
    print(d.shell(f'pm install -r {stage}/rungic.apk', timeout=180), flush=True)
    permissions = ('RECORD_AUDIO', 'CAMERA', 'POST_NOTIFICATIONS', 'BLUETOOTH_CONNECT', 'BLUETOOTH_SCAN', 'READ_PHONE_STATE')
    for permission in permissions:
        d.shell(f'pm grant {APP} android.permission.{permission} || true')
    d.shell(f'appops set {APP} SYSTEM_ALERT_WINDOW allow')
    # Root-owned durable descriptor and payload, published before the controller exists.
    d.shell(f'''mkdir -p {REMOTE}/payload
        chmod 700 {REMOTE} {REMOTE}/payload
        cp -a {stage}/. {REMOTE}/payload/
        chown -R 0:0 {REMOTE}
        {checks.replace(stage + '/', REMOTE + '/payload/')}
        chmod 700 {REMOTE}/payload/rungic-sparse-write
        # Old init_boot rewrites service.d from product on every boot. Magisk's
        # documented product overlay redirects that immutable caller as well.
        if [ -f /product/etc/rungic/firstboot.sh ]; then
            legacy=/data/adb/rungic-install-legacy
            mkdir -p "$legacy"
            chmod 700 "$legacy"
            if [ ! -f "$legacy/firstboot.sh" ]; then
                cp /product/etc/rungic/firstboot.sh "$legacy/firstboot.sh"
                chmod 700 "$legacy/firstboot.sh"
            fi
            mod=/data/adb/modules/rungic-install-compat
            mkdir -p "$mod/system/product/etc/rungic"
            printf '%s\\n' 'id=rungic-install-compat' 'name=Rungic independent installer compatibility' 'version=1' 'versionCode=1' 'author=Rungic' 'description=Route legacy product seeding to the selected independent release.' > "$mod/module.prop"
            cp {REMOTE}/payload/boot-dispatch.sh "$mod/system/product/etc/rungic/firstboot.sh"
            chmod 755 "$mod" "$mod/system" "$mod/system/product" "$mod/system/product/etc" "$mod/system/product/etc/rungic" "$mod/system/product/etc/rungic/firstboot.sh"
            chmod 644 "$mod/module.prop"
            chcon -R u:object_r:system_file:s0 "$mod/system"
            test ! -e "$mod/disable"
            test ! -e "$mod/remove"
        fi
        echo {shlex.quote(args.manifest_sha256)} > {REMOTE}/manifest.sha256
        echo RELEASE_ID={rid} > {REMOTE}/active.env.tmp
        mv {REMOTE}/active.env.tmp {REMOTE}/active.env
        files=/data/user/0/{APP}/files
        mkdir -p "$files"
        owner=$(stat -c %u /data/user/0/{APP})
        case "$owner" in ''|*[!0-9]*) exit 1 ;; esac
        test "$owner" -ge 10000
        # The verified APK starts shared root bridges when its loading UI opens.
        # Grant before publishing the entry point, not only at seed completion.
        # Fixed Magisk 31 schema: INSERT returns no SQL NULL (docs/39).
        /debug_ramdisk/magisk --sqlite "INSERT OR REPLACE INTO policies (uid,policy,until,logging,notification) VALUES($owner,2,0,1,1)"
        label=$(ls -dZ /data/user/0/{APP} | cut -d ' ' -f1)
        echo RELEASE_ID={rid} > "$files/rungic-install-source.properties.tmp"
        chown "$owner:$owner" "$files" "$files/rungic-install-source.properties.tmp"
        chmod 600 "$files/rungic-install-source.properties.tmp"
        chcon "$label" "$files" "$files/rungic-install-source.properties.tmp"
        mv "$files/rungic-install-source.properties.tmp" "$files/rungic-install-source.properties"
        mkdir -p /data/adb/service.d
        cp {REMOTE}/payload/service.sh /data/adb/service.d/00-rungic-firstboot.sh
        chmod 700 /data/adb/service.d/00-rungic-firstboot.sh
        sync
        ''', root=True)
    print('Payload verified; starting independent installation.', flush=True)
    # Device process survives a host disconnect; the service entry retries after reboot.
    d.shell(f'''/data/adb/magisk/busybox setsid /system/bin/sh {REMOTE}/payload/firstboot.sh {REMOTE}/payload </dev/null >/data/adb/rungic-install-launch.log 2>&1 &''', root=True)
    print(json.dumps({'state': 'installing', 'release': rid, 'base': evidence,
                      'status_command': 'standalone.py status with the same --serial and --adb-port'}))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command', required=True)
    b = sub.add_parser('pack')
    for name in ('spec', 'rootfs-gz', 'rootfs-report', 'host-seed', 'host-report', 'kernel-report', 'package-lock', 'package-release', 'apk', 'deps', 'output'):
        b.add_argument('--' + name, type=Path, required=True)
    b.add_argument('--release-id', required=True)
    b.add_argument('--build-plan', type=Path, required=True, help='expected component recipes, cache directories and primary artifact bindings')
    v = sub.add_parser('verify'); v.add_argument('payload', type=Path)
    for action in ('install', 'status'):
        d = sub.add_parser(action)
        d.add_argument('--adb', default='adb')
        d.add_argument('--adb-port', type=int, required=True)
        d.add_argument('--serial', required=True)
        if action == 'install':
            d.add_argument('payload', type=Path)
            d.add_argument('--manifest-sha256', required=True)
    args = p.parse_args()
    if args.command == 'pack': pack(args)
    elif args.command == 'verify':
        m = verify(args.payload); print(json.dumps({'verified': True, 'release': m['release'], 'manifest_sha256': digest(args.payload / 'manifest.json')}))
    elif args.command == 'install': install(args)
    else:
        print(Device(args).shell(f'cat /data/user/0/{APP}/files/rungic-install.properties; tail -n 12 /data/adb/rungic-firstboot.log', root=True))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        sys.exit(f'standalone installation: {error}')
