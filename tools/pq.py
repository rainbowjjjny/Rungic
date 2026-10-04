#!/usr/bin/env python3
"""Upstream components as pinned sources plus patch queues (docs/71).

A component is packages/<name>/: recipe.json (the pinned upstream: files and their sha256) and
debian/ (the complete packaging, or only debian/patches for an upstream that one of this project's
packages builds: recipe kind "upstream" or "git"). Our changes are debian/patches/rungic/*.patch in
DEP-3 form, listed in debian/patches/series after the distribution's own. A recipe's 'overlay' names shared
files of this repository placed into the tree before the patches (see overlay()). Upstream sources
are cached in .work/sources/<name>/; packaging tools run from the pinned toolchain image
(tools/pq/Dockerfile).

  pq.py fetch NAME            download the recipe's files, check their sha256
  pq.py source NAME [--output DIR]
                              upstream + debian/ with every patch applied (quilt state kept):
                              the tree tools/build_on_device.py builds
  pq.py prepare NAME          .work/pq/NAME: git tree with one patch-queue commit per patch
                              (gbp pq import); edit there, then `export`
  pq.py export NAME           gbp pq export; our patches and series back into packages/NAME
  pq.py lint [NAME...]        series and DEP-3/X-Rungic-* fields of every patch of ours
  pq.py verify NAME --against DIR
                              the patched source equals DIR (debian/patches aside)
  pq.py tests [NAME...] [--gaps]
                              which tests cover which patch; --gaps: patches without any
"""
import argparse
import functools
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path, PurePosixPath

WORKSPACE = Path(__file__).resolve().parent.parent
PACKAGES = WORKSPACE / 'packages'
SOURCES = WORKSPACE / '.work/sources'
IMAGE = 'rungic-pq:26.04'
TOPIC = 'rungic'
STATUSES = ('Pending', 'Submitted', 'Backport', 'Denied', 'Inactive-Upstream', 'Inappropriate')
REQUIRED = ('Forwarded', 'Last-Update', 'X-Rungic-Status', 'X-Rungic-Tests', 'X-Rungic-Docs')


def recipe(name):
    path = PACKAGES / name / 'recipe.json'
    if not path.exists():
        raise SystemExit(f'no recipe: {path}')
    return json.loads(path.read_text())


def names():
    return sorted(p.parent.name for p in PACKAGES.glob('*/recipe.json'))


def sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def relative_source_path(value):
    """Recipe paths must stay within the selected upstream tree."""
    path = PurePosixPath(value)
    if not value or path.is_absolute() or '..' in path.parts or str(path) == '.':
        raise SystemExit(f'invalid source path: {value!r}')
    return str(path)


def git_archive_name(name, info):
    suffix = ''
    if info.get('subdir'):
        subdir = relative_source_path(info['subdir'])
        selection = hashlib.sha256(f"{subdir}:{info['tree']}".encode()).hexdigest()[:16]
        suffix = f'-{selection}'
    return f"{name}-{info['commit'][:12]}{suffix}.tar"


def fetch(name, opener=urllib.request.urlopen):
    info = recipe(name)
    cache = SOURCES / name
    cache.mkdir(parents=True, exist_ok=True)
    if info.get('kind') == 'git':
        return fetch_git(name, info, cache)
    for file, want in info['files'].items():
        target = cache / file
        if target.exists() and sha256(target) == want:
            continue
        partial = target.with_suffix(target.suffix + '.part')
        fetch_url = info['fetch'][file] if isinstance(info['fetch'], dict) else info['fetch'].format(file=file)
        with opener(fetch_url) as response, open(partial, 'wb') as out:
            shutil.copyfileobj(response, out)
        have = sha256(partial)
        if have != want:
            partial.unlink()
            raise SystemExit(f'{file}: sha256 {have}, recipe says {want}')
        partial.rename(target)
    return cache


