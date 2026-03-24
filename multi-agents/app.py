"""
智能旅游规划助手 - Streamlit UI (Multi-Agents版本)
基于LangChain + LangGraph的Multi-Agents架构
"""
import streamlit as st
import tempfile
import os
import asyncio
import warnings
import logging
import sys
from typing import List, Dict, Any

# 配置日志来静默MCP客户端的警告
logging.getLogger('mcp').setLevel(logging.ERROR)
logging.getLogger('anyio').setLevel(logging.ERROR)
logging.getLogger('asyncio').setLevel(logging.ERROR)

# suppress warnings
warnings.filterwarnings('ignore', category=DeprecationWarning)
warnings.filterwarnings('ignore', message='.*LangChainDeprecationWarning.*')
warnings.filterwarnings('ignore', category=RuntimeWarning, message='.*async_generator.*')
warnings.filterwarnings('ignore', category=RuntimeWarning, message=".*generator didn't stop.*")
warnings.filterwarnings('ignore', category=RuntimeWarning, message='.*unhandled errors in a TaskGroup.*')
warnings.filterwarnings('ignore', category=RuntimeWarning, message='.*Attempted to exit cancel scope.*')

# 全局异常钩子来静默MCP客户端清理错误
def custom_excepthook(type, value, traceback):
    # 静默MCP SSE客户端的清理错误
    if type.__name__ in ['RuntimeError', 'GeneratorExit', 'BaseExceptionGroup']:
        error_str = str(value)
        if any(keyword in error_str.lower() for keyword in [
            'async_generator', 'generator didn\'t stop', 
            'taskgroup', 'cancel scope', 'sse_client'
        ]):
            return
    # 其他错误正常打印
    sys.__excepthook__(type, value, traceback)

sys.excepthook = custom_excepthook

# 异步任务异常处理
def handle_task_exception(loop, context):
    exception = context.get('exception')
    if exception:
        error_str = str(exception)
        if any(keyword in error_str.lower() for keyword in [
            'async_generator', 'generator didn\'t stop', 
            'taskgroup', 'cancel scope', 'sse_client'
        ]):
            return
    # 其他异常正常处理
    loop.default_exception_handler(context)
from langchain_community.document_loaders import (
    TextLoader,
    PyPDFLoader,
    CSVLoader,
    DirectoryLoader
)
from langchain_chroma import Chroma
from langchain_text_splitters import (
    RecursiveCharacterTextSplitter,
    CharacterTextSplitter
)
from langchain_community.embeddings import DashScopeEmbeddings
from langchain_core.messages import HumanMessage, AIMessage
from dotenv import load_dotenv

# 设置页面配置（必须是第一个Streamlit命令）
st.set_page_config(
    page_title="🗺️ 智能旅游规划助手 (Multi-Agents)",
    page_icon="🗺️",
    layout="wide"
)

# 加载环境变量
env_path = os.path.join(os.path.dirname(__file__), '.env')
if not os.path.exists(env_path):
    env_path = os.path.join(os.path.dirname(__file__), '..', 'aggentic_RAG', '.env')
load_dotenv(dotenv_path=env_path, override=True)

# 添加项目路径到sys.path
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 导入Multi-Agents相关模块（提前导入，避免循环依赖）
from graph.workflow import travel_graph
from graph.state import GlobalState
from chat_history_manager import get_chat_history_manager

def initialize_multi_agents_state() -> GlobalState:
    """初始化Multi-Agents全局状态 - 按新架构"""
    return {
        "user_query": None,
        "messages": [],
        
        # 各 Agent 自己的上下文
        "planner_context": None,
        "executor_context": None,
        "summarizer_context": None,
        
        # 控制流
        "current_agent": None,
        "next_agent": None,
        "is_complete": False
    }

# 初始化聊天历史管理器
chat_manager = get_chat_history_manager()

