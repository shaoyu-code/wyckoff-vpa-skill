#!/usr/bin/env python3
"""Reproduce r3 SOURCE ONLY in temporary storage; never install or contact TV.

Run from a pinned repository checkout. The inherited r1 builder is hash-checked
before import. Both complete source inventories and every patch part are verified.
Unknown current Mac patches are not merged by this tool. No project code executes.
"""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import tempfile
import zipfile

PREFIX = 'global-market-intelligence/'
KIND = 'R2_R3_SOURCE_DELTA_FROM_PUBLISHED_R1_NOT_INSTALLER'


class RebuildError(ValueError):
    pass


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe(name: str) -> str:
    p = PurePosixPath(name)
    if not name or p.is_absolute() or '..' in p.parts or '\\' in name or str(p) != name:
        raise RebuildError('UNSAFE_PACKAGE_PATH')
    return name


def fingerprint(files: dict[str, bytes]) -> str:
    return sha(json.dumps({k: sha(v) for k, v in sorted(files.items())},
                          sort_keys=True, separators=(',', ':')).encode())


def read_checked(package: Path, name: str) -> bytes:
    name = safe(name)
    path = package / name
    if path.resolve(strict=True) != package.resolve() / name or not path.is_file():
        raise RebuildError('PACKAGE_SYMLINK_OR_NONFILE')
    if path.stat().st_size > 20_000_000:
        raise RebuildError('PACKAGE_FILE_TOO_LARGE')
    return path.read_bytes()


def load_delta(package: Path):
    manifest = json.loads(read_checked(package, 'SOURCE_DELTA.json'))
    if manifest.get('artifact_kind') != KIND or manifest.get('production_installable') is not False:
        raise RebuildError('NOT_A_SOURCE_ONLY_DELTA')
    parts = manifest['parts']
    if not isinstance(parts, list) or not 1 <= len(parts) <= 100:
        raise RebuildError('PART_LIST_INVALID')
    chunks = []
    seen = set()
    for part in parts:
        name = part['path']
        if name in seen or not name.startswith('parts/'):
            raise RebuildError('PART_ID_INVALID')
        seen.add(name)
        data = read_checked(package, name)
        if len(data) != part['bytes'] or sha(data) != part['sha256']:
            raise RebuildError('PART_CHECKSUM_MISMATCH:' + name)
        chunks.append(data)
    delta = b''.join(chunks)
    if sha(delta) != manifest['patch_sha256']:
        raise RebuildError('COMBINED_PATCH_MISMATCH')
    files = manifest['files']
    if not isinstance(files, list) or len(files) != len({f['path'] for f in files}):
        raise RebuildError('FILE_LIST_INVALID')
    for item in files:
        safe(item['path'])
    return manifest, delta


def read_source_zip(path: Path) -> dict[str, bytes]:
    records = {}
    with zipfile.ZipFile(path) as archive:
        members = archive.infolist()
        if len(members) > 10000 or sum(i.file_size for i in members) > 150_000_000:
            raise RebuildError('ZIP_LIMIT_EXCEEDED')
        for info in members:
            if info.is_dir() or info.filename == 'CANDIDATE_NOTICE.json':
                continue
            if not info.filename.startswith(PREFIX) or stat.S_ISLNK(info.external_attr >> 16):
                raise RebuildError('ZIP_ROOT_OR_SYMLINK')
            name = safe(info.filename[len(PREFIX):])
            if name in records:
                raise RebuildError('DUPLICATE_ZIP_MEMBER')
            records[name] = archive.read(info)
    return records


def write_source_zip(out: Path, records: dict[str, bytes]):
    # Exclusive creation refuses existing paths including dangling symlinks.
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'wb') as raw:
            with zipfile.ZipFile(raw, 'w', zipfile.ZIP_DEFLATED) as archive:
                for name, data in sorted(records.items()):
                    archive.writestr(PREFIX + name, data)
                archive.writestr('CANDIDATE_NOTICE.json', json.dumps({
                    'status': 'SOURCE_CANDIDATE_NOT_DEPLOYABLE', 'revision': 'r3',
                    'source_files': len(records), 'production_modified': False,
                    'current_mac_compatibility': 'NOT_VERIFIED'}) + '\n')
            raw.flush()
            os.fsync(raw.fileno())
        if read_source_zip(out) != records:
            raise RebuildError('OUTPUT_READBACK_MISMATCH')
    except BaseException:
        out.unlink(missing_ok=True)
        raise


