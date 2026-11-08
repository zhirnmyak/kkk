import tempfile
from pathlib import Path
from unittest import mock
import capability_cache
import reasoning_support
import vision_support


class CacheIsolation:
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(mock.patch.object(capability_cache, "CACHE_PATH", Path(directory) / "cache.json"))
        self.enterContext(mock.patch.dict(reasoning_support._cache, {}, clear=True))
        self.enterContext(mock.patch.dict(vision_support._cache, {}, clear=True))
