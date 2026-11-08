import { useEffect, useRef, useState } from 'react'
import { ArrowLeft, ArrowRight, BookOpen, Check, CircleHelp, FileText, Globe, Layers, Play, RefreshCw, ScanText, Settings2, ShieldCheck, Square, Terminal, Unplug } from 'lucide-react'
import { Badge, Button, Card, Field, Meter, Row, Toggle } from '../stageshow/kit'
import { LookContext } from '../stageshow/kit/looks'
import type { Values } from '../api'
import { desktop, type Capabilities, type CheckResponse, type LaunchOptions, type Runtime, type SetupState } from './bridge'
import DesignPreview from '../../design-reference/static-card'
import './launcher.css'

const STEPS = ['Знакомство', 'MAX', 'Подключение', 'Модель', 'Распознавание', 'Завершение']
const LABELS: Record<string, string> = {
  auto: 'Автоматически', none: 'Без размышлений', minimal: 'Минимальный', low: 'Низкий', medium: 'Средний', high: 'Высокий',
  xhigh: 'Очень высокий', max: 'Максимальный', ultracode: 'Ultracode', openai: 'OpenAI-совместимый API', qoder: 'Локальный шлюз Qoder',
  easyocr: 'EasyOCR · на компьютере', llm: 'Vision · через модель', hidden: 'Скрыто', window: 'Отдельное окно',
  statuses: 'Только статусы', reasoning: 'Размышления и статусы', on: 'Включён', off: 'Выключен',
}
function message(error: unknown) { return error instanceof Error ? error.message : 'Не удалось выполнить действие.' }

