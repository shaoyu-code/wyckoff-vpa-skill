#!/usr/bin/env python3
"""Read-only source export. Never installs, imports project code, or contacts TV.

Archives stay local. Review the archive before sharing it privately; heuristic
secret detection is NOT a guarantee that a source tree contains no private data.
Only explicitly provided Skill roots are read. Internal symlinks are excluded.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import zipfile
from pathlib import Path

MAX_FILE = 4 * 1024 * 1024
MAX_TOTAL = 64 * 1024 * 1024
MAX_FILES = 10000
EXTENSIONS = {'.py', '.js', '.ts', '.mjs', '.cjs', '.tsx', '.jsx', '.md', '.txt',
              '.json', '.yaml', '.yml', '.toml', '.ini', '.cfg', '.html', '.css',
              '.sh', '.lock', '.schema', '.pine'}
NAMES = {'LICENSE', 'VERSION', 'Makefile', 'Dockerfile', '.gitignore'}
EXCLUDED_DIRS = {'.git', '.venv', 'venv', 'node_modules', '__pycache__', '.pytest_cache',
                 'workspace', 'workspaces', 'runs', 'outputs', 'output', 'raw',
                 'private', 'secrets', 'credentials', 'sessions', 'logs', 'cache',
                 '.cache', 'backups', 'books', 'vault', 'archives', 'evidence'}
SECRET_NAMES = re.compile(r'(^\.env($|\.)|(^|[._-])(secrets?|credentials?|cookies?|auth)([._-]|$)|\.(pem|key|p12|pfx)$)', re.I)
SECRET_VALUES = [
    re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    re.compile(r'\b(?:ghp_|github_pat_|sk-proj-)[A-Za-z0-9_-]{20,}'),
    re.compile(r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b'),
    re.compile(r'\beyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b'),
    re.compile(r'''(?i)["']?(?:api[_-]?key|access[_-]?token|secret|password)["']?\s*[:=]\s*["']([A-Za-z0-9_./+=-]{24,})["']'''),
]


class ExportError(ValueError):
    pass


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_regular(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_FILE:
            raise ExportError('Non-regular or oversized source file: ' + path.name)
        data = stream.read(MAX_FILE + 1)
        after = os.fstat(stream.fileno())
    if len(data) > MAX_FILE or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ExportError('Source changed while reading: ' + path.name)
    return data


def collect(roots: dict[str, Path]) -> tuple[dict[str, bytes], dict]:
    if not roots:
        raise ExportError('No source roots supplied')
    files: dict[str, bytes] = {}
    exclusions: list[dict[str, str]] = []
    sources = {}
    total = 0
    for label, given in sorted(roots.items()):
        if not re.fullmatch(r'[a-z][a-z0-9_-]{0,40}', label):
            raise ExportError('Invalid source label')
        root = given.expanduser().resolve(strict=True)
        if not root.is_dir() or not (root / 'SKILL.md').is_file():
            raise ExportError('Expected an installed Skill root: ' + label)
        sources[label] = {'root_identity_sha256': digest(str(root).encode()),
                          'root_entry_was_symlink': given.expanduser().is_symlink()}
        for current, dirs, names in os.walk(root, followlinks=False):
            current_path = Path(current)
            for directory in sorted(dirs[:]):
                child = current_path / directory
                reason = 'symlink' if child.is_symlink() else 'runtime_or_private_directory'
                if child.is_symlink() or directory.lower() in EXCLUDED_DIRS:
                    exclusions.append({'path': label + '/' + child.relative_to(root).as_posix(), 'reason': reason})
                    dirs.remove(directory)
            dirs.sort()
            for name in sorted(names):
                path = current_path / name
                relative = path.relative_to(root)
                key = label + '/' + relative.as_posix()
                if path.is_symlink() or (SECRET_NAMES.search(name) and path.suffix.lower() not in {'.py', '.js', '.ts', '.mjs', '.cjs'}):
                    exclusions.append({'path': key, 'reason': 'symlink_or_private_filename'})
                    continue
                if path.suffix.lower() not in EXTENSIONS and name not in NAMES:
                    exclusions.append({'path': key, 'reason': 'not_source_text'})
                    continue
                if path.resolve(strict=True) != root / relative:
                    raise ExportError('Source path changed during traversal: ' + key)
                data = read_regular(path)
                try:
                    text = data.decode('utf-8')
                except UnicodeDecodeError as exc:
                    raise ExportError('Source text is not UTF-8: ' + key) from exc
                if any(pattern.search(text) for pattern in SECRET_VALUES):
                    raise ExportError('Possible secret; no archive written. Review privately: ' + key)
                total += len(data)
                if total > MAX_TOTAL or len(files) >= MAX_FILES:
                    raise ExportError('Export size/file-count limit exceeded')
                files[key] = data
    manifest = {
        'schema_version': 1, 'artifact_kind': 'PRIVATE_SOURCE_BASELINE_NOT_INSTALLER',
        'production_modified': False, 'network_used': False,
        'coverage': 'source_text_only_with_explicit_exclusions',
        'secret_scan': 'HEURISTIC_NOT_A_PRIVACY_CERTIFICATE',
        'sources': sources, 'files': {k: {'sha256': digest(v), 'bytes': len(v)} for k, v in sorted(files.items())},
        'exclusions': exclusions,
    }
    return files, manifest


def export(roots: dict[str, Path], destination: Path) -> dict:
    destination = destination.expanduser().absolute()
    for root in roots.values():
        if destination.resolve().is_relative_to(root.expanduser().resolve(strict=True)):
            raise ExportError('Archive must be outside every source root')
    if destination.exists() or destination.is_symlink():
        raise ExportError('Refusing to overwrite existing destination')
    if not destination.parent.is_dir():
        raise ExportError('Destination directory must already exist')
    files, manifest = collect(roots)
    second_files, second_manifest = collect(roots)
    if files != second_files or manifest != second_manifest:
        raise ExportError('Source tree changed during collection; retry without modifying source')
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'wb') as raw:
            with zipfile.ZipFile(raw, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                for key, data in sorted(files.items()):
                    archive.writestr(key, data)
                archive.writestr('BASELINE_MANIFEST.json', json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
            raw.flush()
            os.fsync(raw.fileno())
        with zipfile.ZipFile(destination) as archive:
            if archive.testzip() is not None:
                raise ExportError('Archive integrity failure')
            for key, item in manifest['files'].items():
                if digest(archive.read(key)) != item['sha256']:
                    raise ExportError('Archive content mismatch: ' + key)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return {'status': 'SOURCE_EXPORTED_NOT_DEPLOYED', 'path': str(destination),
            'sha256': digest(destination.read_bytes()), 'source_files': len(files),
            'excluded_entries': len(manifest['exclusions']), 'production_modified': False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', action='append', required=True, metavar='LABEL=SKILL_ROOT')
    parser.add_argument('--out', required=True, type=Path, help='New local ZIP outside source directories')
    args = parser.parse_args()
    roots: dict[str, Path] = {}
    try:
        for item in args.source:
            label, separator, path = item.partition('=')
            if not separator or not path or label in roots:
                raise ExportError('Each source needs a unique LABEL=SKILL_ROOT')
            roots[label] = Path(path)
        print(json.dumps(export(roots, args.out), ensure_ascii=False, indent=2))
        return 0
    except (ExportError, OSError) as exc:
        print('SOURCE_EXPORT_BLOCKED: ' + str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
