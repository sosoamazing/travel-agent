"""
全局配置文件
"""
import os
import logging
import subprocess
from pathlib import Path
from dotenv import load_dotenv

# 项目根目录
PROJECT_ROOT = Path(__file__).parent.parent

# 加载环境变量（明确指定.env文件路径）
env_path = PROJECT_ROOT.parent / ".env"
load_dotenv(dotenv_path=env_path, override=True)

# DeepSeek API 配置（所有 LLM 调用统一走这里，key 取自 .env 的 OPENAI_API_KEY）
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.deepseek.com/v1")

# DashScope API配置（用于Qwen模型和Embedding）
DASHSCOPE_API_KEY = os.getenv("DASHSCOPE_API_KEY", "")
QWEN3_API_BASE = os.getenv("QWEN3_API_BASE", "https://dashscope.aliyuncs.com/compatible-mode/v1")

# LangSmith配置（可选，仅用于调试）
LANGCHAIN_TRACING_V2 = os.getenv("LANGCHAIN_TRACING_V2", "false")
LANGCHAIN_API_KEY = os.getenv("LANGCHAIN_API_KEY", "")

# 模型配置（默认值可在 .env 中覆盖）
QWEN3_MODEL = os.getenv("LLM_MAIN_MODEL", "deepseek-v4-pro")  # 主模型，已切换为 DeepSeek V4
QWEN3_TEMPERATURE = float(os.getenv("LLM_MAIN_TEMPERATURE", "0.7"))

# DeepSeek Flash - 轻量快速模型，适合 JSON 修正、价格估算等简单任务
DS_FLASH_MODEL = os.getenv("LLM_FLASH_MODEL", "deepseek-v4-flash")
DS_FLASH_TEMPERATURE = float(os.getenv("LLM_FLASH_TEMPERATURE", "0.1"))


def get_agent_model(agent: str, default: str) -> str:
    """按 agent 精确覆盖模型：.env 中 LLM_MODEL_<AGENT> 存在则优先，否则返回 default。

    例：LLM_MODEL_SUMMARIZER=deepseek-v4-flash 会让 summarizer 节点改用 Flash 模型；
    LLM_MODEL_JSON_FIX=qwen-plus 则反之。agent 名统一大写、'-' 转 '_'。
    """
    key = f"LLM_MODEL_{agent.upper().replace('-', '_')}"
    return os.getenv(key, default)


# ========== 重试 / 并发配置 ==========
# JSON 格式修正最大重试次数（tools/json_utils.fix_json_with_flash）
JSON_FIX_MAX_ATTEMPTS = int(os.getenv("JSON_FIX_MAX_ATTEMPTS", "3"))

# 管理员可查看的测试用户 id 白名单（逗号分隔；obs/trace 仅管理员可访问时，
# 用于放行这些测试用户名下任务的观测查询，便于压测/联调复盘）。
OBS_ADMIN_USER_IDS = {s.strip() for s in os.getenv("OBS_ADMIN_USER_IDS", "").split(",") if s.strip()}
# 城内规划并发上限（0 = 不限并发，全部城市同时执行）
CITY_PLAN_MAX_CONCURRENCY = int(os.getenv("CITY_PLAN_MAX_CONCURRENCY", "3"))
# 预算超支 / 时间非法时的最大自动重规划次数
PLAN_MAX_REPLAN = int(os.getenv("PLAN_MAX_REPLAN", "3"))

# Embedding模型 - 使用与原始项目相同的模型
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-v3")

# RAG配置
# ChromaDB 持久化目录：.env 可覆盖；默认指向仓库根下的 agentic_RAG/data/travel_vectordb
# （历史拼写错误 aggentic_RAG 已修正为 agentic_RAG）
CHROMA_PERSIST_DIR = Path(
    os.getenv("CHROMA_PERSIST_DIR")
    or str(PROJECT_ROOT.parent / "agentic_RAG" / "data" / "travel_vectordb")
)
RAG_CHUNK_SIZE = 500
RAG_CHUNK_OVERLAP = 50
# 检索数量 / 批量载入大小：运行时调优项，.env 可覆盖
RAG_SEARCH_K = int(os.getenv("RAG_SEARCH_K", "3"))
RAG_BATCH_SIZE = int(os.getenv("RAG_BATCH_SIZE", "10"))  # ChromaDB批量载入大小，如遇到API限制可调小

# MCP配置
# MCP servers 配置文件路径：.env 可覆盖（如 MCP_CONFIG_PATH=/etc/app/servers_config.json）
MCP_CONFIG_PATH = os.getenv(
    "MCP_CONFIG_PATH", str(PROJECT_ROOT / "config" / "servers_config.json")
)

# MCP 令牌桶限速配置（每个 MCP 工具独立一个桶）
# 每 MCP_RATE_LIMIT_WINDOW 秒最多 MCP_RATE_LIMIT_MAX 次 API 调用
# 默认每 0.4 秒 1 次，给网络抖动和时钟偏差留余量，避免服务端统计窗口内超限
MCP_RATE_LIMIT_MAX = int(os.getenv("MCP_RATE_LIMIT_MAX", "1"))
MCP_RATE_LIMIT_WINDOW = float(os.getenv("MCP_RATE_LIMIT_WINDOW", "0.4"))

