"""M5 B2: source-only Android Mali server and desktop-safe supervision, without a phone."""
import io
import json
import subprocess
import tarfile
from pathlib import Path

import pytest

import pq
import prepare_virgl_server as prepare

ROOT = Path(__file__).resolve().parents[2]


def archive(path, top, files):
    with tarfile.open(path, 'w:gz') as tar:
        for name, content in files.items():
            data = content.encode()
            entry = tarfile.TarInfo(f'{top}/{name}')
            entry.size = len(data)
            tar.addfile(entry, io.BytesIO(data))


# covers: apps.gpu/E7
@pytest.mark.parametrize('bad', [False, True])
def test_source_preparation_uses_both_pins_and_queue(tmp_path, monkeypatch, bad):
    """Never silently build a corrupted epoxy or import a Termux binary for husky."""
    packages = tmp_path / 'packages'
    cache = tmp_path / 'sources/virglrenderer-android'
    cache.mkdir(parents=True)
    archive(cache / 'virgl.tar.gz', 'virgl', {'COPYING': 'MIT', 'source.c': 'old\n'})
    archive(cache / 'epoxy.tar.gz', 'epoxy', {'COPYING': 'MIT epoxy', 'source.c': 'epoxy\n'})
    recipe = {'kind': 'upstream', 'tarball': 'virgl.tar.gz', 'epoxy_tarball': 'epoxy.tar.gz',
              'files': {p.name: pq.sha256(p) for p in cache.iterdir()}}
    patches = packages / 'virglrenderer-android/debian/patches'
    (patches / 'rungic').mkdir(parents=True)
    (patches.parent.parent / 'recipe.json').write_text(json.dumps(recipe))
    (patches / 'series').write_text('rungic/a.patch\n')
    (patches / 'rungic/a.patch').write_text('--- a/source.c\n+++ b/source.c\n@@ -1 +1 @@\n-old\n+new\n')
    monkeypatch.setattr(pq, 'PACKAGES', packages)
    monkeypatch.setattr(pq, 'SOURCES', tmp_path / 'sources')
    monkeypatch.setattr(pq, 'WORKSPACE', tmp_path)
    if bad:
        (cache / 'epoxy.tar.gz').write_bytes(b'corrupted')
        with pytest.raises(SystemExit, match='sha256'):
            prepare.prepare(tmp_path / '.work/output', offline=True)
        assert not (tmp_path / '.work/output').exists()
    else:
        tree = prepare.prepare(tmp_path / '.work/output', offline=True)
        assert (tree / 'source.c').read_text() == 'new\n'
        assert (tree / 'libepoxy/source.c').read_text() == 'epoxy\n'


# covers: apps.gpu/E6
def test_java_supervisor_and_unified_runner(tmp_path):
    """Run actual retries/state/stop/KGSL tests; the standard suite must run them too."""
    source = ROOT / 'android/app/src/com/rungic/plasma/RenderServer.java'
    test = ROOT / 'android/app/tests/RenderServerTest.java'
    subprocess.run(['javac', '-encoding', 'UTF-8', '-d', str(tmp_path), str(source), str(test)], check=True)
    subprocess.run(['java', '-cp', str(tmp_path), 'com.rungic.plasma.RenderServerTest', str(tmp_path)], check=True)
    runner = (ROOT / 'tools/run-tests.sh').read_text()
    assert 'android/app/src/com/rungic/plasma/RenderServer.java' in runner
    assert 'com.rungic.plasma.RenderServerTest' in runner
