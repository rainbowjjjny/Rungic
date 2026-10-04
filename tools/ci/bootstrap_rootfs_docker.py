#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Thin native-arm64 Docker launcher for bootstrap_rootfs.py.

Reads macOS system proxies before network use; loopback proxies become
host.docker.internal. Prints exact Docker build/run commands. --print-only
prints without reaching Docker. Output lives in a Linux named volume, so
subsequent build_rootfs_image.py --inside and build_host_seed.py --inside runs
must mount that same volume; macOS shared folders do not preserve rootfs metadata.
No package build, device write, automatic image packing or global cleanup.
"""
import argparse
import platform
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile

from bootstrap_rootfs import DEFAULT_MIRROR, MOZILLA_KEYRING, ROOT, validate_inputs


def proxy_environment(text):
    values = dict(re.findall(r'(\w+)\s*:\s*(\S+)', text))
    env = {}
    for scheme, key in [('http', 'HTTP'), ('https', 'HTTPS')]:
        if values.get(key + 'Enable') == '1':
            host = values[key + 'Proxy']
            if host in ('127.0.0.1', 'localhost', '::1'):
                host = 'host.docker.internal'
            env[scheme + '_proxy'] = f'http://{host}:{values[key + "Port"]}'
    return env


def run_command(repo, release, mozilla_keyring, firefox_version, output_name, volume,
                image, mirror, epoch, proxy, package_lock=None):
    cmd = ['docker', 'run', '--rm', '--platform', 'linux/arm64', '--privileged',
           '--mount', f'type=bind,src={ROOT},dst=/src,readonly',
           '--mount', f'type=bind,src={repo},dst=/pool,readonly',
           '--mount', f'type=bind,src={release},dst=/inputs/release.json,readonly',
           '--mount', f'type=bind,src={mozilla_keyring},dst=/inputs/mozilla.asc,readonly',
           '--mount', f'type=volume,src={volume},dst=/output']
    if package_lock:
        cmd += ['--mount', f'type=bind,src={package_lock},dst=/inputs/packages.lock.json,readonly']
    for key, value in sorted(proxy.items()):
        cmd += ['--env', key + '=' + value]
    cmd += [image, 'python3', '/src/tools/ci/bootstrap_rootfs.py',
            '--repo', '/pool', '--release', '/inputs/release.json',
            '--mozilla-keyring', '/inputs/mozilla.asc', '--firefox-version', firefox_version,
            '--output', '/output/' + output_name, '--mirror', mirror,
            '--source-date-epoch', str(epoch)]
    if package_lock:
        cmd += ['--package-lock', '/inputs/packages.lock.json']
    return cmd


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('repo', 'release'):
        p.add_argument('--' + name, required=True, type=Path)
    p.add_argument('--mozilla-keyring', type=Path, default=MOZILLA_KEYRING)
    p.add_argument('--package-lock', type=Path)
    p.add_argument('--firefox-version', required=True)
    p.add_argument('--source-date-epoch', required=True, type=int)
    p.add_argument('--output-name', required=True)
    p.add_argument('--volume', default='rungic-rootfs')
    p.add_argument('--image', default='rungic-rootfs-bootstrap:26.04')
    p.add_argument('--mirror', default=DEFAULT_MIRROR)
    p.add_argument('--print-only', action='store_true')
    args = p.parse_args()
    if not all(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', v)
               for v in (args.output_name, args.volume)):
        p.error('output-name and volume must be simple names, without slashes')
    paths = [args.repo, args.release, args.mozilla_keyring]
    if args.package_lock:
        paths.append(args.package_lock)
    if any(',' in str(path.resolve()) for path in paths + [ROOT]):
        p.error('Docker --mount paths cannot contain commas')
    repo, release, keyring = [path.resolve(strict=True) for path in paths[:3]]
    lock = args.package_lock.resolve(strict=True) if args.package_lock else None
    if not repo.is_dir() or not release.is_file() or not keyring.is_file():
        p.error('repo must be a directory; release and mozilla-keyring must be files')
    if args.source_date_epoch < 0:
        p.error('source-date-epoch must be nonnegative')
    with tempfile.TemporaryDirectory(prefix='rungic-bootstrap-validate-') as work:
        validate_inputs(repo, release, Path(work) / 'output', args.firefox_version,
                        keyring, mirror=args.mirror, package_lock=lock)
    # Site-specific settings are queried now, not copied from historical AGENTS records.
    print('Host:', subprocess.run(['hostname'], capture_output=True, text=True, check=True).stdout.strip(),
          'Architecture:', platform.machine(), flush=True)
    if platform.system() == 'Darwin':
        route = subprocess.run(['route', '-n', 'get', 'default'], capture_output=True, text=True)
        print(route.stdout or route.stderr, flush=True)
        proxy = proxy_environment(subprocess.run(['scutil', '--proxy'], check=True,
                                                 capture_output=True, text=True).stdout)
    else:
        subprocess.run(['ip', 'route'], check=True)
        # A Linux caller supplies its host proxy through the ordinary environment.
        import os
        proxy = {k: os.environ[k] for k in ('http_proxy', 'https_proxy', 'no_proxy') if k in os.environ}
    build = ['docker', 'build', '--platform', 'linux/arm64', '-t', args.image,
             '--build-arg', 'UBUNTU_MIRROR=' + args.mirror]
    for k, v in sorted(proxy.items()):
        build += ['--build-arg', k + '=' + v]
    build += ['-f', str(ROOT / 'tools/ci/bootstrap_rootfs.Dockerfile'), str(ROOT / 'tools/ci')]
    run = run_command(repo, release, keyring, args.firefox_version, args.output_name,
                      args.volume, args.image, args.mirror, args.source_date_epoch, proxy, lock)
    for command in (build, run):
        print(shlex.join(command), flush=True)
    print(f'Linux root tree: volume {args.volume}, /output/{args.output_name}/root', flush=True)
    if args.print_only:
        return
    info = subprocess.run(['docker', 'info', '--format', '{{.OSType}}/{{.Architecture}}'],
                          check=True, capture_output=True, text=True).stdout.strip()
    if info not in ('linux/aarch64', 'linux/arm64'):
        raise ValueError('Docker server must be native Linux arm64; found ' + info)
    subprocess.run(build, check=True)
    # Identity, route and inherited proxy are also checked on the Linux executor.
    probe = ['docker', 'run', '--rm', '--platform', 'linux/arm64']
    for k, v in sorted(proxy.items()):
        probe += ['--env', k + '=' + v]
    subprocess.run(probe + [args.image, 'sh', '-ec',
                   'hostname; uname -m; cat /proc/net/route; env | sort | sed -n "/_proxy=/p"'], check=True)
    subprocess.run(run, check=True)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        sys.exit(f'Docker rootfs bootstrap failed: {error}')
