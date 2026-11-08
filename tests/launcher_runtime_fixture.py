"""A real launcher child restricted to temporary data and offline providers."""
import os
from pathlib import Path
import sys

root = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.update(DATA_DIR=str(root / 'data'), MAX_BOT_ENABLED='0', MAX_BOT_TOKEN='',
    MAX_TOKEN_FILE=str(root / 'no-token'), MAX_ADMIN_ID='', MESSENGER='max', UPDATES_MODE='polling',
    LLM_MOCK='1', EMBED_MOCK='1', OCR_ENABLED='0', IMAGE_ENABLED='0', LLM_PROVIDER='openai',
    LLM_MODEL='offline-model', LLM_API_KEY='offline-key', LLM_BASE_URL='https://example.test/v1',
    MAXIMKA_MODEL_EXPLICIT='', MAXIMKA_QODER_EXPLICIT='', ANONYMIZED_TELEMETRY='False')

import dotenv
dotenv.load_dotenv = lambda *args, **kwargs: False
import config
import console_settings
import admin_setup
import capability_cache
from desktop_launcher import runner

console_settings.SETTINGS_PATH = root / 'data/settings.json'
console_settings.MODEL_HINT_PATH = root / 'data/model.txt'
admin_setup.ADMIN_PATH = root / 'data/admin.json'
capability_cache.CACHE_PATH = root / 'data/capabilities.json'
runner.ROOT = root

if os.environ.get('MAXIMKA_DESKTOP_MODE') == 'terminal':
    import builtins
    import threading
    # Simulate the console waiting for a person; stop must not wait for a key.
    def waiting_input(*args):
        threading.Event().wait(120)
        return '/выход'
    builtins.input = waiting_input

raise SystemExit(runner.main())
