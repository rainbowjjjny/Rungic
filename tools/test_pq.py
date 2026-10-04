#!/usr/bin/env python3
"""tools/pq.py without docker or network: patch headers, lint rules, download checks and the
patch/test matrix (docs/71)."""
import hashlib
import io
import json
import os
import subprocess
import tarfile
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pq

GOOD = """From: Someone <someone@example.org>
Date: Fri, 25 Sep 2026 01:22:16 +0900
Subject: A minimum size above the maximum no longer disconnects
 the client

Why it is needed.

Forwarded: no
Last-Update: 2026-09-26
X-Rungic-Status: Pending
X-Rungic-Tests: L3:session.ready; L1:xdgshellwindow_test (to write)
X-Rungic-Docs: docs/42
---
 src/x.cpp | 1 +
diff --git a/src/x.cpp b/src/x.cpp
"""


class PackageTree(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for name, value in (('PACKAGES', self.root / 'packages'), ('SOURCES', self.root / 'sources')):
            p = patch.object(pq, name, value)
            p.start()
            self.addCleanup(p.stop)

    def package(self, patches, series=None, recipe=None):
        base = self.root / 'packages/demo/debian/patches'
        (base / 'rungic').mkdir(parents=True)
        for name, text in patches.items():
            (base / name).write_text(text)
        (base / 'series').write_text('\n'.join(series if series is not None else patches) + '\n')
        (self.root / 'packages/demo/recipe.json').write_text(json.dumps(recipe or {'files': {}}))


class HeaderTests(unittest.TestCase):
    # covers: delivery.patch-queue/E2
    def test_fields_and_continuation_lines(self):
        fields = pq.header(GOOD)
        self.assertEqual(fields['Subject'], 'A minimum size above the maximum no longer disconnects the client')
        self.assertEqual(fields['X-Rungic-Status'], 'Pending')
        self.assertEqual(fields['Last-Update'], '2026-09-26')
        self.assertNotIn('diff', ''.join(fields))

    # covers: delivery.patch-queue/E2
    def test_plain_dep3(self):
        fields = pq.header('Description: fix it\nAuthor: A <a@b>\nForwarded: not-needed\n\n--- a/x\n+++ b/x\n')
        self.assertEqual(fields['Description'], 'fix it')
        self.assertEqual(fields['Forwarded'], 'not-needed')


class LintTests(PackageTree):
    # covers: delivery.patch-queue/E2
    def test_good_package(self):
        self.package({'ubuntu.patch': 'Description: theirs\n---\n', 'rungic/a.patch': GOOD})
        self.assertEqual(pq.lint_package('demo'), [])

    # covers: delivery.patch-queue/E2
    def test_missing_fields_series_and_bad_values(self):
        bad = GOOD.replace('Forwarded: no\n', '').replace('2026-09-26', '26.9.2026').replace(
            'X-Rungic-Status: Pending', 'X-Rungic-Status: Maybe')
        submitted = GOOD.replace('X-Rungic-Status: Pending', 'X-Rungic-Status: Submitted')
        self.package({'rungic/a.patch': bad, 'rungic/b.patch': submitted, 'rungic/c.patch': GOOD},
                     series=['rungic/a.patch', 'rungic/b.patch', 'rungic/gone.patch'])
        problems = '\n'.join(pq.lint_package('demo'))
        for expected in ('series names a missing patch: rungic/gone.patch', 'rungic/c.patch is not in series',
                         'rungic/a.patch: no Forwarded', "Last-Update '26.9.2026'", "X-Rungic-Status 'Maybe'",
                         'rungic/b.patch: Submitted but Forwarded has no URL'):
            self.assertIn(expected, problems)

    # covers: delivery.patch-queue/E2
    def test_named_acceptance_scenarios_exist_unless_still_to_write(self):
        gone = GOOD.replace('L3:session.ready;', 'L3:cast.agent_screen;')
        planned = GOOD.replace('L3:session.ready;', 'L3:flatpak.dri-kgsl (to write);')
        self.package({'rungic/a.patch': gone, 'rungic/b.patch': planned})
        self.assertEqual(pq.lint_package('demo'), [
            'rungic/a.patch: X-Rungic-Tests names L3:cast.agent_screen, not a scenario of release/acceptance.json'])


class MatrixTests(PackageTree):
    # covers: delivery.patch-queue/E6
    def test_planned_tests_do_not_count(self):
        gap = GOOD.replace('X-Rungic-Tests: L3:session.ready; L1:xdgshellwindow_test (to write)',
                           'X-Rungic-Tests: none (gap: needs a TV)')
        self.package({'rungic/a.patch': GOOD, 'rungic/b.patch': gap})
        rows = {r['patch']: r for r in pq.test_matrix(['demo'])}
        self.assertEqual(rows['a.patch']['tests'], ['L3:session.ready'])
        self.assertEqual(rows['a.patch']['planned'], ['L1:xdgshellwindow_test'])
        self.assertEqual(rows['b.patch']['tests'], [])


class FetchTests(PackageTree):
    # covers: delivery.patch-queue/E1
    def test_checks_sha256_and_keeps_nothing_on_mismatch(self):
        data = b'upstream source'
        self.package({}, recipe={'fetch': 'https://example.org/{file}',
                                 'files': {'demo.orig.tar.xz': hashlib.sha256(data).hexdigest(),
                                           'demo.dsc': hashlib.sha256(b'other').hexdigest()}})
        opener = lambda url: io.BytesIO(data)
        with self.assertRaises(SystemExit) as failure:
            pq.fetch('demo', opener=opener)
        self.assertIn('demo.dsc', str(failure.exception))
        cache = self.root / 'sources/demo'
        self.assertEqual((cache / 'demo.orig.tar.xz').read_bytes(), data)
        self.assertEqual(sorted(p.name for p in cache.iterdir()), ['demo.orig.tar.xz'])

    # covers: delivery.patch-queue/E1
    def test_two_upstreams_keep_separate_urls_and_hashes(self):
        """Husky's Android vtest builds virgl + epoxy, without Termux binaries."""
        contents = {'virgl.tar.gz': b'virgl', 'epoxy.tar.gz': b'epoxy'}
        urls = {name: f'https://example.org/{name}' for name in contents}
        self.package({}, recipe={'fetch': urls, 'files': {
            name: hashlib.sha256(data).hexdigest() for name, data in contents.items()}})
        seen = []
        def opener(url):
            seen.append(url)
            return io.BytesIO(contents[url.rsplit('/', 1)[1]])
        pq.fetch('demo', opener=opener)
        self.assertEqual(seen, list(urls.values()))
        # Every archive is checked again; a corrupted cached epoxy cannot bypass its pin.
        (self.root / 'sources/demo/epoxy.tar.gz').write_bytes(b'corrupted')
        seen.clear()
        pq.fetch('demo', opener=opener)
        self.assertEqual(seen, [urls['epoxy.tar.gz']])


class OverlayTests(PackageTree):
    def setUp(self):
        super().setUp()
        p = patch.object(pq, 'WORKSPACE', self.root)
        p.start()
        self.addCleanup(p.stop)
        (self.root / 'shared').mkdir()
        (self.root / 'shared/ours.cpp').write_text('ours\n')
        self.tree = self.root / 'tree'
        (self.tree / 'src').mkdir(parents=True)
        (self.tree / 'src/theirs.cpp').write_text('theirs\n')

    def overlay(self, entries):
        self.package({}, recipe={'files': {}, 'overlay': entries})
        pq.add_overlay('demo', self.tree)

    # covers: delivery.patch-queue/E3
    def test_new_file(self):
        self.overlay({'src/new.cpp': 'shared/ours.cpp'})
        self.assertEqual((self.tree / 'src/new.cpp').read_text(), 'ours\n')

    # covers: delivery.patch-queue/E3
    def test_upstream_file_needs_its_hash(self):
        with self.assertRaisesRegex(SystemExit, 'exists in the upstream tree'):
            self.overlay({'src/theirs.cpp': 'shared/ours.cpp'})

    # covers: delivery.patch-queue/E3
    def test_replaces_upstream_file_with_the_recorded_hash(self):
        digest = hashlib.sha256(b'theirs\n').hexdigest()
        self.overlay({'src/theirs.cpp': {'from': 'shared/ours.cpp', 'replaces': digest}})
        self.assertEqual((self.tree / 'src/theirs.cpp').read_text(), 'ours\n')

    # covers: delivery.patch-queue/E3
    def test_refuses_once_upstream_changed(self):
        with self.assertRaisesRegex(SystemExit, 'upstream src/theirs.cpp changed'):
            self.overlay({'src/theirs.cpp': {'from': 'shared/ours.cpp', 'replaces': '0' * 64}})

    # covers: delivery.patch-queue/E3
    def test_refuses_when_the_replaced_file_is_gone(self):
        with self.assertRaisesRegex(SystemExit, 'which is gone'):
            self.overlay({'src/gone.cpp': {'from': 'shared/ours.cpp', 'replaces': '0' * 64}})


class VerifyTests(PackageTree):
    # covers: delivery.patch-queue/E5
    def test_unresolvable_symlink_is_a_difference(self):
        mine, ref = self.root / 'mine', self.root / 'ref'
        mine.mkdir()
        ref.mkdir()
        (mine / 'f').write_text('x')
        (ref / 'f').symlink_to('../nowhere/f')
        with patch.object(pq, 'source', return_value=mine):
            ok, text = pq.verify('demo', ref)
        self.assertFalse(ok)
        self.assertIn('No such file', text)


class GitSubtreeTests(PackageTree):
    """Exercise real git archives, subtree hashes and extraction without network/Docker."""
    def setUp(self):
        super().setUp()
        self.repo = self.root / 'upstream'
        self.repo.mkdir()
        self.git('init', '-q')
        for name, content in {'outside': 'not a build input', 'native/src/main.rs': 'host',
                              'native/logs/debug.txt': 'generated', 'other/main.rs': 'other'}.items():
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        self.git('add', '.')
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                 'commit', '-qm', 'baseline')
        self.info = {'kind': 'git', 'git': str(self.repo), 'commit': self.git('rev-parse', 'HEAD'),
                     'tree': self.git('rev-parse', 'HEAD:native'), 'subdir': 'native', 'files': {}}
        self.output = self.root / 'output'

    def git(self, *args):
        env = dict(os.environ, GIT_AUTHOR_DATE='2001-01-01T00:00:00Z', GIT_COMMITTER_DATE='2001-01-01T00:00:00Z')
        return subprocess.check_output(['git', *args], cwd=self.repo, text=True, env=env).strip()

    def write_recipe(self):
        path = self.root / 'packages/demo/recipe.json'
        if path.exists():
            path.write_text(json.dumps(self.info))
        else:
            self.package({}, recipe=self.info)

    # covers: delivery.patch-queue/E4
    def test_subtree_is_selected_and_exclusions_are_applied(self):
        self.info['exclude'] = ['logs']
        self.write_recipe()
        pq.source('demo', self.output)
        self.assertEqual((self.output / 'src/main.rs').read_text(), 'host')
        self.assertFalse((self.output / 'outside').exists())
        self.assertFalse((self.output / 'logs').exists())
        with tarfile.open(pq.orig_tarball('demo')) as archive:
            self.assertEqual({m.mtime for m in archive.getmembers()}, {978307200})

    # covers: delivery.patch-queue/E4
    def test_whole_commit_archive_keeps_existing_layout_and_timestamp(self):
        self.info.pop('subdir')
        self.info['tree'] = self.git('rev-parse', 'HEAD^{tree}')
        self.write_recipe()
        pq.source('demo', self.output)
        self.assertTrue((self.output / 'outside').exists())
        with tarfile.open(pq.orig_tarball('demo')) as archive:
            self.assertEqual(archive.pax_headers['comment'], self.info['commit'])
            self.assertEqual({m.mtime for m in archive.getmembers()}, {978307200})

    # covers: delivery.patch-queue/E4
    def test_changed_subtree_does_not_reuse_previous_archive(self):
        self.write_recipe()
        pq.source('demo', self.output)
        self.info.update(subdir='other', tree=self.git('rev-parse', 'HEAD:other'))
        self.write_recipe()
        pq.source('demo', self.output)
        self.assertEqual((self.output / 'main.rs').read_text(), 'other')
        self.assertFalse((self.output / 'src').exists())

    # covers: delivery.patch-queue/E4
    def test_wrong_tree_hash_rejects_source_even_after_previous_fetch(self):
        self.write_recipe()
        pq.fetch('demo')
        self.info['tree'] = '0' * 40
        self.write_recipe()
        with self.assertRaisesRegex(SystemExit, 'recipe says'):
            pq.fetch('demo')

    # covers: delivery.patch-queue/E4
    def test_missing_exclusion_requires_review(self):
        self.info['exclude'] = ['removed-in-new-version']
        self.write_recipe()
        with self.assertRaisesRegex(SystemExit, 'no longer exists'):
            pq.source('demo', self.output)

    # covers: delivery.patch-queue/E4
    def test_paths_cannot_escape_selected_tree(self):
        for value in ('../outside', '/outside', '.'):
            with self.subTest(value=value), self.assertRaisesRegex(SystemExit, 'invalid source path'):
                pq.relative_source_path(value)


