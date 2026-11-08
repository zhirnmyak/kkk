"""Create a configured source archive without environments, models or Docker layers."""
from pathlib import Path
import zipfile

root = Path(__file__).resolve().parents[1]
artifacts = root / 'artifacts'
artifacts.mkdir(exist_ok=True)
archive = artifacts / 'maxiimka-hackathon-2026-source.zip'
checksums = artifacts / 'SHA256SUMS'
if checksums.exists():
    lines = [line for line in checksums.read_text('utf-8').splitlines()
             if not line.endswith('  ' + archive.name)]
    checksums.write_text('\n'.join(lines) + '\n', encoding='utf-8')
files = set()
for path in root.iterdir():
    if path.is_file() and (path.suffix in {'.py', '.pyw', '.cmd'} or path.name in {
        '.dockerignore', '.env', '.env.example', '.gitattributes', '.gitignore', 'max_token.txt',
        'Dockerfile', 'compose.yaml', 'requirements.txt', 'requirements-linux.lock.txt',
        'README.md', 'README.txt', 'SUBMISSION.md', 'VALIDATION.md',
        'container-check.json', 'key-change-check.json', 'services-check.json', 'services-host-check.json'}):
        files.add(path)
excluded = {'__pycache__', 'node_modules', '.venv', '.runtime', 'dist', 'shots', '.git'}
for name in ('bot', 'certs', 'desktop_launcher', 'docker', 'docs', 'examples',
             'miniapp', 'openai-gateway', 'scripts', 'tests', 'webapp'):
    for path in (root / name).rglob('*'):
        relative = path.relative_to(root)
        if path.is_file() and not (set(relative.parts) & excluded) and path.suffix not in {'.pyc', '.log'}:
            files.add(path)
for name in ('data/settings.json', 'data/admin.json', '.launcher/state.json', 'model-cache/.gitkeep',
             'artifacts/README.md', 'artifacts/image.json', 'artifacts/SHA256SUMS'):
    path = root / name
    if path.is_file():
        files.add(path)
with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as output:
    for path in sorted(files):
        output.write(path, 'maximka-hackathon/' + path.relative_to(root).as_posix())
with zipfile.ZipFile(archive) as check:
    assert check.testzip() is None, 'Source archive failed its CRC check'
print(f'Configured source archive: {len(files)} files; {archive.stat().st_size} bytes; CRC OK')
