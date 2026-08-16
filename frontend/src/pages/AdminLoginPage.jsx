import { useState } from 'react'
import { api } from '../api'
import { setAuth } from '../auth'

export default function AdminLoginPage({ onLogin }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  async function handleSubmit(e) {
    e.preventDefault()
    if (!username.trim() || !password) return
    setError('')
    setLoading(true)
    try {
      const data = await api.adminLogin(username.trim(), password)
      setAuth(data.access_token, data.username, data.role)
      window.location.hash = '#/admin'
      onLogin()
    } catch (err) {
      setError(err.message || '登录失败，请稍后再试')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="admin-login-wrap">
      <form className="admin-login-card" onSubmit={handleSubmit}>
        <div className="admin-login-logo">🛡️</div>
        <h1 className="admin-login-title">管理员登录</h1>
        <p className="admin-login-sub">管理后台仅供管理员访问，用于监控与账号管理</p>

        <label className="auth-label">用户名</label>
        <input
          className="admin-login-input"
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          placeholder="请输入管理员用户名"
          autoComplete="username"
        />

        <label className="auth-label">密码</label>
        <input
          className="admin-login-input"
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          placeholder="请输入密码"
          autoComplete="current-password"
        />

        {error && <div className="auth-error">{error}</div>}

        <button className="admin-login-submit" type="submit" disabled={loading}>
          {loading ? '正在验证…' : '登录'}
        </button>

        <a className="admin-login-back" href="#/">
          返回用户登录
        </a>
      </form>
    </div>
  )
}
