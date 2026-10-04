"""Offline tests: no Docker, SDK compilation or device access."""
# covers: install.rungicos-image/E4
# covers: install.rungicos-image/E7
import copy
import json
from pathlib import Path
import sys

import pytest

import husky_build_plan as h
import standalone


def config(tmp_path):
    return dict(release='20261004.1', firefox='156.0.1~build1', epoch=1791040000,
                volume='rungic-rootfs', pool='/output/husky-20261004.1',
                image='sha256:' + 'a' * 64, size_gib=16, mirror=h.DEFAULT_MIRROR,
                proxy={}, sdk='/sdk', ndk='/ndk', rust='/rust', jdk='/jdk',
                build_tools='/sdk/build-tools/36.0.0', android_jar='/sdk/platforms/android-36/android.jar',
                path='/usr/bin:/bin', docker='/bin/docker', bash='/opt/homebrew/bin/bash',
                pq_image='sha256:' + 'd' * 64,
                release_file=str(tmp_path / 'release.json'), apt_repo=str(tmp_path / 'apt'),
                xkb=str(tmp_path / 'libxkbcommon.so'), xkb_cross=str(tmp_path / 'android-arm64.ini'),
                xkb_sha256='b' * 64, runtime=str(tmp_path / 'runtime.tar.gz'),
                snapshot=str(tmp_path / 'pool.json'))


def test_recipes_describe_actual_mac_commands(tmp_path):
    recipes = h.recipes(config(tmp_path))
    assert set(recipes) == {'husky-rootfs', 'husky-host', 'husky-apk', 'husky-sparse'}
    sparse = recipes['husky-sparse']
    assert sparse['command'] == ['/ndk/bin/clang', '--target=aarch64-linux-android31',
        '--sysroot=/ndk/sysroot', '-static', '-s', '-O2', '-Wall', '-Wextra', '-Werror',
        '{repo}/tools/ci/sparse_write.c', '-o', '{output}/rungic-sparse-write']
    apk = recipes['husky-apk']
    assert apk['environment']['RUNGIC_APK_OCR'] == 'none'
    assert apk['environment']['RUNGIC_PROXY'] == ''
    assert apk['environment']['RUNGIC_NATIVE_OUT'].startswith('{output}/')
    assert apk['environment']['CARGO_TARGET_DIR'].startswith('{output}/')
    assert apk['dependencies']['xkb']['sha256'] == 'b' * 64
    assert 'signing/development/launcher-signing.p12' in apk['sources']
    assert apk['tools']['pq-container']['identity'] == config(tmp_path)['pq_image']
    assert recipes['husky-host']['dependencies']['runtime']['sha256'] == h.RUNTIME_SHA256
    for name in ('husky-rootfs', 'husky-host'):
        assert recipes[name]['tools']['container']['identity'] == config(tmp_path)['image']
        assert any('docker' in str(step) for step in recipes[name]['parameters']['docker_commands'])
    root_steps = str(recipes['husky-rootfs']['parameters']['docker_commands'])
    assert '--platform' in root_steps and '--privileged' in root_steps
    assert '/record' in root_steps  # exporting happens inside the build command


@pytest.mark.parametrize('field,value', [('release', '../oops'), ('volume', 'a/b'),
    ('pool', '/output/../bad'), ('image', 'mutable:latest'), ('epoch', -1), ('size_gib', 2)])
def test_bad_config_refused(tmp_path, field, value):
    c = config(tmp_path)
    c[field] = value
    with pytest.raises(ValueError):
        h.recipes(c)


