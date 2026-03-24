# 🌍 智能旅行规划助手

基于 **LangChain + LangGraph + Streamlit + MCP + RAG** 的智能旅行规划系统，采用 Multi-Agents 架构，结合双模型协作（DeepSeek R1 + Qwen3）、知识检索和实时数据查询，为用户提供智能化的旅行方案。

## 📋 目录

- [项目简介](#项目简介)
- [核心功能](#核心功能)
- [技术架构](#技术架构)
- [系统要求](#系统要求)
- [安装部署](#安装部署)
- [使用指南](#使用指南)
- [数据库管理](#数据库管理)
- [项目结构](#项目结构)
- [API 接口](#api-接口)
- [故障排查](#故障排查)

---

## 🎯 项目简介

这是一个智能旅行规划系统，采用 **Streamlit UI + LangGraph Multi-Agents** 架构，通过自然语言对话为用户生成完整的旅行方案。系统由多个专门的 Agent 协同工作，包括 Planner（规划师）、Executor（执行者）、Summarizer（总结者）和 Feedback Agent（反馈代理），实现高效的任务分解和执行。

### 主要特性

- **Multi-Agents 架构**：
  - 🧠 **Planner Agent**：解析用户需求，提取关键信息
  - 🔧 **Executor Agent**：调用工具执行具体任务
  - 📝 **Summarizer Agent**：综合信息生成最终方案
  - 💬 **Feedback Agent**：处理对话反馈和多轮交互

- **双模型协作**：
  - **DeepSeek R1**：处理复杂推理和多目的地路线优化（预算分配、时间安排）
  - **Qwen3**：负责信息提取、工具调用决策和方案生成

- **实时数据集成** (基于 MCP 协议)：
  - 🚄 12306 火车票查询（自动获取站点代码，查询车次时刻表）
  - 🚗 高德地图自驾路线（自动计算距离、时间、过路费）
  - 🏨 高德地图酒店搜索（根据预算自动筛选）
  - ☀️ 高德地图天气预报（支持多日预报）
  - 📅 八字黄历查询（农历、宜忌、吉日）
  - ✈️ 航班查询（可选，长途>800km 自动触发）

- **知识库检索**：
  - RAG 向量数据库存储旅游攻略
  - 支持 TXT、MD、PDF、CSV 格式导入

---

## 🚀 核心功能

### 1. 智能信息提取
- 从自然语言对话中提取出发地、目的地、日期、预算等关键信息
- 支持相对日期（"明天"、"下周"）自动转换
- 多轮对话上下文保持，支持增量信息更新

### 2. 交通方案对比
- 自动查询火车票信息（车次、时间、票价）
- 计算自驾路线（距离、时间、过路费）
- 综合对比推荐最优方案

### 3. 住宿推荐
- 根据预算自动选择酒店等级关键词
  - 预算 > 500元：五星/豪华
  - 预算 300-500元：品牌连锁
  - 预算 < 300元：经济型/快捷
- 提供酒店名称、价格、地址信息

### 4. 天气与黄历
- 查询旅行日期的天气预报（最多4天）
- 查询农历黄历，分析是否适合出行
- 展示宜忌事项

### 5. 行程规划
- 结合 RAG 知识库和实时 POI 数据
- 生成每日详细行程
- 计算预算分配（交通、住宿、餐饮、门票）

### 6. 用户画像管理
- 保存用户偏好和历史数据
- 支持个性化推荐
- 聊天历史持久化存储

---

## 🏗️ 技术架构

### Multi-Agents 工作流

```
用户输入
   ↓
[Planner Agent] → 信息提取 + 需求分析
   ↓
[Executor Agent] → 工具调用（RAG/12306/高德/黄历）
   ↓
[Summarizer Agent] → 方案合成
   ↓
[Feedback Agent] → 多轮交互优化
   ↓
最终输出
```

### 核心技术栈

**后端**：
- **LangGraph**: Multi-Agents 工作流编排
- **LangChain**: Agent 框架和工具集成
- **Streamlit**: Web UI 界面
- **ChromaDB**: 向量数据库（存储旅游攻略）
- **DashScope**: 阿里云模型服务（Qwen3-plus + text-embedding-v3）
- **DeepSeek API**: 深度推理模型（deepseek-reasoner）
- **MCP (Model Context Protocol)**: 外部工具集成
  - 12306 Server（火车票）
  - Gaode Server（地图、天气、酒店）
  - Bazi Server（黄历）
  - Flight Server（航班，可选）
  - Bing Search Server（搜索，可选）

---

## 💻 系统要求

### 运行环境
- Python >= 3.11
- 8GB+ RAM（用于向量数据库和模型推理）
- Windows/Linux/macOS

### API 密钥
- **DeepSeek API Key**（用于 DeepSeek R1 模型）
- **DashScope API Key**（用于 Qwen3 和文本嵌入）
- **MCP 服务器 URL**（12306、高德地图、八字服务器等）

---

## 🔑 API 密钥获取

### 1. DeepSeek API Key

**用途**：DeepSeek R1 模型用于复杂推理和优化任务

**获取步骤**：

1. 访问 [DeepSeek 开放平台](https://platform.deepseek.com/)
2. 注册账号并登录
3. 进入「API Keys」页面
4. 点击「创建新密钥」
5. 复制生成的 API Key（格式：`sk-xxxxxxxxxxxxxxxx`）

**费用**：按 Token 使用量计费，新用户通常有免费额度

### 2. DashScope API Key（阿里云）

**用途**：Qwen3 模型和文本嵌入（text-embedding-v3）

**获取步骤**：
1. 访问 [阿里云 DashScope](https://dashscope.aliyun.com/)
2. 使用阿里云账号登录（需要实名认证）
3. 进入「API-KEY 管理」
4. 创建新的 API Key
5. 复制生成的 API Key（格式：`sk-xxxxxxxxxxxxxxxx`）

**费用**：

- Qwen3 模型：按 Token 计费，有免费额度
- 文本嵌入：按调用次数计费，新用户有免费额度

### 3. MCP 服务器配置

**MCP（Model Context Protocol）** 是连接外部工具的协议。本项目使用以下 MCP 服务器：

#### 可用的 MCP 服务器：

1. **12306 Server** - 火车票查询
   - 提供商：ModelScope
   - 功能：查询火车车次、票价、时刻表

2. **Gaode Map Server** - 高德地图
   - 提供商：ModelScope
   - 功能：路线规划、酒店查询、天气预报、POI 搜索

3. **Bazi Server** - 八字黄历服务器
   - 提供商：ModelScope
   - 功能：查询农历、黄历宜忌、出行吉日

4. **Bing Search Server** - 必应搜索（可选）
   - 提供商：ModelScope
   - 功能：搜索最新旅游资讯

5. **Flight Server** - 航班查询（可选）
   - 提供商：ModelScope
   - 功能：查询航班信息

#### 如何获取 MCP 服务器 URL：

**方式1：使用 ModelScope 提供的公开服务**

1. 访问 [ModelScope MCP 广场](https://www.modelscope.cn/)
2. 搜索对应的 MCP 服务（如「12306 MCP」、「高德地图 MCP」）
3. 获取服务的 SSE 接口地址

**方式2：自己部署 MCP 服务器**
1. 从 GitHub 获取 MCP 服务器源码
2. 按照服务器文档部署到自己的服务器
3. 使用自己的服务器地址

**注意**：
- MCP 服务器 URL 通常以 `/sse` 结尾（Server-Sent Events）
- 某些 MCP 服务可能需要额外的 API Key（如高德地图需要高德开放平台 Key）
- 建议使用稳定的服务提供商，避免服务中断

---

## 📦 安装部署

### 1. 克隆项目

```bash
git clone <repository-url>
cd travel-agent
```

### 2. 安装依赖

进入 multi-agents 目录并安装依赖：

```bash
cd multi-agents
pip install -r requirements.txt
```

### 3. 配置环境变量

在 `multi-agents` 目录下创建或编辑 `.env` 文件：

```bash
# 模型 API 密钥（必填）
DEEPSEEK_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
DASHSCOPE_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# LangChain 追踪（可选，用于调试）
LANGCHAIN_TRACING_V2=false

# MCP 配置文件路径（默认值）
MCP_CONFIG_PATH=config/servers_config.json

# ChromaDB 向量数据库路径（默认值）
CHROMA_PERSIST_DIR=../data/travel_vectordb
```

**重要**：将 `sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx` 替换为你获取的真实 API Key。

### 4. 配置 MCP 服务器

编辑 `config/servers_config.json`：

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

**配置说明**：
- `mcp_servers`：MCP 服务器列表
  - `name`：服务器名称（用于日志）
  - `url`：服务器 SSE 接口地址
  - `required`：是否必需

**必需的 MCP 服务器**：
- ✅ **12306 Server**：火车票查询
- ✅ **Gaode Server**：地图、酒店、天气
- ✅ **Bazi Server**：黄历查询

### 5. 启动应用

在 multi-agents 目录运行:

```bash
streamlit run app.py
```

应用将在 `http://localhost:8501` 启动。

**验证安装**：
- 启动成功后，浏览器会自动打开 Streamlit UI
- 在侧边栏可以查看已加载的工具列表
- 后台日志会显示 MCP 服务器连接状态

---

## 📖 使用指南

### 简单查询模式

**适用场景**：快速了解某个城市的景点信息

**示例**：
```
用户：苏州有什么好玩的？
用户：推荐一下成都的景点
```

**系统行为**：
- 调用 RAG 知识库和高德地图 POI 搜索
- 返回景点列表和简要介绍

### 完整规划模式

**适用场景**：需要完整的旅行方案

**需要提供的信息**：
- ✅ 出发地：如"上海"
- ✅ 目的地：如"苏州"
- ✅ 旅行天数：如"2天"
- ✅ 预算：如"1000元"
- ✅ 出发日期：如"12月10日" 或 "明天"

**示例**：
```
用户：我想从上海去苏州玩2天，预算1000元，12月10日出发，帮我规划一下
```

**系统行为**：
1. Planner Agent 提取关键信息
2. Executor Agent 调用工具（RAG/12306/高德/黄历）
3. Summarizer Agent 合成完整方案
4. Feedback Agent 支持多轮对话优化

**输出内容**：
- 📋 基本信息（路线、日期、天气、黄历）
- 🚗🚆 交通方案对比（自驾 vs 火车）
- 🏨 住宿推荐（2-3家酒店）
- 📅 每日行程安排
- 💰 预算分配明细
- 💡 特别建议（老人/儿童友好提示）

### 🧠 DeepSeek R1 复杂推理触发条件

**DeepSeek R1** 仅在**复杂场景**下才会被调用，以控制成本和提高效率。

满足以下 **任意一个条件** 就会使用 R1：

1. **复杂的多城市路线**
   - 示例："上海 → 苏州 → 杭州 → 南京，5天"
   - 需要：路线优化、时间分配

2. **紧张的预算优化**
   - 示例："4人去苏州3天，总预算1500元"（人均375元/天）
   - 需要：交通、住宿、餐饮、门票的精细优化

3. **多重冲突的约束条件**
   - 示例："带着两个70岁老人和一个5岁孩子，时间只有1天，要去3个景点"
   - 需要：平衡老人体力、孩子兴趣、时间限制

4. **复杂的优化问题**
   - 示例："最省钱的方案"、"最快到达的路线"、"最多景点的行程"
   - 需要：多目标优化、权衡分析

---

## ️ 数据库管理

本项目使用 **ChromaDB** 作为向量数据库，存储旅游攻略文档。

### 数据库位置

```
data/travel_vectordb/
```

### 导入数据

通过 Streamlit UI 上传文档：

1. 启动应用：`streamlit run app.py`
2. 在左侧边栏找到 **"📚 旅游攻略文档"** 区域
3. 点击上传按钮，选择文档
4. 系统自动完成文本分块和向量化

**支持的格式**：
- `.txt` - 纯文本
- `.md` - Markdown
- `.pdf` - PDF 文档
- `.csv` - CSV 表格

---

## 📁 项目结构

```
travel-agent/
├── multi-agents/                 # Multi-Agents 架构实现
│   ├── agent_nodes/              # 各 Agent 实现
│   │   ├── planner_agent.py      # 规划师 Agent
│   │   ├── executor_agent.py     # 执行者 Agent
│   │   ├── summarizer_agent.py   # 总结者 Agent
│   │   ├── feedback_agent.py     # 反馈 Agent
│   │   └── main_agent.py         # 主 Agent 协调
│   ├── config/                    # 配置文件
│   │   ├── prompts.py             # Prompt 模板
│   │   ├── settings.py            # 全局配置
│   │   └── servers_config.json    # MCP 服务器配置
│   ├── graph/                     # LangGraph 工作流
│   │   ├── workflow.py            # 工作流定义
│   │   └── state.py               # 状态类型定义
│   ├── tools/                     # 工具集
│   │   ├── rag_tool.py            # RAG 向量检索
│   │   ├── mcp_tools.py           # MCP 工具管理器
│   │   ├── context_compressor.py  # 上下文压缩
│   │   └── tool_registry.py       # 工具注册表
│   ├── data/                      # 数据目录
│   │   ├── user_profiles/         # 用户画像
│   │   └── chat_history.db        # 聊天历史数据库
│   ├── app.py                     # Streamlit UI 入口
│   ├── chat_history_manager.py    # 聊天历史管理
│   ├── user_profile_manager.py    # 用户画像管理
│   ├── requirements.txt           # Python 依赖
│   └── .env                       # 环境变量
│
├── README.md                      # 项目文档
├── LICENSE                        # MIT 许可证
└── .gitignore                     # Git 忽略配置
```

**核心文件说明**:
- `multi-agents/app.py`: Streamlit UI 主程序
- `multi-agents/graph/workflow.py`: LangGraph 工作流定义
- `multi-agents/agent_nodes/`: 各专门 Agent 的实现
- `multi-agents/tools/mcp_tools.py`: MCP 工具管理器
- `multi-agents/config/settings.py`: 全局配置

---

## 🐛 故障排查

### 1. 模块导入错误

**问题**：`ModuleNotFoundError: No module named 'xxx'`

**解决**：
```bash
cd multi-agents
pip install -r requirements.txt
```

### 2. MCP 工具调用失败

**问题**：火车票、天气查询返回错误

**解决**：
1. 检查 MCP 服务器 URL 是否正确
2. 验证 `config/servers_config.json` 配置
3. 查看 Streamlit 后台日志

### 3. Streamlit 启动失败

**问题**：Streamlit 无法启动或报错

**解决**：
1. 确认已安装 Streamlit: `pip install streamlit`
2. 检查端口 8501 是否被占用
3. 尝试指定端口：`streamlit run app.py --server.port 8502`

### 4. API Key 错误

**问题**：模型调用返回认证错误

**解决**：
1. 检查 `.env` 文件中的 API Key 是否正确
2. 确认 API Key 没有过期
3. 检查账户是否有足够的额度

---

## 📝 开发说明

### 修改 Prompt

编辑 `multi-agents/config/prompts.py`：

```python
# 修改规划提示词
PLANNER_SYSTEM_PROMPT = """你的自定义提示词..."""
```

### 调整模型参数

编辑 `multi-agents/config/settings.py`：

```python
# 模型温度
QWEN3_TEMPERATURE = 0.7
R1_TEMPERATURE = 0.1

# RAG 分块大小
RAG_CHUNK_SIZE = 500
RAG_CHUNK_OVERLAP = 50
```

### 添加新工具

1. 在 `tools/tool_registry.py` 中添加工具定义
2. 在 `config/servers_config.json` 中配置对应的 MCP 服务器
3. 在 `tools/mcp_tools.py` 中添加工具调用逻辑（如需要）

### 添加新 Agent

1. 在 `agent_nodes/` 目录下创建新的 Agent 文件
2. 在 `graph/workflow.py` 中更新工作流定义
3. 在 `graph/state.py` 中添加必要的状态字段

---

## 🤝 贡献指南

欢迎提交 Issue 和 Pull Request！

1. Fork 本项目
2. 创建特性分支 (`git checkout -b feature/AmazingFeature`)
3. 提交更改 (`git commit -m 'Add some AmazingFeature'`)
4. 推送到分支 (`git push origin feature/AmazingFeature`)
5. 开启 Pull Request

---

## 📄 许可证

本项目采用 MIT 许可证。

---

## 📧 联系方式

如有问题或建议，请提交 Issue 或联系项目维护者。

---

**祝您使用愉快！🎉**
