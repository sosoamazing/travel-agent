// 认证令牌的本地持久化（localStorage）。
const TOKEN_KEY = 'travel_agent_token'
const USERNAME_KEY = 'travel_agent_username'
const ROLE_KEY = 'travel_agent_role'

export function getToken() {
  return localStorage.getItem(TOKEN_KEY)
}

export function setAuth(token, username, role) {
  localStorage.setItem(TOKEN_KEY, token)
  localStorage.setItem(USERNAME_KEY, username)
  if (role) {
    localStorage.setItem(ROLE_KEY, role)
  } else {
    localStorage.removeItem(ROLE_KEY)
  }
}

export function clearAuth() {
  localStorage.removeItem(TOKEN_KEY)
  localStorage.removeItem(USERNAME_KEY)
  localStorage.removeItem(ROLE_KEY)
}

export function getUsername() {
  return localStorage.getItem(USERNAME_KEY)
}

export function getRole() {
  return localStorage.getItem(ROLE_KEY)
}
