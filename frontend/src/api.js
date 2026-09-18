import { clearAuth, getToken } from './auth'

// 后端 FastAPI 地址（可用 VITE_API_BASE_URL 覆盖）
const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || 'http://127.0.0.1:8000'

async function request(path, { method = 'GET', body, auth = true } = {}) {
  const headers = {}
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  if (auth) {
    const token = getToken()
    if (token) headers['Authorization'] = `Bearer ${token}`
  }

  const res = await fetch(`${API_BASE_URL}${path}`, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })

  if (res.status === 401 && auth) {
    clearAuth()
    window.location.href = '/'
    throw new Error('登录已过期，请重新登录')
  }
  if (res.status === 204) {
    return null
  }
  if (!res.ok) {
    let detail = `HTTP ${res.status}`
    try {
      const data = await res.json()
      if (data.detail) {
        detail = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail)
      }
    } catch {
      /* 忽略非 JSON 响应 */
    }
    throw new Error(detail)
  }
  return res.json()
}

export const api = {
  register: (username, password) =>
    request('/auth/register', { method: 'POST', body: { username, password }, auth: false }),
  login: (username, password) =>
    request('/auth/login', { method: 'POST', body: { username, password }, auth: false }),
  adminLogin: (username, password) =>
    request('/auth/admin/login', { method: 'POST', body: { username, password }, auth: false }),
  listAdmins: () => request('/auth/admin/users'),
  createAdmin: (username, password) =>
    request('/auth/admin/users', { method: 'POST', body: { username, password } }),
  deleteAdmin: (username) =>
    request(`/auth/admin/users/${encodeURIComponent(username)}`, { method: 'DELETE' }),
  adminReport: (version) =>
    request(`/admin/report${version ? `?version=${encodeURIComponent(version)}` : ''}`),
  adminTasks: (limit = 50, version) =>
    request(
      `/admin/tasks?limit=${limit}${version ? `&version=${encodeURIComponent(version)}` : ''}`,
    ),
  adminVersions: () => request('/admin/versions'),
  adminReleases: (limit = 50) =>
    request(`/admin/releases?limit=${limit}`),
  me: () => request('/auth/me'),
  health: () => request('/health', { auth: false }),
  listSessions: (limit = 50) => request(`/sessions?limit=${limit}`),
  createSession: () => request('/sessions', { method: 'POST', body: {} }),
  deleteSession: (sessionId) =>
    request(`/sessions/${encodeURIComponent(sessionId)}`, { method: 'DELETE' }),
  getMessages: (sessionId) => request(`/sessions/${sessionId}/messages`),
  chat: (userQuery, sessionId) =>
    request('/chat', { method: 'POST', body: { user_query: userQuery, session_id: sessionId } }),
  getTask: (taskId) => request(`/tasks/${taskId}`),
  getTaskEvents: (taskId, sinceTs = 0, includePayload = false) =>
    request(
      `/tasks/${encodeURIComponent(taskId)}/events?since_ts=${sinceTs}&include_payload=${includePayload}`,
    ),
  adminTrace: (taskId) => request(`/obs/${encodeURIComponent(taskId)}/trace`),
}

// 消费后端 SSE 流（用 fetch 读取，以支持 Authorization 头，EventSource 无法携带自定义头）
export async function streamChat(taskId, { onToken, onProgress, onFinal, onError } = {}) {
  const token = getToken()
  const res = await fetch(`${API_BASE_URL}/tasks/${taskId}/stream`, {
    headers: { Authorization: `Bearer ${token}` },
  })
  if (res.status === 401) {
    clearAuth()
    window.location.href = '/'
    throw new Error('登录已过期，请重新登录')
  }
  if (!res.ok) throw new Error(`HTTP ${res.status}`)

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const parts = buffer.split('\n\n')
    buffer = parts.pop()
    for (const part of parts) {
      const line = part.trim()
      if (!line.startsWith('data:')) continue
      const data = line.slice(5).trim()
      if (!data) continue
      let evt
      try {
        evt = JSON.parse(data)
      } catch {
        continue
      }
      if (evt.type === 'token') onToken?.(evt.text)
      else if (evt.type === 'progress') onProgress?.(evt.node)
      else if (evt.type === 'final') onFinal?.(evt)
      else if (evt.type === 'error') onError?.(evt.text)
    }
  }
}
