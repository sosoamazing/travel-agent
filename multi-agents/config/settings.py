"""
全局配置文件
"""
import os
import logging
from pathlib import Path
from dotenv import load_dotenv

# 项目根目录
PROJECT_ROOT = Path(__file__).parent.parent

# 加载环境变量（明确指定.env文件路径）
env_path = PROJECT_ROOT.parent / ".env"
load_dotenv(dotenv_path=env_path, override=True)

# DeepSeek API配置（用于R1推理模型）
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = "https://api.deepseek.com"

# DashScope API配置（用于Qwen模型和Embedding）
DASHSCOPE_API_KEY = os.getenv("DASHSCOPE_API_KEY", "")
QWEN3_API_BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1"

# LangSmith配置（可选，仅用于调试）
LANGCHAIN_TRACING_V2 = os.getenv("LANGCHAIN_TRACING_V2", "false")
LANGCHAIN_API_KEY = os.getenv("LANGCHAIN_API_KEY", "")

# 模型配置
QWEN3_MODEL = "qwen-plus"  # 使用DashScope API
QWEN3_TEMPERATURE = 0.7

R1_MODEL = "deepseek-reasoner"
R1_TEMPERATURE = 0.1

# Embedding模型 - 使用与原始项目相同的模型
EMBEDDING_MODEL = "text-embedding-v3"

# RAG配置
CHROMA_PERSIST_DIR = PROJECT_ROOT.parent / "aggentic_RAG" / "data" / "travel_vectordb"
RAG_CHUNK_SIZE = 500
RAG_CHUNK_OVERLAP = 50
RAG_SEARCH_K = 3
RAG_BATCH_SIZE = 10  # ChromaDB批量载入大小，如遇到API限制可调小

# MCP配置
MCP_CONFIG_PATH = str(PROJECT_ROOT / "config" / "servers_config.json")

# ========== 日志配置 ==========
LOG_DIR = PROJECT_ROOT.parent / "logs"
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging(level: str = None):
    """配置全局日志系统"""
    log_level = getattr(logging, (level or LOG_LEVEL).upper(), logging.INFO)

    # 确保日志目录存在
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    # 根 logger 配置
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    # 清除已有 handler，避免重复
    root_logger.handlers.clear()

    # 控制台 handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(log_level)
    console_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt=LOG_DATE_FORMAT
    ))
    root_logger.addHandler(console_handler)

    # 文件 handler
    file_handler = logging.FileHandler(
        LOG_DIR / "travel_agent.log",
        encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(
        LOG_FORMAT, datefmt=LOG_DATE_FORMAT
    ))
    root_logger.addHandler(file_handler)

    # 抑制第三方库的噪音
    for lib in ["mcp", "anyio", "asyncio", "chromadb", "httpx", "openai", "urllib3"]:
        logging.getLogger(lib).setLevel(logging.WARNING)
