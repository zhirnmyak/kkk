"""Create a small repository folder from the configured source archive."""
import json
from pathlib import Path, PurePosixPath
import sys
import zipfile

root = Path(__file__).resolve().parents[1]
destination = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else root.with_name('maximka-github')
archive = root / 'artifacts/maxiimka-hackathon-2026-source.zip'
if destination.exists():
    raise SystemExit('The destination already exists. Supply a new folder to preserve existing work.')
if not archive.is_file():
    raise SystemExit('Run python scripts/package-source.py first.')

excluded_files = {'.env', 'max_token.txt', 'docker/bundled/.env',
                  'docker/bundled/max_token.txt', 'docker/bundled/settings.json',
                  'docker/bundled/admin.json'}
excluded_parts = {'.git', '.venv', '.runtime', 'node_modules', '__pycache__',
                  '.agents', 'max-api-docs', 'data', '.launcher'}
configured_values = []
for name in ('max_token.txt', 'docker/bundled/max_token.txt'):
    path = root / name
    if path.is_file():
        configured_values.append(path.read_text('utf-8-sig').strip())
for name in ('data/settings.json', 'docker/bundled/settings.json'):
    path = root / name
    if path.is_file():
        settings = json.loads(path.read_text('utf-8-sig'))
        configured_values.extend(value for key, value in settings.items()
                                 if isinstance(value, str) and any(word in key.lower()
                                                                 for word in ('key', 'token', 'secret')))
for name in ('.env', 'docker/bundled/.env'):
    path = root / name
    if path.is_file():
        for line in path.read_text('utf-8-sig').splitlines():
            key, separator, value = line.partition('=')
            field = key.strip().upper()
            if (separator and not field.startswith('#')
                    and (field in {'KEY', 'TOKEN', 'PASSWORD', 'SECRET'}
                         or field.endswith(('_KEY', '_TOKEN', '_PASSWORD', '_SECRET')))):
                configured_values.append(value.strip().strip('"').strip("'"))
private_values = {value.encode('utf-8') for value in configured_values if len(value) >= 12}

selected = []
with zipfile.ZipFile(archive) as source:
    for entry in source.infolist():
        if entry.is_dir():
            continue
        path = PurePosixPath(entry.filename)
        if (path.is_absolute() or '..' in path.parts or ':' in entry.filename
                or '\\' in entry.filename or path.parts[0] != 'maximka-hackathon'):
            raise SystemExit('Unexpected source archive path.')
        relative = PurePosixPath(*path.parts[1:])
        if relative.as_posix() in excluded_files or set(relative.parts) & excluded_parts:
            continue
        if entry.file_size >= 100 * 1024**2:
            raise SystemExit('A source file exceeds the GitHub Git size limit: ' + str(relative))
        content = source.read(entry)
        if any(value in content for value in private_values):
            raise SystemExit('A configured credential was found in ' + str(relative) + '; values are hidden.')
        selected.append((relative, content))

destination.mkdir(parents=True, exist_ok=False)
for relative, content in selected:
    target = destination.joinpath(*relative.parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
print(f'GitHub folder prepared: {destination}')
print(f'{len(selected)} files; {sum(len(content) for _, content in selected)} bytes; no large archives or configured key files.')
