"""Офлайн-окружение устанавливается до первого импорта приложения."""
import atexit
import os
from pathlib import Path
import shutil
import tempfile

TEST_ROOT = Path(tempfile.mkdtemp(prefix="maximka-suite-"))
os.environ.update({"LLM_MOCK": "1", "EMBED_MOCK": "1", "OCR_ENABLED": "0",
                   "IMAGE_ENABLED": "0", "DATA_DIR": str(TEST_ROOT / "data"),
                   "MESSENGER": "max", "UPDATES_MODE": "polling", "WEBHOOK_SECRET": "",
                   "ANONYMIZED_TELEMETRY": "False"})
atexit.register(shutil.rmtree, TEST_ROOT, ignore_errors=True)
