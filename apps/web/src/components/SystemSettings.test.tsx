import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { SystemSettings } from './SystemSettings'

const { config, apiMock } = vi.hoisted(() => {
  const config = {
    ai: { enabled: false, provider: 'openai_compatible' as const, base_url: 'http://ollama:11434/v1', model: '', api_key: null, api_key_configured: true, temperature: 0, timeout_seconds: 60, review_after_discovery: false },
    radar: { enabled: false, sync_interval_minutes: 120, event_sync_interval_minutes: 30, initial_backfill_days: 90, max_posts_per_sync: 100, agent_enabled: true, candidate_threshold: 0.6, auto_confirm_threshold: 0.92, ocr_enabled: true, ocr_max_posts_per_sync: 12, ocr_timeout_seconds: 30 },
    notifications: { daily_digest_enabled: true, daily_digest_hour: 20, daily_digest_timezone: 'Asia/Shanghai', daily_digest_initial_lookback_days: 7, daily_digest_min_importance: 'normal' as const, daily_digest_max_authors: 20, daily_digest_max_items_per_author: 3, qq_enabled: false, qq_app_id: '', qq_client_secret: null, qq_client_secret_configured: false, qq_user_openid: '' },
    x_session: { configured: false, collector_reachable: false, valid: null, provider: null, last_error: null, proxy_configured: false, user_agent_configured: false },
    deployment: { api_running: true, collector_running: false, social_profile_required: true, x_session_dir: '/app/data/x-session', social_media_dir: '/app/data/social-media', database_url: 'sqlite:////app/data/mangafinder.db', restart_required_fields: [] },
  }
  return {
    config,
    apiMock: {
      configAuth: vi.fn(),
      bootstrapConfigAdmin: vi.fn(),
      loginConfigAdmin: vi.fn(),
      logoutConfigAdmin: vi.fn(),
      systemConfig: vi.fn().mockResolvedValue(config),
      saveSystemConfig: vi.fn(),
      testAgentConfig: vi.fn(), testXConfig: vi.fn(), testQqConfig: vi.fn(),
      importXCookie: vi.fn(), importXStorageState: vi.fn(), clearXSession: vi.fn(),
    },
  }
})

vi.mock('../lib/api', () => ({ api: apiMock }))

describe('system settings', () => {
  afterEach(() => { cleanup(); vi.clearAllMocks() })

  it('bootstraps the first administrator before revealing credentials', async () => {
    apiMock.configAuth.mockResolvedValue({ initialized: false, authenticated: false, username: null })
    apiMock.bootstrapConfigAdmin.mockResolvedValue({ initialized: true, authenticated: true, username: 'admin' })
    render(<SystemSettings onClose={() => undefined} />)

    expect(await screen.findByRole('heading', { name: '创建管理员' })).toBeInTheDocument()
    expect(screen.queryByText('API Key')).not.toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('管理员密码'), { target: { value: 'a secure password' } })
    fireEvent.click(screen.getByRole('button', { name: '创建并进入' }))

    expect(await screen.findByRole('heading', { name: '系统设置' })).toBeInTheDocument()
    expect(apiMock.bootstrapConfigAdmin).toHaveBeenCalledWith('a secure password')
    expect(screen.getByText('采集器尚未连接')).toBeInTheDocument()
  })

  it('keeps an existing API key masked and saves ordinary model changes', async () => {
    apiMock.configAuth.mockResolvedValue({ initialized: true, authenticated: true, username: 'admin' })
    apiMock.saveSystemConfig.mockImplementation(async (next) => ({ config: { ...config, ...next }, changed_keys: ['agent_model'], restart_required_fields: [] }))
    render(<SystemSettings onClose={() => undefined} />)

    await screen.findByRole('heading', { name: '系统设置' })
    fireEvent.click(screen.getByRole('button', { name: /AI 模型/ }))
    expect(await screen.findByText('已配置；留空表示保持不变')).toBeInTheDocument()
    expect(screen.getByPlaceholderText('••••••••••••••••')).toHaveValue('')
    fireEvent.change(screen.getByLabelText('模型名称'), { target: { value: 'deepseek-chat' } })
    fireEvent.click(screen.getByRole('button', { name: '保存设置' }))

    await waitFor(() => expect(apiMock.saveSystemConfig).toHaveBeenCalled())
    const submitted = apiMock.saveSystemConfig.mock.calls[0][0]
    expect(submitted.ai.model).toBe('deepseek-chat')
    expect(submitted.ai.api_key).toBeNull()
  })
})
