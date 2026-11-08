import { chromium } from 'playwright-core'
import assert from 'node:assert/strict'
import { mkdir, writeFile } from 'node:fs/promises'
import path from 'node:path'

const base = process.env.MINIAPP_TEST_URL || 'http://127.0.0.1:5173/app/'
const shots = path.resolve('shots/launcher')
await mkdir(shots, { recursive: true })
const browser = await chromium.launch({ channel: 'chrome' })
const context = await browser.newContext({ viewport: { width: 844, height: 661 }, deviceScaleFactor: 1, locale: 'ru-RU' })
const page = await context.newPage()
const errors = [], remoteRequests = [], passed = []
page.on('pageerror', error => errors.push(error.message))
page.on('request', request => {
  if (!request.url().startsWith(new URL(base).origin) && !request.url().startsWith('data:')) remoteRequests.push(request.url())
})
const button = name => page.getByRole('button', { name, exact: true })
async function click(name) { await button(name).click() }
async function ready(name) { await page.getByRole('heading', { name, exact: true }).waitFor() }
async function shot(name) {
  await page.evaluate(async () => {
    await document.fonts.ready
    await Promise.all(document.getAnimations().filter(animation => animation.effect?.getTiming().iterations !== Infinity)
      .map(animation => animation.finished.catch(() => {})))
  })
  const geometry = await page.evaluate(() => ({
    overflow: document.documentElement.scrollWidth - innerWidth,
    footer: document.querySelector('.ml-footer')?.getBoundingClientRect().bottom,
    height: innerHeight,
    clipped: [...document.querySelectorAll('.ml-step-names, .ml-main')]
      .filter(element => element.scrollWidth > element.clientWidth + 1).map(element => element.className),
    controlsOutside: [...document.querySelectorAll('.ml-footer button, .ml-footer-note')].filter(element => {
      const rect = element.getBoundingClientRect()
      return rect.left < 0 || rect.right > innerWidth + 1
    }).map(element => element.textContent),
  }))
  await page.screenshot({ path: path.join(shots, name + '.png') })
  assert.ok(geometry.overflow <= 1, `${name}: horizontal overflow`)
  assert.ok(geometry.footer <= geometry.height + 1, `${name}: footer below viewport`)
  assert.deepEqual(geometry.clipped, [], `${name}: clipped content`)
  assert.deepEqual(geometry.controlsOutside, [], `${name}: footer controls outside viewport`)
}
try {
  await page.goto(base + '?launcher=1&demo=1')
  await ready(/Учиться проще/)
  assert.equal(await page.locator('.mx-wordmark').innerText(), 'МаксИИмка.')
  await shot('welcome')
  await page.setViewportSize({ width: 724, height: 581 })
  await shot('welcome-compact')
  await page.setViewportSize({ width: 844, height: 661 })
  await click('Начать настройку')
  const maxToggle = page.getByRole('switch', { name: 'Подключить MAX' })
  await maxToggle.click()
  await click('Продолжить')
  await page.getByRole('alert').filter({ hasText: 'Укажи токен MAX' }).waitFor()
  await page.getByLabel('Токен бота MAX', { exact: true }).fill('offline-demo-token')
  await page.getByLabel('ID администратора MAX', { exact: true }).fill('bad-id')
  await click('Продолжить')
  await page.getByRole('alert').filter({ hasText: 'положительным целым' }).waitFor()
  await page.getByLabel('ID администратора MAX', { exact: true }).fill('123456789')
  await shot('max')
  await click('Продолжить')
  await ready('Выбери подключение')
  await page.getByLabel('Провайдер', { exact: true }).selectOption('openai')
  await page.getByLabel('Адрес API', { exact: true }).fill('https://provider.example.test/v1')
  await page.getByLabel('API-ключ', { exact: true }).fill('offline-demo-key')
  await shot('provider')
  await click('Найти модели')
  await ready('Модель для учёбы')
  await page.getByLabel('Модель', { exact: true }).selectOption('gpt-4.1-mini')
  await page.getByLabel('Уровень размышлений', { exact: true }).selectOption('medium')
  await page.getByLabel('Размер контекста', { exact: true }).selectOption('200000')
  await shot('model')
  await click('Ввести название вручную')
  await page.getByLabel('Название модели', { exact: true }).fill('my-private-model')
  await click('Перепроверить')
  await page.getByLabel('Уровень размышлений', { exact: true }).selectOption('high')
  await click('Продолжить')
  await ready('Распознавание материалов')
  const ocr = page.getByRole('switch', { name: 'Распознавать фото и сканы' })
  if (await ocr.getAttribute('aria-checked') !== 'true') await ocr.click()
  await page.getByLabel('Способ распознавания', { exact: true }).selectOption('llm')
  await page.getByLabel('Параллельных страниц', { exact: true }).selectOption('4')
  await shot('ocr')
  await page.setViewportSize({ width: 724, height: 581 })
  await shot('ocr-compact')
  await click('Продолжить')
  await ready('Последние детали')
  await page.getByLabel('Что показывать в терминале', { exact: true }).selectOption('reasoning')
  await page.getByLabel('Индикатор ожидания', { exact: true }).selectOption('on')
  await page.locator('summary').click()
  await page.getByLabel('Параллельных запросов', { exact: true }).selectOption('4')
  await page.getByLabel('Таймаут запроса', { exact: true }).selectOption('300')
  await shot('advanced-compact')
  await page.setViewportSize({ width: 844, height: 661 })
  await page.locator('.ml-main').evaluate(element => { element.scrollTop = 0 })
  await shot('advanced')
  await click('Сохранить настройки')
  await ready('Готов к работе')
  await shot('home')
  passed.push('Six-step setup, token and admin validation, models, manual entry, effort, OCR and all advanced settings')

  await page.getByRole('radio', { name: /Работать в фоне/ }).check()
  assert.equal(await page.getByRole('radio', { name: /Открыть терминал/ }).isChecked(), false)
  await click('Старт')
  await ready('МаксИИмка работает')
  assert.equal(await page.getByRole('checkbox').isDisabled(), true)
  await shot('running')
  await click('Остановить')
  await ready('Готов к работе')
  await page.setViewportSize({ width: 724, height: 581 })
  await shot('home-compact')
  await page.goto(base + '?launcher=1&demo=1&configured=1')
  await ready('Готов к работе')
  assert.equal(await page.getByRole('progressbar').count(), 0)
  await click('Изменить настройки')
  await ready('Подключим бота к MAX')
  await shot('max-compact')
  passed.push('Repeat launch, mutually exclusive terminal/background modes, start/stop, settings reopening, 724px layout')

  const fixture = {
    setup_complete: false,
    settings: { values: { provider: 'openai', base_url: 'https://provider.example.test/v1', model: 'manual-model',
      effort: 'auto', context: 0, gw_parallel: 3, gw_timeout: 180, gw_window: 'hidden', ocr: true,
      ocr_backend: 'easyocr', ocr_workers: 3, display_mode: 'statuses', spinner: 'auto' },
      profiles: { openai: { base_url: 'https://provider.example.test/v1', model: 'manual-model', api_key_set: false },
        qoder: { base_url: 'http://127.0.0.1:8787/v1', model: 'local-model', api_key_set: false } },
      capabilities: { vision: null, efforts: ['auto'] }, choices: { provider: ['openai', 'qoder'], context: [0, 200000],
        ocr_workers: [1, 3, 5], display_mode: ['statuses', 'reasoning'], spinner: ['auto', 'on', 'off'],
        gw_parallel: [1, 3], gw_timeout: [180, 300], gw_window: ['hidden', 'window'] } },
    bot: { enabled: false, admin_id: '', token_set: false }, launch: { panel: true, mode: 'terminal' }, runtime: { status: 'stopped' },
  }
  await page.addInitScript(data => {
    window.checkCalls = []
    window.pywebview = { api: {
      load: async () => ({ ok: true, data }),
      check: async (...args) => {
        window.checkCalls.push(args)
        return args[0] === 'load_models' ? { ok: false, error: 'HTTP 404: /models недоступен' }
          : { ok: true, data: { models: [], message: 'Возможности не подтверждены', capabilities: { efforts: ['auto'], vision: { supported: null }, reasoning: null } } }
      },
    } }
  }, fixture)
  await page.goto(base + '?launcher=1')
  await click('Начать настройку')
  await click('Продолжить')
  await click('Найти модели')
  await page.getByRole('alert').waitFor()
  await click('Ввести модель вручную')
  await page.getByLabel('Название модели').fill('offline-model')
  await click('Продолжить')
  await ready('Распознавание материалов')
  assert.deepEqual(await page.getByLabel('Способ распознавания').locator('option').allTextContents(), ['EasyOCR · на компьютере'])
  const calls = await page.evaluate(() => window.checkCalls)
  assert.deepEqual(calls.map(call => call[0]), ['load_models', 'capabilities'])
  assert.equal(calls[1][1].model, 'offline-model')
  await shot('unknown-capabilities')
  passed.push('Native bridge error recovery, missing /models endpoint, capability check on continue, conservative OCR with unknown support')
  assert.deepEqual(errors, [], 'Browser errors')
  assert.deepEqual(remoteRequests, [], 'Launcher must not load remote scripts or MAX SDK')
  const result = { passed, browserErrors: errors, remoteRequests }
  await writeFile(path.join(shots, 'results.json'), JSON.stringify(result, null, 2))
  console.log(JSON.stringify(result, null, 2))
} finally { await browser.close() }
