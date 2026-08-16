# 记忆系统设计计划

> 状态：已实现（经用户审阅批准后落库，联调验证通过）
> 目标：为旅行规划 Agent 引入分层记忆能力（工作记忆 / 情景记忆 / 语义记忆），感知记忆暂缓。
> 约束：不引入 Qdrant/Neo4j 等外部服务，沿用 SQLite + ChromaDB + JSON 现有技术栈；不改变 graph 拓扑。

---

## 1. 背景与目标

当前项目已有：对话历史（SQLite）、用户档案（JSON）、上下文压缩（LLM 摘要）、领域知识 RAG（ChromaDB）。

缺失的能力：

| 场景 | 现状 |
|---|---|
| 规划到一半，用户追加需求，中间状态丢失 | 每次消息全量重跑 graph，状态在 `GlobalState` 中仅存活单次 |
| "我上次去成都住哪了" 跨会话复用 | 无结构化历史行程 |
| 用户偏好自动累积（越用越懂） | 档案字段被动，仅外部写入 |

本计划通过三层记忆解决：

- **工作记忆**：存"这次规划到哪了"，支持 keep / replan 每城标记，增量继续规划。
- **情景记忆**：沉淀历史行程（episode），用于 few-shot 参考 + 直接问答。
- **语义记忆**：从对话/计划中蒸馏偏好，增量回写用户档案。

---

## 2. 总体架构

```text
┌───────────────────────────────────────────────┐
│ MemoryManager（统一读写/检索/决策入口）            │
├──────────────┬──────────────┬──────────────────┤
│ 工作记忆      │  情景记忆      │   语义记忆        │
│ (会话内状态)  │ (历史行程)     │   (用户画像)      │
├──────────────┼──────────────┼──────────────────┤
│ SQLite       │ SQLite       │ JSON + 蒸馏管道    │
│ TTL 24h      │ 长期保留      │  长期保留         │
└──────────────┴──────────────┴──────────────────┘
```

感知记忆（多模态）：无输入管道，留接口不实现。

---

## 3. 工作记忆（Working Memory）

### 3.1 语义

| 城市状态 | keep（当前基础上继续） | replan（重新规划） |
|---|---|---|
| 没规划 | 正常规划（与 replan 等价） | 正常规划 |
| 没规划完 | 从断点继续，补齐剩余，保留已选景点/酒店 | 清空，从头全量重来 |
| 规划完 | 直接用旧计划，跳过全部工具调用 | 清空，从头全量重来 |

keep = "有就用、缺就补"；replan = "一律归零重来"。

### 3.2 数据结构（JSON 快照，落 SQLite）

```json
{
  "session_id": "default_user_1720000000",
  "user_id": "default_user",
  "transport_total": 850.0,
  "buffer_budget": 200.0,
  "total_budget": 5000.0,
  "updated_at": "2026-08-13T10:00:00",
  "cities": {
    "成都": { "status": "done",    "spent": 1800.0, "locked": true,  "plan": { } },
    "西安": { "status": "partial", "spent": 600.0,  "locked": true,  "plan": { } },
    "重庆": { "status": "pending", "spent": 0,      "locked": false, "plan": null }
  }
}
```

- `locked=true` 的金额在预算分配时全额扣减。
- `status`：pending（没规划）/ partial（没规划完）/ done（规划完）。

### 3.3 持久化

表 `working_memory`：

```sql
CREATE TABLE IF NOT EXISTS working_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    snapshot JSON NOT NULL,           -- 3.2 节完整快照
    ttl_seconds INTEGER DEFAULT 86400,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    UNIQUE(session_id, user_id)
);
```

写入时机（两个）：

1. `plan_all_cities_concurrent_node` 每城规划完成后 → 更新该城 `status/spent/plan` 快照。
2. `city_budget_allocation_node` 完成后 → 更新 `transport_total/buffer_budget/各城预算份额`。

读取时机：

- `extract_params_node` 之前（新消息进入规划流程时），由记忆决策点加载快照，若 `expires_at < now` 则忽略。

### 3.4 预算一致性公式（混合场景）

```text
可用池 = total_budget
        - transport_total
        - Σ(locked=true 城市的已花费)      # keep 且规划完 / keep 且部分规划，已产生的花费全部锁定
        - buffer_budget(5-10% 预留)

→ 将「可用池」重新分配给：replan 城市 + keep 但未完成部分
```

