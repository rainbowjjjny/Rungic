#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Build husky's four CI3 artifacts on the ARM64 Mac, then write --build-plan.

This is the Mac pipeline used on 2026-10-04, not the historical Podman/binary
baseline pipeline. The existing rungic-rootfs volume supplies the COMPLETE deb
pool and packages.lock.json from bootstrap_rootfs_docker.py. A rootfs recipe
replays bootstrap_rootfs.py with that pool/lock, then build_rootfs_image.py
--inside. It never imports an existing rootfs.img.gz or host-seed.tar.gz.

Input policy follows build_artifact.py: tracked source/patch/recipe trees are
content-hashed, including modes, links and xattrs, without host ownership or
mtime. Tool installations (NDK, stable Rust, JDK, SDK build-tools/platform jar)
are content-hashed rather than identified by a version string. libxkbcommon.so
is a pinned binary input from Ubuntu libxkbcommon 1.13.1, with its Meson cross
file recorded; its historical compile is not presented as a source cache hit.
The Alpine 3.22.6/LXC 6.0.4-r0 runtime has the known SHA256 below. The flat APT
repository is a binary tree input; package original source builds are outside
this plan. pq recipes/patches fix Android upstream sources; cargo --locked
checks Rust dependencies as in the existing native script. pq's patch-tool
image is also inspected and recorded, with its tag checked before/after native
source preparation. Native linking points the wrappers at the record's JNI
directory so a legacy .work/deps/android-link-libs cannot become a hidden input.

Mac cannot stat Linux volume ownership. A read-only Docker probe therefore
uses build_artifact.identity(..., ownership=True) for the deb pool/lock and
stores their identities as a dependency JSON. The recipe checks them again
before AND after bootstrap. The newly generated image/root-tree (with /dev,
proc, caches etc excluded by the existing image builder) is hashed in Linux
with ownership=True. Host seed consumes precisely that checked tree, through
rootfs build-record dependencies, and verifies its identity before/after use.
No large tree crosses macOS's shared filesystem. Docker runs by inspected
immutable image ID (sha256), recorded along with the full Docker argv. Only
recipe commands export newly built files into build_artifact's pending record
directory. Failed Linux work is retained; no global cleanup or device writes.

Records establish local input/output consistency, not signed provenance or
byte-identical filesystem rebuilds. Cached Docker builds still validate live
volume inputs before the plan is published. The plan is published only after
all four recipes and standalone.collect_build_manifest have verified it.
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

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools'))
import build_artifact as artifact
from bootstrap_rootfs import DEFAULT_MIRROR
from bootstrap_rootfs_docker import proxy_environment
from build_host_seed import ANDROID_FILES

RUNTIME_SHA256 = '1401a42399462cef33db09b7ce47542c35aaa747faeb185bd0309ca3a541bba4'
BINDINGS = {'husky-rootfs': 'rootfs.img.gz', 'husky-host': 'host-seed.tar.gz',
            'husky-apk': 'rungic.apk', 'husky-sparse': 'rungic-sparse-write'}
SELF = 'tools/ci/husky_build_plan.py'


def write(path, data):
    Path(path).write_text(json.dumps(data, indent=2) + '\n')


def run(command, **kwargs):
    print(shlex.join(command), flush=True)
    return subprocess.run(command, check=True, **kwargs)


def validate(c):
    for key in ('release', 'volume'):
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', c[key]):
            raise ValueError('invalid ' + key)
    if not re.fullmatch(r'/output/[A-Za-z0-9][A-Za-z0-9_.-]*', c['pool']):
        raise ValueError('pool must be a run directory directly under /output')
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', c['image']):
        raise ValueError('container must have an immutable sha256 image ID')
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', c['pq_image']):
        raise ValueError('pq container must have an immutable sha256 image ID')
    if c['epoch'] < 0 or not 8 <= c['size_gib'] <= 128:
        raise ValueError('invalid epoch or image size')
    # Docker --mount cannot represent commas in source paths.
    if any(',' in str(value) for key, value in c.items()
           if key in ('release_file', 'apt_repo', 'runtime', 'snapshot', 'root_record')) or ',' in str(ROOT):
        raise ValueError('Docker mount paths cannot contain commas')