# 确保有当前会话（在侧边栏之前初始化）
if "current_session_id" not in st.session_state:
    last_session = chat_manager.get_last_session()
    if last_session:
        st.session_state.current_session_id = last_session.session_id
        # 加载历史消息
        messages = chat_manager.get_session_messages(last_session.session_id)
        st.session_state.messages = chat_manager.messages_to_streamlit_format(messages)
        print(f"✅ 已加载会话 {last_session.session_id}，共 {len(messages)} 条消息")
    else:
        # 创建新会话
        st.session_state.current_session_id = chat_manager.create_session()
        # 添加欢迎消息
        welcome_msg = {"role": "assistant", "content": "您好！我是智能旅游规划助手🗺️ (Multi-Agents版本)\n\n我可以帮您：\n- 📍 查询景点攻略和美食推荐\n- 🚆 查询火车票和航班信息\n- 🏨 推荐酒店和住宿\n- ☀️ 查询天气预报\n- 🗓️ 查询黄历吉日\n- 🚗 规划自驾路线\n\n请告诉我您的旅行需求吧！"}
        st.session_state.messages = [welcome_msg]
        # 保存欢迎消息到数据库
        chat_manager.add_message(
            session_id=st.session_state.current_session_id,
            message_type="ai",
            content=welcome_msg["content"]
        )
        print(f"✅ 已创建新会话 {st.session_state.current_session_id}")

# 初始化聊天历史
if "messages" not in st.session_state:
    st.session_state.messages = []

if "multi_agents_state" not in st.session_state:
    st.session_state.multi_agents_state = initialize_multi_agents_state()

# 应用标题
st.title("🗺️ 智能旅游规划助手 (Multi-Agents)")
st.markdown("基于Multi-Agents架构的旅游规划系统")

# 侧边栏 - 文档上传和历史会话
with st.sidebar:
    st.header("📚 旅游攻略文档")
    
    # 上传多种格式的文件
    uploaded_files = st.file_uploader(
        label="上传旅游攻略文档",
        type=["txt", "pdf", "csv"],
        accept_multiple_files=True,
        help="支持 TXT、PDF、CSV 格式的文档"
    )
    
    st.markdown("---")
    
    # 历史会话
    st.header("💬 历史会话")
    chat_manager = get_chat_history_manager()
    
    # 新建会话按钮
    if st.button("➕ 新建对话", use_container_width=True):
        new_session_id = chat_manager.create_session()
        st.session_state.current_session_id = new_session_id
        st.session_state.messages = []
        st.session_state.multi_agents_state = initialize_multi_agents_state()
        st.rerun()
    
    # 获取历史会话列表
    sessions = chat_manager.get_user_sessions(limit=50)
    print(f"📊 找到 {len(sessions)} 个历史会话")
    
    if sessions:
        # 显示会话列表
        session_options = {
            session.session_id: f"{session.title} ({session.message_count}条消息)"
            for session in sessions
        }
        
        # 会话选择器
        selected_session_id = st.selectbox(
            "选择历史对话",
            options=list(session_options.keys()),
            format_func=lambda x: session_options[x],
            index=list(session_options.keys()).index(st.session_state.current_session_id) 
            if st.session_state.current_session_id in session_options 
            else 0,
            key="session_selector"
        )
        
        # 如果选择了不同的会话，加载它
        if selected_session_id != st.session_state.current_session_id:
            print(f"🔄 切换到会话: {selected_session_id}")
            st.session_state.current_session_id = selected_session_id
            
            # 加载会话消息
            messages = chat_manager.get_session_messages(selected_session_id)
            print(f"📥 加载到 {len(messages)} 条消息")
            st.session_state.messages = chat_manager.messages_to_streamlit_format(messages)
            
            # 如果没有消息，添加欢迎消息
            if not st.session_state.messages:
                print("⚠️ 会话没有消息，添加欢迎消息")
                welcome_msg = {"role": "assistant", "content": "您好！我是智能旅游规划助手🗺️ (Multi-Agents版本)\n\n我可以帮您：\n- 📍 查询景点攻略和美食推荐\n- 🚆 查询火车票和航班信息\n- 🏨 推荐酒店和住宿\n- ☀️ 查询天气预报\n- 🗓️ 查询黄历吉日\n- 🚗 规划自驾路线\n\n请告诉我您的旅行需求吧！"}
                st.session_state.messages = [welcome_msg]
                # 保存欢迎消息到数据库
                chat_manager.add_message(
                    session_id=st.session_state.current_session_id,
                    message_type="ai",
                    content=welcome_msg["content"]
                )
            
            st.session_state.multi_agents_state = initialize_multi_agents_state()
            st.rerun()
        
        # 删除会话按钮
        if st.button("🗑️ 删除当前会话", use_container_width=True):
            chat_manager.delete_session(st.session_state.current_session_id)
            st.session_state.pop("current_session_id", None)
            st.session_state.messages = []
            st.session_state.multi_agents_state = initialize_multi_agents_state()
            st.rerun()
    else:
        st.info("还没有历史对话，开始新的对话吧！")
        # 初始化第一个会话
        if "current_session_id" not in st.session_state:
            st.session_state.current_session_id = chat_manager.create_session()
    
    st.markdown("---")
    
    # 系统配置
    st.header("⚙️ 系统配置")
    st.info("Multi-Agents架构自动优化执行流程")

