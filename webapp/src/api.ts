export type User = { id: number; first_name: string; last_name: string; is_admin: boolean; bot_username: string; local_mode?: boolean }
export type Material = { id: number | null; title: string; created_at: string | null; fragments: number; pages: number; status: 'ready' | 'processing' }
export type Detail = { id: number; title: string; chunks: { text: string; page?: number | null }[]; truncated: boolean }
export type Values = {
  provider: string; base_url: string; model: string; effort: string; context: number;
  gw_parallel: number; gw_timeout: number; gw_window: string; ocr_backend: string;
  ocr_workers: number; ocr: boolean; display_mode: string; spinner: string;
}
export type Snapshot = {
  values: Values; revision: string; api_key_set: boolean;
  profiles: Record<string, { base_url: string; model: string; api_key_set: boolean }>;
  choices: Record<string, (string | number)[]>;
  capabilities: { vision: unknown; efforts: string[] };
  message?: string;
}
export type CheckResult = { models: string[]; message: string; ok: boolean; revision: string; settings: Snapshot }
declare global {
  interface Window {
    WebApp?: {
      initData: string;
      openMaxLink?: (url: string) => void;
      close?: () => void;
      enableClosingConfirmation?: () => void;
      disableClosingConfirmation?: () => void;
    }
  }
}

export class ApiError extends Error {
  constructor(message: string, public status: number) { super(message) }
}

export const localHost = ['localhost', '127.0.0.1', '[::1]'].includes(location.hostname)
const localParams = new URLSearchParams(location.hash.slice(1))
const localKey = 'maximka-local-access'
let localToken = ''
if (localHost) {
  try {
    if (localParams.getAll('LocalToken').length === 1) {
      localToken = localParams.get('LocalToken') || ''
      sessionStorage.setItem(localKey, localToken)
      history.replaceState(null, '', location.pathname + location.search)
    } else {
      localToken = sessionStorage.getItem(localKey) || ''
    }
  } catch { /* При запрете storage ссылка действует до обновления вкладки. */ }
}

export function getInitData() {
  const params = new URLSearchParams(window.location.hash.slice(1))
  const keys = [...params.keys()]
  if (new Set(keys).size !== keys.length) throw new ApiError('Данные запуска повреждены. Откройте приложение из MAX заново.', 401)
  return window.WebApp?.initData || params.get('WebAppData') || ''
}

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch('/api' + path, {
    ...options, credentials: 'omit', cache: 'no-store',
    headers: { 'Content-Type': 'application/json', Authorization: localToken ? 'Local ' + localToken : 'MAX ' + getInitData(), ...options.headers },
  })
  const data = await response.json().catch(() => ({}))
  if (!response.ok) {
    if (response.status === 401 && localToken) {
      localToken = ''
      try { sessionStorage.removeItem(localKey) } catch {}
    }
    throw new ApiError(typeof data.detail === 'string' ? data.detail : 'Не удалось выполнить запрос. Попробуйте ещё раз.', response.status)
  }
  return data as T
}

export function openInMax(username = '') {
  const bot = /^[\w-]+$/.test(username) ? username : 'ideya_fiks_117_bot'
  const url = 'https://max.ru/' + bot + '?startapp'
  if (window.WebApp?.openMaxLink) window.WebApp.openMaxLink(url)
  else window.open(url, '_blank', 'noopener,noreferrer')
}

export function messageOf(error: unknown) {
  return error instanceof Error ? error.message : 'Не удалось выполнить действие.'
}
