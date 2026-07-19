import { useEffect, useMemo, useState, type ChangeEvent, type FormEvent, type ReactNode } from 'react'
import { api } from '../lib/api'
import type { ConfigAuthStatus, ConnectionTestResult, SystemConfig } from '../types'

type Section = 'overview' | 'ai' | 'x' | 'radar' | 'notifications' | 'deployment'

const sections: Array<{ id: Section, label: string, note: string }> = [
  { id: 'overview', label: '状态概览', note: '连接与待处理项目' },
  { id: 'ai', label: 'AI 模型', note: '审核、判断与摘要' },
  { id: 'x', label: 'X 采集', note: '登录会话与访问检测' },
  { id: 'radar', label: '动态雷达', note: '频率、规则与 OCR' },
  { id: 'notifications', label: '通知', note: '日报与 QQ Bot' },
  { id: 'deployment', label: '部署状态', note: '容器、目录与数据' },
]

function Toggle({ checked, onChange, label, note }: { checked: boolean, onChange: (value: boolean) => void, label: string, note?: string }) {
  return <label className="setting-toggle">
    <span><strong>{label}</strong>{note && <small>{note}</small>}</span>
    <input type="checkbox" checked={checked} onChange={(event) => onChange(event.target.checked)} />
    <i aria-hidden="true" />
  </label>
}

function Field({ label, note, children }: { label: string, note?: string, children: ReactNode }) {
  return <label className="setting-field"><span><strong>{label}</strong>{note && <small>{note}</small>}</span>{children}</label>
}

function HealthCard({ label, value, tone, detail, action }: { label: string, value: string, tone: 'ok' | 'warn' | 'off', detail: string, action?: () => void }) {
  return <article className={`health-card ${tone}`}>
    <div><span className="health-dot" /><small>{label}</small></div>
    <strong>{value}</strong>
    <p>{detail}</p>
    {action && <button onClick={action}>去配置</button>}
  </article>
}

