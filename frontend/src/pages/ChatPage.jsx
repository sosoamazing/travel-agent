import { useEffect, useRef, useState } from 'react'
import { api, streamChat } from '../api'
import { clearAuth, getUsername } from '../auth'
import Markdown from '../components/Markdown'

const WELCOME_MESSAGE = `您好！我是智能旅游规划助手🗺️

我可以帮您：
- 📍 查询景点攻略和美食推荐
- 🚆 查询火车票和航班信息
- 🏨 推荐酒店和住宿
- ☀️ 查询天气预报
- 🗓️ 查询黄历吉日
- 🚗 规划自驾路线

请告诉我您的旅行需求吧！`

export default function ChatPage({ onLogout }) {
  const [sessions, setSessions] = useState([])
  const [currentSessionId, setCurrentSessionId] = useState(null)
  const [messages, setMessages] = useState([])
  const [input, setInput] = useState('')
  const [sending, setSending] = useState(false)
  const [progress, setProgress] = useState('')
  const [health, setHealth] = useState(null)
  const [backendError, setBackendError] = useState('')

  const listRef = useRef(null)

  useEffect(() => {
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight })
  }, [messages, progress])

  async function loadHealth() {
    try {
      setHealth(await api.health())
      setBackendError('')
    } catch (err) {
      setHealth(null)
      setBackendError(err.message)
    }
  }

  async function loadSessionsAndMessages() {
    try {
      const list = await api.listSessions()
      setSessions(list)
      if (list.length > 0) {
        const sid = list[0].session_id
        setCurrentSessionId(sid)
        setMessages(await api.getMessages(sid))
      } else {
        const { session_id } = await api.createSession()
        setCurrentSessionId(session_id)
        setMessages([])
      }
      setBackendError('')
    } catch (err) {
      setBackendError(err.message)
    }
  }

  useEffect(() => {
    loadHealth()
    loadSessionsAndMessages()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  async function handleNewChat() {
    try {
      const { session_id } = await api.createSession()
      setCurrentSessionId(session_id)
      setMessages([])
      const list = await api.listSessions()
      setSessions(list)
    } catch (err) {
      setBackendError(err.message)
    }
  }

  async function handleSelectSession(sessionId) {
    if (sessionId === currentSessionId) return
    try {
      setCurrentSessionId(sessionId)
      setMessages(await api.getMessages(sessionId))
    } catch (err) {
      setBackendError(err.message)
    }
  }

  async function handleDeleteSession(sessionId) {
    if (!window.confirm('确定删除该对话吗？')) return
    try {
      await api.deleteSession(sessionId)
      if (sessionId === currentSessionId) {
        setCurrentSessionId(null)
        setMessages([])
      }
      setSessions(await api.listSessions())
    } catch (err) {
      setBackendError(err.message)
    }
  }

  function handleLogout() {
    clearAuth()
    window.location.hash = '#/'
    onLogout()
  }

  async function handleSend() {
    const text = input.trim()
    if (!text || sending) return
    setInput('')

    let sid = currentSessionId
    if (!sid) {
      try {
        const { session_id } = await api.createSession()
        sid = session_id
        setCurrentSessionId(sid)
      } catch (err) {
        setBackendError(err.message)
        return
      }
    }

    const userMsg = { role: 'user', content: text }
    const assistantMsg = { role: 'assistant', content: '' }
    setMessages((prev) => [...prev, userMsg, assistantMsg])
    setSending(true)
    setProgress('提交中…')

    const updateAssistant = (content) => {
      setMessages((prev) => {
        const next = [...prev]
        next[next.length - 1] = { role: 'assistant', content }
        return next
      })
    }

    try {
      const { task_id } = await api.chat(text, sid)
      let finalText = ''
      let gotFinal = false
      let streamError = null

      await streamChat(task_id, {
        onToken: (t) => {
          finalText += t
          updateAssistant(finalText)
        },
        onProgress: (node) => setProgress(`正在执行 ${node}`),
        onFinal: (evt) => {
          gotFinal = true
          finalText = (evt.state && evt.state.final_answer) || evt.text || finalText
          updateAssistant(finalText)
        },
        onError: (msg) => {
          streamError = msg
        },
      })

      // 兜底：SSE 提前断开但任务已完成时，用轮询接口取最终结果
      if (!gotFinal && !streamError) {
        try {
          const snapshot = await api.getTask(task_id)
          if (snapshot.status === 'succeeded') {
            finalText = (snapshot.result && snapshot.result.final_answer) || finalText
            updateAssistant(finalText)
          } else if (snapshot.status === 'failed') {
            streamError = snapshot.error
          }
        } catch {
          /* 忽略轮询失败 */
        }
      }

      if (streamError) {
        updateAssistant(`处理出错：${streamError}`)
      } else if (!finalText) {
        updateAssistant('处理完成，但没有生成回答。')
      }

      loadSessionsAndMessages()
    } catch (err) {
      updateAssistant(`请求后端服务失败：${err.message}`)
    } finally {
      setProgress('')
      setSending(false)
    }
  }

  const dbOk = health ? health.db?.ok : null
  const mcpCount = health ? (health.mcp?.servers?.length ?? 0) : 0

  return (
    <div className="chat-layout">
      <aside className="sidebar">
        <div className="sidebar-header">
          <div className="brand">🗺️ 智能旅游规划助手</div>
          <div className="user-row">
            <span className="username">{getUsername()}</span>
            <button className="link-btn" onClick={handleLogout}>
              退出
            </button>
          </div>
        </div>

        <button className="new-chat" onClick={handleNewChat}>
          ➕ 新建对话
        </button>

        <div className="session-list">
          {sessions.length === 0 ? (
            <div className="session-empty">还没有历史对话</div>
          ) : (
            sessions.map((s) => (
              <div
                key={s.session_id}
                className={
                  'session-item' + (s.session_id === currentSessionId ? ' active' : '')
                }
              >
                <button
                  type="button"
                  className="session-item-main"
                  onClick={() => handleSelectSession(s.session_id)}
                >
                  <span className="session-title">{s.title}</span>
                  <span className="session-count">{s.message_count} 条</span>
                </button>
                <button
                  type="button"
                  className="session-delete"
                  title="删除对话"
                  onClick={(e) => {
                    e.stopPropagation()
                    handleDeleteSession(s.session_id)
                  }}
                >
                  ✕
                </button>
              </div>
            ))
          )}
        </div>

        <div className="health">
          <div className="health-title">🛠️ 服务状态</div>
          {backendError ? (
            <div className="health-warn">后端未连接：{backendError}</div>
          ) : health ? (
            <>
              <div>后端状态：{health.status || 'unknown'}</div>
              <div>数据库：{dbOk ? '正常' : '异常'}</div>
              <div>MCP 服务器：{mcpCount} 个</div>
            </>
          ) : (
            <div className="health-warn">正在连接后端…</div>
          )}
        </div>
      </aside>

      <main className="main">
        <div className="message-list" ref={listRef}>
          {messages.length === 0 ? (
            <div className="welcome">
              <div className="bubble assistant">
                <Markdown>{WELCOME_MESSAGE}</Markdown>
              </div>
            </div>
          ) : (
            messages.map((m, i) => (
              <div key={i} className={'row ' + m.role}>
                <div className={'bubble ' + m.role}>
                  {m.role === 'assistant' ? (
                    m.content ? (
                      <Markdown>{m.content}</Markdown>
                    ) : sending && i === messages.length - 1 ? (
                      '▌'
                    ) : null
                  ) : (
                    m.content
                  )}
                </div>
              </div>
            ))
          )}
        </div>

        {progress && <div className="progress">⏳ {progress}</div>}

        <div className="input-bar">
          <textarea
            className="chat-input"
            rows={1}
            value={input}
            placeholder="请输入您的旅行需求，例如：我想12月去杭州玩3天"
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault()
                handleSend()
              }
            }}
            disabled={sending}
          />
          <button className="send-btn" onClick={handleSend} disabled={sending || !input.trim()}>
            发送
          </button>
        </div>
      </main>
    </div>
  )
}