def fetch_git(name, info, cache):
    """kind "git": pinned commit, optionally selecting a subdir. The recipe checks the selected
    tree hash; `git archive` makes the same source archive on each build host."""
    repo, commit = cache / 'repo.git', info['commit']
    tarball = cache / git_archive_name(name, info)
    if tarball.exists():
        return cache
    if not repo.exists():
        subprocess.run(['git', 'init', '-q', '--bare', str(repo)], check=True)
    have = subprocess.run(['git', '-C', str(repo), 'cat-file', '-e', f'{commit}^{{commit}}'], capture_output=True)
    if have.returncode:
        subprocess.run(['git', '-C', str(repo), 'fetch', '-q', '--depth=1', info['git'], commit], check=True)
    # Some upstream repositories embed dependencies as ordinary subtrees. Pin each subtree
    # independently without importing unrelated apps, generated caches or other dependencies.
    treeish = f"{commit}:{relative_source_path(info['subdir'])}" if info.get('subdir') else f'{commit}^{{tree}}'
    tree = subprocess.run(['git', '-C', str(repo), 'rev-parse', treeish], capture_output=True,
                          text=True, check=True).stdout.strip()
    if tree != info['tree']:
        raise SystemExit(f"{name}: commit {commit} has tree {tree}, recipe says {info['tree']}")
    partial = tarball.with_suffix('.part')
    archive_ref, archive_options = commit, []
    if info.get('subdir'):
        # A tree object has no timestamp: git archive otherwise uses the current clock.
        # Keep whole-commit archives unchanged; subtree archives use the pinned commit time.
        timestamp = subprocess.run(['git', '-C', str(repo), 'show', '-s', '--format=%ct', commit],
                                   capture_output=True, text=True, check=True).stdout.strip()
        archive_ref, archive_options = treeish, [f'--mtime=@{timestamp}']
    subprocess.run(['git', '-C', str(repo), 'archive', '--format=tar', f'--prefix={name}/', '-o', str(partial),
                    *archive_options, archive_ref], check=True)
    partial.rename(tarball)
    return cache


def docker(workdir, *argv, env=()):
    engine = shutil.which('docker') or shutil.which('podman')
    if not engine:
        raise SystemExit('a Docker-compatible container engine is required for the patch queue')
    podman_options = ['--userns=keep-id', '--security-opt', 'label=disable'] if Path(engine).name == 'podman' else []
    command = [engine, 'run', '--rm', *podman_options, '-u', f'{os.getuid()}:{os.getgid()}', '-e', 'HOME=/tmp',
               *[a for e in env for a in ('-e', e)], '-v', f'{workdir}:/w', '-w', '/w', IMAGE, *argv]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        raise SystemExit(f"{' '.join(argv)}: {result.stdout}{result.stderr}")
    return result.stdout


def orig_tarball(name):
    """The upstream source: a Debian source's .orig tarball, an upstream release tarball
    (kind "upstream", the recipe's 'tarball'), or an archive of a pinned commit (kind "git")."""
    info = recipe(name)
    if info.get('kind') == 'git':
        return fetch(name) / git_archive_name(name, info)
    if info.get('kind') == 'upstream':
        return fetch(name) / info['tarball']
    tars = [f for f in info['files'] if '.orig.tar.' in f and not f.endswith(('.asc', '.sig'))]
    if len(tars) != 1:
        raise SystemExit(f'{name}: recipe needs exactly one .orig.tar.* file')
    return fetch(name) / tars[0]


def overlay(name):
    """The recipe's 'overlay': path in the source tree -> the repository's own shared file (a
    header or bridge source several components build). Placed before the patches apply, never
    patched: one copy of the shared code, as the vendored trees had with symlinks (AGENTS.md).
    An entry {"from": FILE, "replaces": SHA256} replaces an upstream file wholesale (our own
    implementation, where a diff against upstream would say nothing); it is refused once the
    upstream file differs from SHA256, so an upgrade that changes it gets reviewed."""
    return {dest: (src if isinstance(src, dict) else {'from': src})
            for dest, src in recipe(name).get('overlay', {}).items()}


