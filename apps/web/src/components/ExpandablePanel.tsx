import type { ReactNode } from 'react'

export function ExpandablePanel({ open, className = '', children }: {
  open: boolean
  className?: string
  children: ReactNode
}) {
  return (
    <div
      className={`expandable-panel${open ? ' is-open' : ''}${className ? ` ${className}` : ''}`}
      aria-hidden={!open}
      inert={!open}
    >
      <div className="expandable-panel-inner">{children}</div>
    </div>
  )
}