class AndroidHostTests(PackageTree):
    """tools/prepare_android_host.py: the pinned host with its overlay and the separately pinned
    Smithay and Winit, assembled by the real pq.source from small upstreams (no patches: no Docker)."""
    def setUp(self):
        super().setUp()
        p = patch.object(pq, 'WORKSPACE', self.root)
        p.start()
        self.addCleanup(p.stop)
        (self.root / 'android/host/src').mkdir(parents=True)
        (self.root / 'android/host/src/ours.rs').write_text('// a Rungic-only module\n')
        trees = {'android-host': {'Cargo.toml': '[package]\n', 'src/main.rs': 'fn main() {}\n',
                                  'lib/smithay/stale.rs': 'an unpinned copy upstream carries\n'},
                 'smithay': {'Cargo.toml': '[package] smithay\n'}, 'winit': {'Cargo.toml': '[package] winit\n'}}
        for name, files in trees.items():
            top = self.root / 'upstream' / f'{name}-1'
            for path, text in files.items():
                (top / path).parent.mkdir(parents=True, exist_ok=True)
                (top / path).write_text(text)
            tarball = self.root / 'sources' / name / f'{name}-1.tar.gz'
            tarball.parent.mkdir(parents=True)
            with tarfile.open(tarball, 'w:gz') as tar:
                tar.add(top, arcname=top.name)
            recipe = {'kind': 'upstream', 'version': '1', 'tarball': tarball.name, 'fetch': 'https://example.invalid/{file}',
                      'files': {tarball.name: pq.sha256(tarball)}}
            if name == 'android-host':
                recipe.update(exclude=['lib'], overlay={'src/android/ours.rs': 'android/host/src/ours.rs'})
            (self.root / 'packages' / name).mkdir(parents=True)
            (self.root / 'packages' / name / 'recipe.json').write_text(json.dumps(recipe))

    # covers: delivery.patch-queue/E8
    def test_assembled_in_work_from_the_three_pinned_sources(self):
        import prepare_android_host
        output = prepare_android_host.prepare()
        self.assertEqual(output, (self.root / '.work/build/android-host/source').resolve())
        files = sorted(p.relative_to(output).as_posix() for p in output.rglob('*') if p.is_file())
        self.assertEqual(files, ['Cargo.toml', 'lib/smithay/Cargo.toml', 'lib/winit/Cargo.toml', 'src/android/ours.rs',
                                 'src/main.rs'])
        self.assertEqual((output / 'src/android/ours.rs').read_text(), '// a Rungic-only module\n')
        self.assertEqual((output / 'lib/smithay/Cargo.toml').read_text(), '[package] smithay\n')
        # Nothing outside .work: not the repository, not .work itself.
        for elsewhere in (self.root / 'android/host/build', self.root / '.work', self.root / '.work/../out', Path('/tmp/x')):
            with self.subTest(elsewhere=elsewhere), self.assertRaisesRegex(SystemExit, 'inside .work'):
                prepare_android_host.prepare(elsewhere)
        self.assertFalse((self.root / 'android/host/build').exists() or (self.root / 'out').exists())


