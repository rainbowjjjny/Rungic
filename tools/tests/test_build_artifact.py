"""Exercise cache hits, invalidation, corruption and publication with real builds."""
import copy
import json
import os
import shutil
import subprocess
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build_artifact as b


class BuildCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'source').write_text('first')
        (self.root / 'tool').write_text('tool1')
        (self.root / 'dep').write_text('library1')
        self.cache = self.root / 'cache'
        self.recipe = dict(schema=1, component='test', target='test-arch', sources=['source'],
                           tools={'compiler': {'path': str(self.root / 'tool')}},
                           dependencies={'library': {'path': str(self.root / 'dep')}},
                           command=[sys.executable, '-c',
                                    'from pathlib import Path; Path("{output}/artifact").write_text(Path("{repo}/source").read_text())'],
                           outputs=['artifact'])

    def build(self, recipe=None):
        return b.execute(recipe or self.recipe, self.cache, self.root)

    # covers: delivery.build-fingerprint/E1
    def test_same_inputs_reuse_across_unrelated_edit(self):
        first = self.build()
        (self.root / 'README').write_text('unrelated docs')
        second = self.build()
        self.assertFalse(first['reused'])
        self.assertTrue(second['reused'])
        self.assertEqual(first['directory'], second['directory'])

    # covers: delivery.build-fingerprint/E2
    def test_changed_source_dependency_tool_parameters_architecture(self):
        baseline = b.inputs(self.recipe, self.root)
        for field in ('source', 'dep', 'tool'):
            path = self.root / field
            old = path.read_text()
            path.write_text(old + 'changed')
            self.assertNotEqual(baseline, b.inputs(self.recipe, self.root), field)
            path.write_text(old)
        for field, value in [('target', 'other'), ('parameters', {'feature': True}),
                             ('environment', {'CFLAGS': '-O0'})]:
            recipe = copy.deepcopy(self.recipe); recipe[field] = value
            self.assertNotEqual(baseline, b.inputs(recipe, self.root), field)

    # covers: delivery.build-fingerprint/E2
    def test_content_change_builds_new_cache_entry(self):
        first = self.build()
        (self.root / 'source').write_text('second')
        second = self.build()
        self.assertFalse(second['reused'])
        self.assertNotEqual(first['directory'], second['directory'])
        self.assertEqual((Path(second['directory']) / 'artifact').read_text(), 'second')

    # covers: delivery.build-fingerprint/E3
    def test_corrupt_output_cannot_hit(self):
        result = self.build()
        (Path(result['directory']) / 'artifact').write_text('corrupt')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            self.build()

    # covers: delivery.build-fingerprint/E3
    def test_missing_record_cannot_hit(self):
        result = self.build()
        (Path(result['directory']) / 'build.json').unlink()
        with self.assertRaises(FileNotFoundError):
            self.build()

    # covers: delivery.build-fingerprint/E3
    def test_input_mutation_during_build_never_publishes(self):
        self.recipe['command'][-1] += '; Path("{repo}/source").write_text("changed")'
        key = b.digest(b.inputs(self.recipe, self.root))
        with self.assertRaisesRegex(ValueError, 'changed during build'):
            self.build()
        self.assertFalse((self.cache / 'test' / key).exists())

    # covers: delivery.build-fingerprint/E3
    def test_failed_build_never_publishes(self):
        self.recipe['command'] = [sys.executable, '-c', 'raise SystemExit(7)']
        key = b.digest(b.inputs(self.recipe, self.root))
        with self.assertRaisesRegex(ValueError, 'build failed'):
            self.build()
        self.assertFalse((self.cache / 'test' / key).exists())

    # covers: delivery.build-fingerprint/E3
    def test_pinned_binary_mismatch_rejected(self):
        self.recipe['dependencies']['library']['sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'pinned dependency'):
            self.build()

    # covers: delivery.build-fingerprint/E1
    def test_input_locations_do_not_change_identity(self):
        before = b.inputs(self.recipe, self.root)
        other = self.root / 'elsewhere'; other.write_bytes((self.root / 'dep').read_bytes())
        self.recipe['dependencies']['library']['path'] = str(other)
        self.assertEqual(before, b.inputs(self.recipe, self.root))

    # covers: delivery.build-fingerprint/E2
    def test_source_mode_and_symlink_changes_invalidate(self):
        before = b.inputs(self.recipe, self.root)
        (self.root / 'source').chmod(0o755)
        self.assertNotEqual(before, b.inputs(self.recipe, self.root))
        (self.root / 'source').unlink(); (self.root / 'source').symlink_to('dep')
        self.assertNotEqual(before, b.inputs(self.recipe, self.root))

    # covers: delivery.build-fingerprint/E2
    def test_native_xattrs_invalidate_without_following_links(self):
        path = self.root / 'source'
        name = 'com.rungic.build-test' if sys.platform == 'darwin' else 'user.rungic-build-test'
        def set_attr(value):
            if sys.platform == 'darwin':
                subprocess.run(['/usr/bin/xattr', '-w', name, value, str(path)], check=True)
            else:
                os.setxattr(path, name, value.encode())
        set_attr('first')
        self.assertEqual(b.content(path)['xattrs'][name], b'first'.hex())
        first = b.inputs(self.recipe, self.root)
        set_attr('second')
        self.assertNotEqual(first, b.inputs(self.recipe, self.root))
        link = self.root / 'link'
        link.symlink_to(path.name)
        self.assertNotIn(name, b.xattrs(link))
        path.unlink()
        self.assertEqual(b.content(link)['kind'], 'symlink')

    # covers: delivery.build-fingerprint/E3
    def test_forged_record_and_output_traversal_rejected(self):
        report = self.build()['report']
        report['inputs']['target'] = 'different'
        with self.assertRaisesRegex(ValueError, 'fingerprint'):
            b.validate_record(report)
        self.recipe['outputs'] = ['../outside']
        with self.assertRaisesRegex(ValueError, 'relative path'):
            self.build()


@unittest.skipUnless(shutil.which('cc'), 'needs a C compiler')
class RealComponentTests(unittest.TestCase):
    """A real component of the install payload, the sparse image writer (tools/ci/sparse_write.c,
    docs/94's "静态入口/稀疏写入工具" stage), compiled from the repository by this machine's compiler."""

    # covers: delivery.build-fingerprint/E4
    def test_second_call_of_the_same_executor_hits(self):
        root = Path(__file__).resolve().parents[2]
        cc = Path(shutil.which('cc')).resolve()
        recipe = dict(schema=1, component='sparse-write', target=f'{os.uname().machine}-linux',
                      sources=['tools/ci/sparse_write.c'], tools={'cc': {'path': str(cc)}},
                      parameters={'cflags': ['-O2', '-Wall']},
                      command=[str(cc), '-O2', '-Wall', '-o', '{output}/rungic-sparse-write', '{repo}/tools/ci/sparse_write.c'],
                      outputs=['rungic-sparse-write'])
        with tempfile.TemporaryDirectory() as temp:
            cache = Path(temp) / 'cache'
            first = b.execute(recipe, cache, root)
            second = b.execute(copy.deepcopy(recipe), cache, root)
            self.assertFalse(first['reused'])
            self.assertTrue(second['reused'])
            self.assertEqual(second['directory'], first['directory'])
            self.assertEqual(second['report']['input_sha256'], first['report']['input_sha256'])
            self.assertEqual(second['report']['outputs'], first['report']['outputs'])
            self.assertEqual(len(list((cache / 'sparse-write').iterdir())), 2)    # the entry and its lock, no rebuild
            # What the cache holds is the working tool: a sparse image of the expected length.
            tool = Path(second['directory']) / 'rungic-sparse-write'
            image = Path(temp) / 'image'
            data = b'\0' * (2 << 20) + b'header' + b'\0' * 10
            run = subprocess.run([str(tool), str(image), str(len(data))], input=data, capture_output=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(image.read_bytes(), data)


if __name__ == '__main__':
    unittest.main()
