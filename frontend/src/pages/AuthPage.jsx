import { useState } from 'react'
import { api } from '../api'
import { setAuth } from '../auth'

export default function AuthPage({ onLogin }) {
  const [mode, setMode] = useState('login') // login | register
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  async function handleSubmit(e) {
    e.preventDefault()
    setError('')
    setLoading(true)
    try {
      if (mode === 'login') {
        const data = await api.login(username, password)
        setAuth(data.access_token, data.username, data.role)
      } else {
        await api.register(username, password)
        const data = await api.login(username, password)
        setAuth(data.access_token, data.username, data.role)
      }
      window.location.hash = '#/'
      onLogin()
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="auth-wrap">
      <form className="auth-card" onSubmit={handleSubmit}>
        <h1 className="auth-title">🗺️ 智能旅游规划助手</h1>
        <div className="auth-tabs">
          <button
            type="button"
            className={mode === 'login' ? 'auth-tab active' : 'auth-tab'}
            onClick={() => setMode('login')}
          >
            登录
          </button>
          <button
            type="button"
            className={mode === 'register' ? 'auth-tab active' : 'auth-tab'}
            onClick={() => setMode('register')}
          >
            注册
          </button>
        </div>

        <label className="auth-label">用户名</label>
        <input
          className="auth-input"
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          placeholder={mode === 'register' ? '3-32 位字母/数字/下划线' : '请输入用户名'}
          autoComplete="username"
        />

        <label className="auth-label">密码</label>
        <input
          className="auth-input"
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          placeholder={mode === 'register' ? '至少 6 位' : '请输入密码'}
          autoComplete={mode === 'register' ? 'new-password' : 'current-password'}
        />

        {error && <div className="auth-error">{error}</div>}

        <button className="auth-submit" type="submit" disabled={loading}>
          {loading ? '请稍候…' : mode === 'login' ? '登录' : '注册并登录'}
        </button>

        <div className="auth-foot">
          <a className="auth-admin-entry" href="#/admin">
            管理员登录
          </a>
        </div>
      </form>
    </div>
  )
}
