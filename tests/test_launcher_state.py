import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from desktop_launcher import state


class LauncherStateTests(unittest.TestCase):
    def test_temporary_denial_retries_without_losing_json(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'service.json'
            state.write_json(target, {'status': 'starting'})
            replace = Path.replace
            attempts = []
            def busy(source, destination):
                attempts.append(1)
                if len(attempts) <= 3:
                    raise PermissionError('simulated Windows sharing violation')
                return replace(source, destination)
            with mock.patch.object(Path, 'replace', busy):
                state.write_json(target, {'status': 'running'})
            self.assertEqual(len(attempts), 4)
            self.assertEqual(state.read_json(target), {'status': 'running'})
            self.assertEqual(list(Path(directory).glob('*.tmp')), [])

    @unittest.skipUnless(os.name == 'nt', 'Windows file sharing semantics')
    def test_real_windows_reader_can_release_the_file_during_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'service.json'
            state.write_json(target, {'status': 'starting'})
            errors = []
            entered = threading.Event()
            def write():
                entered.set()
                try:
                    state.write_json(target, {'status': 'running'})
                except Exception as exc:
                    errors.append(exc)
            with target.open('rb'):
                thread = threading.Thread(target=write)
                thread.start()
                self.assertTrue(entered.wait(1))
                time.sleep(.15)
                self.assertTrue(thread.is_alive(), 'Replacement should wait while the reader holds the Windows handle')
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(state.read_json(target)['status'], 'running')

    def test_stale_heartbeat_with_a_live_lock_still_counts_as_active(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state.write_json(root / '.launcher/service.json',
                             {'status': 'running', 'heartbeat': time.time() - 60, 'pid': os.getpid()})
            with state.ProcessLock(root / '.launcher/service.lock'):
                self.assertEqual(state.active_service(root)['status'], 'running')
            self.assertIsNone(state.active_service(root))


if __name__ == '__main__':
    unittest.main()