def docker_command(c, kind, mounts=(), readonly=False):
    command = [c['docker'], 'run', '--rm', '--platform', 'linux/arm64']
    if not readonly:
        command += ['--privileged']
    command += ['--mount', 'type=bind,src={repo},dst=/src,readonly', '--mount',
                'type=volume,src=' + c['volume'] + ',dst=/output' + (',readonly' if readonly else '')]
    for src, dest, ro in mounts:
        command += ['--mount', f'type=bind,src={src},dst={dest}' + (',readonly' if ro else '')]
    for key, value in sorted(c['proxy'].items()):
        command += ['--env', key + '=' + value]
    container_config = {key: c[key] for key in ('release', 'firefox', 'epoch', 'volume',
                        'pool', 'image', 'size_gib', 'mirror', 'proxy')}
    command += [c['image'], 'python3', '/src/' + SELF, '_inside', '--kind', kind,
                '--config', json.dumps(container_config, sort_keys=True), '--work', '{work}']
    return command


def recipes(c):
    """Pure recipe generation; dependency fingerprinting is done by build_artifact."""
    validate(c)
    common = [SELF, 'tools/build_artifact.py', 'tools/ci/bootstrap_rootfs_docker.py',
              'tools/ci/bootstrap_rootfs.py', 'tools/ci/build_rootfs_image.py',
              'tools/ci/build_host_seed.py', 'tools/cast_payload.py']
    def base(name, sources, outputs, dependencies, tools, env=None, steps=None):
        runner_config = {'bash': c['bash'], 'docker': c['docker'],
                         'pq_image': c.get('pq_image'), 'docker_commands': steps or []}
        return dict(schema=1, component=name, target='aarch64-linux-android31' if name != 'husky-rootfs' else 'arm64-ubuntu-26.04',
                    sources=sorted(set(common + sources)), outputs=outputs,
                    dependencies=dependencies, tools={'python': {'path': str(Path(sys.executable).resolve())},
                        **{'host-' + key: {'path': path} for key, path in c.get('host_tools', {}).items()}, **tools},
                    environment=env or {}, parameters={'release': c['release'], 'docker_commands': steps or []},
                    command=[sys.executable, '{repo}/' + SELF, '_run', '--kind', name,
                             '--config', json.dumps(runner_config, sort_keys=True), '--output', '{output}'])
    ndk = {'ndk': {'path': c['ndk']}}
    java = {'jdk': {'path': c['jdk']}, 'sdk-build-tools': {'path': c['build_tools']},
            'android-api36': {'path': c['android_jar']}, 'bash': {'path': c['bash']}}
    docker = {'docker': {'path': c['docker']}, 'container': {'identity': c['image']}}
    native_env = dict(ANDROID_HOME=c['sdk'], JAVA_HOME=c['jdk'],
        PATH=c['rust'] + '/bin:' + c['jdk'] + '/bin:' + c['path'],
        RUNGIC_ANDROID_CLANG=c['ndk'] + '/bin/clang', RUNGIC_ANDROID_CLANGXX=c['ndk'] + '/bin/clang++',
        RUNGIC_ANDROID_SYSROOT=c['ndk'] + '/sysroot', RUNGIC_ANDROID_AR=c['ndk'] + '/bin/llvm-ar',
        RUNGIC_ANDROID_BUILD_TOOLS=c['build_tools'], RUNGIC_ANDROID_JAR=c['android_jar'])
    root_mounts = [('{input:release}', '/inputs/release.json', True),
                   ('{input:pool_identity}', '/inputs/pool.json', True), ('{output}', '/record', False)]
    root_steps = [docker_command(c, 'rootfs', root_mounts)]
    root = base('husky-rootfs', ['release/packages.json', 'system/ubuntu-packages.txt',
        'system/ubuntu-excluded-packages.txt', 'system/policy-rc.d',
        'system/config/etc/dpkg/dpkg.cfg.d/zz-rungic-apps',
        'system/config/etc/apt/keyrings/packages.mozilla.org.asc', 'tools/ci/bootstrap_rootfs.Dockerfile'],
        ['rootfs.img.gz', 'rootfs-report.json', 'packages.lock.tsv', 'packages.lock.json',
         'bootstrap-report.json', 'root-tree.identity.json', 'root-location.json'],
        {'release': {'path': c['release_file'], 'origin': 'rungic-release manifest'},
         'pool_identity': {'path': c['snapshot'], 'origin': 'Linux ownership/content hashes of complete bootstrap deb pool and lock'}},
        docker, steps=root_steps)
    root_record = c.get('root_record', '{root_record}')
    host_deps = {'runtime': {'path': c['runtime'], 'sha256': RUNTIME_SHA256,
        'origin': 'Alpine 3.22.6 minirootfs + apk add lxc 6.0.4-r0'},
        'apt_repo': {'path': c['apt_repo'], 'origin': 'materialized Rungic binary APT repository'}}
    for name, output in [('root_location', 'root-location.json'), ('root_identity', 'root-tree.identity.json')]:
        host_deps[name] = {'path': root_record + '/' + output, 'build': root_record + '/build.json'}
    host_mounts = [('{input:runtime}', '/inputs/runtime.tar.gz', True),
        ('{input:apt_repo}', '/pool', True), ('{input:root_location}', '/inputs/root-location.json', True),
        ('{input:root_identity}', '/inputs/root-tree.identity.json', True), ('{output}', '/record', False)]
    host_steps = [docker_command(c, 'host', host_mounts)]
    host = base('husky-host', [source for source, _, _ in ANDROID_FILES] +
        ['tools/build_enter.sh', 'tools/rungic_lxc_enter.c', 'tools/rungic_plasma_enter.c',
         'shared/android/rungic-cast/'], ['host-seed.tar.gz', 'host-seed-report.json'],
        host_deps, {**docker, **ndk, **java}, native_env, host_steps)
    apk_env = dict(native_env, RUNGIC_PROXY='', RUNGIC_APK_OCR='none',
        RUNGIC_VIRGL_PYTHON=sys.executable,
        RUNGIC_NATIVE_OUT='{output}/native/lib/arm64-v8a', RUNGIC_NATIVE_LIBS='{output}/native',
        RUNGIC_JNI_LIBS_DIR='{output}/native/lib/arm64-v8a', CARGO_TARGET_DIR='{output}/cargo',
        RUNGIC_XKBCOMMON_LIB='{input:xkb}', RUNGIC_APK_OUT='{output}/apk',
        RUNGIC_APK_KEYSTORE='{repo}/signing/development/launcher-signing.p12',
        # Prevent the wrappers from reading an undeclared legacy binary directory.
        RUNGIC_ANDROID_LINK_LIBS='{output}/native/lib/arm64-v8a',
        RUSTUP_TOOLCHAIN='stable-aarch64-apple-darwin')
    apk = base('husky-apk', ['android/app/', 'android/host/', 'android/build-native-core.sh',
        'android/build-apk.sh', 'android/build-virgl-server.sh', 'packages/virglrenderer-android/',
        'tools/prepare_virgl_server.py', 'packages/android-host/', 'packages/smithay/', 'packages/winit/',
        'tools/prepare_android_host.py', 'tools/pq.py', 'tools/toolchains/',
        'signing/development/launcher-signing.p12'], ['rungic.apk'],
        {'xkb': {'path': c['xkb'], 'sha256': c['xkb_sha256'], 'origin': 'Ubuntu libxkbcommon 1.13.1 / NDK 28.2 Meson binary'},
         'xkb_cross': {'path': c['xkb_cross'], 'origin': 'libxkbcommon Android arm64 Meson cross file'}},
        {**ndk, **java, 'rust': {'path': c['rust']}, 'docker': {'path': c['docker']},
         'pq-container': {'identity': c['pq_image']}}, apk_env)
    sparse = base('husky-sparse', ['tools/ci/sparse_write.c'], ['rungic-sparse-write'], {}, ndk)
    sparse['command'] = [c['ndk'] + '/bin/clang', '--target=aarch64-linux-android31',
        '--sysroot=' + c['ndk'] + '/sysroot', '-static', '-s', '-O2', '-Wall', '-Wextra', '-Werror',
        '{repo}/tools/ci/sparse_write.c', '-o', '{output}/rungic-sparse-write']
    sparse['sources'] = ['tools/ci/sparse_write.c']
    sparse['tools'] = ndk
    return {r['component']: r for r in (root, host, apk, sparse)}


