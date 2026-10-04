#!/usr/bin/env python3
"""Replay the virgl Android queue and its pinned epoxy without Docker or Termux.

Use pq.py prepare/export virglrenderer-android to maintain upstream patches. This
NDK build replays the same series with the host patch utility (no Debian tools).
Inputs use pq's SHA-256 cache. --offline refuses missing or corrupt inputs before
extracting anything. Generated sources and build output stay in .work.
"""
import argparse
import shutil
import subprocess
import tarfile
from pathlib import Path

import pq

NAME = 'virglrenderer-android'


def unpack(archive, output):
    staging = output.with_name(output.name + '.unpack')
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    with tarfile.open(archive) as tar:
        tar.extractall(staging, filter='data')
    tops = list(staging.iterdir())
    if len(tops) != 1 or not tops[0].is_dir():
        raise SystemExit(f'{archive.name}: expected one source directory')
    tops[0].rename(output)
    staging.rmdir()


def prepare(output=None, offline=False):
    output = Path(output or pq.WORKSPACE / '.work/build/virgl-server/source').resolve()
    work = (pq.WORKSPACE / '.work').resolve()
    if output == work or not output.is_relative_to(work):
        raise SystemExit('virgl sources must be inside .work')
    info = pq.recipe(NAME)
    cache = pq.SOURCES / NAME
    if offline:
        for name, want in info['files'].items():
            path = cache / name
            if not path.is_file() or pq.sha256(path) != want:
                raise SystemExit(f'{name}: missing or incorrect sha256 (offline)')
    else:
        cache = pq.fetch(NAME)
    shutil.rmtree(output, ignore_errors=True)
    unpack(cache / info['tarball'], output)
    unpack(cache / info['epoxy_tarball'], output / 'libepoxy')
    patches = pq.PACKAGES / NAME / 'debian/patches'
    shutil.copytree(patches, output / 'debian/patches')
    for line in (patches / 'series').read_text().splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        name = pq.relative_source_path(line.split()[0])
        with (patches / name).open('rb') as patch:
            subprocess.run(['patch', '-p1', '--batch', '--forward'], stdin=patch, cwd=output, check=True,
                           stdout=subprocess.DEVNULL)
    return output


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output')
    parser.add_argument('--offline', action='store_true')
    args = parser.parse_args()
    print(prepare(args.output, args.offline))
