import { ImageSquare, MagnifyingGlass, ShieldCheck } from '@phosphor-icons/react'
import { useState, type ChangeEvent } from 'react'
import { useAnimatedClose } from '../hooks/useAnimatedClose'
import { api } from '../lib/api'
import type { ImageSearchMatch } from '../types'
import { CoverImage } from './CoverImage'

interface Props {
  onClose: () => void
  onOpenWork: (groupId: number) => void
}

const confidenceLabels: Record<ImageSearchMatch['confidence'], string> = {
  exact: '高度一致',
  strong: '强匹配',
  possible: '可能相似',
}

export function ImageSearch({ onClose, onOpenWork }: Props) {
  const { closing, requestClose } = useAnimatedClose(onClose)
  const [results, setResults] = useState<ImageSearchMatch[]>([])
  const [searching, setSearching] = useState(false)
  const [searched, setSearched] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function selectImage(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0]
    if (!file) return
    setSearching(true)
    setError(null)
    try {
      setResults(await api.imageSearch(file))
      setSearched(true)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '图片搜索失败')
    } finally {
      setSearching(false)
      event.target.value = ''
    }
  }

  return (
    <div className={`modal-backdrop work-detail-backdrop${closing ? ' is-closing' : ''}`} role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) requestClose() }}>
      <section className="work-detail image-search-dialog" role="dialog" aria-modal="true" aria-label="以图搜库">
        <button className="modal-close" onClick={requestClose} aria-label="关闭">×</button>
        <header className="image-search-header">
          <span><ImageSquare size={34} /></span>
          <div><p className="eyebrow">LOCAL VISUAL SEARCH</p><h2>以图搜库</h2><p>上传封面或截图，搜索本地资料库中的相似作品与版本。</p></div>
        </header>
        <label className="image-search-upload">
          <MagnifyingGlass size={22} />
          <span>{searching ? '正在计算视觉指纹…' : '选择封面或截图'}</span>
          <input type="file" accept="image/*" disabled={searching} onChange={(event) => void selectImage(event)} />
        </label>
        <p className="image-search-privacy"><ShieldCheck size={17} />图片只在本机内存中计算 dHash、pHash 和颜色指纹，不保存，也不会发送给第三方。</p>
        {error && <div className="alert" role="alert"><span>{error}</span></div>}
        <div className="image-search-results">
          {results.map((item) => <button key={item.group_id} onClick={() => onOpenWork(item.group_id)}>
            <CoverImage src={item.cover_url} alt="" fallback={<span>冊</span>} />
            <span><strong>{item.group_title}</strong><small>{item.edition_title}</small><em>{confidenceLabels[item.confidence]} · 距离 {item.distance} · {item.mode === 'crop' ? '裁切匹配' : '全图匹配'}</em><small>{item.evidence.join(' · ')}</small></span>
          </button>)}
          {searched && !results.length && <div className="dashboard-empty compact"><ImageSquare size={28} /><p>本地资料库中没有达到保守阈值的相似封面。</p></div>}
        </div>
      </section>
    </div>
  )
}
