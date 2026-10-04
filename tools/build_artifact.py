#!/usr/bin/env python3
"""Content-addressed build execution. A recipe declares inputs, tools and outputs.

Records describe local build consistency, not signed supply-chain attestations.
The caller supplies the expected recipe; a cached record never chooses its own inputs.
"""
import argparse
import ctypes
import fcntl
import functools
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import sys

ROOT = Path(__file__).resolve().parents[1]


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for data in iter(lambda: stream.read(8 * 1024 * 1024), b''):
            h.update(data)
    return h.hexdigest()


@functools.lru_cache(maxsize=1)
def darwin_xattr_api():
    """CPython exposes the Linux xattr API, but not the Darwin API."""
    libc = ctypes.CDLL(None, use_errno=True)
    libc.listxattr.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
    libc.listxattr.restype = ctypes.c_ssize_t
    libc.getxattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p,
                             ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int]
    libc.getxattr.restype = ctypes.c_ssize_t
    return libc


def xattrs(path):
    if hasattr(os, 'listxattr'):
        return {key: os.getxattr(path, key, follow_symlinks=False).hex()
                for key in sorted(os.listxattr(path, follow_symlinks=False))}
    if sys.platform != 'darwin':
        raise ValueError('build input xattr hashing is unsupported on this platform')
    api = darwin_xattr_api()
    encoded = os.fsencode(path)
    # XATTR_NOFOLLOW=1: hash the link itself, as on Linux.
    def read(function, *prefix):
        def call(buffer, size):
            if function == api.listxattr:
                return function(*prefix, buffer, size, 1)
            return function(*prefix, buffer, size, 0, 1)
        size = call(None, 0)
        if size < 0:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code), str(path))
        buffer = ctypes.create_string_buffer(size)
        count = call(buffer, size)
        if count < 0:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code), str(path))
        if count > size:
            raise ValueError(f'xattrs changed while hashing: {path}')
        return buffer.raw[:count]
    names = read(api.listxattr, encoded).split(b'\0')
    return {os.fsdecode(name): read(api.getxattr, encoded, name).hex()
            for name in sorted(names) if name}


def content(path, ownership=False):
    """Hash content, modes, links and xattrs without following directory symlinks.

    Names within a tree participate; its absolute location, mtime and owner on the
    build host do not. Image tree ownership is recorded separately by the recipe.
    """
    path = Path(path)
    info = path.lstat()
    row = {'mode': stat.S_IMODE(info.st_mode)}
    if ownership:
        row.update(uid=info.st_uid, gid=info.st_gid)
    if path.is_symlink():
        row.update(kind='symlink', target=os.readlink(path))
    elif path.is_file():
        row.update(kind='file', sha256=file_hash(path), bytes=info.st_size)
    elif path.is_dir():
        row.update(kind='tree', entries={p.name: content(p, ownership) for p in sorted(path.iterdir())})
    else:
        raise ValueError(f'unsupported build input type: {path}')
    attrs = xattrs(path)
    if attrs:
        row['xattrs'] = attrs
    return row


def identity(path, ownership=False):
    value = content(path, ownership)
    return {'kind': value['kind'], 'sha256': digest(value),
            **({'file_sha256': value['sha256']} if value['kind'] == 'file' else {})}


def safe_relative(name):
    path = Path(name)
    if path.is_absolute() or '..' in path.parts or not path.parts:
        raise ValueError(f'expected a relative path: {name}')
    return path


def inputs(recipe, root=ROOT):
    if recipe.get('schema') != 1 or not re.fullmatch(r'[a-zA-Z0-9_.-]+', recipe['component']):
        raise ValueError('invalid build recipe')
    if not recipe.get('tools') or not recipe.get('target') or not recipe.get('outputs'):
        raise ValueError('build recipe needs tools, target and outputs')
    for name in recipe['outputs']:
        safe_relative(name)
    sources = {name: identity(root / safe_relative(name)) for name in recipe.get('sources', [])}
    dependencies = {}
    for name, dep in sorted(recipe.get('dependencies', {}).items()):
        path = Path(dep['path'])
        actual = identity(path, dep.get('ownership', False))
        if dep.get('sha256') and actual.get('file_sha256') != dep['sha256']:
            raise ValueError(f'pinned dependency changed: {name}')
        if dep.get('build'):
            report = json.loads(Path(dep['build']).read_text())
            validate_record(report)
            if not any(x['sha256'] == actual.get('file_sha256') for x in report['outputs'].values()):
                raise ValueError(f'dependency does not match build record: {name}')
            actual['input_sha256'] = report['input_sha256']
        actual['origin'] = dep.get('origin', 'pinned-binary-input')
        dependencies[name] = actual
    tools = {}
    for name, tool in sorted(recipe['tools'].items()):
        tools[name] = identity(tool['path']) if 'path' in tool else tool['identity']
    return {'schema': 1, 'component': recipe['component'], 'target': recipe['target'],
            'sources': sources, 'dependencies': dependencies, 'tools': tools,
            'parameters': recipe.get('parameters', {}), 'command': recipe['command'],
            'environment': recipe.get('environment', {}), 'outputs': recipe['outputs'],
            'builder_sha256': file_hash(Path(__file__))}