def build_plan(entries):
    if set(entries) != set(BINDINGS):
        raise ValueError('build plan requires exactly four component records')
    return {'schema': 1, 'components': entries,
            'bindings': {output: {'component': name, 'output': output} for name, output in BINDINGS.items()}}


def pool_identity(c):
    return {name: artifact.identity(Path(c['pool']) / name, ownership=True)
            for name in ('debs', 'packages.lock.json')}


def check_identity(actual, path):
    if actual != json.loads(Path(path).read_text()):
        raise ValueError('Linux volume inputs changed: ' + str(path))


def inside(kind, c, work):
    """Executed by the recipe's Docker command, with read-only input mounts."""
    if kind == 'snapshot':
        print(json.dumps(pool_identity(c), sort_keys=True))
        return
    os.umask(0o022)
    # Probe the executor, as required by AGENTS.md, before any build command.
    run(['sh', '-ec', 'hostname; uname -m; cat /proc/net/route; env | sort | sed -n "/_proxy=/p"'])
    if kind == 'rootfs':
        check_identity(pool_identity(c), '/inputs/pool.json')
        run(['python3', '/src/tools/ci/bootstrap_rootfs.py', '--repo', c['pool'] + '/debs',
            '--release', '/inputs/release.json', '--package-lock', c['pool'] + '/packages.lock.json',
            '--firefox-version', c['firefox'], '--source-date-epoch', str(c['epoch']),
            '--mirror', c['mirror'], '--output', str(work)])
        run(['python3', '/src/tools/ci/build_rootfs_image.py', '--inside', '--root', str(work / 'root'),
            '--release', '/inputs/release.json', '--output', str(work / 'image/rootfs.img'),
            '--size-gib', str(c['size_gib']), '--firefox-version', c['firefox']])
        check_identity(pool_identity(c), '/inputs/pool.json')
        tree = work / 'image/root-tree'
        write('/record/root-tree.identity.json', artifact.identity(tree, ownership=True))
        write('/record/root-location.json', {'volume': c['volume'], 'tree': str(tree)})
        for name in ('rootfs.img.gz', 'rootfs-report.json', 'packages.lock.tsv'):
            shutil.copyfile(work / 'image' / name, '/record/' + name)
        for name in ('packages.lock.json', 'bootstrap-report.json'):
            shutil.copyfile(work / name, '/record/' + name)
    elif kind == 'host':
        location = json.loads(Path('/inputs/root-location.json').read_text())
        if location['volume'] != c['volume']:
            raise ValueError('rootfs build belongs to a different volume')
        tree = Path(location['tree'])
        check_identity(artifact.identity(tree, ownership=True), '/inputs/root-tree.identity.json')
        work.mkdir(exist_ok=False)
        runtime = work / 'runtime'
        runtime.mkdir()
        run(['tar', '-xzf', '/inputs/runtime.tar.gz', '--numeric-owner', '-C', str(runtime)])
        run(['python3', '/src/tools/ci/build_host_seed.py', '--inside', '--runtime', str(runtime),
            '--rootfs-tree', str(tree), '--repo', '/pool',
            '--lxc-enter', '/record/enter/rungic-lxc-enter',
            '--plasma-enter', '/record/enter/rungic-plasma-enter',
            '--cast-jar', '/record/cast/rungic-cast.jar', '--output', str(work / 'seed/host-seed.tar.gz')])
        check_identity(artifact.identity(tree, ownership=True), '/inputs/root-tree.identity.json')
        for name in ('host-seed.tar.gz', 'host-seed-report.json'):
            shutil.copyfile(work / 'seed' / name, '/record/' + name)
    else:
        raise ValueError('unknown container operation')


