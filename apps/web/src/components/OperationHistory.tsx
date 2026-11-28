import { useCallback, useEffect, useState } from 'react'
import { api } from '../lib/api'
import type { CatalogOperation } from '../types'

export function OperationHistory({ onChanged }: { onChanged: () => Promise<void> }) {
  const [items, setItems] = useState<CatalogOperation[]>([])
  const [busy, setBusy] = useState(false)
  const [loading, setLoading] = useState(true)
  const [more, setMore] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    let active = true
    void api.catalogOperations().then((rows) => {
      if (active) { setItems(rows); setMore(rows.length === 50) }
    }).catch((reason) => {
      if (active) setError(reason instanceof Error ? reason.message : '无法读取整理记录')
    }).finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [])

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

  return (
    <section className="operation-history-panel" aria-label="整理记录">
      <header className="workspace-subhead">
        <div>
          <h3>整理记录与撤销</h3>
          <p>记录合并、拆分和排除操作；若后续数据未变化，可直接一键撤销。</p>
        </div>
      </header>
      {error && <div className="settings-error" role="alert">{error}</div>}
      {loading ? <p className="workspace-loading-note">正在读取记录…</p> : items.length ? <div className="operation-list">
        {items.map((item) => <article key={item.id}>
          <div><strong>{item.summary}</strong><small>{new Date(item.created_at).toLocaleString('zh-CN')} · 整理前 {item.before_group_ids.length} 部 → 整理后 {item.after_group_ids.length} 部{item.undone_at ? ' · 已撤销' : ''}</small>{item.changes_summary?.length > 0 && <ul>{item.changes_summary.map((change, index) => <li key={index}>{change}</li>)}</ul>}</div>
          {!item.undone_at && <button disabled={busy} onClick={() => void undo(item)}>撤销</button>}
        </article>)}
        {more && <button className="workspace-load-more" disabled={busy} onClick={() => void loadMore()}>加载更多</button>}
      </div> : <div className="dashboard-empty compact"><p>目前还没有整理记录。</p></div>}
    </section>
  )
}
