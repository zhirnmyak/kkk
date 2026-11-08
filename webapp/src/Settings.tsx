import { useEffect, useState } from 'react'
import { Check, ChevronDown, RefreshCw, Save, ScanText, SlidersHorizontal, Terminal, Unplug } from 'lucide-react'
import { api, type CheckResult, type Snapshot, type Values, messageOf } from './api'
import { demoSettings } from './demo'
import { Badge, Button, Card, ErrorBox, Field, PageHeading, Row, Tabs, Toggle } from './ui'

const LABELS: Record<string, string> = {
  auto: 'Автоматически', none: 'Без размышлений', minimal: 'Минимальный', low: 'Низкий', medium: 'Средний',
  high: 'Высокий', xhigh: 'Очень высокий', max: 'Максимальный', ultracode: 'Ultracode',
  easyocr: 'EasyOCR · локально', llm: 'Vision · через модель', hidden: 'Скрыто', window: 'Отдельное окно',
  statuses: 'Только статусы', reasoning: 'Размышления и статусы', on: 'Включён', off: 'Выключен',
  openai: 'OpenAI-совместимый API', qoder: 'Локальный шлюз Qoder',
}

export function Settings({ demo, notify, setDirty }: { demo: boolean; notify: (text: string) => void; setDirty: (dirty: boolean) => void }) {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(demo ? structuredClone(demoSettings) : null)
  const [draft, setDraft] = useState<Values | null>(demo ? { ...demoSettings.values } : null)
  const [key, setKey] = useState<string | undefined>()
  const [tab, setTab] = useState(0)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [result, setResult] = useState<{ text: string; ok: boolean } | null>(null)
  const [models, setModels] = useState<string[]>([])
  const [reload, setReload] = useState(0)
  const dirty = !!snapshot && !!draft && (JSON.stringify(draft) !== JSON.stringify(snapshot.values) || key !== undefined)
  useEffect(() => { setDirty(dirty); return () => setDirty(false) }, [dirty, setDirty])
  useEffect(() => {
    if (demo) return
    const controller = new AbortController()
    api<Snapshot>('/admin/settings', { signal: controller.signal })
      .then(data => { setSnapshot(data); setDraft({ ...data.values }); setKey(undefined); setError('') })
      .catch(err => { if (!controller.signal.aborted) setError(messageOf(err)) })
    return () => controller.abort()
  }, [demo, reload])

  if (!draft || !snapshot) return <div className="mx-settings">
    <PageHeading label="Управление ботом" title="Настройки">Подключение, модель и обработка материалов.</PageHeading>
    {error ? <ErrorBox retry={() => setReload(value => value + 1)}>{error}</ErrorBox> : <p role="status">Загружаю настройки…</p>}
  </div>

  function change<K extends keyof Values>(field: K, value: Values[K]) {
    setDraft(previous => previous && ({ ...previous, [field]: value }))
    setResult(null)
  }
  function provider(value: string) {
    const profile = snapshot!.profiles[value]
    setDraft(previous => previous && ({ ...previous, provider: value, base_url: profile.base_url, model: profile.model, effort: 'auto' }))
    setKey(undefined); setModels([]); setResult(null)
  }
  function select(field: keyof Values, label: string, hint?: string) {
    const items = snapshot!.choices[field] || []
    return <div className="mx-setting-field">
      <Field label={label} value={String(draft![field])} trailing={<ChevronDown size={14} aria-hidden="true" />}
        select={{ disabled: !!busy, options: items.map(value => ({
          value: String(value),
          label: field === 'context' ? (value === 0 ? 'Без ограничения' : Number(value).toLocaleString('ru-RU') + ' токенов')
            : field === 'gw_timeout' ? value + ' секунд'
            : field === 'ocr_workers' && value === 0 ? 'Все страницы сразу'
            : LABELS[String(value)] || String(value),
        })), onChange: value => field === 'provider' ? provider(value) : change(field,
          (typeof draft![field] === 'number' ? Number(value) : value) as never) }} />
      {hint && <p className="mx-hint">{hint}</p>}
    </div>
  }
  async function save() {
    setBusy('save'); setError(''); setResult(null)
    try {
      const patch: Record<string, unknown> = {}
      for (const field of Object.keys(draft!) as (keyof Values)[]) {
        if (draft![field] !== snapshot!.values[field]) patch[field] = draft![field]
      }
      if (key !== undefined) patch.api_key = key
      const data = demo ? { ...snapshot!, values: { ...draft! }, api_key_set: key === undefined ? snapshot!.api_key_set : !!key }
        : await api<Snapshot>('/admin/settings', { method: 'PATCH', body: JSON.stringify({ revision: snapshot!.revision, patch }) })
      setSnapshot(data); setDraft({ ...data.values }); setKey(undefined)
      notify(demo ? 'Пример настроек обновлён. Рабочий бот не изменён.' : data.message || 'Настройки сохранены.')
    } catch (err) { setError(messageOf(err)) } finally { setBusy('') }
  }
  async function check(action: string) {
    setBusy(action); setError(''); setResult(null)
    try {
      if (demo) { setResult({ ok: true, text: 'Демонстрация: здесь появится результат проверки вашего подключения.' }); return }
      const data = await api<CheckResult>('/admin/checks/' + action, { method: 'POST', body: '{}' })
      if (data.revision !== snapshot!.revision) {
        setError('Во время проверки настройки изменились. Обновите настройки.'); return
      }
      setModels(data.models); setResult({ ok: data.ok, text: data.message })
      setSnapshot(data.settings); setDraft({ ...data.settings.values })
    } catch (err) { setError(messageOf(err)) } finally { setBusy('') }
  }
  const blocked = !!busy
  const keySet = snapshot.profiles[draft.provider]?.api_key_set
  return <div className="mx-settings">
    <PageHeading label="Управление ботом" title="Настройки" action={<Badge tone="neutral">Только администратор</Badge>}>
      Изменения применяются ко всему боту.
    </PageHeading>
    <Tabs className="mx-settings-tabs" value={tab} onChange={setTab}
      items={['Подключение', 'Модель', 'Распознавание', 'Дополнительно']} />
    {error && <ErrorBox>{error}<Button disabled={blocked} onClick={() => setReload(value => value + 1)}>Обновить настройки</Button></ErrorBox>}
    <div className="mx-settings-content" role="tabpanel">
      {tab === 0 && <Card pad="large" gap={24}>
        <Row pad={0} lead={<Unplug size={20} />} title="Подключение к модели" subtitle="Выберите провайдера и укажите доступ к API." />
        {select('provider', 'Подключение')}
        <Field label="Адрес API" value={draft.base_url} placeholder="https://api.example.com/v1"
          input={{ disabled: blocked, onChange: event => change('base_url', event.target.value), spellCheck: false, autoCapitalize: 'none', type: 'url' }} />
        <div className="mx-setting-field">
          <Field label="API-ключ" password value={key || ''} placeholder={keySet && key === undefined ? 'Ключ сохранён на сервере' : 'Вставьте API-ключ'}
            input={{ disabled: blocked, autoComplete: 'new-password', onChange: event => setKey(event.target.value), spellCheck: false, autoCapitalize: 'none' }} />
          <div className="mx-key-hint"><p className="mx-hint">Сохранённый ключ не передаётся в приложение.</p>
            {keySet && <Button size="small" variant="ghost" disabled={blocked} onClick={() => setKey(key === '' ? undefined : '')}>{key === '' ? 'Оставить ключ' : 'Удалить ключ'}</Button>}
          </div>
        </div>
        <div className="mx-check-actions">
          <Button size="touch" icon={<RefreshCw />} disabled={blocked || dirty} onClick={() => check('test_connection')}>
            {busy === 'test_connection' ? 'Проверяю…' : 'Проверить подключение'}
          </Button>
          {dirty && <span className="mx-hint">Сначала сохраните изменения</span>}
        </div>
      </Card>}
      {tab === 1 && <Card pad="large" gap={24}>
        <Row pad={0} lead={<SlidersHorizontal size={20} />} title="Параметры модели" subtitle="Ответы и объём контекста для учебных задач." />
        <Field label="Название модели" value={draft.model} input={{ disabled: blocked, list: 'model-options',
          onChange: event => change('model', event.target.value), spellCheck: false, autoCapitalize: 'none' }} />
        <datalist id="model-options">{models.map(model => <option key={model} value={model} />)}</datalist>
        <div className="mx-two-columns">{select('effort', 'Уровень размышлений', 'Доступность зависит от выбранной модели.')}
          {select('context', 'Размер контекста')}</div>
        <div className="mx-check-actions">
          <Button size="touch" disabled={blocked || dirty} icon={<RefreshCw />} onClick={() => check('load_models')}>{busy === 'load_models' ? 'Загружаю…' : 'Загрузить модели'}</Button>
          <Button size="touch" disabled={blocked || dirty} onClick={() => check('test_capabilities')}>{busy === 'test_capabilities' ? 'Проверяю…' : 'Перепроверить модель'}</Button>
        </div>
      </Card>}
      {tab === 2 && <Card pad="large" gap={24}>
        <Row pad={0} lead={<ScanText size={20} />} title="Распознавание текста" subtitle="Для сканов, фотографий и рукописных заметок." />
        <Row className="mx-switch-row" pad={12} title="OCR сканов и рукописи" subtitle={draft.ocr ? 'Распознавание включено' : 'Будет читаться только готовый текст'}>
          <Toggle on={draft.ocr} tabIndex={blocked ? -1 : 0} aria-label="OCR сканов и рукописи" aria-disabled={blocked}
            onClick={() => { if (!blocked) change('ocr', !draft.ocr) }} onKeyDown={event => {
              if (!blocked && [' ', 'Enter'].includes(event.key)) { event.preventDefault(); change('ocr', !draft.ocr) }
            }} />
        </Row>
        <div className="mx-two-columns">{select('ocr_backend', 'Движок OCR', 'Vision использует текущее подключение к модели.')}
          {select('ocr_workers', 'Параллельных страниц')}</div>
        <Button size="touch" disabled={blocked || dirty} icon={<ScanText />} onClick={() => check('test_vision')}>{busy === 'test_vision' ? 'Проверяю…' : 'Проверить Vision для OCR'}</Button>
      </Card>}
      {tab === 3 && <>
        <Card pad="large" gap={24}>
          <Row pad={0} lead={<SlidersHorizontal size={20} />} title="Локальный шлюз" subtitle="Применяется при подключении через Qoder." />
          <div className="mx-two-columns">{select('gw_parallel', 'Параллельных запросов')}{select('gw_timeout', 'Таймаут запроса')}</div>
          {select('gw_window', 'Окно шлюза', 'Изменение этих параметров перезапустит локальный шлюз Qoder.')}
        </Card>
        <Card pad="large" gap={24}>
          <Row pad={0} lead={<Terminal size={20} />} title="Терминал" subtitle="Оформление консоли на компьютере с ботом." />
          <div className="mx-two-columns">{select('display_mode', 'Что показывать')}{select('spinner', 'Индикатор «думаю»')}</div>
        </Card>
      </>}
      {result && <div className={'mx-check-result ' + (result.ok ? '' : 'mx-check-result--error')} role="status">
        {result.ok && <Check size={18} />}<span>{result.text}</span>
      </div>}
    </div>
    <div className="mx-savebar">
      <span className="mx-save-state">{busy === 'save' ? 'Применяю изменения…' : dirty ? 'Есть несохранённые изменения' : <><Check size={16} /> Все изменения сохранены</>}</span>
      <div className="mx-actions">
        {dirty && <Button size="touch" disabled={blocked} onClick={() => { setDraft({ ...snapshot.values }); setKey(undefined); setError('') }}>Отменить</Button>}
        <Button size="touch" variant="primary" icon={<Save />} disabled={!dirty || blocked} onClick={save}>{busy === 'save' ? 'Сохраняю…' : 'Сохранить'}</Button>
      </div>
    </div>
  </div>
}
