# 🌍 智能旅行规划助手

基于 **LangChain + LangGraph + React + FastAPI + MCP + RAG** 的智能旅行规划系统。采用**固定 DAG 的 Multi-Agent 工作流**、**双模型协作**（DeepSeek V4 Pro + Flash）、**知识检索**和**实时数据查询**（12306 / 高德 / 八字 MCP），为用户生成完整的旅行方案，并通过微服务化拆分为 gateway / backend / monitor 三个进程。

## 📋 目录

- [项目简介](#项目简介)
- [核心功能](#核心功能)
- [技术架构](#技术架构)
- [系统要求](#系统要求)
- [安装部署](#安装部署)
- [使用指南](#使用指南)
- [安全说明](#安全说明)
- [API 接口](#api-接口)
- [数据库与观测](#数据库与观测)
- [项目结构](#项目结构)
- [开发说明](#开发说明)
- [故障排查](#故障排查)
- [贡献指南](#贡献指南)

---

## 🎯 项目简介

这是一个智能旅行规划系统，采用 **React 前端 + LangGraph Multi-Agent 工作流 + 微服务架构**。用户用自然语言提出旅行需求，系统自动完成意图分类、参数提取、跨城交通规划、多城市并发城内规划（景点 / 行程 / 酒店）、预算校验与自动重规划，最终生成完整的旅行方案。

### 主要特性

- **固定 DAG 工作流**（非动态 ReAct，链路可预测、可观测）：
  - 意图分类 → 对话 / 反馈 / 信息查询 / 旅行规划 四分支
  - 旅行规划 → 参数提取 → 澄清 / 简单查询 / 市内 / 跨城交通预检
  - 跨城交通 → LLM 按城市分配预算 → 全城市并发城内规划 → 总结

- **双模型协作**（按 agent 可单独覆盖）：
  - **DeepSeek V4 Pro**：复杂推理任务（意图分类、参数提取、路线规划、预算分配、酒店选择、交通方式决策等）
  - **DeepSeek V4 Flash**：轻量任务（方案总结、JSON 修正、酒店价格估价）

- **实时数据集成**（基于 MCP 协议，令牌桶限速）：
  - 🚄 12306 火车票查询
  - 🚗 高德地图自驾路线 / 距离 / 费用
  - 🏨 高德地图酒店搜索
  - ☀️ 高德地图天气预报
  - 📅 八字黄历查询
  - ✈️ 航班查询（可选，长途自动触发）

- **知识库检索**（RAG）：
  - 向量数据库存储旅游攻略，支持 TXT / MD / PDF / CSV 导入

- **三层记忆系统**：
  - **工作记忆**：多轮规划快照，keep / replan 判定，实现"调整需求后重规划"
  - **情景记忆**：历史行程落库，作为 few-shot 参考，跨会话个性化
  - **语义记忆**：从行程蒸馏用户偏好，回写用户档案

- **预算智能管控**：
  - 交通累加校验、按城市 × 天数 × 偏好 × 淡旺季智能分配预算
  - 超预算自动重规划（默认最多 3 次），仍超支则把完整方案交给用户决策

- **工程化能力**：
  - SSE 流式输出（节点进度 + LLM token）
  - Postgres LangGraph checkpointer（断点续跑）
  - 任务并发信号量、LLM/MCP 硬超时、MCP 令牌桶限速
  - 观测落库（`obs_*` 表）+ 独立 monitor 服务 + token 用量统计

---

## 🏗️ 技术架构

### 工作流（固定 DAG）

```
用户输入
   ↓
classify（意图 4 分类）
   ├─ conversation ──→ 对话回复
   ├─ feedback ──────→ 反馈处理（回写情景记忆满意度）
   ├─ information ───→ 信息查询
   └─ travel ────────→ extract_params（参数提取）
                          ├─ 需澄清 → ask_clarification
                          ├─ 简单查询 → simple_rag_search → summarizer
                          ├─ 市内旅游 → city_budget_allocation
                          └─ 跨城旅游 → transport_check（交通预检）
                                           ├─ 超预算 → budget_fail
                                           └─ transport_select → city_budget_allocation
                                                               → plan_all_cities_concurrent（多城市并发城内规划）
                                                                   ├─ 超预算 → budget_fail
                                                                   └─ summarizer → 总结 + 记忆落库
```

### 微服务拓扑

```
React 前端 (JWT, localStorage) ──► gateway :8000 ──► backend :8001
                                     │ 对外鉴权          │ /internal/* 业务 + LangGraph
                                     │  SSE 透传          └─ 写入 obs_* 观测表
                                     └── monitor :8002（直连 PostgreSQL 只读 obs_*，管理员观测）
```

- **gateway**（`:8000`）：对外唯一入口，JWT 本地鉴权后转发到 backend；SSE 透传。
- **backend**（`:8001`）：业务核心，暴露 `/internal/*`，按 `user_id` 做资源归属校验。
- **monitor**（`:8002`）：管理员监控服务，直连 PostgreSQL 只读聚合观测数据。

### 核心技术栈

**后端**：
- **LangGraph**: Multi-Agent 固定 DAG 工作流编排
- **LangChain**: Agent 框架与工具集成
- **FastAPI + uvicorn**: 微服务（gateway / backend / monitor 三进程）
- **PostgreSQL (pgvector)** + SQLAlchemy/asyncpg：业务与观测数据
- **ChromaDB**: 向量数据库（存储旅游攻略）
- **DashScope**: 阿里云模型服务（Embedding）
- **DeepSeek API**: 主模型（deepseek-v4-pro / deepseek-v4-flash）
- **MCP (Model Context Protocol)**: 12306 / Gaode / Bazi / Flight / Bing 外部工具
- **LangGraph Postgres checkpoint**: 断点续跑
- **aiolimiter**: MCP 工具令牌桶限速

**前端**：
- **React 18 + Vite 5**
- react-markdown + remark-gfm（Markdown 渲染）
- fetch 流式读取 SSE（支持携带 Authorization 头）

---

## 💻 系统要求

### 运行环境
- Python >= 3.11
- Node.js >= 18
- PostgreSQL >= 17（建议使用 pgvector 镜像）
- 8GB+ RAM（用于向量数据库和模型推理）
- Windows / Linux / macOS

### API 密钥（必须配置）
- **DeepSeek API Key**（`OPENAI_API_KEY`，主模型）
- **DashScope API Key**（`DASHSCOPE_API_KEY`，Embedding）
- **MCP 服务器 URL**（12306 / 高德 / 八字，见 `config/servers_config.json`）

---

## 📦 安装部署

### 1. 启动数据库（PostgreSQL + pgvector）

```bash
# 项目根目录（travel-agent/）下
docker compose up -d
```

### 2. 安装后端依赖

```bash
cd backend
pip install -r requirements.txt
```

### 3. 安装前端依赖

```bash
cd frontend
npm install
```

### 4. 配置环境变量

在项目根目录（`travel-agent/travel-agent/`）创建或编辑 `.env` 文件：

```bash
# 模型 API 密钥（必填，填入真实 Key）
OPENAI_API_KEY=sk-your-deepseek-key
DASHSCOPE_API_KEY=sk-your-dashscope-key
OPENAI_BASE_URL=https://api.deepseek.com/v1

# PostgreSQL（默认值见下，与 docker-compose 一致）
PG_HOST=localhost
PG_PORT=5432
PG_USER=travel_agent
PG_PASSWORD=travel_agent
PG_DATABASE=travel_agent

# 安全（务必修改，否则有风险，见「安全说明」）
JWT_SECRET=your-strong-random-secret
SUPERADMIN_USERNAME=admin
SUPERADMIN_PASSWORD=your-strong-admin-password
```

> ⚠️ `.env` 已被 `.gitignore` 忽略，**不要提交真实密钥到版本库**。

### 5. 配置 MCP 服务器

编辑 `backend/config/servers_config.json`，填入真实 MCP 服务器地址：

```json
{
    "mcp_servers": [
        {
            "name": "12306 Server",
            "url": "https://your-12306-mcp-server-url/sse",
            "description": "火车票查询服务 - 必需",
            "required": true
        },
        {
            "name": "Gaode Server",
            "url": "https://your-gaode-mcp-server-url/sse",
            "description": "高德地图服务（路线规划、酒店、天气、POI搜索）- 必需",
            "required": true
        },
        {
            "name": "bazi Server",
            "url": "https://your-bazi-mcp-server-url/sse",
            "description": "八字黄历服务（农历、黄历宜忌、出行吉日）- 必需",
            "required": true
        }
    ],
    "agent": {
        "name": "TravelPlannerAssistant",
        "instructions": "你是一名专业的旅行规划智能助手..."
    }
}
```

### 6. 启动应用（三进程）

在项目根目录（`travel-agent/travel-agent/`）下，按顺序启动：

```bash
# 1) backend 内部服务（业务逻辑 + LangGraph 工作流 + 观测写入），端口 8001
uvicorn server:app --host 0.0.0.0 --port 8001 --app-dir backend

# 2) gateway 对外网关（JWT 鉴权 + 转发到 backend），端口 8000
uvicorn gateway.main:app --host 0.0.0.0 --port 8000

# 3) monitor 监控服务（直连 PostgreSQL 只读 obs_* 表），端口 8002
uvicorn monitor.main:app --host 0.0.0.0 --port 8002
```

> Windows 一键启动脚本：`start_services.ps1`（项目根目录执行）。

### 7. 启动前端

```bash
cd frontend
npm run dev   # 默认 http://localhost:5173，通过 VITE_API_BASE_URL 指向 gateway
```

### 验证安装

- `GET http://127.0.0.1:8000/health` → gateway 健康状态
- `GET http://127.0.0.1:8001/internal/health` → backend 健康状态
- `GET http://127.0.0.1:8002/health` → monitor 健康状态
- 前端通过 gateway 的 JWT 接口注册 / 登录 / 对话

---

## 🧭 使用指南

### 简单查询模式
```
用户：苏州有什么好玩的？
用户：推荐一下成都的景点
```
调用 RAG 知识库 + 高德 POI 搜索，返回景点列表和简介。

### 完整规划模式
```
用户：我想从上海去苏州玩2天，预算1000元，12月10日出发，帮我规划一下
```
系统流程：意图分类 → 参数提取 → 交通预检 → 预算分配 → 城内规划 → 总结，输出：
- 📋 基本信息（路线、日期、天气、黄历）
- 🚗🚆 交通方案对比（自驾 vs 火车）
- 🏨 住宿推荐（1-2 家精选 + 备选）
- 📅 每日行程安排
- 💰 预算分配明细
- 💡 特别建议（老人 / 儿童友好提示）

### 多城市 & 预算重规划
- 支持"上海 → 苏州 → 杭州，5天"多城市行程，各城市预算独立分配、并发规划。
- 超预算自动重规划（最多 3 次），仍超支则把完整方案交给用户决定如何调整。

---

## 🔒 安全说明

> ⚠️ 本项目用于本地 / 演示用途，部署到公网前务必处理以下安全项：

1. **密钥**：`OPENAI_API_KEY` / `DASHSCOPE_API_KEY` 只放 `.env`（已 gitignore），**绝不提交到版本库**。
2. **JWT_SECRET**：默认值 `travel-agent-dev-secret-change-me` 仅用于开发，生产必须用强随机串。
3. **超级管理员**：默认 `admin / admin123456` 会自动创建，生产必须立即修改。
4. **CORS**：gateway 默认 `allow_origins=["*"]`，生产应限定为前端域名。
5. **backend 信任边界**：`/internal/*` 接口不做 JWT 鉴权（由 gateway 负责），依赖调用方传入 `user_id` 做归属校验。**backend 必须部署在内网、不对公网暴露**。
6. **前端 token 存储**：JWT 存于 localStorage，存在 XSS 泄露风险；高安全场景建议改为 HttpOnly Cookie。

---

## 📡 API 接口

### gateway（对外，需 JWT）
| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/auth/register` | 注册 |
| POST | `/auth/login` | 登录 |
| GET | `/auth/me` | 当前用户信息 |
| POST | `/auth/admin/login` | 管理员登录 |
| GET/POST/DELETE | `/auth/admin/users` | 管理员管理（superadmin） |
| POST | `/chat` | 提交对话，返回 task_id |
| GET | `/tasks/{task_id}` | 轮询任务状态 |
| POST | `/tasks/{task_id}/resume` | 断点续跑 |
| GET | `/tasks/{task_id}/stream` | SSE 流式输出 |
| GET | `/obs/{task_id}` | 观测追踪详情 |
| GET/POST/DELETE | `/sessions...` | 会话 / 聊天历史 |
| GET | `/admin/report` `/admin/tasks` | 管理员观测报表 |

### backend（内部，/internal/*）
| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/internal/auth/*` | 注册 / 登录 / 管理员管理 |
| POST | `/internal/chat` | 提交对话 |
| GET | `/internal/tasks/{task_id}` | 任务状态 |
| POST | `/internal/tasks/{task_id}/resume` | 断点续跑 |
| GET | `/internal/tasks/{task_id}/stream` | SSE 流 |
| GET | `/internal/obs/{task_id}` | 观测追踪 |
| GET/POST/DELETE | `/internal/sessions...` | 会话 / 消息 |
| GET | `/internal/health` | 健康检查 |

---

## 🗄️ 数据库与观测

- **业务表**：`users` / `chat_sessions` / `chat_messages` / `trip_episodes`（情景记忆）等
- **观测表**：`obs_*`（任务 / 节点 / LLM / MCP / token 统计），由 backend 写入，monitor 只读
- **向量库**：ChromaDB（RAG 旅游攻略），路径由 `CHROMA_PERSIST_DIR` 配置

---

## 📁 项目结构

```
travel-agent/travel-agent/
├── backend/                    # 业务核心 + 内部服务（:8001）
│   ├── agent_nodes/            # LangGraph 各节点（classify / params / transport / city_planning / summarizer / info_query）
│   │   ├── _observability.py   # 运行时观测追踪（写 obs_* 表）
│   │   └── _common.py          # LLM 工厂 / token 统计 / 统一工具调用
│   ├── config/                 # settings / prompts / servers_config.json
│   ├── core/                   # TravelService / TaskManager / checkpoint
│   ├── graph/                  # LangGraph 工作流与状态
│   ├── memory/                 # 三层记忆（working / episodic / semantic）
│   ├── tools/                  # 工具注册表 / MCP / RAG / typecode
│   ├── tests/                  # 基础设施测试
│   ├── server.py               # backend 内部 FastAPI 服务（/internal/*）
│   ├── auth.py                 # JWT / bcrypt 认证
│   ├── db.py                   # PostgreSQL 连接池
│   └── requirements.txt        # Python 依赖
├── gateway/                    # 对外 API 网关（:8000，JWT 鉴权 + 转发）
├── monitor/                    # 独立监控服务（:8002，直连 obs_* 只读）
├── frontend/                   # React 前端（Vite + JWT + SSE）
│   └── src/
├── docs/                       # 设计文档
├── .env                        # 环境变量（JWT / PG / 模型密钥，已 gitignore）
├── docker-compose.yml          # PostgreSQL (pgvector) 服务
└── start_services.ps1          # Windows 一键启动脚本
```

---

## 🛠️ 开发说明

### 修改 Prompt
编辑 `backend/config/prompts.py`。

### 调整模型参数 / 配置
编辑 `backend/config/settings.py`，或通过 `.env` 覆盖（`LLM_MODEL_<AGENT>` 可单独指定 agent 用 Pro 还是 Flash）。

### 添加新工具
1. 在 `backend/tools/registry/` 注册工具定义
2. 在 `backend/config/servers_config.json` 配置对应 MCP 服务器
3. 通过 `_common._call_mcp_tool` 统一调用

### 添加新 Agent 节点
1. 在 `backend/agent_nodes/` 创建节点文件
2. 在 `backend/graph/workflow.py` 注册节点与边
3. 在 `backend/graph/state.py` 添加必要的状态字段

---

## 🐛 故障排查

### 1. 模块导入错误
```bash
cd backend
pip install -r requirements.txt
```

### 2. MCP 工具调用失败
1. 检查 `config/servers_config.json` 的 MCP URL 是否真实可用
2. 查看 backend 日志中的 MCP 连接/调用信息
3. 检查 MCP 令牌桶限速是否触发（`MCP_RATE_LIMIT_MAX`）

### 3. gateway 转发失败
- 确认 backend（:8001）已启动
- 检查 gateway 日志中的转发错误 / 超时

### 4. API Key 错误
1. 检查 `.env` 中的 Key 是否正确
2. 确认账户有足够额度

### 5. 断点续跑不可用
- 确认已安装 `langgraph-checkpoint-postgres` 且 PostgreSQL 可连
- 未启用时服务降级为无 checkpointer 模式（仅失去续跑能力）

---

## 🤝 贡献指南

1. Fork 本项目
2. 创建特性分支（`git checkout -b feature/AmazingFeature`）
3. 提交更改（`git commit -m 'Add some AmazingFeature'`）
4. 推送到分支（`git push origin feature/AmazingFeature`）
5. 开启 Pull Request

---

## 📄 许可证

本项目采用 MIT 许可证。