def build(baseline: Path, out: Path, package: Path | None = None,
          r1_package: Path | None = None):
    package = (package or Path(__file__).resolve().parent).resolve()
    r1_package = (r1_package or package.parent / 'implementation').resolve()
    baseline, out = baseline.expanduser(), out.expanduser().absolute()
    if out.exists() or out.is_symlink() or out.suffix.lower() != '.zip' or not out.parent.is_dir():
        raise RebuildError('OUTPUT_MUST_BE_NEW_ZIP')
    for root in (package, r1_package):
        if out.resolve().is_relative_to(root):
            raise RebuildError('OUTPUT_INSIDE_PACKAGE')
    manifest, delta = load_delta(package)
    builder_bytes = read_checked(r1_package, 'build_candidate.py')
    if sha(builder_bytes) != manifest['r1_builder_sha256']:
        raise RebuildError('R1_BUILDER_CHANGED')
    if baseline.stat().st_size > 150_000_000 or sha(baseline.read_bytes()) != manifest['baseline_zip_sha256']:
        raise RebuildError('HISTORICAL_BASELINE_MISMATCH')
    spec = importlib.util.spec_from_file_location('tv11_pinned_r1_builder', r1_package / 'build_candidate.py')
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    with tempfile.TemporaryDirectory(prefix='tv11-r3-rebuild-') as tmp:
        temp = Path(tmp)
        r1 = temp / 'r1.zip'
        builder.build(baseline, r1, r1_package)
        records = read_source_zip(r1)
        if fingerprint(records) != manifest['r1_source_fingerprint']:
            raise RebuildError('R1_WHOLE_SOURCE_MISMATCH')
        expected = {k: sha(v) for k, v in records.items()}
        for item in manifest['files']:
            if expected.get(item['path']) != item['before_sha256']:
                raise RebuildError('BEFORE_FILE_MISMATCH:' + item['path'])
            expected[item['path']] = item['after_sha256']
        root = temp / 'source'
        root.mkdir()
        for name, data in records.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        patch = temp / 'delta.patch'
        patch.write_bytes(delta)
        for flags in (['--check'], []):
            result = subprocess.run(['git', 'apply', *flags, str(patch)], cwd=root,
                                    capture_output=True, timeout=30)
            if result.returncode:
                raise RebuildError('EXACT_PATCH_FAILED:' + result.stderr.decode(errors='replace')[-600:])
        all_paths = list(root.rglob('*'))
        if any(p.is_symlink() for p in all_paths):
            raise RebuildError('REBUILT_SYMLINK')
        rebuilt = {p.relative_to(root).as_posix(): p.read_bytes() for p in all_paths if p.is_file()}
        if {k: sha(v) for k, v in rebuilt.items()} != expected:
            raise RebuildError('CHANGED_OR_UNCHANGED_FILE_MISMATCH')
        if len(rebuilt) != manifest['source_files'] or fingerprint(rebuilt) != manifest['r3_source_fingerprint']:
            raise RebuildError('R3_WHOLE_SOURCE_MISMATCH')
        write_source_zip(out, rebuilt)
    return {'status': 'SOURCE_CANDIDATE_NOT_DEPLOYABLE', 'revision': 'r3',
            'source_files': len(rebuilt), 'source_fingerprint': fingerprint(rebuilt),
            'sha256': sha(out.read_bytes()), 'production_modified': False,
            'network_used': False, 'project_code_executed': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-zip', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(build(args.baseline_zip, args.out), indent=2))
    except (ValueError, OSError, subprocess.SubprocessError, zipfile.BadZipFile) as exc:
        parser.exit(2, 'SOURCE_REBUILD_BLOCKED: ' + str(exc) + '\n')


if __name__ == '__main__':
    main()
