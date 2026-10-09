"""Candidate ZIP reconstruction only; never a production installation test."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

spec=importlib.util.spec_from_file_location('builder',Path(__file__).with_name('build_candidate.py'))
builder=importlib.util.module_from_spec(spec);spec.loader.exec_module(builder)

class CandidateBuilderTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.pkg=self.root/'pkg';self.pkg.mkdir()
        self.baseline=self.root/'base.zip';self.out=self.root/'candidate.zip'
        with zipfile.ZipFile(self.baseline,'w') as archive:
            archive.writestr('global-market-intelligence/a.py','value = 1\n')
        delta='--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-value = 1\n+value = 2\n'
        (self.pkg/'existing_code.patch').write_text(delta)
        (self.pkg/'new.py').write_text('extra = True\n')
        self.manifest={
         'artifact_kind':'GMI_SOURCE_DELTA_NOT_PRODUCTION_INSTALLER',
         'baseline_zip_sha256':builder.sha(self.baseline.read_bytes()),
         'baseline_root':'global-market-intelligence',
         'patch':{'path':'existing_code.patch','sha256':builder.sha(delta.encode())},
         'files':{'a.py':{'before_sha256':builder.sha(b'value = 1\n'),'after_sha256':builder.sha(b'value = 2\n')},
                  'b.py':{'before_sha256':None,'after_sha256':builder.sha(b'extra = True\n'),'new_source':'new.py'}}}
        self.save()
    def save(self):
        (self.pkg/'PATCH_MANIFEST.json').write_text(json.dumps(self.manifest))
    def run_build(self):
        return builder.build(self.baseline,self.out,self.pkg)
    def test_exact_reconstruction(self):
        original=self.baseline.read_bytes()
        self.assertEqual(self.run_build()['changed_files'],2)
        self.assertEqual(self.baseline.read_bytes(),original)
        with zipfile.ZipFile(self.out) as archive:
            self.assertEqual(archive.read('global-market-intelligence/a.py'),b'value = 2\n')
            self.assertEqual(archive.read('global-market-intelligence/b.py'),b'extra = True\n')
    def test_reject_existing_output(self):
        self.out.write_bytes(b'preserve')
        with self.assertRaises(builder.BuildError):self.run_build()
        self.assertEqual(self.out.read_bytes(),b'preserve')
    def test_wrong_baseline_blocked(self):
        self.baseline.write_bytes(b'wrong')
        with self.assertRaises(builder.BuildError):self.run_build()
        self.assertFalse(self.out.exists())
    def test_modified_patch_blocked(self):
        (self.pkg/'existing_code.patch').write_text('wrong')
        with self.assertRaises(builder.BuildError):self.run_build()
    def test_new_source_tamper_blocked(self):
        (self.pkg/'new.py').write_text('wrong')
        with self.assertRaises(builder.BuildError):self.run_build()
    def test_source_path_escape_blocked(self):
        self.manifest['files']['b.py']['new_source']='../outside.py';self.save()
        with self.assertRaises(builder.BuildError):self.run_build()
    def test_reject_non_zip_destination(self):
        self.out=self.root/'active-skill'
        with self.assertRaises(builder.BuildError):self.run_build()
    def test_after_hash_mismatch_blocked(self):
        self.manifest['files']['a.py']['after_sha256']='0'*64;self.save()
        with self.assertRaises(builder.BuildError):self.run_build()
        self.assertFalse(self.out.exists())

if __name__=='__main__':unittest.main()
