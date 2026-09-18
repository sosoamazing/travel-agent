# 用户反馈与回复质量量化体系

> 状态：**设计方案（待评审）**
> 目标读者：项目作者 / 协作者 / 面试评审
> 关联：`docs/observability-architecture.md`（现有可观测体系）

---

## 1. 问题背景

现有系统已有较完整的**过程观测**（span 树追踪 token/耗时/LLM 调用），但缺少**结果评价**这一环：

| 已有的 | 缺失的 |
|---|---|
| LLM 调用次数与 token 消耗 | 用户是否认为这次回复有价值 |
| 各节点执行耗时与成功率 | 哪些回复用户给了负向反馈 |
| 意图分类准确率（intent 标注意图对比） | 预算规划类回复的质量如何量化 |
| 重规划次数（replan_count） | 多轮对话中用户是否满意地结束了 |

没有用户反馈数据，就无法回答一个最基本的问题：**这次规划用户到底觉得好不好？**

---

## 2. 设计目标

1. **零门槛采集**：用户不需要主动填问卷，反馈行为自动发生
2. **质量可量化**：每轮对话都有可追踪的质量信号，支持聚合统计
3. **与现有观测体系打通**：质量信号与 span 数据在同一视图呈现，形成"过程 + 结果"的完整链路
4. **可干预**：管理员能看到趋势，触发告警或人工复盘

---

## 3. 质量信号设计

分两类：**隐性信号**（自动采集）和**显性信号**（用户主动反馈）。

### 3.1 隐性信号（自动采集，无需用户操作）

| 信号名 | 含义 | 采集方式 |
|---|---|---|
| `satisfaction_score` | 满意度推断分（0~1） | 见下方推断规则 |
| `continuation_rate` | 会话续接率 | 下一次请求在同一 session 且在规划结束后 10 分钟内触发 |
| `replan_triggered` | 是否发生了重规划 | `replan_count > 0` |
| `over_budget` | 是否超出预算 | `over_budget == True` |
| `feedback_null_count` | 连续多少次回复用户未操作（无评价/无追问） | 前端打点 |
| `error_occurred` | 是否有 LLM/MCP 超时或报错 | span 表 result_kind 统计 |
| `intent_match` | 标注意图与实际 classify 结果是否一致 | obs_tasks intent vs query_type |

**满意度推断规则**（无显性反馈时的隐式估算）：

```
if 用户点击了 thumbs_up：
    satisfaction = 1.0
elif 用户点击了 thumbs_down：
    satisfaction = 0.0
elif 用户在规划结束后 5 分钟内发起了新的旅行规划请求：
    # 隐式认可：愿意继续用说明基本满意
    satisfaction = 0.7 + (replan_triggered ? -0.1 : 0)
elif 用户在规划结束后继续追问同类问题：
    satisfaction = 0.5  # 中性，有困惑
else：
    # 沉默用户
    satisfaction = null（不参与平均分计算，但计入沉默率分母）
```

> **原则**：隐性信号只能用于**统计趋势**，不能替代显性反馈。管理员看到的"满意度"默认只算显性打分。

### 3.2 显性信号（用户主动反馈）

| 信号 | 格式 | 说明 |
|---|---|---|
| thumbs_up / thumbs_down | 布尔 | 一键评价，最小成本 |
| star_rating | 1~5 整数 | 可选，精确满意度 |
| feedback_text | 文本 | 可选，用户自主输入（限制 200 字） |
| feedback_type | enum | 分类：`too_expensive` / `wrong_city` / `no_transport` / `other`（帮聚类） |

---

## 4. 数据模型

### 4.1 新建 `turn_feedback` 表