def fake_records(tmp_path):
    c = config(tmp_path)
    for name in ('release_file', 'xkb', 'xkb_cross', 'runtime', 'snapshot'):
        Path(c[name]).write_text('fixture-' + name)
    Path(c['apt_repo']).mkdir()
    c['xkb_sha256'] = h.artifact.file_hash(c['xkb'])
    c['root_record'] = str(tmp_path / 'husky-rootfs')
    tool = tmp_path / 'tool'
    tool.write_text('fixture-tool')
    generated = h.recipes(c)
    entries = {}
    files = {}
    # Use the generated recipes, with fake binary/tool inputs. Only records are
    # simulated; inputs/verify/validate_record and pack's collector are real.
    for component in ('husky-rootfs', 'husky-host', 'husky-apk', 'husky-sparse'):
        recipe = generated[component]
        for value in recipe['tools'].values():
            if 'path' in value:
                value['path'] = str(tool)
        if component == 'husky-host':
            recipe['dependencies']['runtime']['sha256'] = h.artifact.file_hash(c['runtime'])
        rp = tmp_path / (component + '.json')
        rp.write_text(json.dumps(recipe))
        directory = tmp_path / component
        directory.mkdir()
        rows = {}
        for output in recipe['outputs']:
            (directory / output).write_bytes((component + output).encode())
            rows[output] = {'sha256': h.artifact.file_hash(directory / output),
                           'bytes': (directory / output).stat().st_size}
        expected = h.artifact.inputs(recipe)
        report = dict(schema=1, component=component, inputs=expected,
                      input_sha256=h.artifact.digest(expected), outputs=rows)
        (directory / 'build.json').write_text(json.dumps(report))
        entries[component] = {'recipe': str(rp), 'directory': str(directory)}
        files[h.BINDINGS[component]] = rows[h.BINDINGS[component]]
    return entries, files


def test_plan_accepted_by_real_pack_collector(tmp_path):
    entries, files = fake_records(tmp_path)
    plan = h.build_plan(entries)
    path = tmp_path / 'plan.json'
    path.write_text(json.dumps(plan))
    manifest = standalone.collect_build_manifest(path, files)
    assert set(manifest['bindings']) == set(h.BINDINGS.values())
    assert set(manifest['components']) == set(h.BINDINGS)
    # Both current inputs and produced bytes are rechecked by pack.
    recipe = Path(entries['husky-apk']['recipe'])
    data = json.loads(recipe.read_text())
    data['command'].append('changed')
    recipe.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='inputs differ'):
        standalone.collect_build_manifest(path, files)


def test_plan_requires_all_four_records(tmp_path):
    entries, _ = fake_records(tmp_path)
    del entries['husky-rootfs']
    with pytest.raises(ValueError):
        h.build_plan(entries)


def test_host_dependency_resolves_to_verified_root_record(tmp_path):
    c = config(tmp_path)
    c['root_record'] = '/cache/husky-rootfs/fingerprint'
    host = h.recipes(c)['husky-host']
    for name in ('root_location', 'root_identity'):
        assert host['dependencies'][name]['build'] == c['root_record'] + '/build.json'
    assert host['dependencies']['root_identity']['path'].endswith('/root-tree.identity.json')


def test_recipe_fingerprint_changes_for_volume_inputs_and_image(tmp_path):
    c = config(tmp_path)
    for field, value in [('image', 'sha256:' + 'c' * 64), ('firefox', '156.0.2~build1')]:
        other = copy.deepcopy(c)
        other[field] = value
        assert h.artifact.digest(h.recipes(c)['husky-rootfs']) != h.artifact.digest(h.recipes(other)['husky-rootfs'])


def test_plan_directory_is_not_in_recipe_cache_key(tmp_path):
    c = config(tmp_path)
    other = dict(c, snapshot='/different/plan/pool.json', root_record='/different/cache/root')
    original = dict(c, root_record='/original/cache/root')
    for name in h.BINDINGS:
        left, right = h.recipes(original)[name], h.recipes(other)[name]
        # Dependency paths are excluded by build_artifact.inputs; commands and
        # parameters must also avoid embedding those locations.
        for field in ('command', 'parameters', 'environment'):
            assert left[field] == right[field]


def test_volume_pool_content_ownership_and_no_mtime(tmp_path):
    import os
    c = config(tmp_path)
    c['pool'] = str(tmp_path / 'pool')
    pool = Path(c['pool'])
    (pool / 'debs').mkdir(parents=True)
    deb = pool / 'debs/pkg.deb'
    deb.write_bytes(b'package')
    (pool / 'packages.lock.json').write_text('{}')
    first = h.pool_identity(c)
    assert first['debs'] == h.artifact.identity(pool / 'debs', ownership=True)
    os.utime(deb, (1, 1))
    assert h.pool_identity(c) == first
    deb.chmod(0o600)
    assert h.pool_identity(c) != first
    snapshot = tmp_path / 'snapshot.json'
    snapshot.write_text(json.dumps(first))
    with pytest.raises(ValueError, match='inputs changed'):
        h.check_identity(h.pool_identity(c), snapshot)


