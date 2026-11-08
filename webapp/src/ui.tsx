import { useEffect, useId, useRef, type ReactNode } from 'react'
import { AlertCircle, FileText, Image, Presentation, X } from 'lucide-react'
import { Button, Card, Row } from './stageshow/kit'
export { Avatar, Badge, Button, Card, Field, Row, Tabs, Toggle, Toast } from './stageshow/kit'

export function FileIcon({ title, large = false }: { title: string; large?: boolean }) {
  const ext = title.split('.').at(-1)?.toLowerCase() || 'txt'
  const picture = ['jpg', 'jpeg', 'png', 'webp'].includes(ext)
  const Icon = ext === 'pptx' ? Presentation : picture ? Image : FileText
  return <span className={'mx-file-icon ' + (large ? 'mx-file-icon--large' : '')} data-format={ext}>
    <Icon size={large ? 28 : 20} strokeWidth={1.6} aria-hidden="true" />
  </span>
}

export function ErrorBox({ children, retry }: { children: ReactNode; retry?: () => void }) {
  return <div className="mx-error" role="alert"><AlertCircle size={18} aria-hidden="true" />
    <span>{children}</span>{retry && <Button onClick={retry}>Повторить</Button>}
  </div>
}

export function Empty({ title, children, action }: { title: string; children: ReactNode; action?: ReactNode }) {
  return <div className="mx-empty"><FileText size={36} strokeWidth={1.3} aria-hidden="true" />
    <h2>{title}</h2><p>{children}</p>{action}
  </div>
}

export function Modal({ title, children, close, busy = false }: { title: string; children: ReactNode; close: () => void; busy?: boolean }) {
  const ref = useRef<HTMLDialogElement>(null)
  const id = useId()
  useEffect(() => {
    const previous = document.activeElement as HTMLElement
    ref.current?.showModal()
    return () => { ref.current?.close(); previous?.focus() }
  }, [])
  return <dialog ref={ref} className="mx-modal" aria-labelledby={id} onCancel={event => {
    event.preventDefault(); if (!busy) close()
  }}>
    <Card pad="large" gap={20}>
      <div className="mx-modal-heading"><h2 id={id}>{title}</h2>
        <Button aria-label="Закрыть окно" variant="ghost" onClick={close} disabled={busy} icon={<X />} />
      </div>
      {children}
    </Card>
  </dialog>
}

export function NavItem({ icon, children, active, onClick }: { icon: ReactNode; children: ReactNode; active?: boolean; onClick: () => void }) {
  return <Row className="mx-nav-item" lead={icon} title={children} selected={active} role="button" tabIndex={0}
    aria-current={active ? 'page' : undefined} onClick={onClick}
    onKeyDown={event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); onClick() } }} />
}

export function PageHeading({ label, title, children, action }: { label: string; title: string; children: ReactNode; action?: ReactNode }) {
  return <header className="mx-page-heading">
    <div><div className="mx-eyebrow">{label}</div><h1>{title}</h1><p>{children}</p></div>{action}
  </header>
}
