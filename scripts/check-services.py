"""Read-only checks of the bundled MAX token and the provider's model catalog."""
import json
from pathlib import Path
import subprocess
import sys

image = sys.argv[1] if len(sys.argv) > 1 else 'maxiimka:hackathon-2026'
root = Path(__file__).resolve().parents[1]
code = r'''
import asyncio, json, runpy
runpy.run_path('/app/docker/runtime.py', run_name='service_check')
import runtime_settings, console_settings
from bot.max_client import MaxClient
settings = runtime_settings.initialize()
result = {'max_polling_used': False, 'messages_sent': False, 'llm_generation_used': False}
async def check_max():
    client = MaxClient()
    try:
        identity = await client.get_me()
        result['max_api'] = {'ok': bool(identity.get('user_id') or identity.get('username'))}
    except Exception as error:
        result['max_api'] = {'ok': False, 'error_type': type(error).__name__,
                             'http_status': getattr(error, 'status', None),
                             'message': runtime_settings.redact(str(error), settings)}
    finally:
        await client.close()
asyncio.run(check_max())
try:
    models = console_settings.fetch_models(settings)
    result['provider_models'] = {'ok': True, 'selected_model_present': settings['model'] in models}
except Exception as error:
    result['provider_models'] = {'ok': False, 'error_type': type(error).__name__,
                                'message': runtime_settings.redact(str(error), settings)}
print('SERVICES_CHECK=' + json.dumps(result))
'''
process = subprocess.run(['docker', 'run', '--rm', '--env', 'MAX_BOT_ENABLED=0',
                          '--env', 'LLM_MOCK=1', '--entrypoint', 'python', image, '-c', code],
                         capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=90)
if process.returncode:
    raise SystemExit(f'Container service check failed (exit {process.returncode}); secret values are hidden.')
line = next(line for line in process.stdout.splitlines() if line.startswith('SERVICES_CHECK='))
result = json.loads(line.partition('=')[2])
(root / 'services-check.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
print(json.dumps(result, ensure_ascii=False, indent=2))
if not (result['max_api']['ok'] and result['provider_models']['ok']):
    raise SystemExit(1)
