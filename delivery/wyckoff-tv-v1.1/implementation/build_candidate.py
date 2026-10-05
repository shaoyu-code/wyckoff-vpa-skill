#!/usr/bin/env python3
"""Reproduce a source candidate ZIP from an exact historical baseline. NOT an installer.

Reads the specified baseline ZIP only. Uses an isolated temporary directory and
git apply, then verifies all changed files and every unchanged baseline file.
Never writes into a Skill directory, changes registrations, starts TV, or networks.
"""
from __future__ import annotations
import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import tempfile
import zipfile

class BuildError(ValueError):
    pass

def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def safe_name(name: str) -> str:
    p=PurePosixPath(name)
    if not name or p.is_absolute() or '..' in p.parts or '\\' in name or str(p)!=name:
        raise BuildError('Unsafe relative path')
    return name

def load_package(package: Path):
    manifest=json.loads((package/'PATCH_MANIFEST.json').read_text(encoding='utf-8'))
    if manifest.get('artifact_kind')!='GMI_SOURCE_DELTA_NOT_PRODUCTION_INSTALLER':
        raise BuildError('Wrong artifact kind')
    patch=package/safe_name(manifest['patch']['path'])
    patch_data=patch.read_bytes()
    if patch.is_symlink() or sha(patch_data)!=manifest['patch']['sha256']:
        raise BuildError('Patch checksum mismatch')
    new={}
    for name,info in manifest['files'].items():
        safe_name(name)
        if info['before_sha256'] is None:
            source=package/safe_name(info['new_source'])
            if source.resolve()!=package.resolve()/info['new_source']:
                raise BuildError('Internal source symlink is not allowed')
            data=source.read_bytes()
            if sha(data)!=info['after_sha256']:
                raise BuildError('New source checksum mismatch: '+name)
            new[name]=data
    return manifest,patch_data,new

def build(baseline: Path, out: Path, package: Path|None=None):
    package=package or Path(__file__).resolve().parent
    manifest,patch_data,new=load_package(package)
    if out.exists() or out.is_symlink() or not out.parent.is_dir():
        raise BuildError('Output must be a new ZIP in an existing directory')
    if out.suffix.lower()!='.zip':
        raise BuildError('Only a candidate ZIP may be written; this is not an installer')
    if baseline.stat().st_size>150_000_000:
        raise BuildError('Baseline file size limit exceeded')
    original=baseline.read_bytes()
    if sha(original)!=manifest['baseline_zip_sha256']:
        raise BuildError('Historical baseline mismatch; never overwrite current local patches')
    prefix=manifest['baseline_root']+'/'
    records={}
    with zipfile.ZipFile(io.BytesIO(original)) as archive:
        if sum(i.file_size for i in archive.infolist())>150_000_000 or len(archive.infolist())>10000:
            raise BuildError('Baseline archive limits exceeded')
        for info in archive.infolist():
            if info.is_dir():
                continue
            if not info.filename.startswith(prefix) or stat.S_ISLNK(info.external_attr>>16):
                raise BuildError('Unexpected ZIP root or symlink')
            name=safe_name(info.filename[len(prefix):])
            if name in records:
                raise BuildError('Duplicate ZIP member')
            records[name]=archive.read(info)
    for name,info in manifest['files'].items():
        before=info['before_sha256']
        if (before is None and name in records) or (before is not None and sha(records.get(name,b''))!=before):
            raise BuildError('Before-file checksum mismatch: '+name)
    with tempfile.TemporaryDirectory(prefix='gmi-tv11-reproduce-') as tmp:
        root=Path(tmp)/'source';root.mkdir()
        patch=Path(tmp)/'delta.patch';patch.write_bytes(patch_data)
        for name,data in records.items():
            p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data)
        for extra in (['--check'],[]):
            result=subprocess.run(['git','apply',*extra,str(patch.resolve())],cwd=root,
                                  stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=20)
            if result.returncode:
                raise BuildError('Exact patch application failed: '+result.stderr.decode(errors='replace')[-1000:])
        for name,data in new.items():
            p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data)
        rebuilt={p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob('*') if p.is_file()}
        if set(rebuilt)!=set(records)|set(new):
            raise BuildError('Unexpected candidate file set')
        for name,data in rebuilt.items():
            expected=manifest['files'].get(name,{}).get('after_sha256') or sha(records[name])
            if sha(data)!=expected:
                raise BuildError('Rebuilt file mismatch: '+name)
        fd=os.open(out,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        try:
            with os.fdopen(fd,'wb') as stream:
                with zipfile.ZipFile(stream,'w',zipfile.ZIP_DEFLATED) as archive:
                    for name,data in sorted(rebuilt.items()):
                        archive.writestr(prefix+name,data)
                    archive.writestr('CANDIDATE_NOTICE.json',json.dumps({
                        'status':'SOURCE_CANDIDATE_NOT_DEPLOYABLE',
                        'source_files':len(rebuilt),'production_modified':False,
                        'latest_local_patch_compatibility':'NOT_VERIFIED'},indent=2)+'\n')
                stream.flush();os.fsync(stream.fileno())
            with zipfile.ZipFile(out) as archive:
                if archive.testzip() is not None:
                    raise BuildError('Candidate archive verification failed')
                for name,data in rebuilt.items():
                    if archive.read(prefix+name)!=data:
                        raise BuildError('Candidate archive content mismatch')
        except BaseException:
            out.unlink(missing_ok=True)
            raise
    return {'status':'SOURCE_CANDIDATE_NOT_DEPLOYABLE','source_files':len(rebuilt),
            'changed_files':len(manifest['files']),'sha256':sha(out.read_bytes()),
            'production_modified':False}

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-zip',type=Path,required=True)
    parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args()
    try:
        print(json.dumps(build(args.baseline_zip.expanduser(),args.out.expanduser()),ensure_ascii=False,indent=2))
    except (BuildError,OSError,ValueError,subprocess.SubprocessError,zipfile.BadZipFile) as exc:
        parser.exit(2,'CANDIDATE_BUILD_BLOCKED: '+str(exc)+'\n')

if __name__=='__main__':
    main()
