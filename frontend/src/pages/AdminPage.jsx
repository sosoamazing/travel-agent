import { Fragment, useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import { clearAuth, getRole, getUsername } from '../auth'

const TABS = [
  { id: 'nodes', label: '节点耗时' },
  { id: 'nodeTools', label: '节点工具' },
  { id: 'models', label: '模型 Token' },
  { id: 'cost', label: 'Token 成本' },
  { id: 'tools', label: '工具可靠性' },
  { id: 'intent', label: '意图识别' },
  { id: 'errors', label: '错误样本' },
  { id: 'tasks', label: '最近任务' },
]

/* ── 格式化工具 ─────────────────────────── */
function fmtNum(v) {
  return v == null || v === '' ? '—' : Number(v).toLocaleString('zh-CN')
}

function fmtRate(v) {
  if (v == null || v === '') return '—'
  const n = Number(v)
  const pct = n <= 1 ? n * 100 : n
  return `${pct.toFixed(1)}%`
}

// 专用于已经是「百分比 0~100」的字段（llm_cache_hit_rate / node_tools.error_rate /
// model_cost.cache_hit_rate 等），直接展示，不做任何缩放。
function fmtPct(v) {
  if (v == null || v === '') return '—'
  const n = Number(v)
  if (Number.isNaN(n)) return '—'
  return `${n.toFixed(1)}%`
}

function pctToNum(v) {
  if (v == null || v === '') return 0
  const n = Number(v)
  return Math.min(100, n <= 1 ? n * 100 : n)
}

function fmtMs(v) {
  if (v == null || v === '') return '—'
  const n = Number(v)
  if (n < 1000) return `${n.toFixed(0)} ms`
  return `${(n / 1000).toFixed(2)} s`
}

function fmtTime(v) {
  if (!v) return '—'
  const d = new Date(v)
  if (Number.isNaN(d.getTime())) return String(v)
  return d.toLocaleString('zh-CN', { hour12: false })
}

function shortQuery(q) {
  if (!q) return '—'
  return q.length > 60 ? `${q.slice(0, 60)}…` : q
}

function errTone(type) {
  const t = String(type || '').toLowerCase()
  if (t.includes('timeout') || t.includes('rate') || t.includes('429')) return 'warn'
  if (t.includes('auth') || t.includes('403') || t.includes('permission')) return 'info'
  return 'error'
}

function statusInfo(status) {
  const s = String(status || '').toLowerCase()
  if (s === 'succeeded' || s === 'success') return ['成功', 'st-ok']
  if (s === 'failed' || s === 'failure' || s === 'error') return ['失败', 'st-err']
  if (s === 'running' || s === 'processing' || s === 'pending') return ['进行中', 'st-run']
  return [status || '未知', 'st-idle']
}

export default function AdminPage({ onLogout }) {
  const username = getUsername()
  const role = getRole()
  const isSuper = role === 'superadmin'

  const [activeTab, setActiveTab] = useState('nodes')

  const [report, setReport] = useState(null)
  const [tasks, setTasks] = useState([])
  const [reportLoading, setReportLoading] = useState(true)
  const [reportError, setReportError] = useState('')
  const [version, setVersion] = useState('')
  const [versions, setVersions] = useState([])

  const [admins, setAdmins] = useState([])
  const [adminsError, setAdminsError] = useState('')
  const [createUsername, setCreateUsername] = useState('')
  const [createPassword, setCreatePassword] = useState('')
  const [createError, setCreateError] = useState('')
  const [creating, setCreating] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState('')
  const [expandedNodes, setExpandedNodes] = useState(() => new Set())

  const loadReport = useCallback(async (ver = '') => {
    setReportLoading(true)
    setReportError('')
    setVersion(ver)
    try {
      const [r, t, v] = await Promise.all([
        api.adminReport(ver),
        api.adminTasks(50, ver),
        api.adminVersions(),
      ])
      setReport(r)
      setTasks(Array.isArray(t) ? t : [])
      setVersions(Array.isArray(v) ? v : [])
    } catch (err) {
      setReportError(err.message || '加载监控数据失败，请稍后重试')
    } finally {
      setReportLoading(false)
    }
  }, [])

  const loadAdmins = useCallback(async () => {
    if (!isSuper) return
    setAdminsError('')
    try {
      const list = await api.listAdmins()
      setAdmins(Array.isArray(list) ? list : [])
    } catch (err) {
      setAdminsError(err.message || '加载管理员列表失败')
    }
  }, [isSuper])

  useEffect(() => {
    loadReport()
    loadAdmins()
  }, [loadReport, loadAdmins])

  async function handleCreateAdmin(e) {
    e.preventDefault()
    const uname = createUsername.trim()
    if (!uname || createPassword.length < 6) return
    setCreateError('')
    setCreating(true)
    try {
      await api.createAdmin(uname, createPassword)
      setCreateUsername('')
      setCreatePassword('')
      await loadAdmins()
    } catch (err) {
      setCreateError(err.message || '创建失败，请稍后重试')
    } finally {
      setCreating(false)
    }
  }

  async function handleDeleteAdmin(uname) {
    setConfirmDelete('')
    try {
      await api.deleteAdmin(uname)
      await loadAdmins()
    } catch (err) {
      setAdminsError(err.message || '删除失败，请稍后重试')
    }
  }

  function handleVersionChange(e) {
    loadReport(e.target.value)
  }

  // 切换节点展开/收起（不可变更新 Set）
  function toggleNode(node) {
    setExpandedNodes((prev) => {
      const next = new Set(prev)
      if (next.has(node)) next.delete(node)
      else next.add(node)
      return next
    })
  }

  function handleLogout() {
    clearAuth()
    onLogout()
  }

  /* ── 指标卡 ─────────────────────────── */
  const metrics = report
    ? [
        { label: '任务总数', value: fmtNum(report.task_count), cls: 'm-blue' },
        { label: '成功率', value: fmtRate(report.success_rate), cls: 'm-green' },
        { label: '失败任务', value: fmtNum(report.error_count), cls: 'm-red' },
        { label: '意图识别', value: fmtRate(report.intent_recognition?.success_rate), cls: 'm-violet' },
        { label: '总 Token', value: fmtNum(report.tokens?.total), cls: 'm-violet' },
        { label: '平均耗时', value: fmtMs(report.duration?.mean), cls: 'm-cyan' },
        { label: 'LLM 调用', value: fmtNum(report.llm_call_count), cls: 'm-orange' },
        { label: '工具调用', value: fmtNum(report.tool_call_count), cls: 'm-teal' },
        { label: '缓存命中率', value: fmtPct(report.tokens?.cache_hit_rate), cls: 'm-slate' },
      ]
    : []

  /* ── 明细渲染 ───────────────────────── */
  function renderNodes() {
    const nodes = report.node_heatmap || []
    if (nodes.length === 0) return <div className="empty-hint">暂无节点耗时数据</div>
    const maxMean = Math.max(1, ...nodes.map((n) => Number(n.mean_ms) || 0))
    const maxP95 = Math.max(1, ...nodes.map((n) => Number(n.p95_ms) || 0))
    return (
      <div className="admin-table-wrap">
        <table className="admin-table">
          <thead>
            <tr>
              <th>节点</th>
              <th>调用次数</th>
              <th>平均耗时</th>
              <th>P95 耗时</th>
              <th>错误数</th>
              <th>LLM 调用</th>
              <th>MCP 调用</th>
              <th>输入 Token</th>
              <th>输出 Token</th>
              <th>缓存命中率</th>
              <th>LLM 耗时</th>
              <th>LLM 错误</th>
            </tr>
          </thead>
          <tbody>
            {nodes.map((n) => {
              const agents = n.llm_agents || []
              const isOpen = expandedNodes.has(n.node)
              return (
                <Fragment key={n.node}>
                  <tr className={isOpen ? 'node-row-open' : ''}>
                    <td className="cell-name">
                      {agents.length > 0 ? (
                        <button
                          type="button"
                          className={`node-expand${isOpen ? ' open' : ''}`}
                          onClick={() => toggleNode(n.node)}
                          aria-expanded={isOpen}
                          aria-label={`展开 ${n.node} 的 Agent 明细`}
                        >
                          <span className="node-expand-icon">▸</span>
                          <span>{n.node}</span>
                        </button>
                      ) : (
                        n.node
                      )}
                    </td>
                    <td>{fmtNum(n.count)}</td>
                    <td>
                      <span
                        className="heat-cell"
                        style={{ '--heat': Math.min(1, (Number(n.mean_ms) || 0) / maxMean) }}
                      >
                        {fmtMs(n.mean_ms)}
                      </span>
                    </td>
                    <td>
                      <span
                        className="heat-cell"
                        style={{ '--heat': Math.min(1, (Number(n.p95_ms) || 0) / maxP95) }}
                      >
                        {fmtMs(n.p95_ms)}
                      </span>
                    </td>
                    <td className={n.errors ? 'cell-danger' : ''}>{fmtNum(n.errors)}</td>
                    <td>{fmtNum(n.llm_calls)}</td>
                    <td>{fmtNum(n.mcp_calls)}</td>
                    <td>{fmtNum(n.llm_input_tokens)}</td>
                    <td>{fmtNum(n.llm_output_tokens)}</td>
                    <td>{fmtPct(n.llm_cache_hit_rate)}</td>
                    <td>{fmtMs(n.llm_duration_ms)}</td>
                    <td className={n.llm_errors ? 'cell-danger' : ''}>{fmtNum(n.llm_errors)}</td>
                  </tr>
                  {isOpen && (
                    <tr className="node-sub-row">
                      <td className="node-sub-cell" colSpan={12}>
                        <div className="agent-sub-title">Agent 明细（{agents.length}）</div>
                        <div className="agent-sub-wrap">
                          <table className="admin-table agent-sub-table">
                            <thead>
                              <tr>
                                <th>Agent</th>
                                <th>模型</th>
                                <th>调用次数</th>
                                <th>输入 Token</th>
                                <th>输出 Token</th>
                                <th>缓存 Token</th>
                                <th>缓存命中率</th>
                                <th>平均耗时</th>
                                <th>错误数</th>
                              </tr>
                            </thead>
                            <tbody>
                              {agents.map((a) => (
                                <tr key={a.agent}>
                                  <td className="cell-name">{a.agent}</td>
                                  <td className="cell-tool">{a.model || '—'}</td>
                                  <td>{fmtNum(a.calls)}</td>
                                  <td>{fmtNum(a.input_tokens)}</td>
                                  <td>{fmtNum(a.output_tokens)}</td>
                                  <td>{fmtNum(a.cached_input_tokens)}</td>
                                  <td>{fmtPct(a.cache_hit_rate)}</td>
                                  <td>{fmtMs(a.duration_ms)}</td>
                                  <td className={a.error_count ? 'cell-danger' : ''}>
                                    {fmtNum(a.error_count)}
                                  </td>
                                </tr>
                              ))}
                            </tbody>
                          </table>
                        </div>
                      </td>
                    </tr>
                  )}
                </Fragment>
              )
            })}
          </tbody>
        </table>
      </div>
    )
  }

  function renderNodeTools() {
    const rows = report.node_tools || []
    if (rows.length === 0) return <div className="empty-hint">暂无节点工具数据</div>
    // 按节点分组：保持后端返回顺序，用 rowSpan 让节点列纵向合并
    const groups = []
    rows.forEach((r) => {
      const last = groups[groups.length - 1]
      if (last && last.node === r.node) last.rows.push(r)
      else groups.push({ node: r.node, rows: [r] })
    })
    return (
      <div className="admin-table-wrap">
        <table className="admin-table">
          <thead>
            <tr>
              <th>节点</th>
              <th>工具</th>
              <th>调用次数</th>
              <th>错误数</th>
              <th>错误率</th>
              <th>总耗时</th>
              <th>平均耗时</th>
              <th>重试</th>
            </tr>
          </thead>
          <tbody>
            {groups.map((g) =>
              g.rows.map((r, ri) => (
                <tr key={`${g.node}-${r.tool}`}>
                  {ri === 0 && (
                    <td className="node-cell" rowSpan={g.rows.length}>
                      {g.node}
                    </td>
                  )}
                  <td className="cell-tool" title={r.tool}>
                    {r.tool}
                  </td>
                  <td>{fmtNum(r.calls)}</td>
                  <td className={r.errors ? 'cell-danger' : ''}>{fmtNum(r.errors)}</td>
                  <td className={Number(r.error_rate) > 20 ? 'cell-danger' : ''}>
                    {fmtPct(r.error_rate)}
                  </td>
                  <td>{fmtMs(r.total_ms)}</td>
                  <td>{fmtMs(r.avg_ms)}</td>
                  <td>{fmtNum(r.retries)}</td>
                </tr>
              )),
            )}
          </tbody>
        </table>
      </div>
    )
  }

  function renderModels() {
    const rows = report.model_cost || []
    if (rows.length === 0) return <div className="empty-hint">暂无模型 Token 数据</div>
    const maxTotal = Math.max(1, ...rows.map((m) => Number(m.total_tokens) || 0))
    return (
      <div className="admin-table-wrap">
        <table className="admin-table">
          <thead>
            <tr>
              <th>模型</th>
              <th>输入 Token</th>
              <th>输出 Token</th>
              <th>缓存 Token</th>
              <th>总 Token</th>
              <th>调用次数</th>
              <th>缓存命中率</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((m) => (
              <tr key={m.model}>
                <td className="cell-name">{m.model}</td>
                <td>{fmtNum(m.input_tokens)}</td>
                <td>{fmtNum(m.output_tokens)}</td>
                <td>{fmtNum(m.cached_tokens)}</td>
                <td>
                  <span className="token-bar-cell">
                    <span className="token-bar-value">{fmtNum(m.total_tokens)}</span>
                    <span className="pct-track">
                      <span
                        className="pct-fill"
                        style={{
                          width: `${Math.min(100, ((Number(m.total_tokens) || 0) / maxTotal) * 100)}%`,
                        }}
                      />
                    </span>
                  </span>
                </td>
                <td>{fmtNum(m.calls)}</td>
                <td>{fmtPct(m.cache_hit_rate)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    )
  }

  function renderCost() {
    const costs = report.agent_cost || []
    if (costs.length === 0) return <div className="empty-hint">暂无 Token 成本数据</div>
    return (
      <div className="admin-table-wrap">
        <table className="admin-table">
          <thead>
            <tr>
              <th>Agent</th>
              <th>Token 数</th>
              <th>占比</th>
              <th>调用次数</th>
              <th>累计耗时</th>
            </tr>
          </thead>
          <tbody>
            {costs.map((c) => (
              <tr key={c.agent}>
                <td className="cell-name">{c.agent}</td>
                <td>{fmtNum(c.tokens)}</td>
                <td>
                  <span className="pct-cell">
                    <span className="pct-track">
                      <span className="pct-fill" style={{ width: `${pctToNum(c.pct)}%` }} />
                    </span>
                    <span>{fmtRate(c.pct)}</span>
                  </span>
                </td>
                <td>{fmtNum(c.calls)}</td>
                <td>{fmtMs(c.duration_ms)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    )
  }

  function renderTools() {
    const tools = report.tool_reliability || []
    if (tools.length === 0) return <div className="empty-hint">暂无工具可靠性数据</div>
    return (
      <div className="admin-table-wrap">
        <table className="admin-table">
          <thead>
            <tr>
              <th>工具</th>
              <th>调用次数</th>
              <th>错误数</th>
              <th>错误率</th>
              <th>平均耗时</th>
              <th>重试</th>
            </tr>
          </thead>
          <tbody>
            {tools.map((t) => (
              <tr key={t.tool}>
                <td className="cell-name">{t.tool}</td>
                <td>{fmtNum(t.calls)}</td>
                <td>{fmtNum(t.errors)}</td>
                <td className={pctToNum(t.error_rate) > 20 ? 'cell-danger' : ''}>
                  {fmtRate(t.error_rate)}
                </td>
                <td>{fmtMs(t.avg_duration_ms)}</td>
                <td>{fmtNum(t.retries)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    )
  }

  function renderIntent() {
    const ir = report.intent_recognition || {}
    const detail = ir.detail || []
    if (detail.length === 0) return <div className="empty-hint">未查询到数据</div>
    return (
      <div className="admin-table-wrap">
        <div className="intent-summary">
          <span>
            总标注 <b>{fmtNum(ir.total)}</b> 条
          </span>
          <span>
            识别一致 <b>{fmtNum(ir.match)}</b> 条
          </span>
          <span>
            识别成功率 <b className={pctToNum(ir.success_rate) >= 80 ? 'cell-ok' : pctToNum(ir.success_rate) >= 50 ? 'cell-warn' : 'cell-danger'}>{fmtRate(ir.success_rate)}</b>
          </span>
        </div>
        <table className="admin-table">
          <thead>
            <tr>
              <th>意图</th>
              <th>标注数</th>
              <th>识别一致</th>
              <th>成功率</th>
            </tr>
          </thead>
          <tbody>
            {detail.map((d) => (
              <tr key={d.intent}>
                <td className="cell-name">{d.intent}</td>
                <td>{fmtNum(d.total)}</td>
                <td>{fmtNum(d.match)}</td>
                <td className={pctToNum(d.rate) >= 80 ? 'cell-ok' : pctToNum(d.rate) >= 50 ? 'cell-warn' : 'cell-danger'}>
                  {fmtRate(d.rate)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    )
  }

  function renderErrors() {
    const samples = report.error_samples || []
    if (samples.length === 0) return <div className="empty-hint">近期没有错误记录</div>
    return (
      <div className="error-list">
        {samples.map((s, i) => (
          <div className="error-item" key={i}>
            <div className="error-item-top">
              <span className={`err-badge err-${errTone(s.type)}`}>{s.type || '未知类型'}</span>
              <span className="error-time">{fmtTime(s.time)}</span>
            </div>
            <div className="error-message">{s.message}</div>
          </div>
        ))}
      </div>
    )
  }

  function renderTasks() {
    if (tasks.length === 0) return <div className="empty-hint">暂无任务记录</div>
    return (
      <div className="admin-table-wrap">
        <table className="admin-table">
          <thead>
            <tr>
              <th>任务 ID</th>
              <th>用户</th>
              <th>查询</th>
              <th>开始时间</th>
              <th>耗时</th>
              <th>状态</th>
            </tr>
          </thead>
          <tbody>
            {tasks.map((t) => {
              const [statusText, statusCls] = statusInfo(t.status)
              return (
                <tr key={t.task_id}>
                  <td className="cell-id">{t.task_id}</td>
                  <td>{t.user_id || '—'}</td>
                  <td className="cell-query" title={t.user_query}>
                    {shortQuery(t.user_query)}
                  </td>
                  <td>{fmtTime(t.start_ts)}</td>
                  <td>{fmtMs(t.duration_ms)}</td>
                  <td>
                    <span className={`status-badge ${statusCls}`}>{statusText}</span>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    )
  }

  return (
    <div className="admin-page">
      <header className="admin-topbar">
        <div className="admin-brand">
          <span className="admin-brand-icon">🛡️</span>
          <div>
            <div className="admin-brand-title">监控看板</div>
            <div className="admin-brand-sub">旅游规划助手 · 系统后台</div>
          </div>
        </div>
        <div className="admin-topbar-right">
          <span className="admin-user">
            {username}
            <span className={`role-badge ${isSuper ? 'role-super' : 'role-admin'}`}>
              {isSuper ? '超级管理员' : '管理员'}
            </span>
          </span>
          <button className="admin-logout" onClick={handleLogout}>
            退出登录
          </button>
        </div>
      </header>

      <main className="admin-main">
        {isSuper && (
          <section className="admin-card">
            <div className="admin-card-head">
              <h2 className="admin-card-title">管理员账号管理</h2>
              <span className="admin-card-hint">仅超级管理员可见</span>
            </div>

            {adminsError && <div className="admin-error">{adminsError}</div>}

            <form className="admin-create-form" onSubmit={handleCreateAdmin}>
              <input
                className="admin-input"
                placeholder="新管理员用户名"
                value={createUsername}
                onChange={(e) => setCreateUsername(e.target.value)}
                autoComplete="off"
              />
              <input
                className="admin-input"
                type="password"
                placeholder="密码（至少 6 位）"
                value={createPassword}
                onChange={(e) => setCreatePassword(e.target.value)}
                autoComplete="new-password"
              />
              <button
                className="btn-primary"
                type="submit"
                disabled={creating || !createUsername.trim() || createPassword.length < 6}
              >
                {creating ? '创建中…' : '创建管理员'}
              </button>
            </form>
            {createError && <div className="admin-error">{createError}</div>}

            <div className="admin-user-list">
              {admins.length === 0 && !adminsError ? (
                <div className="empty-hint">暂无管理员账号</div>
              ) : (
                admins.map((a) => {
                  const isSuperRow = a.role === 'superadmin'
                  const isSelf = a.username === username
                  const confirming = confirmDelete === a.username
                  return (
                    <div className="admin-user-row" key={a.username}>
                      <div className="admin-user-info">
                        <span className="admin-user-name">{a.username}</span>
                        <span className={`role-badge ${isSuperRow ? 'role-super' : 'role-admin'}`}>
                          {isSuperRow ? '超级管理员' : '管理员'}
                        </span>
                        {isSelf && <span className="tag-self">当前账号</span>}
                      </div>
                      <div className="admin-user-meta">
                        <span className="admin-user-created">
                          创建于 {fmtTime(a.created_at)}
                        </span>
                        {isSuperRow || isSelf ? null : (
                          <>
                            <button
                              className={`btn-delete${confirming ? ' confirming' : ''}`}
                              onClick={() =>
                                confirming
                                  ? handleDeleteAdmin(a.username)
                                  : setConfirmDelete(a.username)
                              }
                            >
                              {confirming ? '确认删除' : '删除'}
                            </button>
                            {confirming && (
                              <button
                                className="btn-cancel"
                                onClick={() => setConfirmDelete('')}
                              >
                                取消
                              </button>
                            )}
                          </>
                        )}
                      </div>
                    </div>
                  )
                })
              )}
            </div>
          </section>
        )}

        {reportError ? (
          <section className="admin-state">
            <div className="admin-state-icon">⚠️</div>
            <div className="admin-state-title">监控数据加载失败</div>
            <div className="admin-state-desc">{reportError}</div>
            <button className="btn-primary" onClick={loadReport}>
              重试
            </button>
          </section>
        ) : reportLoading ? (
          <div className="admin-state">
            <div className="admin-spinner" />
            <div className="admin-state-desc">正在加载监控数据…</div>
          </div>
        ) : report ? (
          <>
            <section className="metric-grid">
              {metrics.map((m) => (
                <div className={`metric-card ${m.cls}`} key={m.label}>
                  <span className="metric-label">{m.label}</span>
                  <span className="metric-value">{m.value}</span>
                </div>
              ))}
            </section>

            <section className="admin-version-filter">
              <label className="admin-version-label">版本</label>
              <select
                className="admin-input admin-version-select"
                value={version}
                onChange={handleVersionChange}
              >
                <option value="">全部版本</option>
                {versions.map((v) => (
                  <option key={v} value={v}>
                    {v}
                  </option>
                ))}
              </select>
              {versions.length > 0 && (
                <span className="admin-version-hint">按代码版本筛选观测指标</span>
              )}
            </section>

            <section className="admin-card">
              <div className="admin-tabs">
                {TABS.map((t) => (
                  <button
                    key={t.id}
                    type="button"
                    className={activeTab === t.id ? 'admin-tab active' : 'admin-tab'}
                    onClick={() => setActiveTab(t.id)}
                  >
                    {t.label}
                  </button>
                ))}
              </div>
              <div className="admin-tab-panel">
                {activeTab === 'nodes' && renderNodes()}
                {activeTab === 'nodeTools' && renderNodeTools()}
                {activeTab === 'models' && renderModels()}
                {activeTab === 'cost' && renderCost()}
                {activeTab === 'tools' && renderTools()}
                {activeTab === 'intent' && renderIntent()}
                {activeTab === 'errors' && renderErrors()}
                {activeTab === 'tasks' && renderTasks()}
              </div>
            </section>
          </>
        ) : null}
      </main>
    </div>
  )
}