def add_overlay(name, tree):
    for dest, entry in overlay(name).items():
        target = Path(tree) / dest
        replaces = entry.get('replaces')
        if target.exists() or target.is_symlink():
            if not replaces:
                raise SystemExit(f'{name}: overlay {dest} exists in the upstream tree')
            if sha256(target) != replaces:
                raise SystemExit(f'{name}: upstream {dest} changed (sha256 {sha256(target)}, the overlay '
                                 f'replaced {replaces}); review it against {entry["from"]}')
            target.unlink()
        elif replaces:
            raise SystemExit(f'{name}: overlay {dest} should replace an upstream file, which is gone')
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(WORKSPACE / entry['from'], target)


def source(name, output=None):
    """Upstream with packages/NAME/debian and every patch applied, quilt state (.pc) kept."""
    output = Path(output or WORKSPACE / f'.work/build/pq/{name}/source').resolve()
    if output.exists():
        shutil.rmtree(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    unpack = output.parent / (output.name + '.unpack')
    shutil.rmtree(unpack, ignore_errors=True)
    unpack.mkdir()
    with tarfile.open(orig_tarball(name)) as tar:
        tar.extractall(unpack, filter='tar')
    [top] = list(unpack.iterdir())
    top.rename(output)
    unpack.rmdir()
    # Explicit exclusions are for upstream's checked-in build/debug output and dependencies
    # prepared separately. Missing paths fail so an upstream upgrade must review the selection.
    for entry in recipe(name).get('exclude', []):
        target = output / relative_source_path(entry)
        if not target.parent.resolve().is_relative_to(output):
            raise SystemExit(f'{name}: excluded path escapes source tree: {entry}')
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(target)
        else:
            raise SystemExit(f'{name}: excluded upstream path no longer exists: {entry}')
    # A pinned upstream without changes and without packaging of its own has no debian/.
    if (PACKAGES / name / 'debian').exists():
        shutil.rmtree(output / 'debian', ignore_errors=True)
        shutil.copytree(PACKAGES / name / 'debian', output / 'debian', symlinks=True)
    add_overlay(name, output)
    series = output / 'debian/patches/series'
    if series.exists() and any(line.strip() and not line.lstrip().startswith('#')
                               for line in series.read_text().splitlines()):
        docker(output, 'quilt', 'push', '-a', '-q', env=('QUILT_PATCHES=debian/patches',))
    return output


def gbp_stub(name, tree):
    """gbp reads the source name from debian/control and a version from debian/changelog. An upstream
    that one of this project's packages builds has only debian/patches: the editing tree (never the
    repository) gets a minimal control and changelog."""
    debian = Path(tree) / 'debian'
    if not (debian / 'control').exists():
        (debian / 'control').write_text(f'Source: {name}\n\nPackage: {name}\nArchitecture: all\n')
    if not (debian / 'changelog').exists():
        (debian / 'changelog').write_text(
            f"{name} ({recipe(name)['version']}) unstable; urgency=medium\n\n  * Upstream (docs/71).\n\n"
            ' -- Rungic <noreply@rungic.invalid>  Sat, 26 Sep 2026 00:00:00 +0000\n')


def prepare(name):
    """git tree for editing: upstream + debian/ on branch rungic, patches as commits (gbp pq)."""
    work = WORKSPACE / '.work/pq' / name
    tree = source(name, WORKSPACE / f'.work/pq/{name}.src')
    series = tree / 'debian/patches/series'
    if series.exists() and any(line.strip() and not line.lstrip().startswith('#')
                               for line in series.read_text().splitlines()):
        docker(tree, 'quilt', 'pop', '-a', '-q', env=('QUILT_PATCHES=debian/patches',))
    shutil.rmtree(tree / '.pc', ignore_errors=True)
    shutil.rmtree(work, ignore_errors=True)
    tree.rename(work)
    gbp_stub(name, work)
    for args in (('init', '-q', '-b', 'rungic'), ('add', '-A'),
                 ('-c', 'user.name=Rungic', '-c', 'user.email=noreply@rungic.invalid', 'commit', '-q', '-m',
                  f"{name} {recipe(name)['version']} with packages/{name}/debian (patches unapplied)")):
        subprocess.run(['git', *args], cwd=work, check=True, capture_output=True)
    docker(work, 'gbp', 'pq', 'import')
    return work


def export(name):
    """gbp pq export; only our topic and the series go back (the distribution's patches keep their
    original form, which gbp would rewrite)."""
    work = WORKSPACE / '.work/pq' / name
    docker(work, 'gbp', 'pq', 'export')
    dest = PACKAGES / name / 'debian/patches'
    shutil.rmtree(dest / TOPIC, ignore_errors=True)
    generated = work / 'debian/patches'
    if (generated / TOPIC).exists():
        shutil.copytree(generated / TOPIC, dest / TOPIC)
        shutil.copy2(generated / 'series', dest / 'series')
    else:
        # A new source with an empty patch queue has no topic directory yet;
        # gbp writes its first exported patch at the root of debian/patches.
        entries = [line.strip() for line in (generated / 'series').read_text().splitlines()
                   if line.strip() and not line.lstrip().startswith('#')]
        if not entries or any('/' in entry or not (generated / entry).is_file() for entry in entries):
            raise SystemExit(f'{name}: unexpected new patch queue layout')
        (dest / TOPIC).mkdir(parents=True)
        for entry in entries:
            shutil.copy2(generated / entry, dest / TOPIC / entry)
        (dest / 'series').write_text(''.join(f'{TOPIC}/{entry}\n' for entry in entries))
    return sorted(p.name for p in (dest / TOPIC).glob('*.patch'))


def header(text):
    """DEP-3 fields of a patch (git format-patch or plain DEP-3), up to the diff or '---'."""
    fields, key = {}, None
    for line in text.splitlines():
        if line.startswith(('---', 'diff ', 'Index: ')):
            break
        match = re.match(r'^([A-Za-z][A-Za-z0-9-]*): ?(.*)$', line)
        if match:
            key = match[1]
            fields[key] = match[2]
        elif line.startswith(' ') and key:
            fields[key] += ' ' + line.strip()
        else:
            key = None
    return fields


def lint_package(name):
    problems = []
    patches = PACKAGES / name / 'debian/patches'
    series = [l.split()[0] for l in (patches / 'series').read_text().splitlines()
              if l.strip() and not l.startswith('#')] if (patches / 'series').exists() else []
    ours = sorted(str(p.relative_to(patches)) for p in (patches / TOPIC).glob('*.patch'))
    for entry in series:
        if not (patches / entry).exists():
            problems.append(f'series names a missing patch: {entry}')
    for entry in ours:
        if entry not in series:
            problems.append(f'{entry} is not in series')
    for entry in ours:
        fields = header((patches / entry).read_text(errors='replace'))
        for key in REQUIRED:
            if not fields.get(key):
                problems.append(f'{entry}: no {key}')
        if not (fields.get('Subject') or fields.get('Description')):
            problems.append(f'{entry}: no Subject/Description')
        if not (fields.get('From') or fields.get('Author') or fields.get('Origin')):
            problems.append(f'{entry}: no From/Author/Origin')
        if fields.get('Last-Update') and not re.fullmatch(r'\d{4}-\d{2}-\d{2}', fields['Last-Update']):
            problems.append(f"{entry}: Last-Update {fields['Last-Update']!r} is not YYYY-MM-DD")
        status = fields.get('X-Rungic-Status', '')
        if status and status.split()[0].rstrip(';') not in STATUSES:
            problems.append(f'{entry}: X-Rungic-Status {status!r} is not one of {", ".join(STATUSES)}')
        if status.startswith('Submitted') and not fields.get('Forwarded', '').startswith('http'):
            problems.append(f'{entry}: Submitted but Forwarded has no URL')
        # An acceptance scenario named as a test must exist, unless it is marked as still to write.
        for segment in fields.get('X-Rungic-Tests', '').split(';'):
            if re.search(r'\(to (?:write|extend)\)', segment):
                continue
            for scenario in re.findall(r'\bL3:([a-z][a-z0-9_-]*\.[a-z0-9_.-]*[a-z0-9_])(?![/\w])', segment):
                if scenario not in scenarios():
                    problems.append(f'{entry}: X-Rungic-Tests names L3:{scenario}, not a scenario of release/acceptance.json')
    return problems


@functools.cache
def scenarios():
    return {s['id'] for s in json.loads((WORKSPACE / 'release/acceptance.json').read_text())['scenarios']}


def tree_diff(mine, theirs, exclude=()):
    """`diff -r -q` of two trees; lines for every difference. An empty directory present on one side
    only is not one (git does not track empty directories, so a vendored tree lacks them); stderr is
    (a symlink that does not resolve is an error, not a match)."""
    result = subprocess.run(['diff', '-r', '-q', *[a for x in exclude for a in ('-x', x)], str(mine), str(theirs)],
                            capture_output=True, text=True)
    lines = []
    for line in result.stdout.splitlines():
        m = re.match(r'Only in (.+): (.+)$', line)
        if m and (Path(m[1]) / m[2]).is_dir() and not any((Path(m[1]) / m[2]).iterdir()):
            continue
        lines.append(line)
    if result.returncode > 1:
        lines.append(result.stderr.strip())
    return lines


def verify(name, against):
    tree = source(name)
    # A reference without debian/ (a vendored upstream our own package built) is compared without it.
    skip = () if (Path(against) / 'debian').exists() else ('debian',)
    lines = tree_diff(tree, against, ('.pc', 'patches', '.git', *skip))
    return not lines, '\n'.join(lines)


def test_matrix(selected):
    rows = []
    for name in selected:
        patches = PACKAGES / name / 'debian/patches'
        for patch in sorted((patches / TOPIC).glob('*.patch')):
            fields = header(patch.read_text(errors='replace'))
            tests = fields.get('X-Rungic-Tests', '')
            covered, planned = [], []
            for segment in re.split(r'[;]', tests):
                ids = re.findall(r'\b(?:L[0-3]|probe):[^\s,;()]+', segment)
                # "(to write)", "(to extend)": a test that does not exist yet does not cover the patch
                (planned if re.search(r'\(to (?:write|extend)\)', segment) else covered).extend(ids)
            rows.append({'package': name, 'patch': patch.name, 'status': fields.get('X-Rungic-Status', ''),
                         'tests': covered, 'planned': planned, 'note': tests})
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    for command in ('fetch', 'prepare', 'export'):
        sub.add_parser(command).add_argument('name')
    p = sub.add_parser('source')
    p.add_argument('name')
    p.add_argument('--output')
    p = sub.add_parser('lint')
    p.add_argument('names', nargs='*')
    p = sub.add_parser('verify')
    p.add_argument('name')
    p.add_argument('--against', required=True)
    p = sub.add_parser('tests')
    p.add_argument('names', nargs='*')
    p.add_argument('--gaps', action='store_true')
    args = parser.parse_args()
    if args.command == 'fetch':
        print(fetch(args.name))
    elif args.command == 'source':
        print(source(args.name, args.output))
    elif args.command == 'prepare':
        print(prepare(args.name))
    elif args.command == 'export':
        print('\n'.join(export(args.name)))
    elif args.command == 'lint':
        problems = {n: lint_package(n) for n in (args.names or names())}
        for name, items in problems.items():
            for item in items:
                print(f'{name}: {item}')
        return 1 if any(problems.values()) else 0
    elif args.command == 'verify':
        ok, text = verify(args.name, args.against)
        print(f'{args.name}: patched source equals {args.against}' if ok else text or 'diff failed')
        return 0 if ok else 1
    elif args.command == 'tests':
        rows = test_matrix(args.names or names())
        for row in rows:
            if args.gaps and row['tests']:
                continue
            planned = f"  planned: {', '.join(row['planned'])}" if row['planned'] else ''
            print(f"{row['package']}/{row['patch']}: {', '.join(row['tests']) or '-'}  [{row['status'].split(' [')[0]}]{planned}"
                  + (f"  {row['note']}" if args.gaps or not row['tests'] else ''))
    return 0


if __name__ == '__main__':
    sys.exit(main())