# 文档上传现在是可选的
if not uploaded_files:
    st.info("💡 提示：您可以选择上传旅游攻略文档，或直接进行实时查询")

# 配置RAG检索器
@st.cache_resource(ttl="1h")
def configure_rag_retriever(uploaded_files):
    """配置RAG检索器 - 支持多种文件格式"""
    docs = []
    
    # 创建临时目录存储上传的文件
    temp_dir = tempfile.TemporaryDirectory()
    
    for file in uploaded_files:
        temp_filepath = os.path.join(temp_dir.name, file.name)
        with open(temp_filepath, "wb") as f:
            f.write(file.getvalue())
        
        # 根据文件类型选择合适的 loader
        file_extension = os.path.splitext(file.name)[1].lower()
        
        try:
            if file_extension == ".txt":
                loader = TextLoader(temp_filepath, encoding="utf-8")
            elif file_extension == ".pdf":
                loader = PyPDFLoader(temp_filepath)
            elif file_extension == ".csv":
                loader = CSVLoader(temp_filepath, encoding="utf-8")
            else:
                st.warning(f"跳过不支持的文件格式: {file.name}")
                continue
            
            docs.extend(loader.load())
            st.success(f"✅ 已加载: {file.name}")
        except Exception as e:
            st.error(f"加载 {file.name} 失败: {e}")
    
    # 文档分割
    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=500,
        chunk_overlap=100
    )
    splits = text_splitter.split_documents(docs)
    
    # 使用Qwen的Embedding模型
    embeddings = DashScopeEmbeddings(
        model="text-embedding-v3",
        dashscope_api_key=os.getenv("DASHSCOPE_API_KEY")
    )
    
    # 创建向量数据库
    vectordb = Chroma.from_documents(splits, embeddings)
    
    # 返回检索器
    return vectordb.as_retriever(search_kwargs={"k": 5})

# 配置检索器（如果有文档）
if uploaded_files:
    with st.spinner("正在加载文档并建立索引..."):
        retriever = configure_rag_retriever(uploaded_files)
        st.success(f"✅ 已加载 {len(uploaded_files)} 个文档")
else:
    retriever = None

# 启用嵌套asyncio支持（安全处理 uvloop）
try:
    import nest_asyncio
    import asyncio
    loop = asyncio.get_event_loop_policy().get_event_loop()
    if 'uvloop' not in str(type(loop)):
        nest_asyncio.apply()
    # 设置异步任务异常处理器
    loop.set_exception_handler(handle_task_exception)
except Exception:
    pass

# 创建全局event loop用于MCP调用
import asyncio
_mcp_loop = asyncio.new_event_loop()
_mcp_loop.set_exception_handler(handle_task_exception)
_mcp_manager = None

