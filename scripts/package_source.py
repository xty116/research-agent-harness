"""Build an explicit, checked source-only upload directory and archive. No credentials or run history."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'release'
UPLOAD = OUTPUT / 'github-upload'
DIRECTORIES = ('.github', 'config', 'docs', 'evals', 'examples', 'scripts', 'skills', 'src', 'tests')
FILES = ('.env.example', '.gitignore', 'AGENTS.md', 'README.md', 'pyproject.toml', 'uv.lock')
EXCLUDE = {'docs/deployment-manifest.json', 'docs/dashboard.jpg'}


def source_files():
    files = [ROOT / name for name in FILES]
    for name in DIRECTORIES:
        files.extend(p for p in (ROOT / name).rglob('*') if p.is_file())
    return sorted(p for p in files if p.relative_to(ROOT).as_posix() not in EXCLUDE
                  and not p.name.endswith(('.local.yaml', '.local.md', '.pyc'))
                  and '__pycache__' not in p.parts and p.name != '.DS_Store')


def main():
    files = source_files()
    for path in files:
        relative = path.relative_to(ROOT)
        if path.is_symlink() or relative.is_absolute() or '..' in relative.parts:
            raise SystemExit(f'Unsafe upload path: {relative}')
        if path.suffix in {'.sqlite3', '.env', '.plist'}:
            raise SystemExit(f'Private runtime artifact: {relative}')
        if path.suffix not in {'.jpg', '.png'}:
            text = path.read_text()
            if re.search(r'(?:sk-[A-Za-z0-9]{20,}|tvly-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})', text):
                raise SystemExit(f'Credential-shaped string: {relative}')
            if re.search(r'/(?:Users|home)/[^/\s]+/', text):
                raise SystemExit(f'Private machine path: {relative}')
    if UPLOAD.exists():
        shutil.rmtree(UPLOAD)  # only this generated, ignored upload directory
    UPLOAD.mkdir(parents=True)
    manifest = []
    for path in files:
        relative = path.relative_to(ROOT)
        target = UPLOAD / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        manifest.append({'path': relative.as_posix(), 'bytes': path.stat().st_size,
                         'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    archive = OUTPUT / 'research-agent-harness-source.zip'
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as zipped:
        for item in manifest:
            zipped.write(UPLOAD / item['path'], item['path'])
    (OUTPUT / 'source-manifest.json').write_text(json.dumps(manifest, indent=2))
    print(json.dumps({'files': len(files), 'archive_bytes': archive.stat().st_size,
                      'directory': str(UPLOAD), 'archive': str(archive)}))


if __name__ == '__main__':
    main()