def pq_image():
    """The pinned patch-queue toolchain image (tools/pq/Dockerfile) is here."""
    import shutil
    engine = shutil.which('docker') or shutil.which('podman')
    return bool(engine) and subprocess.run([engine, 'image', 'inspect', pq.IMAGE], capture_output=True).returncode == 0


UBUNTU_PATCH = """Description: The distribution's own fix, in quilt's form (gbp would rewrite it)
Author: Ubuntu Developers <ubuntu-devel-discuss@lists.ubuntu.com>
Forwarded: not-needed
Index: demo-1.0/src/a.c
===================================================================
--- demo-1.0.orig/src/a.c
+++ demo-1.0/src/a.c
@@ -1,4 +1,4 @@
 int a(void)
 {
-    return 1;
+    return 2;
 }
"""

# As gbp pq export writes ours (the index line carries the blobs' real ids).
OUR_PATCH = """From: Someone <someone@example.org>
Date: Fri, 25 Sep 2026 01:22:16 +0900
Subject: b() returns three

Why it is needed.

Forwarded: no
Last-Update: 2026-09-26
X-Rungic-Status: Pending
X-Rungic-Tests: L0:round-trip
X-Rungic-Docs: docs/71
---
 src/b.c | 2 +-
 1 file changed, 1 insertion(+), 1 deletion(-)

diff --git a/src/b.c b/src/b.c
index 88954f6..473b4ce 100644
--- a/src/b.c
+++ b/src/b.c
@@ -1,4 +1,4 @@
 int b(void)
 {
-    return 1;
+    return 3;
 }
"""


