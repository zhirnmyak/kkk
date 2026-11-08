"""Verify API-key replacement and persistence using an isolated Docker volume."""
import json
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request

root = Path(__file__).resolve().parents[1]
image = sys.argv[1] if len(sys.argv) > 1 else 'maxiimka:hackathon-2026'
suffix = secrets.token_hex(6)
container = 'maxiimka-key-check-' + suffix
volume = 'maxiimka-key-check-data-' + suffix
replacement = 'test-only-' + secrets.token_urlsafe(24)
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def docker(*args, input=None):
    result = subprocess.run(['docker', *args], input=input, text=True,
                            encoding='utf-8', errors='replace', capture_output=True, timeout=120)
    if result.returncode:
        raise RuntimeError(f'Docker {args[0]} failed (exit {result.returncode}); credential output is hidden.')
    return result.stdout


def start():
    docker('run', '--detach', '--init', '--name', container,
           '--publish', '127.0.0.1::8000', '--volume', volume + ':/app/data',
           '--env', 'MAX_BOT_ENABLED=0', '--env', 'LLM_MOCK=0', image)
    address = 'http://' + docker('port', container, '8000/tcp').strip().splitlines()[0]
    deadline = time.monotonic() + 60
    while True:
        try:
            with opener.open(address + '/health', timeout=2) as response:
                assert json.load(response)['status'] == 'ok'
            break
        except (OSError, ValueError):
            if time.monotonic() >= deadline:
                raise RuntimeError('Temporary container did not become ready')
            time.sleep(.25)
    logs = docker('logs', container)
    match = re.search(r'#LocalToken=([A-Za-z0-9_-]+)', logs)
    assert match, 'Local administrator link is missing'
    return address, match.group(1)


def request(address, token, path, *, patch=None):
    headers = {'Authorization': 'Local ' + token}
    body = None
    if patch is not None:
        body = json.dumps(patch).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(address + path, data=body, headers=headers,
                                 method='PATCH' if patch is not None else 'GET')
    with opener.open(req, timeout=15) as response:
        raw = response.read()
    assert replacement.encode() not in raw, 'The API disclosed the stored key'
    return json.loads(raw)


def verify_saved_key():
    check = """
import json, sys
from pathlib import Path
expected = sys.stdin.read()
saved = json.loads(Path('/app/data/settings.json').read_text())
bundled = json.loads(Path('/opt/maxiimka-config/settings.json').read_text())
assert saved['api_key'] == expected, 'Replacement was not persisted'
assert saved['openai_api_key'] == expected, 'Provider profile was not updated'
assert bundled.get('api_key') != expected, 'Bundled default was modified'
print('Saved profile and bundled default: OK')
"""
    docker('exec', '-i', container, 'python', '-c', check, input=replacement)


try:
    docker('volume', 'create', '--label', 'maxiimka.check=key-change', volume)
    address, token = start()
    previous = request(address, token, '/api/admin/settings')
    updated = request(address, token, '/api/admin/settings', patch={
        'revision': previous['revision'],
        'patch': {'provider': 'openai', 'api_key': replacement},
    })
    assert updated['api_key_set'] and updated['profiles']['openai']['api_key_set']
    verify_saved_key()
    print('Replacement key saved through the panel API; key remains hidden.', flush=True)
    docker('rm', '--force', container)
    address, token = start()
    restored = request(address, token, '/api/admin/settings')
    assert restored['api_key_set']
    verify_saved_key()
    result = {'image': image, 'api_key_replacement': True, 'stored_key_hidden': True,
              'persists_after_container_recreation': True, 'bundled_default_preserved': True,
              'panel_available_with_invalid_llm_key': True,
              'live_max_polling': False, 'external_llm_calls': False}
    (root / 'key-change-check.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, indent=2))
finally:
    subprocess.run(['docker', 'rm', '--force', container], capture_output=True, timeout=30)
    subprocess.run(['docker', 'volume', 'rm', volume], capture_output=True, timeout=30)