export function SystemSettings({ onClose }: { onClose: () => void }) {
  const [auth, setAuth] = useState<ConfigAuthStatus | null>(null)
  const [password, setPassword] = useState('')
  const [config, setConfig] = useState<SystemConfig | null>(null)
  const [draft, setDraft] = useState<SystemConfig | null>(null)
  const [section, setSection] = useState<Section>('overview')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<ConnectionTestResult | null>(null)
  const [cookieHeader, setCookieHeader] = useState('')
  const [storageState, setStorageState] = useState<Record<string, unknown> | null>(null)
  const [storageName, setStorageName] = useState('')

  const dirty = useMemo(() => {
    if (!config || !draft) return false
    return JSON.stringify({ ai: config.ai, radar: config.radar, notifications: config.notifications })
      !== JSON.stringify({ ai: draft.ai, radar: draft.radar, notifications: draft.notifications })
  }, [config, draft])

  async function loadConfig() {
    const next = await api.systemConfig()
    setConfig(next)
    setDraft(structuredClone(next))
  }

  useEffect(() => {
    void api.configAuth().then(async (next) => {
      setAuth(next)
      if (next.authenticated) await loadConfig()
    }).catch((reason) => setError(reason instanceof Error ? reason.message : '无法读取管理员状态'))
  }, [])

  async function authenticate(event: FormEvent) {
    event.preventDefault()
    setBusy(true); setError(null)
    try {
      const next = auth?.initialized
        ? await api.loginConfigAdmin(password)
        : await api.bootstrapConfigAdmin(password)
      setAuth(next); setPassword(''); await loadConfig()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '登录失败')
    } finally { setBusy(false) }
  }

  async function act(action: () => Promise<ConnectionTestResult | void>) {
    setBusy(true); setError(null); setNotice(null)
    try {
      const result = await action()
      if (result) setNotice(result)
      await loadConfig()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '操作失败')
    } finally { setBusy(false) }
  }

  async function save() {
    if (!draft) return
    setBusy(true); setError(null); setNotice(null)
    try {
      const result = await api.saveSystemConfig({ ai: draft.ai, radar: draft.radar, notifications: draft.notifications })
      setConfig(result.config); setDraft(structuredClone(result.config))
      setNotice({ ok: true, message: result.changed_keys.length ? '设置已保存' : '没有需要保存的更改' })
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '保存失败')
    } finally { setBusy(false) }
  }

  async function importSession() {
    if (!cookieHeader.trim() && !storageState) return
    await act(async () => {
      const result = storageState
        ? await api.importXStorageState(storageState)
        : await api.importXCookie(cookieHeader.trim())
      setCookieHeader(''); setStorageState(null); setStorageName('')
      return result
    })
  }

  async function logout() {
    setBusy(true); setError(null)
    try { await api.logoutConfigAdmin(); onClose() }
    catch (reason) { setError(reason instanceof Error ? reason.message : '退出失败') }
    finally { setBusy(false) }
  }

  async function chooseStorage(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0]
    if (!file) return
    setError(null)
    try {
      const body = JSON.parse(await file.text()) as Record<string, unknown>
      setStorageState(body); setStorageName(file.name); setCookieHeader('')
    } catch { setError('这个 storage-state.json 不是有效的 JSON 文件') }
    event.target.value = ''
  }

  if (!auth) return <section className="settings-loading"><span className="spinner" /> 正在读取安全状态…</section>

  if (!auth.authenticated) return <section className="admin-gate">
    <button className="settings-back" onClick={onClose}>返回作品</button>
    <div className="admin-gate-card">
      <p className="eyebrow">ADMIN / PROTECTED</p>
      <h1>{auth.initialized ? '管理员登录' : '创建管理员'}</h1>
      <p>{auth.initialized ? '敏感凭证和采集会话只对管理员开放。' : '首次使用需要创建管理员密码，密码至少 12 位。'}</p>
      <form onSubmit={authenticate}>
        <label htmlFor="admin-password">管理员密码</label>
        <input id="admin-password" type="password" autoComplete={auth.initialized ? 'current-password' : 'new-password'} minLength={12} value={password} onChange={(event) => setPassword(event.target.value)} autoFocus />
        {error && <div className="settings-error" role="alert">{error}</div>}
        <button disabled={busy || password.length < 12}>{busy ? '请稍候…' : auth.initialized ? '登录设置中心' : '创建并进入'}</button>
      </form>
    </div>
  </section>

  if (!draft || !config) return <section className="settings-loading"><span className="spinner" /> 正在读取系统设置…</section>

  const x = config.x_session
  return <section className="settings-page">
    <header className="settings-header">
      <div><p className="eyebrow">SYSTEM / CONFIGURATION</p><h1>系统设置</h1><p>管理连接、模型和自动化规则；敏感凭证不会回显。</p></div>
      <div className="settings-header-actions"><button onClick={() => void logout()}>退出管理员</button><button onClick={onClose}>返回作品</button></div>
    </header>

    <div className="settings-layout">
      <nav className="settings-nav" aria-label="设置分类">
        {sections.map((item) => <button className={section === item.id ? 'active' : ''} onClick={() => { setSection(item.id); setNotice(null); setError(null) }} key={item.id}><strong>{item.label}</strong><small>{item.note}</small></button>)}
      </nav>

      <div className="settings-content">
        {error && <div className="settings-error" role="alert">{error}</div>}
        {notice && <div className={`settings-notice ${notice.ok ? 'ok' : 'bad'}`}>{notice.message}</div>}

        {section === 'overview' && <>
          <div className="settings-section-title"><div><span>OVERVIEW</span><h2>连接状态</h2><p>先处理异常项目，再调整自动化规则。</p></div><button onClick={() => void act(async () => undefined)} disabled={busy}>重新检测</button></div>
          <div className="health-grid">
            <HealthCard label="X 登录会话" value={!x.configured ? '未配置' : x.valid === false ? '会话失效' : x.collector_reachable ? '已连接' : '等待采集器'} tone={x.collector_reachable && x.configured && x.valid !== false ? 'ok' : 'warn'} detail={x.last_error ?? (x.provider ? `采集方式：${x.provider}` : '导入专用账号的登录会话')} action={() => setSection('x')} />
            <HealthCard label="社交采集器" value={x.collector_reachable ? '运行中' : '未连接'} tone={x.collector_reachable ? 'ok' : 'off'} detail={x.collector_reachable ? '隔离容器可以正常响应' : '检查 social Compose profile 与容器网络'} action={() => setSection('deployment')} />
            <HealthCard label="AI 模型" value={config.ai.enabled && config.ai.model ? config.ai.model : '规则模式'} tone={config.ai.enabled && config.ai.model ? 'ok' : 'warn'} detail={config.ai.api_key_configured ? `${config.ai.provider} · 凭证已配置` : `${config.ai.provider} · 未配置云端凭证`} action={() => setSection('ai')} />
            <HealthCard label="QQ 通知" value={config.notifications.qq_enabled && config.notifications.qq_client_secret_configured ? '已配置' : '站内通知'} tone={config.notifications.qq_enabled && config.notifications.qq_client_secret_configured ? 'ok' : 'off'} detail={config.notifications.daily_digest_enabled ? `日报每天 ${String(config.notifications.daily_digest_hour).padStart(2, '0')}:00` : '每日摘要未启用'} action={() => setSection('notifications')} />
          </div>
          {config.deployment.social_profile_required && <div className="settings-callout"><strong>采集器尚未连接</strong><p>请先在 Dockge 中启用 social Compose profile。应用页面无法自行启动缺失的 Docker 服务。</p></div>}
        </>}

        {section === 'ai' && <>
          <div className="settings-section-title"><div><span>MODEL</span><h2>AI 模型</h2><p>同一套模型用于作品聚合、作者新作判断和动态摘要。</p></div><button onClick={() => void act(() => api.testAgentConfig(draft.ai))} disabled={busy}>测试连接</button></div>
          <div className="settings-card">
            <Toggle checked={draft.ai.enabled} onChange={(enabled) => setDraft({ ...draft, ai: { ...draft.ai, enabled } })} label="启用 AI 分析" note="关闭后动态雷达继续使用本地规则" />
            <div className="settings-form-grid">
              <Field label="服务商"><select value={draft.ai.provider} onChange={(event) => setDraft({ ...draft, ai: { ...draft.ai, provider: event.target.value as 'openai_compatible' | 'deepseek' } })}><option value="openai_compatible">OpenAI Compatible</option><option value="deepseek">DeepSeek</option></select></Field>
              <Field label="模型名称"><input value={draft.ai.model} onChange={(event) => setDraft({ ...draft, ai: { ...draft.ai, model: event.target.value } })} placeholder="例如 deepseek-chat" /></Field>
              <Field label="Base URL" note="需要兼容 /chat/completions"><input value={draft.ai.base_url} onChange={(event) => setDraft({ ...draft, ai: { ...draft.ai, base_url: event.target.value } })} /></Field>
              <Field label="API Key" note={draft.ai.api_key_configured ? '已配置；留空表示保持不变' : '本地 Ollama 可以留空'}><div className="secret-input"><input type="password" value={draft.ai.api_key ?? ''} onChange={(event) => setDraft({ ...draft, ai: { ...draft.ai, api_key: event.target.value } })} placeholder={draft.ai.api_key_configured ? '••••••••••••••••' : 'sk-…'} autoComplete="new-password" />{draft.ai.api_key_configured && <button onClick={() => setDraft({ ...draft, ai: { ...draft.ai, api_key: '', api_key_configured: false } })}>清除已有 Key</button>}</div></Field>
              <Field label="请求超时（秒）"><input type="number" min="5" max="300" value={draft.ai.timeout_seconds} onChange={(event) => setDraft({ ...draft, ai: { ...draft.ai, timeout_seconds: Number(event.target.value) } })} /></Field>
              <Field label="温度"><input type="number" min="0" max="2" step="0.1" value={draft.ai.temperature} onChange={(event) => setDraft({ ...draft, ai: { ...draft.ai, temperature: Number(event.target.value) } })} /></Field>
            </div>
            <Toggle checked={draft.ai.review_after_discovery} onChange={(review_after_discovery) => setDraft({ ...draft, ai: { ...draft.ai, review_after_discovery } })} label="作品扫描后自动细查" note="只生成审核建议，不会直接合并作品" />
          </div>
        </>}

        {section === 'x' && <>
          <div className="settings-section-title"><div><span>X SESSION</span><h2>X 采集</h2><p>使用部署者控制的专用账号会话读取公开作者动态。</p></div><button onClick={() => void act(() => api.testXConfig())} disabled={busy || !x.configured}>测试访问</button></div>
          <div className="x-session-summary"><div><span className={`health-dot ${x.collector_reachable && x.configured ? '' : 'warn'}`} /><div><strong>{x.configured ? '登录会话已导入' : '尚未导入登录会话'}</strong><small>{x.collector_reachable ? `采集器已连接 · ${x.provider ?? 'browser'}` : x.last_error ?? '采集器未连接'}</small></div></div>{x.configured && <button onClick={() => { if (window.confirm('确认清除 X 登录会话？作者动态将停止同步。')) void act(() => api.clearXSession()) }} disabled={busy}>清除会话</button>}</div>
          <div className="settings-card">
            <h3>导入或替换会话</h3><p className="setting-help">推荐使用专用 X 账号。Cookie 只会发送给本机后端，保存后不会在页面中回显。</p>
            <div className="session-methods">
              <div><strong>粘贴 Cookie 请求头</strong><textarea value={cookieHeader} onChange={(event) => { setCookieHeader(event.target.value); if (event.target.value) { setStorageState(null); setStorageName('') } }} placeholder="auth_token=…; ct0=…; twid=…" spellCheck={false} /><small>至少需要 auth_token 和 ct0</small></div>
              <div className="session-divider"><span>或</span></div>
              <div><strong>上传 storage-state.json</strong><label className="file-picker"><input type="file" accept="application/json,.json" onChange={(event) => void chooseStorage(event)} /><span>{storageName || '选择 JSON 文件'}</span></label><small>只保留 x.com 域下的 Cookie</small></div>
            </div>
            <div className="settings-card-actions"><button onClick={() => void importSession()} disabled={busy || (!cookieHeader.trim() && !storageState)}>安全保存并验证</button></div>
          </div>
          <div className="settings-card compact"><h3>采集环境</h3><div className="deployment-list"><div><span>代理</span><strong>{x.proxy_configured ? '已通过 Dockge 配置' : '未配置'}</strong></div><div><span>User-Agent</span><strong>{x.user_agent_configured ? '已配置' : '使用采集器默认值'}</strong></div></div><p className="setting-help">代理地址和 User-Agent 属于容器启动参数，需要在 Dockge 修改并重启采集器。</p></div>
        </>}

        {section === 'radar' && <>
          <div className="settings-section-title"><div><span>AUTOMATION</span><h2>动态雷达</h2><p>控制同步范围、候选判断和图片文字识别。</p></div></div>
          <div className="settings-card">
            <Toggle checked={draft.radar.enabled} onChange={(enabled) => setDraft({ ...draft, radar: { ...draft.radar, enabled } })} label="启用作者动态雷达" note="首次开启或关闭调度器需要重启主应用" />
            <div className="settings-form-grid three">
              <Field label="常规同步间隔（分钟）"><input type="number" min="5" value={draft.radar.sync_interval_minutes} onChange={(event) => setDraft({ ...draft, radar: { ...draft.radar, sync_interval_minutes: Number(event.target.value) } })} /></Field>
              <Field label="活跃作者间隔（分钟）"><input type="number" min="5" value={draft.radar.event_sync_interval_minutes} onChange={(event) => setDraft({ ...draft, radar: { ...draft.radar, event_sync_interval_minutes: Number(event.target.value) } })} /></Field>
              <Field label="首次回溯天数"><input type="number" min="1" max="365" value={draft.radar.initial_backfill_days} onChange={(event) => setDraft({ ...draft, radar: { ...draft.radar, initial_backfill_days: Number(event.target.value) } })} /></Field>
              <Field label="单次最大帖子数"><input type="number" min="1" max="500" value={draft.radar.max_posts_per_sync} onChange={(event) => setDraft({ ...draft, radar: { ...draft.radar, max_posts_per_sync: Number(event.target.value) } })} /></Field>
              <Field label="OCR 帖子上限"><input type="number" min="0" max="100" value={draft.radar.ocr_max_posts_per_sync} onChange={(event) => setDraft({ ...draft, radar: { ...draft.radar, ocr_max_posts_per_sync: Number(event.target.value) } })} /></Field>
              <Field label="OCR 超时（秒）"><input type="number" min="5" max="180" value={draft.radar.ocr_timeout_seconds} onChange={(event) => setDraft({ ...draft, radar: { ...draft.radar, ocr_timeout_seconds: Number(event.target.value) } })} /></Field>
              <Field label="图片归档上限（GB）" note="达到上限后只淘汰未固定的普通图片"><input type="number" min="1" max="1000" value={draft.radar.media_cache_max_gb} onChange={(event) => setDraft({ ...draft, radar: { ...draft.radar, media_cache_max_gb: Number(event.target.value) } })} /></Field>
              <Field label="图片最长边（px）"><input type="number" min="640" max="4096" value={draft.radar.media_max_dimension} onChange={(event) => setDraft({ ...draft, radar: { ...draft.radar, media_max_dimension: Number(event.target.value) } })} /></Field>
              <Field label="WebP 质量"><input type="number" min="40" max="95" value={draft.radar.media_webp_quality} onChange={(event) => setDraft({ ...draft, radar: { ...draft.radar, media_webp_quality: Number(event.target.value) } })} /></Field>
              <Field label="删除二次确认间隔（小时）"><input type="number" min="1" max="168" value={draft.radar.delete_confirm_hours} onChange={(event) => setDraft({ ...draft, radar: { ...draft.radar, delete_confirm_hours: Number(event.target.value) } })} /></Field>
            </div>
            <Toggle checked={draft.radar.agent_enabled} onChange={(agent_enabled) => setDraft({ ...draft, radar: { ...draft.radar, agent_enabled } })} label="使用 AI 判断新作" />
            <Toggle checked={draft.radar.ocr_enabled} onChange={(ocr_enabled) => setDraft({ ...draft, radar: { ...draft.radar, ocr_enabled } })} label="识别帖子图片文字" />
          </div>
          <div className="settings-card threshold-card"><h3>信号阈值</h3><div className="threshold-row"><label>候选阈值 <output>{draft.radar.candidate_threshold.toFixed(2)}</output><input type="range" min="0" max="1" step="0.01" value={draft.radar.candidate_threshold} onChange={(event) => setDraft({ ...draft, radar: { ...draft.radar, candidate_threshold: Number(event.target.value) } })} /></label><label>自动确认阈值 <output>{draft.radar.auto_confirm_threshold.toFixed(2)}</output><input type="range" min="0" max="1" step="0.01" value={draft.radar.auto_confirm_threshold} onChange={(event) => setDraft({ ...draft, radar: { ...draft.radar, auto_confirm_threshold: Number(event.target.value) } })} /></label></div><div className="threshold-legend"><span>低于候选：归档</span><span>候选区间：人工审核</span><span>高置信：自动确认</span></div></div>
        </>}

        {section === 'notifications' && <>
          <div className="settings-section-title"><div><span>DELIVERY</span><h2>通知</h2><p>控制每日作者摘要和 QQ 官方机器人私聊。</p></div></div>
          <div className="settings-card"><h3>每日摘要</h3><Toggle checked={draft.notifications.daily_digest_enabled} onChange={(daily_digest_enabled) => setDraft({ ...draft, notifications: { ...draft.notifications, daily_digest_enabled } })} label="启用自动日报" /><div className="settings-form-grid three"><Field label="发送小时"><input type="number" min="0" max="23" value={draft.notifications.daily_digest_hour} onChange={(event) => setDraft({ ...draft, notifications: { ...draft.notifications, daily_digest_hour: Number(event.target.value) } })} /></Field><Field label="时区"><input value={draft.notifications.daily_digest_timezone} onChange={(event) => setDraft({ ...draft, notifications: { ...draft.notifications, daily_digest_timezone: event.target.value } })} /></Field><Field label="最低重要程度"><select value={draft.notifications.daily_digest_min_importance} onChange={(event) => setDraft({ ...draft, notifications: { ...draft.notifications, daily_digest_min_importance: event.target.value as 'low' | 'normal' | 'high' } })}><option value="low">低</option><option value="normal">普通</option><option value="high">高</option></select></Field><Field label="回溯天数"><input type="number" min="1" max="90" value={draft.notifications.daily_digest_initial_lookback_days} onChange={(event) => setDraft({ ...draft, notifications: { ...draft.notifications, daily_digest_initial_lookback_days: Number(event.target.value) } })} /></Field><Field label="最大作者数"><input type="number" min="1" max="100" value={draft.notifications.daily_digest_max_authors} onChange={(event) => setDraft({ ...draft, notifications: { ...draft.notifications, daily_digest_max_authors: Number(event.target.value) } })} /></Field><Field label="每位作者条目数"><input type="number" min="1" max="20" value={draft.notifications.daily_digest_max_items_per_author} onChange={(event) => setDraft({ ...draft, notifications: { ...draft.notifications, daily_digest_max_items_per_author: Number(event.target.value) } })} /></Field></div></div>
          <div className="settings-card"><div className="card-heading-action"><h3>QQ Bot</h3><button onClick={() => void act(() => api.testQqConfig(draft.notifications))} disabled={busy}>测试发送</button></div><Toggle checked={draft.notifications.qq_enabled} onChange={(qq_enabled) => setDraft({ ...draft, notifications: { ...draft.notifications, qq_enabled } })} label="启用 QQ 私聊通知" /><div className="settings-form-grid"><Field label="App ID"><input value={draft.notifications.qq_app_id} onChange={(event) => setDraft({ ...draft, notifications: { ...draft.notifications, qq_app_id: event.target.value } })} /></Field><Field label="User OpenID"><input value={draft.notifications.qq_user_openid} onChange={(event) => setDraft({ ...draft, notifications: { ...draft.notifications, qq_user_openid: event.target.value } })} /></Field><Field label="Client Secret" note={draft.notifications.qq_client_secret_configured ? '已配置；留空表示保持不变' : undefined}><div className="secret-input"><input type="password" value={draft.notifications.qq_client_secret ?? ''} onChange={(event) => setDraft({ ...draft, notifications: { ...draft.notifications, qq_client_secret: event.target.value } })} placeholder={draft.notifications.qq_client_secret_configured ? '••••••••••••••••' : '输入 Client Secret'} autoComplete="new-password" />{draft.notifications.qq_client_secret_configured && <button onClick={() => setDraft({ ...draft, notifications: { ...draft.notifications, qq_client_secret: '', qq_client_secret_configured: false } })}>清除已有 Secret</button>}</div></Field></div></div>
        </>}

        {section === 'deployment' && <>
          <div className="settings-section-title"><div><span>RUNTIME</span><h2>部署状态</h2><p>这些项目来自 Docker 和宿主机，只在这里检查，不在线改写。</p></div></div>
          <div className="settings-card"><div className="deployment-list"><div><span>MangaFinder API</span><strong className="state-ok">运行中</strong></div><div><span>Social Collector</span><strong className={config.deployment.collector_running ? 'state-ok' : 'state-bad'}>{config.deployment.collector_running ? '运行中' : '未连接'}</strong></div><div><span>X 会话目录</span><code>{config.deployment.x_session_dir}</code></div><div><span>社交媒体目录</span><code>{config.deployment.social_media_dir}</code></div><div><span>数据库</span><code>{config.deployment.database_url}</code></div></div></div>
          <div className="settings-callout"><strong>在 Dockge 中修改</strong><p>Compose profile、数据卷路径、代理网络和容器间令牌决定服务能否启动，需要在部署环境中修改。</p><code>COMPOSE_PROFILES=social</code><code>MANGAFINDER_SOCIAL_COLLECTOR_TOKEN=…</code><code>MANGAFINDER_X_SESSION_HOST_DIR=…</code></div>
        </>}
      </div>
    </div>

    <footer className={`settings-savebar ${dirty ? 'visible' : ''}`}><span>{dirty ? '有尚未保存的更改' : '所有更改均已保存'}</span><div><button onClick={() => setDraft(structuredClone(config))} disabled={!dirty || busy}>取消更改</button><button className="primary" onClick={() => void save()} disabled={!dirty || busy}>{busy ? '保存中…' : '保存设置'}</button></div></footer>
  </section>
}