# ========== 外部调用硬超时（秒） ==========
# 给 LLM / MCP 等不可控 I/O 加硬超时，防止单个慢请求/卡死拖垮 asyncio.gather 并发组。
# 不同模型/工具合理时延差异大，故做成环境变量可覆盖。
LLM_TIMEOUT_SEC = float(os.getenv("LLM_TIMEOUT_SEC", "120"))             # LLM 非流式 ainvoke 超时
LLM_STREAM_TIMEOUT_SEC = float(os.getenv("LLM_STREAM_TIMEOUT_SEC", "300"))  # LLM 流式 astream 总时长上限
MCP_TIMEOUT_SEC = float(os.getenv("MCP_TIMEOUT_SEC", "60"))              # 单个 MCP 工具调用超时

# ========== JWT 认证配置 ==========
# 生产环境务必通过 .env 覆盖 JWT_SECRET，默认值仅用于本地开发
JWT_SECRET = os.getenv("JWT_SECRET", "travel-agent-dev-secret-change-me")
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_MINUTES = int(os.getenv("JWT_EXPIRE_MINUTES", "1440"))  # 默认 24 小时

# ========== 超级管理员配置 ==========
# 首次启动自动创建超级管理员账号（仅当库中不存在任何 superadmin 角色用户时）
SUPERADMIN_USERNAME = os.getenv("SUPERADMIN_USERNAME", "admin")
SUPERADMIN_PASSWORD = os.getenv("SUPERADMIN_PASSWORD", "admin123456")

# ========== PostgreSQL 配置 ==========
PG_HOST = os.getenv("PG_HOST", "localhost")
PG_PORT = int(os.getenv("PG_PORT", "5432"))
PG_USER = os.getenv("PG_USER", "travel_agent")
PG_PASSWORD = os.getenv("PG_PASSWORD", "travel_agent")
PG_DATABASE = os.getenv("PG_DATABASE", "travel_agent")

# ========== Redis 任务态（可选；未配置则跳过，降级内存 + PG）==========
REDIS_URL = os.getenv("REDIS_URL", "").strip()
# 任务结束后 Redis Hash 过期时间（秒），覆盖「刚结束仍被轮询」窗口
TASK_REDIS_TTL_SEC = int(os.getenv("TASK_REDIS_TTL_SEC", "86400"))
# 渲染后的 LLM input 落库截断上限（字符）
LLM_PAYLOAD_MAX_CHARS = int(os.getenv("LLM_PAYLOAD_MAX_CHARS", "32768"))

# ========== 自驾费用配置（.env 可覆盖，业务参考价） ==========
# 92号汽油每升价格（元），2026 年参考价
FUEL_PRICE_PER_LITER = float(os.getenv("FUEL_PRICE_PER_LITER", "7.80"))
# 车辆百公里油耗（升/百公里），家用轿车参考值
FUEL_CONSUMPTION_PER_100KM = float(os.getenv("FUEL_CONSUMPTION_PER_100KM", "8.0"))
# 高速公路通行费（元/公里），全国平均约 0.5 元/公里
HIGHWAY_TOLL_PER_KM = float(os.getenv("HIGHWAY_TOLL_PER_KM", "0.50"))
# 自驾距离阈值（公里）：超过此距离不推荐自驾
DRIVING_MAX_DISTANCE_KM = float(os.getenv("DRIVING_MAX_DISTANCE_KM", "800"))

# ========== 版本号（AGENT_VERSION） ==========
# 任务级观测数据记录代码版本号。格式：{git commit 序号}-{短hash}（如 11-253e9ba）。
# commit 序号用 git rev-list --count 计算，每次提交严格 +1，天然有序，方便 monitor 按版本排序。
# 解析优先级：TRAVEL_AGENT_VERSION 环境变量 > git commit 序号+短hash > backend.__version__ > "unknown"。
# 所有异常一律吞掉，绝不允许因读 git 失败导致 import 崩溃。
def _resolve_agent_version() -> str:
    """解析当前代码版本号（commit 序号-短hash，有序）。"""
    env_ver = os.getenv("TRAVEL_AGENT_VERSION", "").strip()
    if env_ver:
        return env_ver
    try:
        # 先取 commit 总数（从仓库第一个 commit 到 HEAD 的数量，严格递增）
        result = subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=str(PROJECT_ROOT.parent),  # PROJECT_ROOT.parent 即 git 仓库根目录
            capture_output=True,
            text=True,
            timeout=5,
        )
        count = (result.stdout or "").strip()
        if result.returncode == 0 and count:
            # 再取短 hash 用于追溯具体 commit
            hash_res = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=str(PROJECT_ROOT.parent),
                capture_output=True,
                text=True,
                timeout=5,
            )
            short = (hash_res.stdout or "").strip()
            if hash_res.returncode == 0 and short:
                return f"{count}-{short}"
            return count
    except Exception:
        pass
    try:
        import backend
        ver = getattr(backend, "__version__", "")
        if ver:
            return str(ver)
    except Exception:
        pass
    return "unknown"


AGENT_VERSION = _resolve_agent_version()

# ========== 意图测试模式 ==========
# INTENT_TEST_MODE=1 测试模式：前端 intent 不参与路由，classify 用 LLM 自己分类（用于对比意图识别是否成功）
# INTENT_TEST_MODE=0 真实模式：前端带合法 intent 时直接用 intent 路由，跳过 LLM 分类
INTENT_TEST_MODE = os.getenv("INTENT_TEST_MODE", "0") == "1"

# ========== 日志配置 ==========
# 日志目录：.env 可覆盖（部署到容器/服务器时常用，如 LOG_DIR=/var/log/travel-agent）
LOG_DIR = Path(os.getenv("LOG_DIR", str(PROJECT_ROOT.parent / "logs")))
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