def execute_command(kind, c, output):
    if kind == 'husky-apk':
        def check_pq():
            actual = run([c['docker'], 'image', 'inspect', '--format', '{{.Id}}', 'rungic-pq:26.04'],
                         capture_output=True, text=True).stdout.strip()
            if actual != c['pq_image']:
                raise ValueError('pq tool image changed during native build')
        check_pq()
        run(['bash', str(ROOT / 'android/build-native-core.sh')])
        check_pq()
        run([c['bash'], str(ROOT / 'android/build-apk.sh')])
        apks = list((output / 'apk').glob('Rungic-*.apk'))
        if len(apks) != 1:
            raise ValueError('APK script did not produce exactly one versioned APK')
        shutil.copyfile(apks[0], output / 'rungic.apk')
    elif kind in ('husky-rootfs', 'husky-host'):
        if kind == 'husky-host':
            for name in ('lxc', 'plasma'):
                run(['sh', str(ROOT / 'tools/build_enter.sh'), name],
                    env=dict(os.environ, RUNGIC_ENTER_OUT=str(output / 'enter')))
            run(['bash', str(ROOT / 'shared/android/rungic-cast/build.sh')],
                env=dict(os.environ, RUNGIC_CAST_OUT=str(output / 'cast')))
        replacements = {'{repo}': str(ROOT), '{output}': str(output),
                        '{work}': '/output/husky-build-' + output.name}
        # build_artifact already expanded {repo}/{output}/{input:*} in this JSON.
        for template in c['docker_commands']:
            command = []
            for arg in template:
                for key, value in replacements.items():
                    arg = arg.replace(key, value)
                command.append(arg)
            run(command)
    else:
        raise ValueError('unknown build operation')