async def run_multi_agents(user_query: str, state: GlobalState = None) -> GlobalState:
    """
    运行Multi-Agents旅游规划系统 - 按新架构
    
    Args:
        user_query: 用户查询
        state: 可选的已有状态（用于多轮对话）
    
    Returns:
        更新后的状态
    """
    if state is None:
        state = initialize_multi_agents_state()
    
    # 每次新查询开始时：
    # 1. 保留全局对话历史（messages）
    # 2. 重置各子 Agent 的上下文
    state["user_query"] = user_query
    state["messages"].append(HumanMessage(content=user_query))
    state["is_complete"] = False
    state["current_agent"] = None
    state["next_agent"] = None
    
    # 重置各子 Agent 的上下文，让它们重新开始
    state["planner_context"] = None
    state["executor_context"] = None
    state["summarizer_context"] = None
    
    result = await travel_graph.ainvoke(state)
    
    # 把AI的回复也添加到对话历史中，支持多轮对话
    final_answer = None
    
    # 检查是否是 main_agent 直接返回的回答
    if result.get("is_complete") and result.get("messages"):
        # 遍历 messages 找最后一条 AI 消息
        for msg in reversed(result["messages"]):
            try:
                if isinstance(msg, AIMessage):
                    final_answer = msg.content
                    break
                elif hasattr(msg, 'type') and msg.type == 'ai':
                    final_answer = msg.content
                    break
            except Exception:
                continue
    
    # 如果 main_agent 没有直接回答，检查 summarizer 和 planner
    if not final_answer:
        if result.get("summarizer_context") and result["summarizer_context"].get("final_summary"):
            final_answer = result["summarizer_context"]["final_summary"]
        elif result.get("planner_context") and result["planner_context"].get("clarification_question"):
            final_answer = result["planner_context"]["clarification_question"]
    
    # 只有当 final_answer 不是已经在 messages 里时才添加
    if final_answer:
        # 检查是否已经在 messages 里了
        already_added = False
        for msg in result["messages"]:
            try:
                if isinstance(msg, AIMessage) and msg.content == final_answer:
                    already_added = True
                    break
                if hasattr(msg, 'type') and msg.type == 'ai' and getattr(msg, 'content', '') == final_answer:
                    already_added = True
                    break
            except Exception:
                continue
        
        if not already_added:
            result["messages"].append(AIMessage(content=final_answer))
    
    return result

# 显示历史消息
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

def summarize_chat_history(llm, middle_messages):
    """使用 LLM 总结中间对话的关键信息"""
    try:
        from langchain_openai import ChatOpenAI
        
        conversation_text = ""
        for msg in middle_messages:
            role = "用户" if msg["role"] == "user" else "助手"
            conversation_text += f"{role}: {msg['content']}\n"
        
        summary_prompt = f"""请为以下对话历史生成一个简洁的关键信息摘要，主要包括：
1. 用户的主要需求和问题
2. 已经查询过的信息（如景点、天气、交通等）
3. 重要的决策和结论

对话历史：
{conversation_text}

请用中文回答，摘要不要超过300字。"""
        
        response = llm.invoke(summary_prompt)
        return response.content
    except Exception as e:
        print(f"⚠️ 对话摘要生成失败: {e}")
        return "[... 中间对话摘要生成失败，已省略 ...]"

