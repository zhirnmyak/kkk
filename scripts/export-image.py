"""Export the configured Docker image as a compressed release asset."""
import gzip
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

root = Path(__file__).resolve().parents[1]
artifacts = root / 'artifacts'
artifacts.mkdir(exist_ok=True)
image = sys.argv[1] if len(sys.argv) > 1 else 'maxiimka:hackathon-2026'
info = json.loads(subprocess.check_output(['docker', 'image', 'inspect', image], text=True))[0]
assert (info['Os'], info['Architecture']) == ('linux', 'amd64'), 'Expected a Linux/amd64 image'
archive = artifacts / 'maxiimka-hackathon-2026-linux-amd64.tar.gz'
temporary = archive.with_suffix(archive.suffix + '.partial')
started = time.monotonic()
total = 0
report_at = 256 * 1024**2
print('Exporting the Docker image directly to gzip...', flush=True)
with temporary.open('wb') as output:
    with subprocess.Popen(['docker', 'save', image], stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
        with gzip.GzipFile(filename='', mode='wb', fileobj=output, compresslevel=1, mtime=0) as compressed:
            while chunk := process.stdout.read(4 * 1024**2):
                compressed.write(chunk)
                total += len(chunk)
                if total >= report_at:
                    print(f'Packed {total / 1024**2:.0f} MiB of Docker data', flush=True)
                    report_at += 256 * 1024**2
        error = process.stderr.read()
        if process.wait() != 0:
            raise SystemExit('Docker save failed. The incomplete .partial file was not published.')
temporary.replace(archive)
with archive.open('rb') as stream:
    digest = hashlib.file_digest(stream, 'sha256').hexdigest()
(artifacts / 'SHA256SUMS').write_text(f'{digest}  {archive.name}\n', encoding='utf-8')
metadata = {'image': image, 'id': info['Id'], 'platform': 'linux/amd64',
            'archive': archive.name, 'archive_bytes': archive.stat().st_size,
            'sha256': digest, 'docker_save_bytes': total}
(artifacts / 'image.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
print(json.dumps(metadata, ensure_ascii=False, indent=2), flush=True)
print(f'Finished in {time.monotonic() - started:.1f} seconds.', flush=True)
