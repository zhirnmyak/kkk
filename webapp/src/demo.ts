import type { Detail, Material, Snapshot, User } from './api'

// Только для явно обозначенного ?demo=1. Эти данные не отправляются в API.
export const demoUser: User = {
  id: 1001, first_name: 'Александр', last_name: '', is_admin: true, bot_username: 'ideya_fiks_117_bot',
}
export const demoFiles: Material[] = [
  { id: 1, title: 'Клетка и её органоиды.pdf', created_at: '2026-11-08 09:42:00', fragments: 24, pages: 12, status: 'ready' },
  { id: 2, title: 'Митоз и мейоз.pptx', created_at: '2026-11-07 14:20:00', fragments: 18, pages: 9, status: 'ready' },
  { id: 3, title: 'Законы Ньютона.pdf', created_at: '2026-11-07 11:08:00', fragments: 16, pages: 8, status: 'ready' },
  { id: 4, title: 'История. Эпоха Просвещения.txt', created_at: '2026-11-06 17:31:00', fragments: 11, pages: 0, status: 'ready' },
  { id: 5, title: 'Заметки по химии.jpg', created_at: '2026-11-06 08:15:00', fragments: 6, pages: 1, status: 'ready' },
  { id: 6, title: 'Основы генетики.pdf', created_at: '2026-11-05 12:00:00', fragments: 20, pages: 10, status: 'ready' },
]
export function demoDetail(file: Material): Detail {
  return { id: file.id!, title: file.title, truncated: false, chunks: [
    { page: 1, text: file.id === 1 ? 'Клетка — структурная и функциональная единица живого организма. Клетки получают вещества из окружающей среды, преобразуют энергию и передают наследственную информацию.' : 'Пример извлечённого текста учебного материала. В рабочем приложении здесь отображаются фрагменты вашего документа с номерами страниц.' },
    { page: 2, text: 'Ядро хранит наследственную информацию в молекулах ДНК и участвует в регуляции процессов клетки. Ядерная оболочка отделяет содержимое ядра от цитоплазмы.' },
    { page: 3, text: 'Митохондрии участвуют в клеточном дыхании и синтезе АТФ. Складки внутренней мембраны — кристы — увеличивают площадь, на которой происходят эти процессы.' },
  ] }
}
export const demoSettings: Snapshot = {
  revision: 'demo-1', api_key_set: true,
  values: { provider: 'openai', base_url: 'https://api.openai.com/v1', model: 'gpt-4o-mini',
    effort: 'auto', context: 0, gw_parallel: 3, gw_timeout: 180, gw_window: 'hidden',
    ocr_backend: 'easyocr', ocr_workers: 3, ocr: true, display_mode: 'statuses', spinner: 'auto' },
  profiles: {
    openai: { base_url: 'https://api.openai.com/v1', model: 'gpt-4o-mini', api_key_set: true },
    qoder: { base_url: 'http://127.0.0.1:8787/v1', model: 'Qwen3.8-Flash', api_key_set: true },
  },
  choices: {
    provider: ['openai', 'qoder'], effort: ['auto', 'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultracode'],
    context: [0, 100000, 200000, 400000, 1000000], gw_parallel: [1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
    gw_timeout: [60, 120, 180, 300, 600], gw_window: ['hidden', 'window'],
    ocr_backend: ['easyocr', 'llm'], ocr_workers: [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 0],
    display_mode: ['statuses', 'reasoning'], spinner: ['auto', 'on', 'off'],
  },
  capabilities: { vision: null, efforts: ['auto', 'low', 'medium', 'high'] },
}