def compress_chat_history(messages, llm=None, max_recent_turns=6, summary_threshold=10, max_total_tokens=4000):
    """智能压缩聊天历史 - 支持 LLM 摘要"""
    total_turns = (len(messages) - 1) // 2
    
    if total_turns <= max_recent_turns or len(messages) <= max_recent_turns * 2 + 1:
        return messages
    
    compressed = []
    
    if messages:
        compressed.append(messages[0])
    
    recent_messages_count = min(max_recent_turns * 2, len(messages) - 1)
    start_idx = max(1, len(messages) - recent_messages_count)
    
    if start_idx > 1:
        middle_messages = messages[1:start_idx]
        
        if llm and total_turns >= summary_threshold:
            summary = summarize_chat_history(llm, middle_messages)
            compressed.append({
                "role": "assistant",
                "content": f"[对话摘要] {summary}"
            })
        else:
            compressed.append({
                "role": "assistant",
                "content": "[... 中间对话已省略 ...]"
            })
    
    compressed.extend(messages[start_idx:])
    
    total_chars = sum(len(msg.get("content", "")) for msg in compressed)
    char_limit = max_total_tokens * 4
    
    while total_chars > char_limit and len(compressed) > 3:
        if len(compressed) > 2 and not compressed[1].get("content", "").startswith("[对话摘要]") and compressed[1].get("content") != "[... 中间对话已省略 ...]":
            removed = compressed.pop(1)
            total_chars -= len(removed.get("content", ""))
        else:
            break
    
    return compressed

# 聊天输入框
if user_query := st.chat_input(placeholder="请输入您的旅行需求，例如：我想12月去杭州玩3天"):
    # 保存用户消息到数据库
    chat_manager.add_message(
        session_id=st.session_state.current_session_id,
        message_type="user",
        content=user_query
    )
    
    st.session_state.messages.append({"role": "user", "content": user_query})
    
    with st.chat_message("user"):
        st.markdown(user_query)
    
    with st.chat_message("assistant"):
        with st.spinner("🤖 Multi-Agents正在协作规划您的旅行..."):
            try:
                import asyncio
                
                # 运行Multi-Agents系统
                result = asyncio.run(
                    run_multi_agents(
                        user_query, 
                        st.session_state.multi_agents_state
                    )
                )
                
                # 更新状态
                st.session_state.multi_agents_state = result
                
                # 显示当前执行的Agent信息
                current_agent = result.get("current_agent", "unknown")
                st.info(f"🤖 当前处理Agent: {current_agent}")
                
                # 获取回答 - 从新的上下文结构
                answer = "处理完成，但没有生成回答。"
                if result.get("planner_context") and result["planner_context"].get("needs_clarification", False):
                    answer = result["planner_context"].get("clarification_question", "请提供更多信息")
                elif result.get("summarizer_context") and result["summarizer_context"].get("final_summary"):
                    answer = result["summarizer_context"]["final_summary"]
                elif result.get("is_complete") and result.get("messages"):
                    # 检查最后一条消息是否是AI的回复
                    last_msg = result["messages"][-1] if result["messages"] else None
                    if last_msg and hasattr(last_msg, 'type') and last_msg.type == 'ai':
                        answer = last_msg.content
                
                # 保存AI回答到数据库
                chat_manager.add_message(
                    session_id=st.session_state.current_session_id,
                    message_type="ai",
                    content=answer
                )
                
                # 添加助手消息
                st.session_state.messages.append({"role": "assistant", "content": answer})
                
                # 显示回答
                st.markdown(answer)
                
            except Exception as e:
                import traceback
                traceback.print_exc()
                error_msg = f"抱歉，Multi-Agents处理您的请求时出现错误：{str(e)}"
                st.error(error_msg)
                
                # 保存错误消息到数据库
                chat_manager.add_message(
                    session_id=st.session_state.current_session_id,
                    message_type="ai",
                    content=error_msg
                )
                
                st.session_state.messages.append({"role": "assistant", "content": error_msg})

# 侧边栏 - 显示当前配置
with st.sidebar:
    st.markdown("---")
    st.header("📊 当前配置")
    st.write(f"- 文档数量: {len(uploaded_files)}")
    st.write(f"- 对话轮数: {len(st.session_state.messages)}")
    
    st.markdown("---")
    st.header("🤖 Multi-Agents架构")
    st.write("包含以下Agent:")
    st.write("- 🏠 Main Agent (协调者)")
    st.write("- 📋 Planner Agent (规划者)")
    st.write("- ⚡ Executor Agent (执行者)")
    st.write("- 📝 Summarizer Agent (总结者)")
    st.write("- 💭 Feedback Agent (反馈者)")
