"""Prepare the credentials deliberately bundled in the hackathon image; never print them."""
import json
from pathlib import Path
import shutil

root = Path(__file__).resolve().parents[1]
bundled = root / 'docker/bundled'
bundled.mkdir(parents=True, exist_ok=True)
for name in ('.env', 'max_token.txt'):
    path = root / name
    if not path.is_file():
        raise SystemExit(f'Create {name} before preparing the configured image.')
    shutil.copyfile(path, bundled / name)
for name in ('settings.json', 'admin.json'):
    path = root / 'data' / name
    if path.is_file():
        json.loads(path.read_text('utf-8'))
        shutil.copyfile(path, bundled / name)
print('Configuration copied to docker/bundled. Secret values were not printed.')

