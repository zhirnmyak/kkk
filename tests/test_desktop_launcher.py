from tests import TEST_ROOT  # Offline defaults before application imports.
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import httpx
import admin_setup
import config
import console_settings
import gateway_manager
import model_checks
from desktop_launcher import bootstrap, service
from desktop_launcher.state import ProcessLock, active_service, read_json, write_json
from tests.cache_test_support import CacheIsolation

ROOT = Path(__file__).resolve().parents[1]


class SetupTests(CacheIsolation, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (self.root / '.env.example').write_text('EXTRA_SETTING=keep-me\n', encoding='utf-8')
        self.enterContext(mock.patch.dict(config.__dict__))
        self.enterContext(mock.patch.dict(os.environ, {
            'MAX_ADMIN_ID': '', 'MAXIMKA_MODEL_EXPLICIT': '', 'MAXIMKA_QODER_EXPLICIT': '',
            'LLM_MODEL': 'old-model', 'LLM_REASONING_EFFORT': 'auto', 'OCR_ENABLED': '1',
        }))
        for module, name, target in (
            (console_settings, 'SETTINGS_PATH', self.root / 'data/settings.json'),
            (console_settings, 'MODEL_HINT_PATH', self.root / 'data/model.txt'),
            (admin_setup, 'ADMIN_PATH', self.root / 'data/admin.json'),
        ):
            self.enterContext(mock.patch.object(module, name, target))
        config.LLM_PROVIDER = 'openai'
        config.LLM_BASE_URL = 'https://provider.example.test/v1'
        config.LLM_API_KEY = 'previous-api-secret'
        config.LLM_MODEL = 'old-model'
        config.MAX_TOKEN = ''
        config.MAX_BOT_ENABLED = False
        self.setup = service.SetupService(self.root)
        self.addCleanup(self.setup.close)

    def payload(self):
        return {'patch': {'provider': 'openai', 'base_url': 'https://provider.example.test/v1',
                          'api_key': 'offline-provider-secret', 'model': 'selected-model',
                          'ocr': True, 'ocr_backend': 'easyocr', 'ocr_workers': 5,
                          'context': 200000, 'display_mode': 'reasoning', 'spinner': 'on'},
                'bot': {'enabled': True, 'token': 'offline-max-secret', 'admin_id': '123456789'},
                'launch': {'panel': True, 'mode': 'background'}}

    def test_first_setup_and_reload_persist_settings_without_returning_secrets(self):
        self.assertFalse(self.setup.load()['setup_complete'])
        result = self.setup.save(self.payload())
        self.assertTrue(result['setup_complete'])
        self.assertEqual(result['bot']['admin_id'], '123456789')
        self.assertTrue(result['bot']['token_set'])
        self.assertEqual(result['settings']['values']['model'], 'selected-model')
        self.assertEqual(result['settings']['values']['ocr_workers'], 5)
        self.assertNotIn('offline-provider-secret', json.dumps(result))
        self.assertNotIn('offline-max-secret', json.dumps(result))
        self.assertEqual((self.root / 'max_token.txt').read_text().strip(), 'offline-max-secret')
        self.assertIn('EXTRA_SETTING=keep-me', (self.root / '.env').read_text())
        self.assertEqual(console_settings.load()['api_key'], 'offline-provider-secret')
        self.assertEqual(service.SetupService(self.root).load(), result)

    def test_invalid_input_does_not_write_any_setup_files(self):
        for section, key, value in (('bot', 'admin_id', '0'), ('bot', 'token', 'bad\ntoken'),
                                    ('patch', 'base_url', 'file:///private'), ('launch', 'mode', 'both')):
            payload = self.payload()
            payload[section][key] = value
            with self.subTest(field=key), self.assertRaises(ValueError):
                self.setup.save(payload)
            self.assertEqual(list(self.root.iterdir()), [self.root / '.env.example'])
        self.assertEqual(config.LLM_API_KEY, 'previous-api-secret')

    def test_late_save_error_restores_files_and_live_settings(self):
        self.setup.save(self.payload())
        before = {path: path.read_bytes() for path in self.root.rglob('*') if path.is_file()}
        environment = os.environ.copy()
        config_before = (config.LLM_MODEL, config.LLM_API_KEY, config.MAX_TOKEN)
        original = write_json
        def fail_marker(path, value):
            if Path(path).name == 'state.json':
                raise OSError('simulated disk write failure')
            original(path, value)
        payload = self.payload()
        payload['patch']['model'] = 'changed-model'
        payload['bot']['token'] = 'changed-token'
        with mock.patch.object(service, 'write_json', side_effect=fail_marker), self.assertRaises(OSError):
            self.setup.save(payload)
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        self.assertEqual(os.environ.copy(), environment)
        self.assertEqual((config.LLM_MODEL, config.LLM_API_KEY, config.MAX_TOKEN), config_before)

    def test_models_use_entered_api_key_and_metadata_for_effort_and_vision(self):
        requests = []
        def reply(request):
            requests.append(request)
            return httpx.Response(200, json={'data': [{'id': 'selected-model',
                'input_modalities': ['text', 'image'], 'reasoning_effort': ['low', 'high']} ]})
        client = httpx.Client
        with mock.patch.object(httpx, 'Client', side_effect=lambda **kw: client(**kw, transport=httpx.MockTransport(reply))), \
                mock.patch.object(gateway_manager, 'start_for_setup') as gateway:
            models = self.setup.check('load_models', self.payload()['patch'])
            caps = self.setup.check('capabilities', self.payload()['patch'])['capabilities']
        self.assertEqual(models['models'], ['selected-model'])
        self.assertEqual(requests[0].headers['authorization'], 'Bearer offline-provider-secret')
        self.assertEqual(str(requests[0].url), 'https://provider.example.test/v1/models')
        self.assertEqual(len(requests), 1, 'Metadata avoids unnecessary paid probes')
        self.assertEqual(caps['efforts'], ('auto', 'low', 'high'))
        self.assertIs(caps['vision']['supported'], True)
        gateway.assert_not_called()
        self.assertFalse(console_settings.SETTINGS_PATH.exists())

    def test_network_failure_does_not_claim_model_capabilities(self):
        client = httpx.Client
        with mock.patch.object(httpx, 'Client', side_effect=lambda **kw: client(**kw,
                transport=httpx.MockTransport(lambda request: httpx.Response(503, text='unavailable')))):
            result = self.setup.check('capabilities', self.payload()['patch'], True)
        self.assertEqual(result['capabilities']['efforts'], ('auto',))
        self.assertIsNone(result['capabilities']['vision']['supported'])
        self.assertIn('HTTP 503', result['message'])

    def test_cancel_check_waits_for_cleanup_and_leaves_settings_untouched(self):
        entered = threading.Event()
        def checking(_):
            entered.set()
            while True:
                model_checks.check_cancelled()
                time.sleep(.01)
        result = []
        with mock.patch.object(console_settings, 'fetch_models', side_effect=checking):
            thread = threading.Thread(target=lambda: result.append(service.Bridge(self.setup).check('load_models', {})))
            thread.start()
            self.assertTrue(entered.wait(2))
            self.setup.cancel_check()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertFalse(result[0]['ok'])
        self.assertIn('отменена', result[0]['error'])
        self.assertFalse(console_settings.SETTINGS_PATH.exists())

    def test_qoder_cleanup_only_stops_the_setup_child(self):
        child = mock.Mock()
        child.poll.return_value = None
        self.setup.owned_gateway = child
        with mock.patch.object(gateway_manager, 'stop_owned') as stop:
            self.setup.close()
            self.setup.close()
        stop.assert_called_once_with(child)
        with mock.patch.object(gateway_manager, '_managed', return_value=''), \
                mock.patch.object(gateway_manager, 'is_up', return_value=True), \
                mock.patch.object(gateway_manager, '_spawn') as spawn:
            self.assertIsNone(gateway_manager.start_for_setup({'provider': 'qoder'}))
        spawn.assert_not_called()


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix='МаксИИмка setup ')))
        self.installer = bootstrap.Installer(self.root)
        self.installer.state_dir.mkdir()

    def test_creates_real_venv_from_a_path_with_spaces_and_reuses_it(self):
        self.assertTrue(self.installer.ensure_environment())
        output = subprocess.check_output([str(self.installer.python), '-c',
            'import sys, pip; print(sys.prefix)'], encoding='utf-8', env=self.installer.env)
        self.assertEqual(Path(output.strip()).resolve(), self.installer.venv.resolve())
        with mock.patch.object(self.installer, 'run', wraps=self.installer.run) as run:
            self.assertFalse(self.installer.ensure_environment())
        self.assertEqual(run.call_count, 1, 'Second launch checks Python without creating another environment')

    def test_interrupted_environment_is_preserved_before_recreation(self):
        self.installer.venv.mkdir()
        (self.installer.venv / 'interrupted.txt').write_text('recoverable')
        with mock.patch.object(self.installer, 'run'):
            self.assertTrue(self.installer.ensure_environment())
        recovered = list(self.installer.state_dir.glob('venv-interrupted-*/interrupted.txt'))
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].read_text(), 'recoverable')

    def test_failed_import_on_repeat_launch_repairs_dependencies(self):
        calls = []
        def run(args):
            calls.append([str(arg) for arg in args])
            if len(calls) == 1:
                raise RuntimeError('missing package')
        with mock.patch.object(self.installer, 'run', side_effect=run):
            self.installer.ensure_dependencies(['requirements.txt', 'desktop_launcher/requirements.txt'], install=False)
        self.assertTrue(any('--force-reinstall' in args for args in calls))
        self.assertIn('check', calls[-2])
        self.assertEqual(calls[-1][-1], bootstrap.IMPORT_CHECK)

    def test_cancellation_terminates_the_owned_install_process(self):
        self.installer.report = lambda phase, line, fraction: self.installer.cancelled.set() if line == 'ready' else None
        started = time.monotonic()
        with self.assertRaises(bootstrap.Cancelled):
            self.installer.run([sys.executable, '-c', "import time; print('ready', flush=True); time.sleep(30)"])
        self.assertLess(time.monotonic() - started, 8)

    def test_failed_build_does_not_mark_ready_and_retry_can_complete(self):
        for name in ('requirements.txt', 'desktop_launcher/requirements.txt', 'webapp/package.json',
                     'webapp/package-lock.json', 'webapp/index.html', 'webapp/vite.config.ts', 'webapp/tsconfig.json',
                     'webapp/src/main.tsx', 'webapp/design-reference/card.tsx',
                     'webapp/node_modules/vite/package.json', 'webapp/node_modules/typescript/package.json',
                     'webapp/node_modules/react/package.json'):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{}')
        for method, value in (('ensure_environment', False), ('ensure_dependencies', None),
                              ('ensure_node', Path('npm.cmd')), ('ensure_webview', None)):
            self.enterContext(mock.patch.object(self.installer, method, return_value=value))
        with mock.patch.object(self.installer, 'run', side_effect=RuntimeError('build failed')), self.assertRaises(RuntimeError):
            self.installer.prepare()
        self.assertFalse((self.installer.state_dir / 'install.json').exists())
        def build(args, **kwargs):
            if 'build' in args:
                path = self.root / 'webapp/dist/index.html'
                path.parent.mkdir(parents=True)
                path.write_text('<html>ready</html>')
        with mock.patch.object(self.installer, 'run', side_effect=build):
            self.installer.prepare()
        self.assertIn('build', read_json(self.installer.state_dir / 'install.json'))
        with mock.patch.object(self.installer, 'run') as run:
            self.installer.prepare()
        run.assert_not_called()


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.runtime = service.RuntimeManager(self.root)
        self.enterContext(mock.patch.object(config, 'HTTP_PORT', 0))
        write_json(self.root / '.launcher/state.json', {'setup_complete': True})

    def test_terminal_and_background_get_distinct_process_flags_and_no_second_start(self):
        for mode in ('terminal', 'background'):
            def launch(args, **kw):
                write_json(self.root / '.launcher/service.json', {'run_id': kw['env']['MAXIMKA_DESKTOP_RUN'],
                    'status': 'running', 'heartbeat': time.time(), 'mode': mode, 'panel': False, 'pid': 123})
                self.assertEqual(kw['env']['HTTP_HOST'], '127.0.0.1')
                self.assertEqual(kw['env']['MAX_LOCAL_MINIAPP'], '0')
                self.assertEqual(kw['env']['MAXIMKA_DESKTOP_MODE'], mode)
                if os.name == 'nt':
                    self.assertEqual(kw['creationflags'], subprocess.CREATE_NEW_CONSOLE if mode == 'terminal' else subprocess.CREATE_NO_WINDOW)
                return mock.Mock()
            with mock.patch.object(service.subprocess, 'Popen', side_effect=launch) as popen:
                self.assertEqual(self.runtime.start({'mode': mode, 'panel': False})['status'], 'running')
                with self.assertRaises(ValueError):
                    self.runtime.start({'mode': mode, 'panel': False})
                self.assertEqual(popen.call_count, 1)
            write_json(self.root / '.launcher/service.json', {'status': 'stopped'})

    def test_start_failure_clears_starting_state(self):
        with mock.patch.object(service.subprocess, 'Popen', side_effect=OSError('no Python')), self.assertRaises(OSError):
            self.runtime.start({'mode': 'background', 'panel': True})
        self.assertIsNone(active_service(self.root))
        self.assertEqual(self.runtime.status()['status'], 'stopped')

    def test_external_panel_url_is_never_opened(self):
        write_json(self.root / '.launcher/service.json', {'status': 'running', 'heartbeat': time.time(),
                   'panel': True, 'local_url': 'https://example.test/app/'})
        with mock.patch.object(service.webbrowser, 'open') as open_url, self.assertRaises(ValueError):
            self.runtime.open_panel()
        open_url.assert_not_called()

    def test_real_background_and_terminal_lifecycle_with_isolated_data(self):
        original_popen = subprocess.Popen
        children = []
        def launch(args, **kw):
            # Test the real runner with a private data directory, no MAX token and
            # a hidden console. The process-mode flags are asserted separately.
            kw['creationflags'] = bootstrap.NO_WINDOW
            kw['cwd'] = ROOT
            kw['stdin'] = subprocess.DEVNULL
            with (self.root / '.launcher/fixture.log').open('a', encoding='utf-8') as log:
                kw['stdout'] = kw['stderr'] = log
                child = original_popen([sys.executable, str(ROOT / 'tests/launcher_runtime_fixture.py'), str(self.root)], **kw)
            children.append(child)
            return child
        def cleanup():
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=5)
        self.addCleanup(cleanup)
        with mock.patch.object(service.subprocess, 'Popen', side_effect=launch), \
                mock.patch.object(service.webbrowser, 'open', return_value=True):
            for mode in ('background', 'terminal'):
                with self.subTest(mode=mode):
                    self.assertEqual(self.runtime.start({'panel': True, 'mode': mode})['status'], 'running')
                    current = active_service(self.root)
                    parsed = urlsplit(current['local_url'])
                    base = f'http://127.0.0.1:{parsed.port}'
                    with httpx.Client(trust_env=False, timeout=5) as client:
                        self.assertEqual(client.get(base + '/health').status_code, 200)
                        self.assertEqual(client.get(base + '/api/me').status_code, 401)
                        token = parse_qs(parsed.fragment)['LocalToken'][0]
                        self.assertEqual(client.get(base + '/api/me', headers={'Authorization': 'Local ' + token}).status_code, 200)
                    # A new window can manage a process it did not create.
                    reopened = service.RuntimeManager(self.root)
                    self.assertEqual(reopened.status()['status'], 'running')
                    self.assertEqual(reopened.stop()['status'], 'stopped')
                    self.assertEqual(children[-1].wait(timeout=8), 0)
                    self.assertEqual(read_json(self.root / '.launcher/stop.json')['run_id'], current['run_id'])

    def test_process_lock_is_released_after_error(self):
        path = self.root / '.launcher/process.lock'
        with self.assertRaises(ValueError):
            with ProcessLock(path):
                with self.assertRaises(RuntimeError):
                    with ProcessLock(path):
                        self.fail('Second lock acquired')
                raise ValueError('interrupted')
        with ProcessLock(path):
            pass


if __name__ == '__main__':
    unittest.main()
