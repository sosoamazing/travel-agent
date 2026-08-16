"""核心业务服务层（不依赖 Streamlit）。

供 FastAPI 路由直接调用，把 LangGraph 工作流、DB / MCP / Memory 的初始化与执行
从 UI 层彻底解耦。
"""
from core.service import get_service, TravelService

__all__ = ["get_service", "TravelService"]
