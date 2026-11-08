import { chromium } from 'playwright-core'
import { spawn } from 'node:child_process'
import { createInterface } from 'node:readline'
import { once } from 'node:events'
import assert from 'node:assert/strict'
import path from 'node:path'
import { mkdir, writeFile } from 'node:fs/promises'

const root = path.resolve('..')
const python = path.join(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python')
const child = spawn(python, [path.join(root, 'tests/miniapp_smoke_server.py')], {
  cwd: root, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'],
  env: { ...process.env, PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8' },
})
let startupError = ''
child.stderr.on('data', data => { startupError += data.toString() })
const reader = createInterface({ input: child.stdout })
let browser
try {
  const url = await new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error('QA server startup timeout: ' + startupError)), 30000)
    reader.on('line', line => {
      try { const data = JSON.parse(line); if (data.url) { clearTimeout(timeout); resolve(data.url) } } catch {}
    })
    child.once('error', error => { clearTimeout(timeout); reject(error) })
    child.once('exit', code => { clearTimeout(timeout); reject(new Error('QA server exited ' + code + ': ' + startupError)) })
  })
  browser = await chromium.launch({ channel: 'chrome' })
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 }, locale: 'ru-RU' })
  const errors = []
  page.on('pageerror', error => errors.push(error.message))
  await page.goto(url)
  await page.getByText('Локальная админка', { exact: true }).waitFor()
  await page.getByRole('button', { name: 'Локальный конспект.pdf', exact: true }).waitFor()
  assert.equal(new URL(page.url()).hash, '')
  await page.getByRole('button', { name: 'Читать весь текст' }).click()
  await page.getByRole('dialog').getByText(/Этот материал читается из локального индекса/).waitFor()
  await page.getByRole('button', { name: 'Закрыть окно' }).click()
  await mkdir('shots', { recursive: true })
  await page.screenshot({ path: 'shots/local-real-data.png', fullPage: true })
  await page.getByRole('button', { name: 'Настройки', exact: true }).click()
  await page.getByLabel('API-ключ', { exact: true }).fill('offline-replacement-secret')
  const savedResponse = page.waitForResponse(response => response.url().includes('/api/admin/settings') && response.request().method() === 'PATCH')
  await page.getByRole('button', { name: 'Сохранить', exact: true }).click()
  const saved = await savedResponse
  assert.equal(saved.status(), 200)
  assert.ok(!(await saved.text()).includes('offline-replacement-secret'))
  await page.getByRole('tab', { name: 'Модель', exact: true }).click()
  await page.getByLabel('Название модели', { exact: true }).fill('offline-updated-model')
  await page.getByRole('button', { name: 'Сохранить', exact: true }).click()
  await page.waitForFunction(() => [...document.querySelectorAll('button')].some(button => button.textContent.includes('Сохранить') && button.disabled))
  await page.reload()
  await page.getByRole('button', { name: 'Настройки', exact: true }).click()
  await page.getByRole('tab', { name: 'Модель', exact: true }).click()
  assert.equal(await page.getByLabel('Название модели', { exact: true }).inputValue(), 'offline-updated-model')
  await page.getByRole('button', { name: 'Материалы', exact: true }).click()
  await page.getByRole('button', { name: 'Удалить материал', exact: true }).click()
  await page.getByRole('button', { name: 'Удалить', exact: true }).click()
  await page.getByRole('heading', { name: 'Первый материал — начало подготовки' }).waitFor()
  await page.reload()
  await page.getByRole('heading', { name: 'Первый материал — начало подготовки' }).waitFor()
  assert.deepEqual(errors, [])
  const result = { passed: ['Random local sign-in', 'SQLite / Chroma list and preview',
    'Real settings API saves and redacts secrets', 'Saved model and login survive refresh',
    'Confirmed deletion persists in real database'], browserErrors: errors }
  await writeFile('shots/live-ui-results.json', JSON.stringify(result, null, 2))
  console.log(JSON.stringify(result, null, 2))
} finally {
  await browser?.close()
  const exit = once(child, 'exit').catch(() => {})
  child.stdin.end('stop\n')
  await Promise.race([exit, new Promise(resolve => setTimeout(resolve, 5000))])
  if (child.exitCode === null) child.kill()
  reader.close()
}
