import { useEffect, useMemo, useState } from 'react'
import { api } from '../lib/api'
import type { ActivityItem, Author, AuthorDigest, ReleaseSignal, SocialAccount, SocialAccountSuggestion, SocialPost, SocialStatus, WorkGroup } from '../types'

const kindLabels: Record<string, string> = {
  new_release: '新作预告', release_preview: '样张公开', cover_reveal: '封面公开',
  event_participation: '参展动态', preorder: '预售', on_sale: '正式发售',
  reprint: '再版 / 旧刊', delay: '延期', cancellation: '取消', other: '其他',
}

const activityLabels: Record<string, string> = {
  creation_progress: '创作进度', release: '作品动态', event: '展会活动', sales: '销售通贩',
  artwork: '插画公开', collaboration: '合作项目', schedule_notice: '重要公告',
  personal: '日常近况', other: '其他动态',
}

interface Props {
  status: SocialStatus
  signals: ReleaseSignal[]
  authors: Author[]
  works: WorkGroup[]
  selectedAuthorId: number | null
  busy: boolean
  focusSignalId: number | null
  onClose: () => void
  onChanged: () => Promise<void>
  onOpenWork: (id: number) => void
}

export function SocialRadar({ status, signals, authors, works, selectedAuthorId, busy, focusSignalId, onClose, onChanged, onOpenWork }: Props) {
  const [filter, setFilter] = useState('all')
  const [view, setView] = useState<'overview' | 'releases' | 'raw'>('overview')
  const [accounts, setAccounts] = useState<SocialAccount[]>([])
  const [suggestions, setSuggestions] = useState<SocialAccountSuggestion[]>([])
  const [activities, setActivities] = useState<ActivityItem[]>([])
  const [digest, setDigest] = useState<AuthorDigest | null>(null)
  const [rawPosts, setRawPosts] = useState<SocialPost[]>([])
  const [postType, setPostType] = useState('')
  const [handle, setHandle] = useState('')
  const [finding, setFinding] = useState(false)
  const [localError, setLocalError] = useState<string | null>(null)
  const [links, setLinks] = useState<Record<number, number>>({})

  async function loadAccounts() {
    if (!selectedAuthorId) { setAccounts([]); return }
    setAccounts(await api.socialAccounts(selectedAuthorId))
  }

  async function loadActivityData(nextPostType = postType) {
    if (!selectedAuthorId) { setActivities([]); setDigest(null); setRawPosts([]); return }
    const [nextActivities, nextDigest, nextPosts] = await Promise.all([
      api.socialActivity(selectedAuthorId), api.socialDigest(selectedAuthorId),
      api.socialPosts(selectedAuthorId, nextPostType || undefined),
    ])
    setActivities(nextActivities); setDigest(nextDigest); setRawPosts(nextPosts)
  }

  useEffect(() => { void Promise.all([loadAccounts(), loadActivityData('')]).catch((error) => setLocalError(String(error))) }, [selectedAuthorId])
  useEffect(() => {
    if (!focusSignalId) return
    window.setTimeout(() => document.getElementById(`signal-${focusSignalId}`)?.scrollIntoView({ block: 'center' }), 50)
  }, [focusSignalId])

  const visible = useMemo(() => signals.filter((signal) => {
    if (selectedAuthorId && signal.author_id !== selectedAuthorId) return false
    return filter === 'all' || signal.status === filter || signal.kind === filter
  }), [filter, selectedAuthorId, signals])

  async function act(action: () => Promise<unknown>) {
    setLocalError(null)
    try { await action(); await Promise.all([loadAccounts(), loadActivityData(), onChanged()]) }
    catch (error) { setLocalError(error instanceof Error ? error.message : '操作失败') }
  }

  async function findAccounts() {
    if (!selectedAuthorId) return
    setFinding(true); setLocalError(null)
    try { setSuggestions(await api.socialAccountSuggestions(selectedAuthorId)) }
    catch (error) { setLocalError(error instanceof Error ? error.message : '查找账号失败') }
    finally { setFinding(false) }
  }

  async function bindAccount(nextHandle: string) {
    if (!selectedAuthorId || !nextHandle.trim()) return
    await act(() => api.addSocialAccount(selectedAuthorId, nextHandle.trim()))
    setHandle(''); setSuggestions([])
  }

  return (
    <div className="modal-backdrop social-radar-backdrop" role="presentation" onMouseDown={(event) => { if (event.currentTarget === event.target) onClose() }}>
      <section className="social-radar" role="dialog" aria-modal="true" aria-label="作者动态雷达">
        <button className="modal-close" onClick={onClose} aria-label="关闭">×</button>
        <header className="radar-header">
          <div><p className="eyebrow">AUTHOR / ACTIVITY CENTER</p><h2>作者动态中心</h2><p>汇总作者最近近况；新作与重要变更仍走独立高精度提醒。</p></div>
          <div className="radar-health">
            <span className={status.enabled ? 'ok' : 'off'}>{status.enabled ? '雷达已启用' : '雷达未启用'}</span>
            <small>Agent {status.agent_configured ? '已连接' : '规则模式'} · QQ {status.qq_configured ? '已连接' : '未配置'}</small>
          </div>
        </header>

        {!status.enabled && <div className="radar-warning">请在 Dockge 中设置 MANGAFINDER_SOCIAL_ENABLED=true，并启用 social Compose profile。</div>}
        {localError && <div className="alert">{localError}</div>}

        <section className="account-panel">
          <div className="account-heading">
            <div><strong>{selectedAuthorId ? `${authors.find((item) => item.id === selectedAuthorId)?.name} 的 X 账号` : '先在左侧选择作者'}</strong><small>候选账号只有经过你的确认才会监控</small></div>
            {selectedAuthorId && <button onClick={() => void findAccounts()} disabled={!status.enabled || finding || busy}>{finding ? '查找中…' : '自动查找候选'}</button>}
          </div>
          {selectedAuthorId && <>
            <div className="bound-accounts">
              {accounts.map((account) => <article key={account.id}>
                {account.avatar_url ? <img src={account.avatar_url} alt="" /> : <span>@</span>}
                <div><a href={account.profile_url ?? `https://x.com/${account.handle}`} target="_blank" rel="noreferrer">@{account.handle}</a><small>{account.status === 'confirmed' ? `已确认 · ${account.last_synced_at ? `上次同步 ${new Date(account.last_synced_at).toLocaleString()}` : '等待首次同步'}` : '待确认'}{account.sync_error ? ` · ${account.sync_error}` : ''}</small></div>
                <button onClick={() => void act(() => api.deleteSocialAccount(selectedAuthorId, account.id))}>移除</button>
              </article>)}
              {!accounts.length && <small>还没有绑定账号。</small>}
            </div>
            {suggestions.length > 0 && <div className="account-suggestions">{suggestions.map((item) => <button key={item.handle} onClick={() => void bindAccount(item.handle)}><b>@{item.handle}</b><span>{item.display_name} · {Math.round(item.score * 100)}%</span><em>确认绑定</em></button>)}</div>}
            <div className="manual-account"><input value={handle} onChange={(event) => setHandle(event.target.value)} placeholder="手动输入 @handle" disabled={!status.enabled} /><button disabled={!status.enabled || !handle.trim() || busy} onClick={() => void bindAccount(handle)}>确认并监控</button><button disabled={!status.enabled || !accounts.some((item) => item.status === 'confirmed') || busy} onClick={() => void act(() => api.syncSocial(selectedAuthorId))}>立即扫描</button></div>
          </>}
        </section>

        <nav className="activity-tabs" aria-label="动态中心视图">
          <button className={view === 'overview' ? 'active' : ''} onClick={() => setView('overview')}>最近近况</button>
          <button className={view === 'releases' ? 'active' : ''} onClick={() => setView('releases')}>作品情报</button>
          <button className={view === 'raw' ? 'active' : ''} onClick={() => setView('raw')}>原始动态</button>
        </nav>

        {view === 'overview' && <>
          <section className="digest-card">
            <div className="digest-heading"><div><span>ROLLING 7 DAYS</span><h3>{digest ? `${digest.author_name} 最近在做什么` : '最近 7 天摘要'}</h3></div>{selectedAuthorId && <button onClick={() => void act(async () => { setDigest(await api.refreshSocialDigest(selectedAuthorId)) })}>重新总结</button>}</div>
            {digest ? <><p className="digest-summary">{digest.summary}</p><div className="digest-highlights">{digest.highlights.map((item, index) => <article className={`importance-${item.importance}`} key={`${item.text}-${index}`}><span>{activityLabels[item.category] ?? item.category} · {item.factuality === 'fact' ? '明确事实' : item.factuality === 'plan' ? '作者计划' : '推测'}</span><p>{item.text}</p><small>证据 {item.post_ids.map((id, evidenceIndex) => { const post = rawPosts.find((candidate) => candidate.id === id); return post ? <span key={id}>{evidenceIndex > 0 ? '、' : ''}<a href={post.url} target="_blank" rel="noreferrer">帖子 {id} ↗</a></span> : <span key={id}>{evidenceIndex > 0 ? '、' : ''}帖子 {id}</span> })}</small></article>)}</div>{digest.uncertainties.length > 0 && <div className="digest-uncertain"><strong>仍不确定</strong><ul>{digest.uncertainties.map((item) => <li key={item}>{item}</li>)}</ul></div>}<small className="digest-source">{digest.generated_by === 'agent' ? `Agent 总结 · ${digest.model ?? ''}` : '规则降级摘要'}{digest.error ? ` · Agent 暂不可用：${digest.error}` : ''}</small></> : <p className="review-empty">{selectedAuthorId ? '扫描到作者动态后，这里会生成最近 7 天摘要。' : '先在左侧选择作者。'}</p>}
          </section>
          <section className="activity-list">
            {activities.map((item) => { const post = item.posts.at(-1); const image = post?.media.find((media) => media.type === 'image')?.url; return <article className={`${item.is_read ? '' : 'unread'} importance-${item.importance}`} key={item.id}><div className="activity-thumb">{image ? <img src={image} alt="" /> : <span>动</span>}</div><div><div className="activity-meta"><b>{activityLabels[item.category] ?? item.category}</b><span>{item.author_name}</span><time>{new Date(item.ended_at).toLocaleString()}</time></div><h3>{item.headline}</h3><p>{item.summary}</p><div className="activity-actions"><span>{item.importance === 'critical' ? '紧急' : item.importance === 'high' ? '重要' : item.importance === 'normal' ? '一般' : '低优先级'} · 置信度 {Math.round(item.confidence * 100)}%</span>{post && <a href={post.url} target="_blank" rel="noreferrer" onClick={() => void api.markActivityRead(item.id)}>查看原帖 ↗</a>}</div></div></article> })}
            {!activities.length && <div className="review-empty">还没有可展示的作者近况。</div>}
          </section>
        </>}

        {view === 'releases' && <><nav className="radar-filters" aria-label="作品情报筛选">
          {[['all', '全部'], ['pending', '待确认'], ['confirmed', '已确认'], ['new_release', '新作'], ['event_participation', '参展'], ['rejected', '已排除']].map(([value, label]) => <button className={filter === value ? 'active' : ''} onClick={() => setFilter(value)} key={value}>{label}</button>)}
        </nav><section className="signal-list">
          {visible.map((signal) => {
            const post = signal.posts.at(-1)
            const image = post?.media.find((item) => item.type === 'image' && item.url)?.url
            return <article className={`${signal.is_read ? '' : 'unread'} ${focusSignalId === signal.id ? 'focused' : ''}`} id={`signal-${signal.id}`} key={signal.id}>
              <div className="signal-media">{image ? <img src={image} alt="帖子媒体" /> : <span>情</span>}</div>
              <div className="signal-copy">
                <div className="signal-meta"><span className={`signal-kind kind-${signal.kind}`}>{kindLabels[signal.kind] ?? signal.kind}</span><b>{signal.author_name}</b><time>{new Date(post?.posted_at ?? signal.created_at).toLocaleString()}</time></div>
                <h3>{signal.title ?? '标题尚未确认'}</h3>
                <p className="signal-post-text">{post?.text || post?.ocr_text || '帖子没有可读取的文字'}</p>
                <div className="signal-facts"><span>置信度 {Math.round(signal.confidence * 100)}%</span>{signal.event_code && <span>{signal.event_code}</span>}{signal.booth && <span>摊位 {signal.booth}</span>}<span>状态 {signal.status}</span></div>
                <div className="signal-evidence"><strong>支持证据</strong>{signal.evidence.length ? <ul>{signal.evidence.map((item) => <li key={item}>{item}</li>)}</ul> : <p>没有足够证据</p>}{signal.counter_evidence.length > 0 && <><strong className="counter">反证 / 风险</strong><ul>{signal.counter_evidence.map((item) => <li key={item}>{item}</li>)}</ul></>}</div>
                <div className="signal-actions">
                  {post && <a href={post.url} target="_blank" rel="noreferrer" onClick={() => void api.markSocialSignalRead(signal.id)}>查看原帖 ↗</a>}
                  {signal.linked_group_id ? <button onClick={() => onOpenWork(signal.linked_group_id!)}>打开关联作品</button> : <><select value={links[signal.id] ?? ''} onChange={(event) => setLinks({ ...links, [signal.id]: Number(event.target.value) })}><option value="">关联现有作品…</option>{works.map((work) => <option value={work.id} key={work.id}>{work.title}</option>)}</select><button disabled={!links[signal.id]} onClick={() => void act(() => api.linkSocialSignal(signal.id, links[signal.id]))}>关联</button></>}
                  {signal.status === 'pending' && <><button className="reject" onClick={() => void act(() => api.reviewSocialSignal(signal.id, 'reject'))}>不是新情报</button><button className="confirm" onClick={() => void act(() => api.reviewSocialSignal(signal.id, 'confirm'))}>确认情报</button></>}
                </div>
              </div>
            </article>
          })}
          {!visible.length && <div className="review-empty">当前筛选下没有作者动态。</div>}
        </section></>}

        {view === 'raw' && <section className="raw-posts"><div className="raw-post-filter"><strong>原始事实流</strong><select value={postType} onChange={(event) => { const value = event.target.value; setPostType(value); void loadActivityData(value) }}><option value="">全部类型</option><option value="original">原创</option><option value="quote">引用</option><option value="reply">回复</option><option value="retweet">转推</option></select></div>{rawPosts.map((post) => <article key={post.id}><div><b>{post.post_type === 'original' ? '原创' : post.post_type === 'quote' ? '引用' : post.post_type === 'reply' ? '回复' : '转推'}</b><time>{new Date(post.posted_at).toLocaleString()}</time></div><p>{post.text || post.ocr_text || '无文字内容'}</p>{post.media.length > 0 && <div className="raw-media">{post.media.filter((item) => item.type === 'image' && item.url).slice(0, 4).map((item) => <img src={item.url} alt="" key={item.url} />)}</div>}<a href={post.url} target="_blank" rel="noreferrer">打开原帖 ↗</a></article>)}{!rawPosts.length && <div className="review-empty">还没有采集到原始动态。</div>}</section>}
      </section>
    </div>
  )
}
