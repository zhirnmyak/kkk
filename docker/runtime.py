"""Start the configured hackathon container without an interactive setup."""
import os
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

bundled = Path('/opt/maxiimka-config')
load_dotenv(bundled / '.env', override=False)
data = Path(os.environ.get('DATA_DIR', '/app/data'))
data.mkdir(parents=True, exist_ok=True)
for name in ('settings.json', 'admin.json'):
    source = bundled / name
    destination = data / name
    if source.is_file() and not destination.exists():
        shutil.copyfile(source, destination)

import console_settings
console_settings.SETTINGS_PATH = data / 'settings.json'
console_settings.MODEL_HINT_PATH = data / 'model.txt'

import main
import uvicorn

if __name__ == '__main__':
    uvicorn.run(main.app, host='0.0.0.0', port=int(os.environ.get('HTTP_PORT', '8000')),
                proxy_headers=False, access_log=False)