def snapshot(c):
    command = docker_command(c, 'snapshot', readonly=True)
    command = [arg.replace('{repo}', str(ROOT)).replace('{work}', '/unused') for arg in command]
    return json.loads(run(command, capture_output=True, text=True).stdout)


def build(c, output, cache):
    """Execute recipes sequentially and publish a plan only after pack's checks."""
    entries = {}
    for name in ('husky-sparse', 'husky-apk', 'husky-rootfs', 'husky-host'):
        recipe = recipes(c)[name]
        rp = output / (name + '.recipe.json')
        write(rp, recipe)
        result = artifact.execute(recipe, cache)
        directory = result['directory']
        artifact.verify(directory, artifact.inputs(recipe))
        entries[name] = {'recipe': str(rp), 'directory': directory}
        if name == 'husky-rootfs':
            c = dict(c, root_record=directory)
    # Recheck live Linux inputs even if build_artifact reused all file records.
    check_identity(snapshot(c), c['snapshot'])
    location = json.loads((Path(c['root_record']) / 'root-location.json').read_text())
    probe = docker_command(c, 'snapshot', readonly=True)
    # The pool snapshot and root identity have distinct scopes.
    probe = probe[:probe.index(c['image']) + 1] + ['python3', '-c',
        'import sys,json; sys.path.insert(0,"/src/tools"); import build_artifact as a; '
        'print(json.dumps(a.identity(sys.argv[1],ownership=True)))', location['tree']]
    probe = [arg.replace('{repo}', str(ROOT)) for arg in probe]
    actual = json.loads(run(probe, capture_output=True, text=True).stdout)
    check_identity(actual, Path(c['root_record']) / 'root-tree.identity.json')
    plan_file = output / 'build-plan.pending.json'
    write(plan_file, build_plan(entries))
    import standalone
    files = {binding: artifact.verify(entries[name]['directory'])['outputs'][binding]
             for name, binding in BINDINGS.items()}
    standalone.collect_build_manifest(plan_file, files)
    target = output / 'build-plan.json'
    plan_file.replace(target)
    print('Build plan:', target)
    for name, binding in BINDINGS.items():
        print(binding + ':', Path(entries[name]['directory']) / binding)
    return target


