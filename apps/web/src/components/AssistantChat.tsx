import { useEffect, useRef, useState, type FormEvent } from 'react'
import { api } from '../lib/api'
import type { AssistantCitation, AssistantMessage, AssistantProposal, AssistantReply } from '../types'

interface Props {
  open: boolean
  onClose: () => void
}

interface Turn extends AssistantMessage {
  citations?: AssistantCitation[]
  proposal?: AssistantProposal | null
  proposalStatus?: 'confirmed' | 'expired'
}

const welcome: Turn = {
  role: 'assistant',
  content: '你好，我是 MangaFinder 助手。可以问我作者近期动态、最近发现的作品，或某部作品的资料。回答根据本地已采集数据，并会标出来源。',
}

const examples = [
  '最近发现了哪些新作品？',
  '有哪些作者最近提到了新刊或预售？',
  '告诉我某位作者最近的动态和扫描情况',
]

function formatTime(value: string) {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return new Intl.DateTimeFormat('zh-CN', {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(date)
}

export function AssistantChat({ open, onClose }: Props) {
  const [messages, setMessages] = useState<Turn[]>([welcome])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [confirming, setConfirming] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const messagesRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)

  useEffect(() => {
    if (!open) return
    inputRef.current?.focus()
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [open, onClose])

  useEffect(() => {
    if (open) messagesRef.current?.scrollTo({ top: messagesRef.current.scrollHeight, behavior: 'smooth' })
  }, [messages, open, busy])

  async function submit(event: FormEvent) {
    event.preventDefault()
    const question = input.trim()
    if (!question || busy) return
    const userTurn: Turn = { role: 'user', content: question }
    const next = [...messages, userTurn]
    setMessages(next)
    setInput('')
    setBusy(true)
    setError(null)
    try {
      const reply: AssistantReply = await api.assistantChat(
        next.slice(-12).map(({ role, content }) => ({ role, content })),
      )
      setMessages([...next, {
        role: 'assistant',
        content: reply.answer,
        citations: reply.citations,
        proposal: reply.proposal,
      }])
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '助手暂时无法回答，请稍后重试')
    } finally {
      setBusy(false)
    }
  }

  async function confirm(proposal: AssistantProposal) {
    if (confirming) return
    setConfirming(proposal.id)
    setError(null)
    try {
      const result = await api.confirmAssistantProposal(proposal.id)
      setMessages((current) => [
        ...current.map((turn) => turn.proposal?.id === proposal.id
          ? { ...turn, proposalStatus: 'confirmed' as const }
          : turn),
        { role: 'assistant', content: result.message },
      ])
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : '确认修改失败'
      setError(message)
      if (message.includes('过期') || message.includes('已变化') || message.includes('已失效')) {
        setMessages((current) => current.map((turn) => turn.proposal?.id === proposal.id
          ? { ...turn, proposalStatus: 'expired' as const }
          : turn))
      }
    } finally {
      setConfirming(null)
    }
  }

  if (!open) return null

  return (
    <div className="assistant-backdrop" role="presentation" onMouseDown={(event) => {
      if (event.target === event.currentTarget) onClose()
    }}>
      <section className="assistant-dialog" role="dialog" aria-modal="true" aria-label="MangaFinder AI 助手">
        <header className="assistant-header">
          <div className="assistant-mark" aria-hidden="true">AI</div>
          <div>
            <p className="eyebrow">MANGAFINDER / DEEPSEEK FLASH</p>
            <h2>资料库助手</h2>
            <p>依据本地已采集的数据回答 · 修改需你确认</p>
          </div>
          <button className="assistant-close" onClick={onClose} aria-label="关闭助手">×</button>
        </header>

        <div className="assistant-messages" ref={messagesRef} aria-live="polite">
          {messages.length === 1 && <div className="assistant-examples" aria-label="问题示例">
            {examples.map((example) => <button key={example} onClick={() => setInput(example)}>{example}</button>)}
          </div>}
          {messages.map((turn, index) => <article className={'assistant-turn ' + turn.role} key={index + '-' + turn.role}>
            <div className="assistant-bubble">
              <p>{turn.content}</p>
              {turn.citations?.length ? <div className="assistant-citations">
                <span>来源</span>
                {turn.citations.map((source) => <a key={source.url + source.label} href={source.url} target="_blank" rel="noreferrer">{source.label} ↗</a>)}
              </div> : null}
              {turn.proposal && <section className="assistant-proposal" aria-label="待确认的修改预览">
                <div className="assistant-proposal-heading">
                  <strong>{turn.proposal.title}</strong>
                  <span>待你确认</span>
                </div>
                <p>{turn.proposal.reason}</p>
                <dl>{turn.proposal.changes.map((change) => <div key={change.field}>
                  <dt>{change.field}</dt>
                  <dd><span>{change.before}</span><b>→</b><span>{change.after}</span></dd>
                </div>)}</dl>
                <small>预览有效至 {formatTime(turn.proposal.expires_at)}</small>
                {turn.proposalStatus === 'confirmed'
                  ? <div className="assistant-confirmed">✓ 已保存</div>
                  : turn.proposalStatus === 'expired'
                    ? <div className="assistant-expired">预览已失效，请重新提出修改</div>
                    : <button className="assistant-confirm" disabled={Boolean(confirming)} onClick={() => void confirm(turn.proposal!)}>
                        {confirming === turn.proposal.id ? '正在保存…' : '确认并保存这项修改'}
                      </button>}
              </section>}
            </div>
          </article>)}
          {busy && <div className="assistant-thinking" role="status"><span className="spinner" />正在查询资料库并整理回答…</div>}
        </div>

        {error && <div className="assistant-error" role="alert">{error}</div>}
        <form className="assistant-composer" onSubmit={(event) => void submit(event)}>
          <textarea
            ref={inputRef}
            value={input}
            onChange={(event) => setInput(event.target.value)}
            placeholder="问问作者动态、作品发现或资料…"
            maxLength={3000}
            rows={2}
            disabled={busy}
            aria-label="输入问题"
          />
          <button type="submit" disabled={busy || !input.trim()} aria-label="发送问题">
            {busy ? '…' : '发送'}
          </button>
          <small>资料库内容会发送给 DeepSeek 用于生成回答；助手不会自动修改数据库。</small>
        </form>
      </section>
    </div>
  )
}
