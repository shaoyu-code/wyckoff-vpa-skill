"""Packaging tests, not TV tests. Uses synthetic sources except explicit roundtrip."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('r3_builder', HERE / 'build_r3.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class DeltaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.package = self.root / 'pkg'
        (self.package / 'parts').mkdir(parents=True)
        self.data = b'synthetic delta\n'
        self.m = {'artifact_kind': builder.KIND, 'production_installable': False,
                  'parts': [{'path': 'parts/01.patchpart', 'bytes': len(self.data),
                             'sha256': builder.sha(self.data)}],
                  'patch_sha256': builder.sha(self.data), 'files': []}
        (self.package / 'parts/01.patchpart').write_bytes(self.data)
        self.save()
    def save(self):
        (self.package / 'SOURCE_DELTA.json').write_text(json.dumps(self.m))
    def test_exact_parts(self):
        self.assertEqual(builder.load_delta(self.package)[1], self.data)
    def test_changed_part(self):
        (self.package / 'parts/01.patchpart').write_bytes(b'changed')
        with self.assertRaisesRegex(builder.RebuildError, 'CHECKSUM'):
            builder.load_delta(self.package)
    def test_missing_part(self):
        (self.package / 'parts/01.patchpart').unlink()
        with self.assertRaises(OSError): builder.load_delta(self.package)
    def test_duplicate_part(self):
        self.m['parts'] *= 2; self.save()
        with self.assertRaisesRegex(builder.RebuildError, 'PART_ID'):
            builder.load_delta(self.package)
    def test_path_escape(self):
        self.m['parts'][0]['path'] = 'parts/../../outside'; self.save()
        with self.assertRaisesRegex(builder.RebuildError, 'UNSAFE'):
            builder.load_delta(self.package)
    def test_part_symlink(self):
        target = self.package / 'parts/01.patchpart'; target.unlink()
        outside = self.root / 'outside'; outside.write_bytes(self.data); target.symlink_to(outside)
        with self.assertRaisesRegex(builder.RebuildError, 'SYMLINK'):
            builder.load_delta(self.package)
    def test_combined_hash(self):
        self.m['patch_sha256'] = 'wrong'; self.save()
        with self.assertRaisesRegex(builder.RebuildError, 'COMBINED'):
            builder.load_delta(self.package)
    def test_file_path_escape(self):
        self.m['files'] = [{'path': '../escape'}]; self.save()
        with self.assertRaisesRegex(builder.RebuildError, 'UNSAFE'):
            builder.load_delta(self.package)
    def test_not_installer(self):
        self.m['production_installable'] = True; self.save()
        with self.assertRaisesRegex(builder.RebuildError, 'SOURCE_ONLY'):
            builder.load_delta(self.package)
    def test_output_no_overwrite(self):
        out = self.root / 'out.zip'; out.write_bytes(b'keep')
        with self.assertRaises(builder.RebuildError):
            builder.build(self.root / 'none.zip', out, self.package)
        self.assertEqual(out.read_bytes(), b'keep')
    def test_zip_readback(self):
        out = self.root / 'out.zip'; records = {'SKILL.md': b'synthetic'}
        builder.write_source_zip(out, records)
        self.assertEqual(builder.read_source_zip(out), records)
        self.assertEqual(out.stat().st_mode & 0o777, 0o600)
    def test_zip_verification_failure_removes_only_new_output(self):
        out = self.root / 'out.zip'
        with patch.object(builder, 'read_source_zip', return_value={}):
            with self.assertRaisesRegex(builder.RebuildError, 'READBACK'):
                builder.write_source_zip(out, {'SKILL.md': b'synthetic'})
        self.assertFalse(out.exists())
    def test_duplicate_zip_member(self):
        import warnings, zipfile
        path = self.root / 'bad.zip'
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            with zipfile.ZipFile(path, 'w') as z:
                z.writestr(builder.PREFIX + 'a', b'x'); z.writestr(builder.PREFIX + 'a', b'y')
        with self.assertRaisesRegex(builder.RebuildError, 'DUPLICATE'):
            builder.read_source_zip(path)
    def test_zip_traversal(self):
        import zipfile
        path = self.root / 'bad.zip'
        with zipfile.ZipFile(path, 'w') as z: z.writestr(builder.PREFIX + '../a', b'x')
        with self.assertRaisesRegex(builder.RebuildError, 'UNSAFE'): builder.read_source_zip(path)


if __name__ == '__main__': unittest.main()