- keep 已锁定金额固定不变；replan 城市按现有 [city_budget_allocation_node](file:///d:/ai-agent-project/travel-agent/travel-agent/backend/agent_nodes/city_planning.py) 的 LLM 分配 + 按比例缩放逻辑处理。
- buffer_budget 吸收重分配后的轻微超支。

### 3.5 节点改造点

| 节点 | 改动 |
|---|---|
| 记忆决策点（新增，位于 extract_params 后） | 读工作记忆快照 + 新需求 → LLM 输出 `{城市: "keep" / "replan"}` 判定 |
| `plan_all_cities_concurrent_node` | keep 且 done → 直接加载旧计划（跳过工具调用）；keep 且 partial → 从断点继续；replan → 全量重规划。仍用 `asyncio.gather` |
| `city_budget_allocation_node` | 按 3.4 公式排除 locked 城市后分配 |
| `summarizer_node` | 规划结束汇总时持久化最终快照 |

graph 拓扑不变，只改节点内部行为。

---

## 4. 情景记忆（Episodic Memory）

### 4.1 表结构

```sql
CREATE TABLE IF NOT EXISTS trip_episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    origin TEXT,
    destination TEXT NOT NULL,
    start_date TEXT,
    end_date TEXT,
    nights INTEGER,
    total_budget REAL,
    total_spent REAL,
    transport_mode TEXT,
    transport_cost REAL,
    hotels JSON,          -- [{name, price, area}]
    attractions JSON,     -- [{name, city}]
    feedback TEXT,
    satisfaction INTEGER, -- 0-5
    summary TEXT,         -- LLM 生成的行程总结
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_episodes_user_dest ON trip_episodes(user_id, destination);
CREATE INDEX IF NOT EXISTS idx_episodes_user_created ON trip_episodes(user_id, created_at);
```

### 4.2 写入

- 时机：`summarizer_node` 生成最终计划后。
- 方式：结构化字段（origin/destination/nights/budget/hotels/attractions）直接从 `city_plans` JSON 提取；`summary` 走一次轻量 LLM 调用（复用 context_compressor 的摘要思路）。
- 反馈回流：`handle_feedback_node` 收到满意度时，更新对应 episode 的 `satisfaction/feedback`。

### 4.3 检索与使用

- **few-shot 注入**：`extract_params_node` / 规划 prompt 前，按「目的地相同 > 预算区间相同 > 出发地相同」的优先级检索，最多注入 1-2 条（`summary` + 关键字段），避免 prompt 过载。
- **直接问答**：用户问"上次去 X 住哪/花了多少"，从 episode 表直接检索返回，不触发规划流程。

---

## 5. 语义记忆（Semantic Memory）

### 5.1 原理

- 情景记忆 = 发生过的事实（事件级）。
- 语义记忆 = 从事实中蒸馏的规律（偏好级），直接指导规划。

示例：

```
情景：8月成都 3天2晚 4000元 住春熙路地铁站旁；11月西安 2天1晚 2500元 住钟楼地铁站旁
语义：→ "偏好地铁站附近酒店" 写入 hotel_preference
```

### 5.2 蒸馏管道

- 触发时机：
  1. `context_compressor.compress()` 已抽取 `user_preferences`（interests/dislikes）→ 增量回写档案。
  2. `summarizer_node` 规划完成后，从 city_plans + 用户反馈中抽取新偏好。
- 回写目标：[user_profile_manager.py](file:///d:/ai-agent-project/travel-agent/travel-agent/backend/user_profile_manager.py) 现有字段（travel_style / hotel_preference / cuisine_preference / liked_activities / disliked_activities / budget_level 等），去重后追加。
- 存储：复用现有 JSON 档案，不引入 Neo4j。

### 5.3 注入

`format_profile_for_prompt()` 已具备格式化能力，规划 prompt 直接引用，无需新增。

---

## 6. 不做的事

- 感知记忆（多模态）：无输入管道，暂缓。
- Qdrant / Neo4j 外部服务：不引入，SQLite + ChromaDB 足够。
- 修改 graph 拓扑：保持 [workflow.py](file:///d:/ai-agent-project/travel-agent/travel-agent/backend/graph/workflow.py) 固定流程架构。

---

## 7. 实施步骤

1. 新建 `memory/` 模块：`base.py`（快照/事件数据类）、`working.py`（读写 + TTL）、`episodic.py`（读写 + 检索）、`semantic.py`（蒸馏回写）、`manager.py`（统一入口）。
2. 数据库迁移：建 `working_memory` 与 `trip_episodes` 两张表（沿用 [chat_history_manager.py](file:///d:/ai-agent-project/travel-agent/travel-agent/backend/chat_history_manager.py) 的 SQLite 初始化模式）。
3. 改造 `plan_all_cities_concurrent_node`：支持 keep/replan 分叉（keep-done 直接加载、keep-partial 断点续、replan 全量）。
4. 新增记忆决策点（extract_params 后）：LLM 输出每城 keep/replan 判定。
5. 改造 `city_budget_allocation_node`：按 3.4 公式排除 locked 城市。
6. `summarizer_node`：抽取 episode 落库 + 回写偏好 + 持久化最终工作记忆快照。
7. `handle_feedback_node`：更新 episode 满意度。
8. 接入 `extract_params_node`：注入情景 few-shot + 语义偏好（复用 format_profile_for_prompt）。
9. 联调验证：场景覆盖（新规划 / 追加需求 / 改某城 / 跨会话问历史 / 预算收紧）。

## 8. 联调验证结果（已完成）

| 场景 | 结果 |
|---|---|
| 新规划（无快照） | ✅ 各城默认 replan，等价正常规划 |
| 追加需求（keep 复用） | ✅ 成都 keep 直接复用旧计划跳过工具调用；西安 replan 重规划 |
| 改某城 | ✅ 真实 LLM 判定成都=keep/西安=replan/重庆=keep；pool=1000 仅重分西安，成都/重庆锁定沿用 |
| 跨会话问历史 | ✅ "我上次去成都住哪了" 命中 episode 直接回答（春熙路酒店），不触发工具 |
| 预算收紧 | ✅ locked 花费+交通超新预算 → 可用池归零 → budget_fail 分支 |

修复过程中发现的 bug：`episodic.search_episodes` 在无 destination/budget/origin 过滤条件时直接返回空列表，导致跨会话直接问答与反馈回写无法命中；已修复为兜底按 user_id 返回最近行程。
