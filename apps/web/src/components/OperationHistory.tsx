import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../lib/api'
import type { CatalogOperation } from '../types'
import { useAnimatedClose } from '../hooks/useAnimatedClose'

export function OperationHistory({ onClose, onChanged }: { onClose: () => void, onChanged: () => Promise<void> }) {
  const [items, setItems] = useState<CatalogOperation[]>([])
  const [busy, setBusy] = useState(false)
  const [loading, setLoading] = useState(true)
  const [more, setMore] = useState(false)
  const [error, setError] = useState('')
  const closeRef = useRef<HTMLButtonElement>(null)
  const { closing, requestClose } = useAnimatedClose(onClose)

  useEffect(() => {
    let active = true
    void api.catalogOperations().then((rows) => {
      if (active) { setItems(rows); setMore(rows.length === 50) }
    }).catch((reason) => {
      if (active) setError(reason instanceof Error ? reason.message : '无法读取整理记录')
    }).finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [])

  useEffect(() => {
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null
    closeRef.current?.focus()
    return () => previous?.focus()
  }, [])
  useEffect(() => {
    const handle = (event: KeyboardEvent) => { if (event.key === 'Escape') requestClose() }
    document.addEventListener('keydown', handle)
    return () => document.removeEventListener('keydown', handle)
  }, [requestClose])

  const loadMore = useCallback(async () => {
    if (!items.length) return
    setBusy(true); setError('')
    try {
      const rows = await api.catalogOperations(items[items.length - 1].id)
      setItems((current) => [...current, ...rows]); setMore(rows.length === 50)
    } catch (reason) { setError(reason instanceof Error ? reason.message : '加载失败') }
    finally { setBusy(false) }
  }, [items])

  async function undo(item: CatalogOperation) {
    if (!window.confirm('确认撤销这次整理？如果相关数据后来发生变化，系统会拒绝撤销。')) return
    setBusy(true); setError('')
    try {
      const updated = await api.undoCatalogOperation(item.id)
      setItems((current) => current.map((row) => row.id === updated.id ? updated : row))
      await onChanged()
    } catch (reason) { setError(reason instanceof Error ? reason.message : '撤销失败') }
    finally { setBusy(false) }
  }

  return <div className={`modal-backdrop${closing ? ' is-closing' : ''}`} role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) requestClose() }}>
    <section className="merge-review operation-history" role="dialog" aria-modal="true" aria-label="整理记录">
      <button ref={closeRef} className="modal-close" onClick={requestClose} aria-label="关闭">×</button>
      <p className="eyebrow">CATALOG / HISTORY</p><h2>整理记录</h2>
      <p>记录升级后的合并、拆分和排除。相关数据发生变化时，撤销会被阻止。</p>
      {error && <div className="settings-error" role="alert">{error}</div>}
      {loading ? <p>正在读取记录…</p> : items.length ? <div className="operation-list">
        {items.map((item) => <article key={item.id}>
          <div><strong>{item.summary}</strong><small>{new Date(item.created_at).toLocaleString('zh-CN')} · 整理前 {item.before_group_ids.length} 部 → 整理后 {item.after_group_ids.length} 部{item.undone_at ? ' · 已撤销' : ''}</small>{item.changes_summary?.length > 0 && <ul>{item.changes_summary.map((change, index) => <li key={index}>{change}</li>)}</ul>}</div>
          {!item.undone_at && <button disabled={busy} onClick={() => void undo(item)}>撤销</button>}
        </article>)}
        {more && <button disabled={busy} onClick={() => void loadMore()}>加载更多</button>}
      </div> : <p>目前还没有整理记录。</p>}
    </section>
  </div>
}