def test_build_executes_records_and_resolves_host_dependency(tmp_path, monkeypatch):
    from types import SimpleNamespace
    c = config(tmp_path)
    snapshot = {'debs': {'kind': 'tree', 'sha256': 'a' * 64}}
    Path(c['snapshot']).write_text(json.dumps(snapshot))
    identity = {'kind': 'tree', 'sha256': 'b' * 64}
    seen = []
    def fixture_recipes(current):
        generated = {}
        for name, output in h.BINDINGS.items():
            files = {output: name}
            dependencies = {}
            if name == 'husky-rootfs':
                files.update({'root-location.json': json.dumps({'volume': c['volume'], 'tree': '/output/generated/root-tree'}),
                              'root-tree.identity.json': json.dumps(identity)})
            if name == 'husky-host' and 'root_record' in current:
                seen.append(current['root_record'])
                dependencies['root_identity'] = {'path': current['root_record'] + '/root-tree.identity.json',
                    'build': current['root_record'] + '/build.json'}
            generated[name] = dict(schema=1, component=name, target='fixture', sources=[],
                tools={'python': {'path': sys.executable}}, dependencies=dependencies,
                outputs=list(files), command=[sys.executable, '-c',
                    'from pathlib import Path; import json; '
                    'files=json.loads(' + repr(json.dumps(files)) + '); '
                    '[Path("{output}",n).write_text(v) for n,v in files.items()]'])
        return generated
    monkeypatch.setattr(h, 'recipes', fixture_recipes)
    monkeypatch.setattr(h, 'snapshot', lambda _: snapshot)
    monkeypatch.setattr(h, 'run', lambda *args, **kwargs: SimpleNamespace(stdout=json.dumps(identity)))
    output = tmp_path / 'plan'
    output.mkdir()
    target = h.build(c, output, tmp_path / 'cache')
    plan = json.loads(target.read_text())
    assert seen
    for component, entry in plan['components'].items():
        report = h.artifact.verify(entry['directory'])
        assert Path(entry['directory']).name == report['input_sha256']
        assert Path(entry['directory'], h.BINDINGS[component]).read_text() == component
    files = {artifact: h.artifact.verify(entry['directory'])['outputs'][artifact]
        for artifact, binding in plan['bindings'].items()
        for entry in [plan['components'][binding['component']]]}
    standalone.collect_build_manifest(target, files)
    # Even file-cache hits must refuse a changed live Linux input snapshot.
    monkeypatch.setattr(h, 'snapshot', lambda _: {'changed': True})
    retry = tmp_path / 'retry-plan'
    retry.mkdir()
    with pytest.raises(ValueError, match='inputs changed'):
        h.build(c, retry, tmp_path / 'cache')
    assert not (retry / 'build-plan.json').exists()


def test_enter_override_builds_in_record_not_shared_workspace(tmp_path):
    import os
    import subprocess
    compiler = tmp_path / 'clang'
    compiler.write_text('#!/bin/sh\nwhile [ "$#" -gt 0 ]; do\n'
                        'if [ "$1" = -o ]; then shift; printf binary > "$1"; exit 0; fi\nshift\ndone\nexit 1\n')
    compiler.chmod(0o755)
    sysroot = tmp_path / 'sysroot'
    sysroot.mkdir()
    output = tmp_path / 'record/enter'
    env = dict(os.environ, RUNGIC_ANDROID_CLANG=str(compiler),
               RUNGIC_ANDROID_SYSROOT=str(sysroot), RUNGIC_ENTER_OUT=str(output))
    script = Path(h.ROOT, 'tools/build_enter.sh')
    for name in ('lxc', 'plasma'):
        result = subprocess.run(['sh', str(script), name], env=env, check=True,
                                capture_output=True, text=True)
        assert Path(result.stdout.strip()).parent == output
        assert Path(result.stdout.strip()).read_text() == 'binary'