@unittest.skipUnless(pq_image(), f'needs the patch-queue image {pq.IMAGE} (tools/pq/Dockerfile)')
class RoundTripTests(PackageTree):
    """prepare and export with the real quilt and gbp of the pinned image, on a small component with
    an Ubuntu patch and one of ours (docs/71)."""
    def setUp(self):
        super().setUp()
        p = patch.object(pq, 'WORKSPACE', self.root)
        p.start()
        self.addCleanup(p.stop)
        upstream = self.root / 'upstream/demo-1.0/src'
        upstream.mkdir(parents=True)
        for name in ('a', 'b'):
            (upstream / f'{name}.c').write_text(f'int {name}(void)\n{{\n    return 1;\n}}\n')
        tarball = self.root / 'sources/demo/demo-1.0.tar.gz'
        tarball.parent.mkdir(parents=True)
        with tarfile.open(tarball, 'w:gz') as tar:
            tar.add(upstream.parent, arcname='demo-1.0')
        self.package({'ubuntu-fix.patch': UBUNTU_PATCH, 'rungic/0001-b-returns-three.patch': OUR_PATCH},
                     recipe={'kind': 'upstream', 'version': '1.0', 'tarball': tarball.name,
                             'files': {tarball.name: pq.sha256(tarball)}, 'fetch': 'https://example.invalid/{file}'})
        debian = self.root / 'packages/demo/debian'
        (debian / 'source').mkdir()
        (debian / 'source/format').write_text('3.0 (quilt)\n')
        self.patches = debian / 'patches'

    def files(self):
        return {str(p.relative_to(self.patches)): p.read_text() for p in sorted(self.patches.rglob('*')) if p.is_file()}

    # covers: delivery.patch-queue/E7
    def test_prepare_then_export_changes_no_patch(self):
        before = self.files()
        work = pq.prepare('demo')
        # The editing tree: one commit per patch, ours last, on gbp's patch-queue branch.
        log = subprocess.run(['git', 'log', '--format=%s', 'patch-queue/rungic'], cwd=work, capture_output=True,
                             text=True, check=True).stdout.splitlines()
        self.assertEqual(log[0], 'b() returns three')
        self.assertEqual(pq.export('demo'), ['0001-b-returns-three.patch'])
        self.assertEqual(self.files(), before)
        # gbp rewrote the distribution's patch in its own tree; only ours and the series came back.
        self.assertNotEqual((work / 'debian/patches/ubuntu-fix.patch').read_text(), before['ubuntu-fix.patch'])


if __name__ == '__main__':
    unittest.main()
