import { useEffect, useState } from 'react'
import { clearAuth, getRole, getToken } from './auth'
import AuthPage from './pages/AuthPage'
import ChatPage from './pages/ChatPage'
import AdminLoginPage from './pages/AdminLoginPage'
import AdminPage from './pages/AdminPage'

function currentHash() {
  return window.location.hash || '#/'
}

export default function App() {
  const [isLoggedIn, setIsLoggedIn] = useState(!!getToken())
  const [hash, setHash] = useState(currentHash())

  // 未登录时监听 hash 变化，在用户登录页与管理员登录页之间切换
  useEffect(() => {
    const onHashChange = () => setHash(currentHash())
    window.addEventListener('hashchange', onHashChange)
    return () => window.removeEventListener('hashchange', onHashChange)
  }, [])

  if (!isLoggedIn) {
    if (hash.startsWith('#/admin')) {
      return <AdminLoginPage onLogin={() => setIsLoggedIn(true)} />
    }
    return <AuthPage onLogin={() => setIsLoggedIn(true)} />
  }

  const role = getRole()
  const isAdmin = role === 'admin' || role === 'superadmin'

  function handleLogout() {
    clearAuth()
    setIsLoggedIn(false)
    window.location.hash = '#/'
  }

  if (isAdmin) {
    return <AdminPage onLogout={handleLogout} />
  }
  return <ChatPage onLogout={handleLogout} />
}