def validate_record(report):
    if report.get('schema') != 1 or digest(report['inputs']) != report.get('input_sha256'):
        raise ValueError('invalid build input fingerprint')
    if report.get('component') != report['inputs']['component'] or not report.get('outputs'):
        raise ValueError('invalid build component/output inventory')
    if set(report['outputs']) != set(report['inputs']['outputs']):
        raise ValueError('build output inventory differs from declared outputs')
    for name, entry in report['outputs'].items():
        safe_relative(name)
        if not re.fullmatch('[0-9a-f]{64}', entry['sha256']) or entry['bytes'] < 0:
            raise ValueError('invalid build output digest')


def verify(directory, expected=None):
    directory = Path(directory)
    report = json.loads((directory / 'build.json').read_text())
    validate_record(report)
    if expected is not None and report['inputs'] != expected:
        raise ValueError('cached build inputs differ from requested inputs')
    for name, entry in report['outputs'].items():
        path = directory / safe_relative(name)
        if path.is_symlink() or not path.resolve().is_relative_to(directory.resolve()) or not path.is_file():
            raise ValueError(f'invalid build output: {name}')
        if path.stat().st_size != entry['bytes'] or file_hash(path) != entry['sha256']:
            raise ValueError(f'build output checksum mismatch: {name}')
    return report


def execute(recipe, cache, root=ROOT):
    before = inputs(recipe, root)
    key = digest(before)
    parent = Path(cache) / recipe['component']
    parent.mkdir(parents=True, exist_ok=True)
    target = parent / key
    with (parent / (key + '.lock')).open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if target.exists():
            # Corruption is not a silent cache miss; preserve evidence for the caller.
            report = verify(target, before)
            return {'reused': True, 'directory': str(target), 'report': report}
        stage = Path(tempfile.mkdtemp(prefix=key + '.pending-', dir=parent))
        try:
            temporary = stage / '.tmp'
            temporary.mkdir()
            replacements = {'{output}': str(stage), '{repo}': str(root)}
            replacements.update({'{input:' + name + '}': str(Path(dep['path']).resolve())
                                 for name, dep in recipe.get('dependencies', {}).items()})
            def expand(text):
                for placeholder, value in replacements.items():
                    text = text.replace(placeholder, value)
                return text
            environment = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'),
                           'HOME': os.environ['HOME'], 'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8',
                           'TMPDIR': str(temporary.resolve()),
                           'PYTHONPYCACHEPREFIX': str(root / '.work/cache/python')}
            environment.update({key: expand(value) for key, value in recipe.get('environment', {}).items()})
            with (stage / 'build.log').open('wb') as log:
                result = subprocess.run([expand(arg) for arg in recipe['command']], cwd=root,
                                        env=environment, stdout=log, stderr=subprocess.STDOUT)
            if result.returncode:
                raise ValueError(f'build failed ({result.returncode}); log: {stage / "build.log"}')
            if inputs(recipe, root) != before:
                raise ValueError(f'inputs changed during build; unpublished output: {stage}')
            outputs = {}
            for name in recipe['outputs']:
                path = stage / name
                if path.is_symlink() or not path.resolve().is_relative_to(stage.resolve()) or not path.is_file():
                    raise ValueError(f'missing/unsafe declared output: {name}')
                outputs[name] = {'sha256': file_hash(path), 'bytes': path.stat().st_size}
            report = {'schema': 1, 'component': recipe['component'], 'input_sha256': key,
                      'inputs': before, 'outputs': outputs}
            (stage / 'build.json').write_text(json.dumps(report, indent=2) + '\n')
            stage.rename(target)
            verify(target, before)
            return {'reused': False, 'directory': str(target), 'report': report}
        except BaseException:
            # Failed output stays under .pending-* for diagnosis; never becomes a hit.
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('recipe', type=Path)
    parser.add_argument('--cache', type=Path, default=ROOT / '.work/build-cache')
    parser.add_argument('--check', action='store_true', help='verify the expected cache entry without building')
    args = parser.parse_args()
    recipe = json.loads(args.recipe.read_text())
    if args.check:
        expected = inputs(recipe)
        directory = args.cache / recipe['component'] / digest(expected)
        report = verify(directory, expected)
        result = {'reused': True, 'directory': str(directory), 'report': report}
    else:
        result = execute(recipe, args.cache)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
