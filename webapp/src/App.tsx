import { useCallback, useEffect, useMemo, useState } from 'react'
import { ArrowLeft, ArrowUpRight, BookOpen, Check, ChevronRight, FileText, FolderOpen, Layers,
  Plus, RefreshCw, Search, Settings2, ShieldCheck, Trash2 } from 'lucide-react'
import { api, type Detail, type Material, type User, localHost, messageOf, openInMax } from './api'
import { demoDetail, demoFiles, demoUser } from './demo'
import { Settings } from './Settings'
import { Avatar, Badge, Button, Card, Empty, ErrorBox, Field, FileIcon,
  Modal, NavItem, PageHeading, Row, Toast } from './ui'
import { LookContext } from './stageshow/kit/looks'

function dateLabel(value: string | null) {
  if (!value) return 'Сейчас'
  return new Date(value.replace(' ', 'T') + 'Z').toLocaleDateString('ru-RU', { day: 'numeric', month: 'short' })
}
function formatLabel(title: string) {
  const extension = title.split('.').at(-1)?.toUpperCase()
  return extension && extension.length < 6 ? extension : 'ТЕКСТ'
}
type Screen = 'files' | 'settings'
type Owner = { user_id: number; files: number; label?: string }

export default function App({ forceDemo = false }: { forceDemo?: boolean }) {
  const queryParams = new URLSearchParams(location.search)
  const demo = forceDemo || queryParams.get('demo') === '1'
  const [user, setUser] = useState<User | null>(demo ? { ...demoUser, is_admin: queryParams.get('role') !== 'student' } : null)
  const [authError, setAuthError] = useState('')
  const [screen, setScreen] = useState<Screen>(queryParams.get('screen') === 'settings' && demo ? 'settings' : 'files')
  const [files, setFiles] = useState<Material[]>(demo ? [...demoFiles] : [])
  const [loading, setLoading] = useState(!demo)
  const [error, setError] = useState('')
  const [search, setSearch] = useState('')
  const [type, setType] = useState('all')
  const [selected, setSelected] = useState<number | null>(demo ? 1 : null)
  const [detail, setDetail] = useState<Detail | null>(demo ? demoDetail(demoFiles[0]) : null)
  const [detailError, setDetailError] = useState('')
  const [mobileDetail, setMobileDetail] = useState(false)
  const [reload, setReload] = useState(0)
  const [owner, setOwner] = useState<number | null>(null)
  const [owners, setOwners] = useState<Owner[]>([])
  const [modal, setModal] = useState<'upload' | 'delete' | 'preview' | 'leave' | null>(null)
  const [busy, setBusy] = useState(false)
  const [actionError, setActionError] = useState('')
  const [toast, setToast] = useState('')
  const [dirty, setDirty] = useState(false)
  const [destination, setDestination] = useState<Screen>('files')
  const notify = useCallback((text: string) => setToast(text), [])
  const ownerQuery = owner && owner !== user?.id ? '?owner_id=' + owner : ''
  const isOwn = owner === null || owner === user?.id
  const active = files.find(file => file.id === selected && selected !== null) || null
  const visible = useMemo(() => files.filter(file => file.title.toLocaleLowerCase('ru').includes(search.toLocaleLowerCase('ru')) &&
    (type === 'all' || formatLabel(file.title) === type)), [files, search, type])

  useEffect(() => {
    if (demo) return
    const controller = new AbortController()
    api<User>('/me', { signal: controller.signal }).then(setUser)
      .catch(err => { if (!controller.signal.aborted) setAuthError(messageOf(err)) })
    return () => controller.abort()
  }, [demo])
  useEffect(() => {
    if (!user || demo) return
    const controller = new AbortController()
    setLoading(true); setError('')
    api<{ files: Material[] }>('/files' + ownerQuery, { signal: controller.signal })
      .then(data => {
        setFiles(data.files)
        setSelected(previous => data.files.some(file => file.id === previous) ? previous : data.files.find(file => file.id !== null)?.id ?? null)
      }).catch(err => { if (!controller.signal.aborted) setError(messageOf(err)) })
      .finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [user, demo, ownerQuery, reload])
  useEffect(() => {
    if (!user?.is_admin || demo) return
    const controller = new AbortController()
    api<{ users: Owner[] }>('/admin/users', { signal: controller.signal })
      .then(data => setOwners(data.users)).catch(() => {})
    return () => controller.abort()
  }, [user, demo, reload])
  useEffect(() => {
    if (!active) { setDetail(null); setDetailError(''); return }
    if (demo) { setDetail(demoDetail(active)); return }
    const controller = new AbortController()
    setDetail(null); setDetailError('')
    api<Detail>('/files/' + selected + ownerQuery, { signal: controller.signal }).then(setDetail)
      .catch(err => { if (!controller.signal.aborted) setDetailError(messageOf(err)) })
    return () => controller.abort()
  }, [selected, ownerQuery, demo, active?.title, reload])
  useEffect(() => {
    if (demo || !files.some(file => file.status === 'processing')) return
    const timer = setInterval(() => setReload(value => value + 1), 5000)
    return () => clearInterval(timer)
  }, [demo, files])
  useEffect(() => {
    if (!toast) return
    const timer = setTimeout(() => setToast(''), 6500)
    return () => clearTimeout(timer)
  }, [toast])
  useEffect(() => {
    if (window.WebApp?.initData) {
      if (dirty) window.WebApp.enableClosingConfirmation?.()
      else window.WebApp.disableClosingConfirmation?.()
    }
    function leave(event: BeforeUnloadEvent) { event.preventDefault(); event.returnValue = '' }
    if (dirty) window.addEventListener('beforeunload', leave)
    return () => window.removeEventListener('beforeunload', leave)
  }, [dirty])
  useEffect(() => {
    if (selected !== null && !visible.some(file => file.id === selected)) {
      setSelected(visible.find(file => file.id !== null)?.id ?? null)
    }
  }, [visible, selected])

  function navigate(next: Screen) {
    if (next === screen) return
    if (dirty) { setDestination(next); setModal('leave'); return }
    setScreen(next); setMobileDetail(false)
  }
  function selectFile(file: Material) {
    if (!file.id || file.status === 'processing') return
    setSelected(file.id); setMobileDetail(true)
  }
  async function remove() {
    if (!active) return
    setBusy(true); setActionError('')
    try {
      if (!demo) await api('/files/' + active.id + ownerQuery, { method: 'DELETE' })
      setFiles(previous => previous.filter(file => file.id !== active.id))
      setSelected(null); setMobileDetail(false); setModal(null)
      notify(demo ? 'Материал удалён из демонстрационного списка.' : 'Материал и связанные задания удалены.')
    } catch (err) { setActionError(messageOf(err)) } finally { setBusy(false) }
  }
  async function study(kind: 'summary' | 'quiz' | 'cards') {
    if (!active) return
    setBusy(true); setActionError('')
    try {
      if (demo) { notify('В рабочем приложении задание по этому материалу появится в чате.'); return }
      const data = await api<{ message: string }>('/files/' + active.id + '/study', {
        method: 'POST', body: JSON.stringify({ kind, count: kind === 'quiz' ? 3 : 5 }),
      })
      notify(data.message)
    } catch (err) { setActionError(messageOf(err)) } finally { setBusy(false) }
  }

  const content = !user ? <div className="mx-auth">
    <div className="mx-wordmark">МаксИИмка<span>.</span></div>
    <Card pad="large" gap={24}>
      <BookOpen size={32} strokeWidth={1.4} />
      <h1>{authError ? (localHost ? 'Открой локальную ссылку' : 'Ваше пространство для учёбы') : 'Открываем материалы…'}</h1>
      <p>{authError && localHost ? 'Открой полную ссылку администратора из терминала. После перезапуска бота нужна новая ссылка.' : authError || 'Подключаемся к вашему аккаунту MAX.'}</p>
      {authError && !localHost && <Button size="touch" variant="primary" onClick={() => openInMax()} trailingIcon={<ArrowUpRight />}>Открыть в MAX</Button>}
    </Card>
  </div> : <>
    <aside className="mx-sidebar">
      <div className="mx-brand"><div className="mx-wordmark">МаксИИмка<span>.</span></div><span>Помощник в учёбе</span></div>
      <div className="mx-nav-label">Рабочее пространство</div>
      <nav aria-label="Основная навигация">
        <NavItem icon={<FolderOpen size={19} />} active={screen === 'files'} onClick={() => navigate('files')}>Материалы</NavItem>
        {user.is_admin && <NavItem icon={<Settings2 size={19} />} active={screen === 'settings'} onClick={() => navigate('settings')}>Настройки</NavItem>}
      </nav>
      <div className="mx-account"><Avatar name={user.first_name || 'Ученик'} size={34} />
        <div><strong>{user.first_name || 'Ученик'}</strong><span>{user.is_admin ? 'Администратор' : 'Личное пространство'}</span></div>
        {user.is_admin && <ShieldCheck size={17} aria-label="Администратор" />}
      </div>
    </aside>
    <div className="mx-workspace">
      <div className="mx-topbar">
        <span className="mx-breadcrumb">Личное пространство <ChevronRight size={14} /> <strong>{screen === 'files' ? 'Материалы' : 'Настройки'}</strong></span>
        <span className="mx-mobile-brand">МаксИИмка<span>.</span></span>
        {demo ? <Badge tone="warning">Демонстрация · пример данных</Badge> : <Badge tone="neutral">{user.local_mode ? 'Локальная админка' : 'MAX'}</Badge>}
      </div>
      <main>
        {screen === 'settings' && user.is_admin ? <Settings demo={demo} notify={notify} setDirty={setDirty} /> :
          <div className={'mx-library ' + (mobileDetail && active ? 'mx-library--detail' : '')}>
            <div className="mx-library-body">
              <PageHeading label="Всё, что нужно для подготовки" title={isOwn ? 'Мои материалы' : 'Материалы пользователя'}
                action={isOwn && <Button size="touch" variant="primary" icon={<Plus />} onClick={() => setModal('upload')}>Добавить материал</Button>}>
                {isOwn ? 'Твои конспекты, презентации и заметки — в одном месте.' : 'Просмотр и управление материалами выбранного пользователя.'}
              </PageHeading>
              {user.is_admin && owners.some(item => item.user_id !== user.id) && <Field label="Чьи материалы" className="mx-owner-field"
                value={String(owner || user.id)} select={{ options: [
                  { value: String(user.id), label: 'Мои материалы' },
                  ...owners.filter(item => item.user_id !== user.id).map(item => ({ value: String(item.user_id), label: (item.label || 'Пользователь ' + item.user_id) + ' · ' + item.files + ' мат.' })),
                ], onChange: value => { setOwner(Number(value)); setSelected(null); setFiles([]); setMobileDetail(false) } }} />}
              <div className="mx-library-tools">
                <Field value={search} className="mx-search" placeholder="Найти в материалах" leading={<Search size={18} aria-hidden="true" />}
                  input={{ 'aria-label': 'Поиск материалов', type: 'search', onChange: event => setSearch(event.target.value) }} />
                <Button size="touch" variant="ghost" icon={<RefreshCw />} aria-label="Обновить материалы" disabled={loading}
                  onClick={() => { demo ? notify('Это демонстрационный список материалов.') : setReload(value => value + 1) }} />
              </div>
              <div className="mx-file-filters" aria-label="Формат материалов">
                {[['all', 'Все материалы'], ['PDF', 'PDF'], ['PPTX', 'Презентации'], ['TXT', 'Тексты']].map(([value, label]) =>
                  <Button key={value} variant={type === value ? 'tertiary' : 'ghost'} aria-pressed={type === value} onClick={() => setType(value)}>
                    {label}{value === 'all' ? '  ' + files.length : ''}
                  </Button>)}
              </div>
              {error && <ErrorBox retry={() => setReload(value => value + 1)}>{error}</ErrorBox>}
              {!error && loading && !files.length ? <div className="mx-loading" role="status">Загружаю материалы…</div> :
                !error && visible.length ? <div className="mx-file-list" aria-label="Материалы">
                  <div className="mx-list-heading"><span>Название</span><span>Добавлено</span></div>
                  {visible.map(file => <Row key={file.id || file.title} className="mx-file-row" selected={selected === file.id && file.id !== null}
                    pad={12} role="button" tabIndex={file.status === 'ready' ? 0 : -1} aria-label={file.title}
                    aria-disabled={file.status === 'processing'} onClick={() => selectFile(file)}
                    onKeyDown={event => { if (['Enter', ' '].includes(event.key)) { event.preventDefault(); selectFile(file) } }}
                    lead={<FileIcon title={file.title} />} title={file.title}
                    subtitle={file.status === 'processing' ? 'Обрабатывается…' : formatLabel(file.title) + ' · ' + (file.pages ? file.pages + ' стр. · ' : '') + file.fragments + ' фрагм.'}>
                    <span className="mx-file-date">{dateLabel(file.created_at)}</span>
                    <ChevronRight className="mx-file-chevron" size={16} aria-hidden="true" />
                  </Row>)}
                </div> : !error && !loading && <Empty title={files.length ? 'Ничего не найдено' : 'Первый материал — начало подготовки'}>
                  {files.length ? 'Попробуй другое название или выбери все форматы.' : !isOwn ? 'У выбранного пользователя пока нет материалов.' : user.local_mode ? 'Добавь файл через /файл в терминале или отправь боту в MAX. Затем обнови список материалов.' : 'Отправь боту конспект, презентацию или фото страницы. После обработки материал появится здесь.'}
                </Empty>}
              <div className="mx-library-footer"><ShieldCheck size={16} /><span>{isOwn ? 'Эти материалы доступны только тебе и администратору.' : 'Просмотр материалов выбранного пользователя.'}</span></div>
              <Card tone="sunken" pad="large" className="mx-library-tip">
                <div><BookOpen size={20} /><strong>От материала — к пониманию</strong></div>
                <p>Выбери документ, чтобы получить конспект, проверить себя тестом или сделать карточки для повторения.</p>
              </Card>
            </div>
            <aside className="mx-inspector" aria-label="Просмотр материала">
              {active ? <>
                <div className="mx-inspector-bar"><Button size="touch" variant="ghost" className="mx-detail-back"
                  icon={<ArrowLeft />} onClick={() => setMobileDetail(false)}>Материалы</Button>
                  <span className="mx-inspector-caption">Выбранный материал</span>
                  <Button variant="ghost" size="touch" aria-label="Удалить материал" disabled={busy || active.status === 'processing'}
                    icon={<Trash2 />} onClick={() => { setActionError(''); setModal('delete') }} />
                </div>
                <div className="mx-material-title"><FileIcon title={active.title} large /><Badge tone="neutral">{formatLabel(active.title)}</Badge>
                  <h2>{active.title.replace(/\.[^.]+$/, '')}</h2>
                  <p>{active.pages ? active.pages + ' страниц · ' : ''}{active.fragments} фрагментов для поиска</p>
                </div>
                {isOwn && <div className="mx-study-actions">
                  <Button size="touch" variant="primary" disabled={busy || active.status !== 'ready'} icon={<Check />} onClick={() => study('quiz')}>Создать тест</Button>
                  <div className="mx-actions">
                    <Button size="touch" disabled={busy || active.status !== 'ready'} icon={<FileText />} onClick={() => study('summary')}>Конспект</Button>
                    <Button size="touch" disabled={busy || active.status !== 'ready'} icon={<Layers />} onClick={() => study('cards')}>Карточки</Button>
                  </div>
                  <p className="mx-hint">Готовые задания появятся в чате с ботом.</p>
                </div>}
                {actionError && modal !== 'delete' && <ErrorBox>{actionError}</ErrorBox>}
                <div className="mx-excerpt-heading"><h3>Извлечённый текст</h3><Badge tone="success">Обработан</Badge></div>
                {detailError ? <ErrorBox retry={() => setReload(value => value + 1)}>{detailError}</ErrorBox> :
                  !detail ? <p className="mx-hint" role="status">Загружаю текст…</p> : <>
                    <Card pad="large" className="mx-excerpt">
                      <div className="mx-page-number">{detail.chunks[0]?.page ? 'СТРАНИЦА ' + detail.chunks[0].page : 'ФРАГМЕНТ МАТЕРИАЛА'}</div>
                      <h3>{active.title.replace(/\.[^.]+$/, '')}</h3>
                      <p>{detail.chunks[0]?.text || 'Текст не найден в индексе.'}</p>
                    </Card>
                    <Button size="touch" variant="ghost" trailingIcon={<ArrowUpRight />} disabled={!detail.chunks.length} onClick={() => setModal('preview')}>Читать весь текст</Button>
                  </>}
              </> : <Empty title="Выбери материал">Здесь появится текст и действия для подготовки.</Empty>}
            </aside>
          </div>}
      </main>
    </div>
    {user.is_admin && <nav className="mx-bottom-nav" aria-label="Навигация на телефоне">
      <NavItem icon={<FolderOpen size={21} />} active={screen === 'files'} onClick={() => navigate('files')}>Материалы</NavItem>
      {user.is_admin && <NavItem icon={<Settings2 size={21} />} active={screen === 'settings'} onClick={() => navigate('settings')}>Настройки</NavItem>}
    </nav>}
    {modal === 'upload' && <Modal title="Добавить материал" close={() => setModal(null)}>
      {user.local_mode ? <>
        <p>В терминале бота введи <code>/файл test-material/kletka.txt</code> или укажи путь к своему файлу.</p>
        <p>После обработки обнови список. Если задан ID MAX, выбери «Консоль» в поле «Чьи материалы». Файлы из чата MAX тоже появятся здесь.</p>
      </> : <p>Закрой мини-приложение и прикрепи файл скрепкой в этом чате. После обработки материал появится в списке.</p>}
      <p className="mx-hint">PDF, PPTX до 15 слайдов, TXT, фотографии и ZIP с материалами.</p>
      <Button size="touch" variant="primary" onClick={() => setModal(null)}>Понятно</Button>
    </Modal>}
    {modal === 'delete' && active && <Modal title="Удалить материал?" close={() => setModal(null)} busy={busy}>
      <p className="mx-delete-name">{active.title}</p>
      <p>Также будут удалены связанные тесты, карточки и результаты. Восстановить их в приложении не получится.</p>
      {actionError && <ErrorBox>{actionError}</ErrorBox>}
      <div className="mx-modal-actions"><Button size="touch" disabled={busy} onClick={() => setModal(null)}>Отмена</Button>
        <Button size="touch" variant="danger" disabled={busy} onClick={remove}>{busy ? 'Удаляю…' : 'Удалить'}</Button></div>
    </Modal>}
    {modal === 'preview' && detail && <Modal title="Текст материала" close={() => setModal(null)}>
      <p className="mx-preview-title">{detail.title}</p>
      <div className="mx-full-text">{detail.chunks.map((chunk, index) => <section key={index}>
        <span>{chunk.page ? 'Страница ' + chunk.page : 'Фрагмент ' + (index + 1)}</span><p>{chunk.text}</p>
      </section>)}</div>
      {detail.truncated && <p className="mx-hint">Показаны первые 200 фрагментов. Остальной текст доступен боту при поиске.</p>}
    </Modal>}
    {modal === 'leave' && <Modal title="Есть несохранённые изменения" close={() => setModal(null)}>
      <p>Остаться в настройках или выйти без сохранения?</p>
      <div className="mx-modal-actions"><Button size="touch" onClick={() => setModal(null)}>Остаться</Button>
        <Button size="touch" variant="primary" onClick={() => { setDirty(false); setScreen(destination); setModal(null) }}>Выйти без сохранения</Button></div>
    </Modal>}
  </>
  return <LookContext.Provider value={{ theme: 'light', palette: 'accent', fidelity: 'realistic', layout: 'wide' }}>
    <div className={'mx-app' + (user?.is_admin ? ' mx-app--admin' : '')}>{content}<div className="mx-toast-host"><Toast show={!!toast} icon={<Check size={16} />}>{toast}</Toast></div></div>
  </LookContext.Provider>
}