def main():
    # Private recipe/container operations need no Docker discovery or host SDK.
    if len(sys.argv) > 1 and sys.argv[1] in ('_run', '_inside'):
        p = argparse.ArgumentParser()
        p.add_argument('operation')
        p.add_argument('--kind', required=True)
        p.add_argument('--config', required=True)
        p.add_argument('--output', type=Path)
        p.add_argument('--work', type=Path)
        args = p.parse_args()
        c = json.loads(args.config)
        if args.operation == '_run':
            execute_command(args.kind, c, args.output)
        else:
            inside(args.kind, c, args.work)
        return
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--release', required=True)
    p.add_argument('--firefox-version', default='156.0.1~build1')
    p.add_argument('--source-date-epoch', required=True, type=int)
    p.add_argument('--input-root', type=Path, default=ROOT, help='checkout holding .work binary inputs')
    p.add_argument('--output', type=Path, help='new recipe/plan directory (must not exist)')
    p.add_argument('--cache', type=Path, default=ROOT / '.work/build-cache')
    p.add_argument('--volume', default='rungic-rootfs')
    p.add_argument('--pool-run', help='existing bootstrap run name; default husky-RELEASE')
    p.add_argument('--image', default='rungic-rootfs-bootstrap:26.04')
    p.add_argument('--size-gib', type=int, default=16)
    p.add_argument('--mirror', default=DEFAULT_MIRROR)
    args = p.parse_args()
    if platform.system() != 'Darwin' or platform.machine() != 'arm64':
        p.error('this pipeline requires the native ARM64 Mac build host')
    run(['hostname'])
    run(['uname', '-m'])
    run(['route', '-n', 'get', 'default'])
    proxy = proxy_environment(run(['scutil', '--proxy'], capture_output=True, text=True).stdout)
    info = run(['docker', 'info', '--format', '{{.OSType}}/{{.Architecture}}'],
               capture_output=True, text=True).stdout.strip()
    if info not in ('linux/aarch64', 'linux/arm64'):
        p.error('Docker must run native Linux arm64: ' + info)
    image = run(['docker', 'image', 'inspect', '--format', '{{.Id}}', args.image],
                capture_output=True, text=True).stdout.strip()
    pq_image = run(['docker', 'image', 'inspect', '--format', '{{.Id}}', 'rungic-pq:26.04'],
                   capture_output=True, text=True).stdout.strip()
    host_home = Path.home()
    sdk = host_home / 'Library/Android/sdk'
    inputs = args.input_root.resolve()
    output = (args.output or ROOT / '.work/husky' / ('build-plan-' + args.release)).resolve()
    for path in (output, args.cache.resolve()):
        if not path.is_relative_to(ROOT / '.work') or path == ROOT / '.work':
            p.error('output and cache must be inside this checkout\'s .work directory')
    xkb = inputs / '.work/deps/libxkbcommon/build-android/libxkbcommon.so'
    c = dict(release=args.release, firefox=args.firefox_version, epoch=args.source_date_epoch,
        volume=args.volume, pool='/output/' + (args.pool_run or 'husky-' + args.release), image=image,
        size_gib=args.size_gib, mirror=args.mirror, proxy=proxy, sdk=str(sdk),
        ndk=str(sdk / 'ndk/28.2.13676358/toolchains/llvm/prebuilt/darwin-x86_64'),
        rust=str(host_home / '.rustup/toolchains/stable-aarch64-apple-darwin'),
        jdk=run(['/usr/libexec/java_home'], capture_output=True, text=True).stdout.strip(),
        build_tools=str(sorted((sdk / 'build-tools').iterdir(), key=lambda x: [int(v) for v in re.findall(r'\d+', x.name)])[-1]),
        android_jar=str(sdk / 'platforms/android-36/android.jar'), path=os.environ['PATH'],
        docker=str(Path(shutil.which('docker')).resolve()), bash='/opt/homebrew/bin/bash',
        release_file=str(inputs / '.work/apt/releases' / (args.release + '.json')),
        apt_repo=str(inputs / '.work/apt/repo'), xkb=str(xkb), xkb_sha256=artifact.file_hash(xkb),
        xkb_cross=str(inputs / '.work/deps/libxkbcommon/android-arm64.ini'),
        runtime=str(inputs / '.work/husky/alpine/alpine-lxc-runtime.tar.gz'),
        snapshot=str(output / 'pool.identity.json'), pq_image=pq_image,
        host_tools={name: str(Path(shutil.which(name)).resolve())
                    for name in ('bash', 'sh', 'zip', 'sed', 'find', 'git', 'patch', 'meson', 'ninja', 'pkg-config')},
        )
    # rustup is wherever this machine installed it (~/.cargo/bin, Homebrew, ...).
    rustup = shutil.which('rustup')
    if rustup is None:
        p.error('rustup not found on PATH')
    c['host_tools']['rustup'] = str(Path(rustup).resolve())
    validate(c)
    if json.loads(Path(c['release_file']).read_text())['version'] != args.release:
        p.error('release manifest version differs from --release')
    if artifact.file_hash(c['runtime']) != RUNTIME_SHA256:
        p.error('pinned Alpine LXC runtime SHA256 mismatch')
    Path(c['xkb_cross']).resolve(strict=True)
    Path(c['apt_repo']).resolve(strict=True)
    output.mkdir(parents=True, exist_ok=False)
    write(c['snapshot'], snapshot(c))
    build(c, output, args.cache.resolve())


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        sys.exit(f'husky build plan failed: {error}')
