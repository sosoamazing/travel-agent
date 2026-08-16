"""
智能旅游规划助手 - Streamlit 前端（纯 UI，前后端分离）

本文件不再直接 import 任何业务模块（LangGraph / DB / MCP / Memory）。
所有后端调用统一走 HTTP 请求本机 FastAPI 网关（见 gateway/main.py）。

启动顺序：
    1) 先启动 backend：   uvicorn server:app --host 0.0.0.0 --port 8001 --app-dir .
    2) 再启动网关：       uvicorn gateway.main:app --host 0.0.0.0 --port 8000
    3) 最后启动前端：     streamlit run app.py
"""
import os
import json

import requests
import streamlit as st

# 后端地址（可用环境变量覆盖）
API_BASE_URL = os.getenv("API_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
USER_ID = os.getenv("TRAVEL_USER_ID", "default_user")

# 页面配置（必须是第一个 Streamlit 命令）
st.set_page_config(
    page_title="🗺️ 智能旅游规划助手",
    page_icon="🗺️",
    layout="wide",
)

WELCOME_MESSAGE = (
    "您好！我是智能旅游规划助手🗺️\n\n我可以帮您：\n- 📍 查询景点攻略和美食推荐\n"
    "- 🚆 查询火车票和航班信息\n- 🏨 推荐酒店和住宿\n- ☀️ 查询天气预报\n"
    "- 🗓️ 查询黄历吉日\n- 🚗 规划自驾路线\n\n请告诉我您的旅行需求吧！"
)

BACKEND_HINT = "请先启动网关服务：uvicorn gateway.main:app --host 0.0.0.0 --port 8000"


# ──────────────────────────────────────────────────────────
# HTTP 客户端辅助（后端调用统一走这里）
# ──────────────────────────────────────────────────────────

def _get(path: str, params: dict = None, timeout: int = 10):
    resp = requests.get(f"{API_BASE_URL}{path}", params=params, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _post(path: str, body: dict = None, timeout: int = 15):
    resp = requests.post(f"{API_BASE_URL}{path}", json=body, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _stream_chat(task_id: str):
    """消费 SSE 流：逐个 yield dict 事件（progress / token / final / error）。"""
    url = f"{API_BASE_URL}/tasks/{task_id}/stream"
    with requests.get(url, stream=True, timeout=(10, 600)) as resp:
        resp.raise_for_status()
        for raw in resp.iter_lines(decode_unicode=True):
            if not raw or not raw.startswith("data:"):
                continue
            data = raw[5:].strip()
            if data:
                yield json.loads(data)


def _create_new_session() -> str:
    session_id = _post("/sessions", body={"user_id": USER_ID})["session_id"]
    st.session_state.current_session_id = session_id
    st.session_state.messages = []
    return session_id


# ──────────────────────────────────────────────────────────
# 会话初始化
# ──────────────────────────────────────────────────────────

if "current_session_id" not in st.session_state:
    try:
        sessions = _get("/sessions", params={"user_id": USER_ID, "limit": 1})
        if sessions:
            sid = sessions[0]["session_id"]
            st.session_state.current_session_id = sid
            st.session_state.messages = _get(f"/sessions/{sid}/messages")
        else:
            _create_new_session()
    except Exception:
        # 后端未启动时给占位状态，避免页面直接崩溃
        st.session_state.current_session_id = None
        st.session_state.messages = []

if "messages" not in st.session_state:
    st.session_state.messages = []


# ──────────────────────────────────────────────────────────
# 页面标题
# ──────────────────────────────────────────────────────────

st.title("🗺️ 智能旅游规划助手")
st.markdown("基于 LangGraph 固定工作流的多智能体旅游规划系统（前后端分离）")


# ──────────────────────────────────────────────────────────
# 侧边栏：历史会话 + 服务状态
# ──────────────────────────────────────────────────────────

with st.sidebar:
    st.header("💬 历史会话")

    if st.button("➕ 新建对话", use_container_width=True):
        try:
            _create_new_session()
        except Exception as e:
            st.error(f"无法新建会话：{e}\n\n{BACKEND_HINT}")
        else:
            st.rerun()

    try:
        sessions = _get("/sessions", params={"user_id": USER_ID, "limit": 50})
    except Exception as e:
        sessions = []
        st.error(f"无法连接后端服务：{e}\n\n{BACKEND_HINT}")

    if sessions:
        session_options = {
            s["session_id"]: f"{s['title']} ({s['message_count']}条消息)"
            for s in sessions
        }
        current_sid = st.session_state.current_session_id
        selected_sid = st.selectbox(
            "选择历史对话",
            options=list(session_options.keys()),
            format_func=lambda x: session_options[x],
            index=list(session_options.keys()).index(current_sid)
            if current_sid in session_options else 0,
            key="session_selector",
        )
        if selected_sid != current_sid:
            st.session_state.current_session_id = selected_sid
            try:
                st.session_state.messages = _get(f"/sessions/{selected_sid}/messages")
            except Exception as e:
                st.error(f"加载会话失败：{e}")
            st.rerun()
    else:
        st.info("还没有历史对话，开始新的对话吧！")

    st.markdown("---")

    st.header("🛠️ 服务状态")
    try:
        health = _get("/health", timeout=5)
        db_ok = health.get("db", {}).get("ok")
        mcp_servers = health.get("mcp", {}).get("servers", [])
        st.write(f"- 后端状态：{health.get('status', 'unknown')}")
        st.write(f"- 数据库：{'正常' if db_ok else '异常'}")
        st.write(f"- MCP 服务器：{len(mcp_servers)} 个")
    except Exception:
        st.warning(f"后端未连接\n\n{BACKEND_HINT}")


# ──────────────────────────────────────────────────────────
# 聊天消息展示
# ──────────────────────────────────────────────────────────

if not st.session_state.messages:
    with st.chat_message("assistant"):
        st.markdown(WELCOME_MESSAGE)
else:
    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])


# ──────────────────────────────────────────────────────────
# 聊天输入
# ──────────────────────────────────────────────────────────

if user_query := st.chat_input(placeholder="请输入您的旅行需求，例如：我想12月去杭州玩3天"):
    # 确保有会话
    if not st.session_state.current_session_id:
        try:
            _create_new_session()
        except Exception as e:
            st.error(f"无法创建会话：{e}\n\n{BACKEND_HINT}")
            st.stop()

    st.session_state.messages.append({"role": "user", "content": user_query})
    with st.chat_message("user"):
        st.markdown(user_query)

    with st.chat_message("assistant"):
        placeholder = st.empty()
        status_placeholder = st.empty()
        full_response = ""
        result = None
        got_final = False
        error_text = None

        try:
            task_id = _post("/chat", body={
                "user_query": user_query,
                "session_id": st.session_state.current_session_id,
                "user_id": USER_ID,
            })["task_id"]

            for event in _stream_chat(task_id):
                etype = event.get("type")
                if etype == "token":
                    full_response += event.get("text", "")
                    placeholder.markdown(full_response + "▌")
                elif etype == "progress":
                    status_placeholder.caption(f"⏳ {event.get('node', '')}")
                elif etype == "final":
                    full_response = event.get("text") or full_response
                    result = event.get("state")
                    got_final = True
                    break
                elif etype == "error":
                    error_text = event.get("text")
                    break

            # 兜底：SSE 提前断开但任务已完成时，用轮询接口取最终结果
            if not got_final and not error_text:
                try:
                    snapshot = _get(f"/tasks/{task_id}")
                    if snapshot.get("status") == "succeeded":
                        result = snapshot.get("result")
                        full_response = (result or {}).get("final_answer") or full_response
                        got_final = True
                    elif snapshot.get("status") == "failed":
                        error_text = snapshot.get("error")
                except Exception:
                    pass

            status_placeholder.empty()

            if error_text:
                st.error(f"处理出错：{error_text}")
            else:
                answer = (result or {}).get("final_answer") or full_response or "处理完成，但没有生成回答。"
                placeholder.markdown(answer)
                st.session_state.messages.append({"role": "assistant", "content": answer})

        except Exception as e:
            status_placeholder.empty()
            error_msg = f"请求后端服务失败：{e}\n\n{BACKEND_HINT}"
            st.error(error_msg)
