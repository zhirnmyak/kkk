import { demoSettings } from '../demo'
import type { Snapshot, Values } from '../api'

export type Capabilities = {
  vision?: { supported: boolean | null; source?: string } | null
  reasoning?: { supported: boolean | null; source?: string } | null
  efforts: string[]
}
export type LaunchOptions = { panel: boolean; mode: 'terminal' | 'background' }
export type Runtime = { status: 'stopped' | 'starting' | 'running' | 'stopping'; message?: string; mode?: string; panel?: boolean; pid?: number }
export type SetupState = { setup_complete: boolean; settings: Snapshot; bot: { enabled: boolean; admin_id: string; token_set: boolean }; launch: LaunchOptions; runtime: Runtime }
export type CheckResponse = { models: string[]; message: string; capabilities: Capabilities }

declare global {
  interface Window {
    pywebview?: { api: Record<string, (...args: unknown[]) => Promise<{ ok: boolean; data?: unknown; error?: string }>> }
  }
}

const demo = new URLSearchParams(location.search).get('demo') === '1'
let demoState: SetupState = {
  setup_complete: new URLSearchParams(location.search).get('configured') === '1',
  settings: structuredClone(demoSettings),
  bot: { enabled: false, admin_id: '', token_set: false },
  launch: { panel: true, mode: 'terminal' },
  runtime: { status: 'stopped' },
}

export async function desktop<T>(method: string, ...args: unknown[]): Promise<T> {
  if (demo) {
    await new Promise(resolve => setTimeout(resolve, 160))
    let data: unknown
    if (method === 'load') data = structuredClone(demoState)
    if (method === 'check') data = { models: ['gpt-4o-mini', 'gpt-4.1-mini'], message: 'Демонстрация: возможности модели проверены.',
      capabilities: { vision: { supported: true, source: 'metadata' }, reasoning: { supported: true, source: 'metadata' }, efforts: ['auto', 'low', 'medium', 'high'] } }
    if (method === 'save') {
      const payload = args[0] as { patch: Partial<Values>; bot: SetupState['bot']; launch: LaunchOptions }
      demoState = { ...demoState, setup_complete: true, settings: { ...demoState.settings, values: { ...demoState.settings.values, ...payload.patch } },
        bot: payload.bot, launch: payload.launch }
      data = structuredClone(demoState)
    }
    if (method === 'start') {
      demoState.launch = args[0] as LaunchOptions
      demoState.runtime = { status: 'running', ...demoState.launch, message: 'Демонстрация запуска' }
      data = demoState.runtime
    }
    if (method === 'stop') { demoState.runtime = { status: 'stopped' }; data = demoState.runtime }
    if (method === 'status') data = demoState.runtime
    return data as T
  }
  if (!window.pywebview?.api) {
    await new Promise<void>((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('Открой МаксИИмку через start.cmd — этот экран работает в приложении Python.')), 8000)
      window.addEventListener('pywebviewready', () => { clearTimeout(timer); resolve() }, { once: true })
    })
  }
  const response = await window.pywebview!.api[method](...args)
  if (!response.ok) throw new Error(response.error || 'Не удалось выполнить действие.')
  return response.data as T
}