```sql
CREATE TABLE turn_feedback (
    feedback_id      BIGSERIAL PRIMARY KEY,
    task_id        TEXT NOT NULL REFERENCES obs_tasks(task_id),
    turn_index     INTEGER NOT NULL DEFAULT 0,     -- 同 session 内第几轮回复
    session_id     TEXT NOT NULL,
    user_id        TEXT NOT NULL,

    -- 显性反馈
    thumbs_up      BOOLEAN,
    star_rating    SMALLINT CHECK (star_rating BETWEEN 1 AND 5),
    feedback_text  TEXT,
    feedback_type  TEXT,                          -- too_expensive / wrong_city / no_transport / other

    -- 隐性推断（自动写入）
    inferred_score REAL,                           -- 0.0~1.0，未知则 NULL

    -- 关联的规划结果（方便 join）
    query_type     TEXT,                          -- conversation / travel / information / feedback
    cities         TEXT[],                        -- 本轮涉及城市

    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_feedback_task   ON turn_feedback(task_id);
CREATE INDEX idx_feedback_user  ON turn_feedback(user_id);
CREATE INDEX idx_feedback_session ON turn_feedback(session_id);
```

### 4.2 写入时机

| 操作 | 写入内容 |
|---|---|
| 用户点击 👍/👎 | 即时写入 `thumbs_up/thumbs_down + inferred_score` |
| 用户提交评分/文本 | 补全 `star_rating / feedback_text / feedback_type` |
| 对话结束（规划正常完成或超预算终止） | 写入一条终态记录（`thumbs_up = NULL` 表示沉默用户） |
| `continuation_rate` 计算 | 后台定时任务扫描 session 注入下一轮的 `continuation` 事件 |

### 4.3 与现有表的关系

```
obs_tasks            — 已有：task_id / intent / query_type / start_ts / end_ts / result_kind
turn_feedback       — 新增：每轮反馈（1:1 或 1:N 于 obs_tasks）
trip_episodes       — 已有：完整行程归档，含 satisfaction 字段（场景级总评，非轮级）
city_plans          — 已有：规划结果（用户是否采纳的参考）
```

> `turn_feedback` 与 `trip_episodes` 是互补关系：
> - `trip_episodes`：一次完整旅行规划的结果归档（场景级）
> - `turn_feedback`：每轮对话的即时反馈（轮级）
> - 两者通过 `task_id` 关联，可联合查询

---

## 5. 前端交互设计

### 5.1 评价触发时机

在 AI 回复的末尾（流式输出结束后）显示评价组件：

```
┌─────────────────────────────────────────────────┐
│ 你的杭州3天行程规划如下：                        │
│ ...                                              │
│                                                   │
│ 👍 有帮助   👎 没帮助    ⭐ (可选)              │
│ [  反馈文本（可选，不超过200字）                ] │
│                        [提交]                   │
└─────────────────────────────────────────────────┘
```

**规则：**
- 规划类回复（travel intent）默认显示评价组件
- 信息查询类（information intent）可选择显示（减少干扰）
- 对话类（conversation）不显示（无规划结果）
- 用户未操作，组件 60 秒后自动收起（不阻塞）

### 5.2 交互细节

1. **👍/👎 点击即写库**：无需点"提交"，点击后组件变为"已反馈"状态，减少一步
2. **⭐ 扩展评分**：点击 👍 后才显示星级选择（减少大部分用户的操作成本）
3. **追问场景**：用户追问后，前一条回复的评价组件隐藏（避免误评）
4. **反馈文本**：可选，点击 👍 后展开；点击 👎 后默认展开（因为负向反馈更值得收集）
5. **同类问题标识**：`feedback_type` 下拉框默认折叠，只有在点了 👎 时要求选择

---

## 6. Monitor 监控页面增强

### 6.1 新增"质量分析"Tab

在现有的 monitor 分析页面（analyze.py）新增一个 Tab，呈现：

**6.1.1 质量概览**
- 今日/本周/本月 平均满意度（⭐ 星级均分 / 👍 好评率）
- 趋势图（折线图，粒度：天/周/月）
- 沉默率：用户看完就走（无任何反馈）的占比

