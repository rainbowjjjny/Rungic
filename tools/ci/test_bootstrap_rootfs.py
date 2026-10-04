"""Offline contracts for the from-scratch CI2 builder; never run apt or Docker."""
import hashlib
import json
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

import pytest

import bootstrap_rootfs as b


@pytest.fixture
def inputs(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    manifest = {'version': '20261004.1', 'packages': {'rungic-test': '1+rungic1', 'libcoupled': '2'}}
    spec = {'rebuilt': {}, 'project': {'rungic-test': {}}, 'coupled': ['libcoupled']}
    paths = {}
    for name, data in [('release', manifest), ('spec', spec)]:
        paths[name] = tmp_path / (name + '.json')
        paths[name].write_text(json.dumps(data))
    for name, data in [('runtime', 'bash\nlocales\n'), ('excluded', '# omitted\nangelfish\n'),
                       ('mozilla_keyring', 'test-key')]:
        paths[name] = tmp_path / name
        paths[name].write_text(data)
    stanzas = []
    for name, version, arch in [('rungic-test', '1+rungic1', 'arm64'),
                                 ('rungic-release', manifest['version'], 'all'),
                                 ('firefox', '150.0', 'arm64')]:
        deb = repo / (name + '.deb')
        deb.write_bytes(name.encode())
        stanzas.append(f'Package: {name}\nVersion: {version}\nArchitecture: {arch}\n'
                       f'Filename: ./{deb.name}\nSHA256: {hashlib.sha256(deb.read_bytes()).hexdigest()}\n')
    (repo / 'Packages').write_text('\n'.join(stanzas))
    (repo / 'Release').write_text('Origin: rungic\nLabel: rungic\n')
    return dict(repo=repo, output=tmp_path / 'out', firefox_version='150.0', **paths)


# covers: install.rungicos-image/E6
def test_selection_from_all_release_sections():
    spec = {'rebuilt': {'mesa': {'packages': ['libegl-mesa0', 'libgbm1']}},
            'project': {'rungic-plasma-session': {}}, 'coupled': ['plasma-workspace']}
    assert b.release_names(spec) == {'libegl-mesa0', 'libgbm1', 'rungic-plasma-session', 'plasma-workspace'}


# covers: install.rungicos-image/E6
def test_validate_and_closure(inputs):
    plan = b.validate_inputs(**inputs)
    assert 'rungic-release=20261004.1' in plan['include']
    assert 'firefox=150.0' in plan['include']
    assert 'locales' in plan['include']
    assert plan['pins']['libcoupled'] == '2'
    assert plan['repository_sha256']['rungic-test.deb']
    assert not inputs['output'].exists()


# covers: install.rungicos-image/E6
@pytest.mark.parametrize('name,value', [('firefox_version', '1; touch /tmp/pwn'),
                                      ('mirror', 'http://mirror.invalid/\ndeb evil'),
                                      ('mirror', 'file:///tmp/archive')])
def test_reject_command_and_source_injection(inputs, name, value):
    with pytest.raises(ValueError):
        b.validate_inputs(**dict(inputs, **{name: value}))


# covers: install.rungicos-image/E6
@pytest.mark.parametrize('kind', ['directory', 'file', 'broken-link'])
def test_no_overwrite(inputs, kind):
    output = inputs['output']
    if kind == 'directory':
        output.mkdir()
    elif kind == 'file':
        output.write_text('keep')
    else:
        output.symlink_to('missing')
    with pytest.raises(ValueError, match='exists'):
        b.validate_inputs(**inputs)


# covers: install.rungicos-image/E6
@pytest.mark.parametrize('damage', ['missing', 'hash', 'escape', 'architecture', 'remote'])
def test_repository_rejected(inputs, damage):
    repo = inputs['repo']
    index = repo / 'Packages'
    text = index.read_text()
    if damage == 'missing':
        (repo / 'rungic-test.deb').unlink()
    elif damage == 'hash':
        (repo / 'rungic-test.deb').write_bytes(b'changed')
    elif damage == 'escape':
        text = text.replace('./rungic-test.deb', '../rungic-test.deb')
    elif damage == 'architecture':
        text = text.replace('Architecture: arm64', 'Architecture: amd64')
    else:
        (repo / 'rungic-test.deb').unlink()
        (repo / 'rungic-test.deb.remote').write_text('{}')
    index.write_text(text)
    with pytest.raises(ValueError):
        b.validate_inputs(**inputs)


# covers: install.rungicos-image/E6
def test_manifest_missing_or_extra_names(inputs):
    data = json.loads(inputs['release'].read_text())
    data['packages']['extra'] = '1'
    inputs['release'].write_text(json.dumps(data))
    with pytest.raises(ValueError, match='selection'):
        b.validate_inputs(**inputs)


# covers: install.rungicos-image/E2
def test_excluded_requested_and_dependencies(inputs):
    inputs['runtime'].write_text('angelfish\n')
    with pytest.raises(ValueError, match='excluded'):
        b.validate_inputs(**inputs)
    assert 'Pin-Priority: -1' in b.exclusion_preferences(['angelfish'])
    with pytest.raises(ValueError, match='excluded'):
        b.check_installed({'angelfish': ('1', 'arm64')}, {}, ['angelfish'])


# covers: install.rungicos-image/E3
def test_pins_match_image_builder(inputs, tmp_path):
    from build_rootfs_image import write_release_preferences
    manifest = json.loads(inputs['release'].read_text())
    write_release_preferences(tmp_path, manifest)
    assert b.preferences(dict(manifest['packages'], **{'rungic-release': manifest['version']}),
                         manifest['version']) == (tmp_path / 'etc/apt/preferences.d/rungic-release').read_text()


# covers: install.rungicos-image/E6
def test_sources_have_only_configured_archive_and_local_pool():
    text = b.sources('http://mirror.invalid/ubuntu-ports', Path('/pool'))
    assert 'resolute-updates' in text and 'resolute-security' in text
    assert 'main universe restricted multiverse' in text
    assert 'deb [trusted=yes] file:///pool ./' in text
    assert 'mozilla' not in text


# covers: install.rungicos-image/E6
def test_replay_requires_full_matching_lock(inputs):
    lock = inputs['repo'].parent / 'lock.json'
    lock.write_text(json.dumps({'schema': 1, 'packages': {
        'rungic-test': {'version': '999', 'architecture': 'arm64', 'sha256': 'a' * 64}}}))
    with pytest.raises(ValueError, match='lock'):
        b.validate_inputs(**inputs, package_lock=lock)
    with pytest.raises(ValueError, match='closure'):
        b.check_installed({'bash': ('1', 'arm64')}, {'bash': '1'}, [], {'bash', 'extra'})


# covers: install.rungicos-image/E6
def test_command_sets_pins_before_resolution_and_uses_native_hooks(inputs):
    plan = b.validate_inputs(**inputs)
    cmd = b.mmdebstrap_command(plan, Path('/plan.json'), 123)
    assert '--architectures=arm64' in cmd and '--variant=minbase' in cmd
    assert '--mode=root' in cmd and '--format=directory' in cmd
    assert any(x.startswith('--setup-hook=') for x in cmd)
    assert any(x.startswith('--customize-hook=') for x in cmd)
    assert '--skip=essential/unlink' in cmd
    assert not any('qemu' in x for x in cmd)


# covers: install.rungicos-image/E6
def test_docker_wrapper_mounts_linux_output_and_translates_proxy(inputs):
    import bootstrap_rootfs_docker as d
    assert d.proxy_environment('HTTPEnable : 1\nHTTPProxy : 127.0.0.1\nHTTPPort : 6152\n') == {
        'http_proxy': 'http://host.docker.internal:6152'}
    cmd = d.run_command(inputs['repo'], inputs['release'], inputs['mozilla_keyring'],
                        '150.0', 'husky-test', 'rungic-rootfs', 'builder', b.DEFAULT_MIRROR, 123, {})
    assert '--privileged' in cmd and 'linux/arm64' in cmd
    assert 'type=volume,src=rungic-rootfs,dst=/output' in cmd
    assert f'type=bind,src={inputs["repo"]},dst=/pool,readonly' in cmd
    assert '/output/husky-test' in cmd


# covers: install.rungicos-image/E1
def test_environment_does_not_reserve_developer_home():
    env = b.build_environment({'PYTHONPYCACHEPREFIX': '/home/person/cache', 'HOME': '/home/person',
                               'https_proxy': 'http://proxy.invalid:123'}, 123)
    assert env['HOME'] == '/root' and env['SOURCE_DATE_EPOCH'] == '123'
    assert 'PYTHONPYCACHEPREFIX' not in env
    assert env['https_proxy'] == 'http://proxy.invalid:123'


# covers: install.rungicos-image/E6
def test_full_lock_replay_pins_base_packages_and_exact_artifact_bytes(inputs):
    plan = b.validate_inputs(**inputs)
    records = {name: dict(version=version, architecture='all' if name == 'rungic-release' else 'arm64',
                         sha256='a' * 64) for name, version in plan['pins'].items()}
    records.update({name: dict(version='1', architecture='arm64', sha256='b' * 64)
                    for name in ('bash', 'locales', 'ubuntu-keyring', 'libc6')})
    lock = inputs['repo'].parent / 'lock.json'
    lock.write_text(json.dumps({'schema': 1, 'packages': records}))
    replay = b.validate_inputs(**inputs, package_lock=lock)
    assert replay['pins']['libc6'] == '1'
    assert 'libc6=1' in replay['include']
    assert replay['locked'] == records


# covers: install.rungicos-image/E6
@pytest.mark.parametrize('changed', [False, True])
def test_artifact_collection_checks_real_bytes_with_fake_dpkg(inputs, changed):
    plan = b.validate_inputs(**inputs)
    output = Path(plan['output'])
    output.mkdir()
    cache = output / 'root/var/cache/apt/archives'
    cache.mkdir(parents=True)
    (cache / 'libc6.deb').write_bytes(b'upstream bytes')
    installed = {'libc6': ('1', 'arm64'), 'firefox': ('150.0', 'arm64')}
    if changed:
        plan['locked'] = {'libc6': dict(version='1', architecture='arm64', sha256='0' * 64)}

    def fake_run(command, **kwargs):
        if command[0] == 'apt-ftparchive':
            return SimpleNamespace(stdout=b'fake index\n')
        name = Path(command[2]).stem
        versions = {'libc6': '1', 'firefox': '150.0', 'rungic-release': '20261004.1', 'rungic-test': '1+rungic1'}
        arch = 'all' if name == 'rungic-release' else 'arm64'
        return SimpleNamespace(stdout=f'Package: {name}\nVersion: {versions[name]}\nArchitecture: {arch}\n')

    with patch.object(b.subprocess, 'run', side_effect=fake_run):
        if changed:
            with pytest.raises(ValueError, match='differs from package lock'):
                b.collect_artifacts(output / 'root', plan, installed)
        else:
            lock = b.collect_artifacts(output / 'root', plan, installed)
            assert set(lock['packages']) == set(installed)
            assert lock['packages']['libc6']['sha256'] == hashlib.sha256(b'upstream bytes').hexdigest()
            assert (output / 'debs/Release').is_file()


# covers: install.rungicos-image/E1
# covers: install.rungicos-image/E3
def test_customize_creates_locked_template_and_preserves_package_service_policy(inputs, monkeypatch):
    plan = b.validate_inputs(**inputs)
    root = Path(plan['output']) / 'root'
    root.mkdir(parents=True)
    installed = {name: (version, 'arm64') for name, version in plan['pins'].items()}
    installed['rungic-release'] = (plan['manifest']['version'], 'all')
    b.write(root, 'var/lib/dpkg/status', '\n\n'.join(
        f'Package: {n}\nVersion: {v}\nArchitecture: {a}\nStatus: install ok installed'
        for n, (v, a) in installed.items()))
    b.write(root, 'usr/share/rungic/release.json', json.dumps(plan['manifest']))
    b.write(root, 'usr/share/rungic/account-protocol', '2\n')
    b.write(root, 'etc/passwd', 'root:x:0:0::/root:/bin/bash\n')
    b.write(root, 'etc/shadow', 'root:!:0::::::\n')
    b.write(root, 'etc/apt/sources.list', 'build-only pool')
    b.write(root, 'etc/ssh/ssh_host_ed25519_key', 'build identity')
    b.write(root, 'etc/apt/keyrings/packages.mozilla.org.asc', inputs['mozilla_keyring'].read_text())
    b.setup(root, plan)
    calls = []

    def fake_chroot(tree, *command):
        calls.append(command)
        if command[0] == '/usr/sbin/useradd':
            b.write(tree, 'etc/passwd', 'root:x:0:0::/root:/bin/bash\nrungic:x:1000:1000::/home/rungic:/bin/bash\n')
            b.write(tree, 'etc/shadow', 'root:!:0::::::\nrungic:!:0::::::\n')
            (tree / 'home/rungic').mkdir(parents=True)
        return SimpleNamespace(stdout='', returncode=0)

    monkeypatch.setenv('SOURCE_DATE_EPOCH', '123')
    real_stat = Path.stat
    def template_stat(path, *args, **kwargs):
        actual = real_stat(path, *args, **kwargs)
        if path == root / 'home/rungic':
            return SimpleNamespace(st_uid=1000, st_gid=1000, st_mode=actual.st_mode)
        return actual
    with patch.object(b, 'chroot', side_effect=fake_chroot), \
         patch.object(b, 'collect_artifacts', return_value={'schema': 1, 'packages': {}}), \
         patch.object(Path, 'stat', template_stat):
        b.customize(root, plan)
    assert ('/usr/sbin/usermod', '--lock', 'rungic') in calls
    assert ('/usr/sbin/usermod', '--lock', 'root') in calls
    assert ('/usr/bin/systemctl', 'is-enabled', 'ssh.socket') in calls
    assert not any('disable' in command or 'mask' in command for command in calls)
    assert not (root / 'etc/ssh/ssh_host_ed25519_key').exists()
    assert (root / 'etc/machine-id').read_bytes() == b''
    assert not (root / 'etc/apt/preferences.d/zz-bootstrap-excluded').exists()
    assert 'file:' not in (root / 'etc/apt/sources.list.d/ubuntu.sources').read_text()
    assert (Path(plan['output']) / 'packages.lock.tsv').is_file()
