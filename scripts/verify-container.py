"""Check the real image without receiving messages or calling the configured LLM."""
import json
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request

image = sys.argv[1] if len(sys.argv) > 1 else 'maxiimka:hackathon-2026'
name = 'maxiimka-check-' + secrets.token_hex(4)
root = Path(__file__).resolve().parents[1]

def redact(text):
    values = []
    for directory in (root, root / 'docker/bundled'):
        token_path = directory / 'max_token.txt'
        if token_path.is_file():
            values.append(token_path.read_text('utf-8-sig').strip())
        env_path = directory / '.env'
        if env_path.is_file():
            for line in env_path.read_text('utf-8-sig').splitlines():
                key, separator, value = line.partition('=')
                if separator and any(word in key.upper() for word in ('KEY', 'TOKEN', 'SECRET', 'PASSWORD')):
                    values.append(value.strip().strip('\"').strip("'"))
    for path in (root / 'data/settings.json', root / 'docker/bundled/settings.json'):
        if path.is_file():
            settings = json.loads(path.read_text('utf-8-sig'))
            values.extend(value for key, value in settings.items()
                          if isinstance(value, str) and any(word in key.lower() for word in ('key', 'token', 'secret')))
    for value in sorted(set(values), key=len, reverse=True):
        if len(value) >= 4:
            text = text.replace(value, '[redacted]')
    return re.sub(r'#LocalToken=[A-Za-z0-9_-]+', '#LocalToken=[redacted]', text)

def docker(*args):
    result = subprocess.run(['docker', *args], text=True, encoding='utf-8', errors='replace',
                            capture_output=True, timeout=300)
    if result.returncode:
        details = redact(result.stderr)[-6000:]
        raise RuntimeError(f'Docker command failed: {args[0]} (exit {result.returncode})\n{details}')
    return result.stdout

opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
try:
    docker('run', '--detach', '--rm', '--init', '--name', name,
           '--publish', '127.0.0.1::8000', '--env', 'MAX_BOT_ENABLED=0',
           '--env', 'LLM_MOCK=1', image)
    published = docker('port', name, '8000/tcp').strip().splitlines()[0]
    base = 'http://' + published
    deadline = time.monotonic() + 90
    while True:
        try:
            with opener.open(base + '/health', timeout=2) as response:
                assert json.load(response)['status'] == 'ok'
            break
        except (OSError, ValueError):
            if time.monotonic() > deadline:
                raise RuntimeError('Container did not become ready in 90 seconds')
            time.sleep(.4)
    logs = docker('logs', name)
    match = re.search(r'#LocalToken=([A-Za-z0-9_-]+)', logs)
    assert match, 'No local panel token in the container startup log'
    token = match.group(1)

    def get(path, *, authenticated=False, headers=None):
        headers = dict(headers or {})
        if authenticated:
            headers['Authorization'] = 'Local ' + token
        request = urllib.request.Request(base + path, headers=headers)
        try:
            with opener.open(request, timeout=10) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    assert get('/api/me')[0] == 401
    status, payload = get('/api/me', authenticated=True)
    assert status == 200, (status, payload.decode())
    assert json.loads(payload)['is_admin'] is True
    assert get('/api/files', authenticated=True)[0] == 200
    assert get('/api/admin/settings', authenticated=True)[0] == 200
    assert get('/api/me', authenticated=True, headers={'Origin': 'http://example.test'})[0] == 401
    assert get('/api/me', authenticated=True, headers={'X-Forwarded-For': '127.0.0.1'})[0] == 401
    status, html = get('/app/')
    assert status == 200 and b'/app/assets/' in html
    print('HTTP, panel and local administrator access: OK', flush=True)
    print('Checking PDF, TXT, semantic search and OCR inside Linux...', flush=True)

    code = r'''
import json, os
from pathlib import Path
os.environ['DATA_DIR'] = '/tmp/document-check'
os.environ['LLM_MOCK'] = '1'
os.environ['MAX_BOT_ENABLED'] = '0'
import pymupdf, rag
from PIL import Image, ImageDraw, ImageFont
import easyocr, numpy as np
doc = pymupdf.open()
page = doc.new_page()
page.insert_textbox(pymupdf.Rect(40, 40, 500, 600),
    'The nucleus stores DNA. Mitochondria provide the cell with energy. ' * 10, fontsize=12)
pages = rag.extract_pdf_auto(doc.tobytes())
assert pages and 'nucleus' in pages[0]
count = rag.add_document(990001, 'check.pdf', pages=pages)
assert count > 0
assert rag.search(990001, 'Where is DNA stored?', 2)
assert rag.add_document(990002, 'check.txt', full_text='Cell membranes regulate exchange of substances. ' * 8) > 0
reader = easyocr.Reader(['ru', 'en'], gpu=False, verbose=False, download_enabled=False)
picture = Image.new('RGB', (380, 120), 'white')
ImageDraw.Draw(picture).text((20, 25), 'CELL 42', fill='black', font=ImageFont.load_default(size=44))
text = reader.readtext(np.array(picture), detail=0)
assert text, 'OCR returned no text from the sample image'
for name in ('.agents', 'max-api-docs', 'docs', 'tests', 'desktop_launcher', '.venv', 'node_modules'):
    assert not (Path('/app') / name).exists(), name
assert Path('/opt/venv/bin/python').exists()
settings = json.loads(Path('/opt/maxiimka-config/settings.json').read_text())
assert settings.get('api_key') or settings.get(settings['provider'] + '_api_key')
assert Path('/opt/maxiimka-config/max_token.txt').read_text().strip()
print('DOCUMENT_CHECK=' + json.dumps({'pdf_fragments': count, 'txt_indexed': True,
    'semantic_search': True, 'ocr_recognized': bool(text), 'configured_keys_present': True,
    'unneeded_folders_absent': True, 'linux_venv_present': True}))
'''
    output = docker('exec', name, 'python', '-c', code)
    line = next(line for line in output.splitlines() if line.startswith('DOCUMENT_CHECK='))
    documents = json.loads(line.partition('=')[2])
    assert json.loads(get('/health')[1])['status'] == 'ok'
    versions = docker('exec', name, 'python', '-m', 'pip', 'freeze', '--all')
    (root / 'requirements-linux.lock.txt').write_text(versions, encoding='utf-8')
    result = {'image': image, 'http': True, 'panel': True, 'docker_local_auth': True,
              'foreign_origin_rejected': True, 'forwarded_headers_rejected': True,
              'max_polling_disabled_for_check': True, 'documents': documents}
    (root / 'container-check.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))
finally:
    subprocess.run(['docker', 'rm', '--force', '--volumes', name], capture_output=True)

