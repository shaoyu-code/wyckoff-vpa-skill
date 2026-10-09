import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import zipfile

MODULE_PATH = Path(__file__).resolve().parents[1] / 'tools' / 'collect_runtime_baseline.py'
spec = importlib.util.spec_from_file_location('collector', MODULE_PATH)
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)


class BaselineExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / 'skill'
        self.root.mkdir()
        (self.root / 'SKILL.md').write_text('---\nname: example\n---\n', encoding='utf-8')
        (self.root / 'main.py').write_text('value = 1\n', encoding='utf-8')
        self.roots = {'gmi': self.root}
        self.out = self.base / 'baseline.zip'

    def test_auth_source_code_retained(self):
        (self.root / 'auth.py').write_text('def login(): pass\n')
        files, _ = collector.collect(self.roots)
        self.assertIn('gmi/auth.py', files)

    def test_empty_roots_refused(self):
        with self.assertRaises(collector.ExportError):
            collector.export({}, self.out)

    def test_destination_symlink_refused(self):
        destination = self.base / 'existing.zip'
        destination.write_bytes(b'preserve')
        self.out.symlink_to(destination)
        with self.assertRaises(collector.ExportError):
            collector.export(self.roots, self.out)
        self.assertEqual(destination.read_bytes(), b'preserve')

    def test_roundtrip(self):
        result = collector.export(self.roots, self.out)
        self.assertEqual(result['status'], 'SOURCE_EXPORTED_NOT_DEPLOYED')
        self.assertEqual(result['source_files'], 2)
        with zipfile.ZipFile(self.out) as z:
            self.assertEqual(z.read('gmi/main.py'), b'value = 1\n')
            m = json.loads(z.read('BASELINE_MANIFEST.json'))
            self.assertEqual(m['files']['gmi/main.py']['sha256'], hashlib.sha256(b'value = 1\n').hexdigest())
            self.assertFalse(m['production_modified'])
            self.assertNotIn(str(self.root), json.dumps(m))

    def test_source_not_modified(self):
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        collector.export(self.roots, self.out)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})

    def test_overwrite_refused(self):
        self.out.write_bytes(b'preserve')
        with self.assertRaises(collector.ExportError):
            collector.export(self.roots, self.out)
        self.assertEqual(self.out.read_bytes(), b'preserve')

    def test_output_inside_source_refused(self):
        with self.assertRaises(collector.ExportError):
            collector.export(self.roots, self.root / 'export.zip')

    def test_private_files_excluded(self):
        for name in ['.env', '.env.production', 'auth.json', 'cookies.txt', 'private.key']:
            (self.root / name).write_text('PRIVATE', encoding='utf-8')
        files, manifest = collector.collect(self.roots)
        self.assertEqual(len(files), 2)
        self.assertEqual(len(manifest['exclusions']), 5)

    def test_runtime_directories_excluded(self):
        for name in ['workspace', 'runs', '.venv', 'node_modules', 'raw', 'books']:
            (self.root / name).mkdir()
            (self.root / name / 'private.json').write_text('{}')
        files, _ = collector.collect(self.roots)
        self.assertEqual(len(files), 2)

    def test_binary_book_excluded(self):
        (self.root / 'book.pdf').write_bytes(b'%PDF')
        files, manifest = collector.collect(self.roots)
        self.assertEqual(len(files), 2)
        self.assertEqual(manifest['exclusions'][0]['reason'], 'not_source_text')

    def test_internal_file_symlink_excluded(self):
        secret = self.base / 'outside.txt'
        secret.write_text('PRIVATE')
        (self.root / 'escape.txt').symlink_to(secret)
        files, _ = collector.collect(self.roots)
        self.assertNotIn('gmi/escape.txt', files)

    def test_internal_directory_symlink_excluded(self):
        (self.root / 'escape').symlink_to(self.base, target_is_directory=True)
        files, _ = collector.collect(self.roots)
        self.assertEqual(len(files), 2)

    def test_registered_root_symlink_preserved(self):
        link = self.base / 'registered'
        link.symlink_to(self.root, target_is_directory=True)
        result = collector.export({'gmi': link}, self.out)
        self.assertEqual(result['source_files'], 2)
        self.assertTrue(link.is_symlink())

    def test_secret_detection_blocks_all(self):
        (self.root / 'config.py').write_text('token = "' + 'ghp_' + 'a' * 35 + '"')
        with self.assertRaises(collector.ExportError):
            collector.export(self.roots, self.out)
        self.assertFalse(self.out.exists())

    def test_private_key_blocks(self):
        (self.root / 'config.py').write_text('-----BEGIN PRIVATE KEY-----')
        with self.assertRaises(collector.ExportError):
            collector.export(self.roots, self.out)
        self.assertFalse(self.out.exists())

    def test_label_traversal_refused(self):
        with self.assertRaises(collector.ExportError):
            collector.export({'../other': self.root}, self.out)

    def test_root_requires_skill(self):
        (self.root / 'SKILL.md').unlink()
        with self.assertRaises(collector.ExportError):
            collector.export(self.roots, self.out)

    def test_changed_source_refused(self):
        original = collector.collect
        calls = 0
        def changed(roots):
            nonlocal calls
            calls += 1
            if calls == 2:
                (self.root / 'main.py').write_text('value = 2\n')
            return original(roots)
        with mock.patch.object(collector, 'collect', side_effect=changed):
            with self.assertRaises(collector.ExportError):
                collector.export(self.roots, self.out)
        self.assertFalse(self.out.exists())

    def test_non_utf8_refused(self):
        (self.root / 'bad.py').write_bytes(b'\xff\xfe')
        with self.assertRaises(collector.ExportError):
            collector.export(self.roots, self.out)

    def test_oversized_source_refused(self):
        with mock.patch.object(collector, 'MAX_FILE', 1):
            with self.assertRaises(collector.ExportError):
                collector.export(self.roots, self.out)

    def test_private_archive_permissions(self):
        collector.export(self.roots, self.out)
        self.assertEqual(self.out.stat().st_mode & 0o777, 0o600)

    def test_two_roots(self):
        other = self.base / 'engine'
        other.mkdir()
        (other / 'SKILL.md').write_text('engine')
        result = collector.export({'gmi': self.root, 'wyckoff': other}, self.out)
        self.assertEqual(result['source_files'], 3)

    def test_failed_zip_verification_removes_archive(self):
        with mock.patch.object(zipfile.ZipFile, 'testzip', return_value='broken'):
            with self.assertRaises(collector.ExportError):
                collector.export(self.roots, self.out)
        self.assertFalse(self.out.exists())


if __name__ == '__main__':
    unittest.main()
