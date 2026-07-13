import type { AgentStatus, MergeSuggestion, WorkGroup } from '../types'

const decisionLabel = {
  same_work: '倾向同一作品',
  different_work: '倾向不同作品',
  uncertain: '无法确定',
}

function CandidateWork({ groupId, title, group, busy, onOpen }: {
  groupId: number
  title: string
  group: WorkGroup | null
  busy: boolean
  onOpen: (groupId: number) => void
}) {
  return (
    <button
      type="button"
      className="candidate-work"
      disabled={busy || !group}
      onClick={() => onOpen(groupId)}
      aria-label={`查看《${title}》的作品详情`}
    >
      <span className="candidate-cover">
        {group?.cover_url ? <img src={group.cover_url} alt={`${title} 封面`} loading="lazy" /> : <span>暂无封面</span>}
      </span>
      <span className="candidate-copy">
        <strong>{title}</strong>
        <small>{group ? `${group.edition_count} 个版本${group.year ? ` · ${group.year}` : ''}${group.language ? ` · ${group.language.toUpperCase()}` : ''}` : '作品资料不可用'}</small>
        <span className="source-badges">
          {group?.providers.map((provider) => <span className={`source-badge source-${provider}`} key={provider}>{provider}</span>)}
        </span>
        <em>点击查看全部版本 →</em>
      </span>
    </button>
  )
}

export function MergeReview({ suggestions, agentStatus, busy, onRunAgent, onOpenGroup, onAccept, onReject, onClose }: {
  suggestions: MergeSuggestion[]
  agentStatus: AgentStatus | null
  busy: boolean
  onRunAgent: () => void
  onOpenGroup: (groupId: number) => void
  onAccept: (id: number) => void
  onReject: (id: number) => void
  onClose: () => void
}) {
  return (
    <div className="modal-backdrop" role="presentation">
      <section className="merge-review" role="dialog" aria-modal="true" aria-label="聚合候选审核">
        <button className="modal-close" onClick={onClose} aria-label="关闭">×</button>
        <p className="eyebrow">MATCH REVIEW</p>
        <h2>聚合候选</h2>
        <p>这些作品有相似迹象，但证据不足以安全自动合并，请人工确认。</p>
        <div className="agent-review-toolbar">
          <div>
            <strong>Agent 证据复核</strong>
            <small>{agentStatus?.configured ? `${agentStatus.model} · 只读建议，不会自动合并` : '尚未在 Dockge 中启用并配置模型'}</small>
          </div>
          <button disabled={busy || !agentStatus?.configured} onClick={onRunAgent}>细查全部候选</button>
        </div>
        <div className="suggestion-list">
          {suggestions.map((item) => (
            <article key={item.id}>
              <div className="candidate-pair">
                <CandidateWork groupId={item.source_group_id} title={item.source_title} group={item.source_group} busy={busy} onOpen={onOpenGroup} />
                <span className="candidate-vs">可能是<br />同一部</span>
                <CandidateWork groupId={item.target_group_id} title={item.target_title} group={item.target_group} busy={busy} onOpen={onOpenGroup} />
              </div>
              <p className="candidate-reason">{item.reasons.join(' · ')} · 置信度 {Math.round(item.confidence * 100)}%</p>
              {item.soft_conflicts.length > 0 && <div className="candidate-warning"><strong>软警告，不阻止 Agent</strong><span>{item.conflict_details.join(' · ') || item.soft_conflicts.join(' · ')}</span></div>}
              {item.hard_conflicts.length > 0 && <div className="candidate-warning hard"><strong>硬冲突</strong><span>{item.conflict_details.join(' · ') || item.hard_conflicts.join(' · ')}</span></div>}
              {item.agent_review && (
                <div className={`agent-verdict agent-${item.agent_review.decision ?? item.agent_review.status}`}>
                  <strong>{item.agent_review.decision ? decisionLabel[item.agent_review.decision] : item.agent_review.status === 'blocked' ? '硬规则已拦截' : 'Agent 检查失败'}</strong>
                  {item.agent_review.confidence !== null && <span>置信度 {Math.round(item.agent_review.confidence * 100)}%</span>}
                  <p>{item.agent_review.rationale ?? item.agent_review.error}</p>
                  {(item.agent_review.evidence_codes.length > 0 || item.agent_review.conflict_codes.length > 0) && <small>{[...item.agent_review.evidence_codes, ...item.agent_review.conflict_codes].join(' · ')}</small>}
                </div>
              )}
              <div className="candidate-actions"><button disabled={busy} onClick={() => onReject(item.id)}>保持分开</button><button disabled={busy} onClick={() => onAccept(item.id)}>确认合并</button></div>
            </article>
          ))}
          {!suggestions.length && <div className="review-empty">没有待确认候选</div>}
        </div>
      </section>
    </div>
  )
}