**6.1.2 意图维度质量对比**
| Intent | 样本数 | 好评率 | 平均耗时 | 平均 token | 重规划率 |
|---|---|---|---|---|---|
| travel | 1,240 | 78.3% | 12.4s | 3,842 | 8.2% |
| information | 892 | 85.1% | 3.1s | 420 | — |
| conversation | 430 | 91.0% | 1.2s | 180 | — |

**6.1.3 负向反馈聚类**
- 按 `feedback_type` 分组统计 TOP 频次
- `too_expensive` 的任务平均预算 vs 实际花费对比
- `wrong_city` 的典型 case（task_id 可跳转查看详情）

**6.1.4 单任务质量详情**
- 在现有的 trace 查看页，每 task_id 增加一个"质量"tab：
  - 该任务的满意度（显性/隐性）
  - 关联的 feedback_text 列表
  - 与同类意图平均分的对比

**6.1.5 告警规则**
- 连续 5 个任务好评率 < 60% → 发送管理员通知（邮件/Slack）
- 单任务 token 消耗 > 平均值 3σ → 触发 LLM 成本异常告警

---

## 7. 关键技术改造点

### 7.1 前端（AdminPage.jsx）

- 在 ChatPage 的 AI 回复流式结束后注入评价组件（React state 驱动）
- 评价数据 POST 到 gateway 新端点
- 评价组件样式参考 ChatGPT/Claude 的 feedback widget

### 7.2 后端

**新增 API：**
```
POST /internal/feedback        — 提交一条反馈
GET  /internal/feedback/{task_id}  — 查某任务的反馈详情
GET  /internal/feedback/stats — 聚合统计（供 monitor 调用）
```

**写入路径：**
```
前端评价组件
  → gateway POST /internal/feedback
    → backend service.submit_feedback()
      → 写 turn_feedback 表
        → 异步更新 trip_episodes.satisfaction（如果该 task 关联一个 episode）
```

### 7.3 数据库迁移

一条 Alembic 或手写 SQL 迁移脚本：
- 建 `turn_feedback` 表
- 在 `obs_tasks` 表加 `avg_satisfaction` / `feedback_count` 冗余字段（可选，优化 join 查询）

### 7.4 与现有 memory 的关系

- `episodic.search_episodes()` 已有 `satisfaction` 字段，可与 `turn_feedback.thumbs_up` 打通
- `decide_city_flags()` 的重规划判定逻辑不受影响（feedback 只读不写）

---

## 8. 分阶段实施

### 阶段 1（最小闭环，立竿见影）
- 建 `turn_feedback` 表
- 后端新增 `POST /internal/feedback` 写入接口
- 前端 AI 回复末尾加 👍/👎 按钮（无 stars/text/feedback_type）
- monitor 新增"好评率趋势"一个指标图

> 目标：2~3 天可上线，立刻能看到用户好评/差评分布

### 阶段 2（增强版）
- 补全 stars / feedback_text / feedback_type
- monitor 新增意图维度质量对比表
- 负向反馈聚类统计

### 阶段 3（智能化）
- 连续沉默用户触发"您对这次规划满意吗？"主动问询
- 隐性满意度推断规则落地
- 告警规则配置化

---

## 9. 面试叙事（可选背诵）

> "在 span 观测体系的基础上，我加了用户反馈层来解决'AI 回答好不好的问题'。
> 隐性信号（重规划次数/预算超限/会话续接率）和显性信号（点赞/评分）分开采集，
> 目的是既不打扰用户（不需要主动填问卷），又不丢失质量数据。
> 反馈写入一张专门的 `turn_feedback` 表，通过 `task_id` 和 span 数据 join，
> 在 monitor 里就能看到'这一次回复消耗了多少 token、耗时多久、用户给了几分'——
> 过程和结果在同一个视图里。"

---

## 附：文档变更记录

| 版本 | 日期 | 说明 |
|---|---|---|
| v1 | 2026-08-20 | 初稿：用户反馈与质量量化体系设计 |