export default function Launcher() {
  const demo = new URLSearchParams(location.search).get('demo') === '1'
  const [state, setState] = useState<SetupState | null>(null)
  const [draft, setDraft] = useState<Values | null>(null)
  const [step, setStep] = useState(0)
  const [apiKey, setApiKey] = useState<string | undefined>()
  const [token, setToken] = useState<string | undefined>()
  const [admin, setAdmin] = useState('')
  const [botEnabled, setBotEnabled] = useState(false)
  const [launch, setLaunch] = useState<LaunchOptions>({ panel: true, mode: 'terminal' })
  const [runtime, setRuntime] = useState<Runtime>({ status: 'stopped' })
  const [models, setModels] = useState<string[]>([])
  const [manualModel, setManualModel] = useState(false)
  const [caps, setCaps] = useState<Capabilities>({ efforts: ['auto'] })
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [ready, setReady] = useState(false)
  const alive = useRef(true)
  const checkedModel = useRef('')

  function modelKey(values: Values) { return [values.provider, values.base_url, values.model, apiKey ?? 'saved'].join('\n') }

  function accept(data: SetupState) {
    setState(data); setDraft({ ...data.settings.values }); setAdmin(data.bot.admin_id)
    setBotEnabled(data.bot.enabled); setLaunch(data.launch); setRuntime(data.runtime)
    setCaps({ vision: data.settings.capabilities.vision as Capabilities['vision'], efforts: data.settings.capabilities.efforts })
    setApiKey(undefined); setToken(undefined)
  }
  useEffect(() => {
    alive.current = true
    desktop<SetupState>('load').then(data => {
      if (!alive.current) return
      accept(data); setStep(data.setup_complete ? -1 : 0); setReady(true)
    }).catch(err => { if (alive.current) setError(message(err)) })
    return () => { alive.current = false }
  }, [])
  useEffect(() => {
    if (!ready || step !== -1) return
    const timer = setInterval(() => desktop<Runtime>('status').then(value => {
      if (alive.current) setRuntime(value)
    }).catch(() => {}), 1000)
    return () => clearInterval(timer)
  }, [ready, step])

  const active = runtime.status !== 'stopped'
  function change<K extends keyof Values>(field: K, value: Values[K]) {
    setDraft(previous => previous && ({ ...previous, [field]: value })); setError(''); setNotice('')
  }
  function patch(values = draft!) { return { ...values, ...(apiKey !== undefined ? { api_key: apiKey } : {}) } }
  function provider(value: string) {
    const profile = state!.settings.profiles[value]
    setDraft(previous => previous && ({ ...previous, provider: value, base_url: profile.base_url, model: profile.model, effort: 'auto' }))
    setApiKey(undefined); setModels([]); setCaps({ efforts: ['auto'] }); setError(''); setNotice('')
    checkedModel.current = ''
  }
  async function check(action: string, values = draft!, force = false) {
    setBusy(action); setError(''); setNotice('')
    try {
      const result = await desktop<CheckResponse>('check', action, patch(values), force)
      setCaps(result.capabilities)
      if (action === 'capabilities') {
        checkedModel.current = modelKey(values)
        setDraft(previous => previous && ({ ...previous,
          effort: result.capabilities.efforts.includes(previous.effort) ? previous.effort : 'auto',
          ocr_backend: result.capabilities.vision?.supported !== true ? 'easyocr' : previous.ocr_backend }))
      }
      setNotice(result.message)
      return result
    } catch (err) { setError(message(err)); return null } finally { setBusy('') }
  }
  async function next() {
    setError(''); setNotice('')
    if (step === 1) {
      if (botEnabled && !(token === undefined ? state!.bot.token_set : token.trim())) {
        setError('Укажи токен MAX или выключи подключение — его можно настроить позже.'); return
      }
      if (admin && (!/^[1-9]\d*$/.test(admin) || BigInt(admin) >= 2n ** 63n)) {
        setError('ID администратора должен быть положительным целым числом.'); return
      }
    }
    if (step === 2) {
      try {
        const url = new URL(draft!.base_url)
        if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash) throw new Error()
      } catch { setError('Укажи полный адрес API, например https://api.openai.com/v1.'); return }
      const result = await check('load_models')
      if (!result) return
      setModels(result.models)
      const model = result.models.includes(draft!.model) ? draft!.model : result.models[0]
      const values = { ...draft!, model, effort: 'auto' }
      setDraft(values); setManualModel(false); setStep(3)
      await check('capabilities', values)
      return
    }
    if (step === 3 && !draft!.model.trim()) { setError('Выбери или введи название модели.'); return }
    if (step === 3 && checkedModel.current !== modelKey(draft!)) {
      const result = await check('capabilities')
      if (!result) return
    }
    if (step === 5) {
      setBusy('save')
      try {
        const data = await desktop<SetupState>('save', { patch: patch(),
          bot: { enabled: botEnabled, admin_id: admin.trim(), ...(token !== undefined ? { token } : {}) }, launch })
        accept(data); setStep(-1); setNotice('Настройки сохранены. Можно запускать МаксИИмку.')
      } catch (err) { setError(message(err)) } finally { setBusy('') }
      return
    }
    setStep(value => value + 1)
  }
  function select(field: keyof Values, label: string, options?: (string | number)[], disabled = false) {
    const choices = options || state!.settings.choices[field] || []
    return <Field label={label} value={String(draft![field])} select={{ disabled: !!busy || disabled,
      options: choices.map(value => ({ value: String(value), label:
        field === 'context' ? value === 0 ? 'Без ограничения' : Number(value).toLocaleString('ru') + ' токенов'
        : field === 'gw_timeout' ? value + ' секунд'
        : field === 'ocr_workers' && value === 0 ? 'Все страницы сразу'
        : LABELS[String(value)] || String(value) })),
      onChange: value => field === 'provider' ? provider(value) : change(field, (typeof draft![field] === 'number' ? Number(value) : value) as never) }} />
  }
  function toggle(label: string, hint: string, value: boolean, onChange: () => void) {
    return <Row className="ml-toggle" pad={14} title={label} subtitle={hint}>
      <Toggle on={value} role="switch" aria-label={label} tabIndex={busy ? -1 : 0} aria-disabled={!!busy}
        onClick={() => { if (!busy) onChange() }} onKeyDown={event => {
          if (!busy && [' ', 'Enter'].includes(event.key)) { event.preventDefault(); onChange() }
        }} />
    </Row>
  }
  async function start() {
    setBusy('start'); setError(''); setNotice('')
    try { setRuntime(await desktop<Runtime>('start', launch)) }
    catch (err) { setError(message(err)) } finally { setBusy('') }
  }
  async function stop() {
    setBusy('stop'); setError('')
    try { setRuntime(await desktop<Runtime>('stop')) }
    catch (err) { setError(message(err)) } finally { setBusy('') }
  }

  return <LookContext.Provider value={{ theme: 'light', palette: 'accent', fidelity: 'realistic', layout: 'wide' }}>
    <div className="mx-app mx-launcher">
      <header className="ml-header"><div className="mx-wordmark">МаксИИмка<span>.</span></div>
        {demo ? <Badge tone="warning">Демонстрация</Badge> : <span>Твой помощник в учёбе</span>}
      </header>
      {ready && step >= 0 && <div className="ml-progress" aria-label={'Шаг ' + (step + 1) + ' из ' + STEPS.length}>
        <div className="ml-step-names">{STEPS.map((name, index) => <span key={name} data-state={index < step ? 'done' : index === step ? 'current' : 'next'}>
          {index < step ? <Check size={12} /> : <i>{index + 1}</i>}{name}
        </span>)}</div>
        <div role="progressbar" aria-valuemin={0} aria-valuemax={6} aria-valuenow={step + 1}><Meter value={(step + 1) / 6} /></div>
      </div>}
      <main className={'ml-main ' + (step === 0 ? 'ml-main--welcome' : '')}>
        {!ready || !draft || !state ? <div className="ml-loading"><h1>Открываем МаксИИмку…</h1><p>{error || 'Подготавливаем мастер настройки.'}</p></div> : <>
          {step === 0 && <div className="ml-welcome">
            <div><Badge tone="neutral">Всё готово к настройке</Badge><h1>Учиться проще,<br />когда всё под рукой.</h1>
              <p>Подключим модель и соберём твоё рабочее пространство. Это займёт несколько шагов.</p>
              <div className="ml-welcome-points">
                <Row pad={0} lead={<FileText size={18} />} title="Материалы и конспекты" />
                <Row pad={0} lead={<Layers size={18} />} title="Тесты и карточки" />
                <Row pad={0} lead={<Settings2 size={18} />} title="Все настройки в одном месте" />
              </div>
            </div>
            <div className="ml-welcome-art" aria-hidden="true"><DesignPreview /></div>
          </div>}
          {step === 1 && <>
            <div className="ml-title"><ShieldCheck /><div><h1>Подключим бота к MAX</h1><p>Доступ к боту и права администратора.</p></div></div>
            {toggle('Подключить MAX', 'Можно выключить и сначала попробовать локальную панель.', botEnabled, () => setBotEnabled(value => !value))}
            <div className="ml-form">
              <Field label="Токен бота MAX" password value={token || ''} placeholder={state.bot.token_set && token === undefined ? 'Токен уже сохранён' : 'Вставь токен бота'}
                input={{ disabled: !!busy, autoComplete: 'new-password', onChange: event => { setToken(event.target.value); setError('') }, spellCheck: false }} />
              <p className="ml-hint">Токен выдаётся в MAX для бизнеса при создании чат-бота.</p>
              <Field label="ID администратора MAX" value={admin} placeholder="Например, 123456789"
                input={{ disabled: !!busy, inputMode: 'numeric', onChange: event => { setAdmin(event.target.value); setError('') }, autoComplete: 'off' }} />
              <p className="ml-hint">Свой ID показывает команда /id в боте. Это поле можно заполнить позже.</p>
            </div>
          </>}
          {step === 2 && <>
            <div className="ml-title"><Unplug /><div><h1>Выбери подключение</h1><p>Модели загрузятся из API твоего провайдера.</p></div></div>
            <div className="ml-form">
              {select('provider', 'Провайдер')}
              <Field label="Адрес API" value={draft.base_url} placeholder="https://api.openai.com/v1"
                input={{ disabled: !!busy, type: 'url', autoCapitalize: 'none', spellCheck: false, onChange: event => change('base_url', event.target.value) }} />
              <Field label="API-ключ" password value={apiKey || ''} placeholder={state.settings.profiles[draft.provider]?.api_key_set && apiKey === undefined ? 'Ключ уже сохранён' : 'Вставь ключ провайдера'}
                input={{ disabled: !!busy, autoComplete: 'new-password', spellCheck: false, onChange: event => setApiKey(event.target.value) }} />
              <p className="ml-hint">Ключ сохраняется только на этом компьютере. Подойдут OpenAI и совместимые с его API сервисы.</p>
              {draft.provider === 'qoder' && <p className="ml-hint">Для Qoder нужен установленный и авторизованный Qoder CLI.</p>}
            </div>
          </>}
          {step === 3 && <>
            <div className="ml-title"><BookOpen /><div><h1>Модель для учёбы</h1><p>Проверим доступные уровни размышлений и работу с изображениями.</p></div></div>
            <div className="ml-form">
              {models.length && !manualModel ? <Field label="Модель" value={draft.model} select={{
                disabled: !!busy, options: models.map(name => ({ value: name, label: name })),
                onChange: name => { const next = { ...draft, model: name, effort: 'auto' }; setDraft(next); void check('capabilities', next) },
              }} /> : <Field label="Название модели" value={draft.model} input={{ disabled: !!busy, spellCheck: false,
                onChange: event => { setDraft({ ...draft, model: event.target.value, effort: 'auto', ocr_backend: 'easyocr' });
                  setCaps({ efforts: ['auto'] }); setError(''); setNotice('') } }} />}
              <div className="ml-inline"><Button variant="ghost" size="small" disabled={!!busy} onClick={() => setManualModel(value => !value)}>
                {manualModel && models.length ? 'Выбрать из списка' : 'Ввести название вручную'}
              </Button><Button variant="ghost" size="small" disabled={!!busy || !draft.model.trim()} icon={<RefreshCw />} onClick={() => check('capabilities', draft, true)}>Перепроверить</Button></div>
              <div className="ml-columns">{select('effort', 'Уровень размышлений', caps.efforts)}{select('context', 'Размер контекста')}</div>
              <div className="ml-capabilities">
                <Badge tone={caps.reasoning?.supported === true ? 'success' : 'neutral'}>Размышления: {caps.reasoning?.supported === true ? 'поддерживаются' : caps.reasoning?.supported === false ? 'без effort' : 'не подтверждены'}</Badge>
                <Badge tone={caps.vision?.supported === true ? 'success' : 'neutral'}>Изображения: {caps.vision?.supported === true ? 'поддерживаются' : caps.vision?.supported === false ? 'не поддерживаются' : 'не подтверждены'}</Badge>
              </div>
              <Button size="touch" disabled={!!busy || !draft.model.trim()} onClick={() => check('test_connection')}>Проверить ответ модели</Button>
            </div>
          </>}
          {step === 4 && <>
            <div className="ml-title"><ScanText /><div><h1>Распознавание материалов</h1><p>Фото страниц и сканы станут текстом для подготовки.</p></div></div>
            {toggle('Распознавать фото и сканы', 'Готовый текст PDF и презентаций читается в любом случае.', draft.ocr, () => change('ocr', !draft.ocr))}
            <div className="ml-form">
              <div className="ml-columns">
                {select('ocr_backend', 'Способ распознавания', caps.vision?.supported === true ? ['easyocr', 'llm'] : ['easyocr'], !draft.ocr)}
                {select('ocr_workers', 'Параллельных страниц', undefined, !draft.ocr)}
              </div>
              <Card tone="sunken" pad="large" gap={12}>
                <Row pad={0} lead={<ScanText size={20} />} title={caps.vision?.supported === true ? 'Модель умеет читать изображения' : 'Локальный OCR готов к работе'}
                  subtitle={caps.vision?.supported === true ? 'Можно использовать Vision через выбранное API.' : 'EasyOCR работает на компьютере. Его веса скачаются при первом распознавании.'} />
              </Card>
              <Button size="touch" disabled={!!busy} icon={<RefreshCw />} onClick={() => check('capabilities', draft, true)}>Проверить поддержку OCR моделью</Button>
            </div>
          </>}
          {step === 5 && <>
            <div className="ml-title"><Settings2 /><div><h1>Последние детали</h1><p>Всё это можно будет изменить позже.</p></div></div>
            <div className="ml-form">
              <div className="ml-columns">{select('display_mode', 'Что показывать в терминале')}{select('spinner', 'Индикатор ожидания')}</div>
              <details className="ml-advanced" open={draft.provider === 'qoder'}><summary>Параметры локального шлюза Qoder</summary>
                <div className="ml-columns">{select('gw_parallel', 'Параллельных запросов')}{select('gw_timeout', 'Таймаут запроса')}</div>
                {select('gw_window', 'Окно шлюза')}
              </details>
              <Card tone="sunken" pad="large" gap={12}>
                <Row pad={0} lead={<Check size={20} />} title={draft.model}
                  subtitle={(botEnabled ? 'MAX подключится при запуске' : 'Локальный режим без MAX') + ' · OCR ' + (draft.ocr ? 'включён' : 'выключен')} />
              </Card>
            </div>
          </>}
          {step === -1 && <>
            <div className="ml-home-heading"><div><h1>{active ? 'МаксИИмка работает' : 'Готов к работе'}</h1><p>{draft.model}</p></div>
              <Badge tone={runtime.status === 'running' ? 'success' : 'neutral'}>{runtime.status === 'running' ? 'Запущен' : runtime.status === 'starting' ? 'Запускается' : runtime.status === 'stopping' ? 'Останавливается' : 'Остановлен'}</Badge>
            </div>
            <Card pad="large" gap={0} className="ml-launch-options">
              <label><input type="checkbox" checked={launch.panel} disabled={active || !!busy} onChange={event => setLaunch(value => ({ ...value, panel: event.target.checked }))} />
                <Row pad={12} lead={<Globe size={21} />} title="Открыть панель" subtitle="Материалы и настройки в локальном браузере" /></label>
              <div className="ml-options-rule" />
              <label><input type="radio" name="run-mode" checked={launch.mode === 'terminal'} disabled={active || !!busy} onChange={() => setLaunch(value => ({ ...value, mode: 'terminal' }))} />
                <Row pad={12} lead={<Terminal size={21} />} title="Открыть терминал" subtitle="Общаться с ботом и видеть его работу" /></label>
              <label><input type="radio" name="run-mode" checked={launch.mode === 'background'} disabled={active || !!busy} onChange={() => setLaunch(value => ({ ...value, mode: 'background' }))} />
                <Row pad={12} lead={<Layers size={21} />} title="Работать в фоне" subtitle="Бот работает без окна терминала" /></label>
            </Card>
            <div className="ml-home-tools"><Button size="touch" variant="ghost" disabled={active || !!busy} icon={<Settings2 />}
              onClick={() => { setStep(1); setError(''); setNotice('') }}>Изменить настройки</Button>
              {runtime.status === 'running' && runtime.panel && <Button size="touch" onClick={() => desktop('open_panel').catch(err => setError(message(err)))}>Открыть панель</Button>}
            </div>
            <p className="ml-hint">{active ? runtime.mode === 'background' ? 'Это окно можно закрыть. Бот продолжит работать; остановить его можно здесь.' : 'Бот работает в отдельном терминале. Завершить работу можно здесь или командой /выход.' : 'Панель открывается отдельно. Для бота выбирается один режим: терминал или фон.'}</p>
          </>}
          <div className="ml-feedback" aria-live="polite">
            {busy && <div className="ml-checking"><RefreshCw size={15} className="ml-spin" /><span>{busy === 'load_models' ? 'Получаем список моделей…' : busy === 'capabilities' ? 'Проверяем возможности модели…' : busy === 'save' ? 'Сохраняем настройки…' : busy === 'start' ? 'Запускаем МаксИИмку…' : busy === 'stop' ? 'Останавливаем…' : 'Проверяем подключение…'}</span>
              {['load_models', 'capabilities', 'test_connection'].includes(busy) && <Button variant="ghost" size="small" onClick={() => desktop('cancel_check')}>Отменить</Button>}</div>}
            {error && <div className="ml-error" role="alert"><CircleHelp size={17} /><span>{error}</span></div>}
            {step === -1 && !active && !error && runtime.message && runtime.message !== 'МаксИИмка остановлена' &&
              <div className="ml-error" role="alert"><CircleHelp size={17} /><span>{runtime.message}</span></div>}
            {!busy && !error && notice && <p className="ml-notice"><Check size={15} />{notice}</p>}
            {error && step === 2 && <Button variant="ghost" size="small" onClick={() => { setManualModel(true); setStep(3); setError('') }}>Ввести модель вручную</Button>}
            {error && step === 3 && <Button variant="ghost" size="small" onClick={() => {
              setDraft({ ...draft, effort: 'auto', ocr_backend: 'easyocr' }); setCaps({ efforts: ['auto'] });
              setStep(4); setError(''); setNotice('Возможности модели можно проверить позже. Пока выбран локальный OCR.')
            }}>Продолжить без проверки</Button>}
          </div>
        </>}
      </main>
      {ready && <footer className="ml-footer">
        <div>{step >= 0 ? <span className="ml-footer-note">{step + 1} / {STEPS.length} · {STEPS[step]}</span>
          : <span className="ml-footer-note"><ShieldCheck size={14} /> Настройки сохранены на компьютере</span>}</div>
        <div className="ml-footer-buttons">
          {step > 0 && <Button size="touch" variant="ghost" disabled={!!busy} icon={<ArrowLeft />} onClick={() => { setStep(value => value - 1); setError(''); setNotice('') }}>Назад</Button>}
          {step >= 0 ? <Button size="touch" variant="primary" disabled={!!busy} trailingIcon={<ArrowRight />} onClick={next}>
            {step === 0 ? 'Начать настройку' : step === 5 ? 'Сохранить настройки' : step === 2 ? 'Найти модели' : 'Продолжить'}</Button>
            : active ? <Button size="touch" variant="secondary" disabled={!!busy} icon={<Square />} onClick={stop}>Остановить</Button>
            : <Button size="touch" variant="primary" disabled={!!busy} icon={<Play />} onClick={start}>Старт</Button>}
        </div>
      </footer>}
    </div>
  </LookContext.Provider>
}
