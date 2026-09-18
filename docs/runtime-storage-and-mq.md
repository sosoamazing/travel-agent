# 运行态存储、LLM 载荷与消息队列

> 状态：**已定裁剪，按此落地，细节可后调**
> 日期：2026-09-18
> 关联：`docs/observability-design.md`（span 树）、`docs/task-state-externalization.md` / `docs/mq-design.md`（已按本文裁剪，后两者中未实施部分勿当现状）

本文回答三件事：用户怎么增量查任务、LLM 提示词怎么分表、要不要上消息队列。

---

## 1. 分层（先把职责切开）

| 数据 | 存哪 | 生命周期 | 谁读 |
|---|---|---|---|
| 进行中任务状态 / 刚结束的业务结果 | Redis Hash `task:status:{task_id}` | 运行中无 TTL；结束后 TTL 24h | 前端轮询、多实例查状态 |
| 任务头 + span 树 + 业务结果副本 | PostgreSQL `obs_*` | 长期 | monitor、复盘、Redis miss |
| 提示词模板 | PostgreSQL `prompt_versions` | 模板变更才新增一行；agent 每次取最新 | agent 干活 |
| 系统发布清单 | PostgreSQL `agent_releases` | 管理员显式记录 | 管理端 changelog |
| 某次调用实际送进模型的内容 | PostgreSQL `obs_llm_payloads` | 随 span | 复盘「当时模型看见了什么」 |
| SSE token / 进度 | 仍进程内 `asyncio.Queue` | 仅当前执行进程 | 前端 stream |

热路径不扫大 TEXT。列表和增量只扫 span 元数据；需要正文再按 `span_id` 取 payload。

---

## 2. 增量查询

用户查某个任务的执行情况：先用 `task_id` 定位（主键），再用 `user_id` 做归属校验，再用时间戳下限拉新 span。

```sql
-- 任务列表（按用户）
CREATE INDEX idx_obs_tasks_user_ts ON obs_tasks (user_id, start_ts DESC);

-- 某任务 span：无时间 = since_ts=0 拉全量；有下限则追加。不另建单列 task_id。
CREATE INDEX idx_node_spans_task_ts ON obs_node_spans (task_id, start_ts);
CREATE INDEX idx_llm_spans_task_ts  ON obs_llm_spans  (task_id, start_ts);
CREATE INDEX idx_mcp_spans_task_ts  ON obs_mcp_spans  (task_id, start_ts);
```

接口：

```
GET /tasks/{task_id}/events?since_ts={unix_float}
```

- 鉴权：`task_id` 命中后检查 `user_id`。
- 查询：`start_ts > since_ts`，按 `(start_ts, span_id)` 排序。
- 返回 `next_since_ts`，前端下次带上。同毫秒重复时客户端用 `span_id` 去重。
- **不要**把 `(user_id, task_id, ts)` 当定位主键：`task_id` 已全局唯一。

进行中任务优先读 Redis 快照；span 增量读 PG（写缓冲会在该接口上先 flush 本任务）。

---

## 3. LLM：版本表 + 嵌入内容表

拆的是「模板」和「这一次渲染结果」，不是「输入 / 输出」两张日志表。

```
prompt_versions          运行时目录：agent + usage + version(hash) + 模板正文
agent_releases           管理侧清单：发布号 + git + 当时 prompt_set 快照 + note
obs_llm_spans            调用元数据：token / 状态 / 时间 / prompt_id(agent/usage)+version / output
obs_llm_payloads         1:1 span_id：渲染后的完整 input（截断）
```

- `prompt_versions` 只在模板变更时插入（`ON CONFLICT DO NOTHING`），不随调用膨胀。
- agent 干活：`WHERE agent=? AND usage=? ORDER BY created_at DESC LIMIT 1`。不经过 Redis，不 JOIN `agent_releases`。
- 版本用内容 hash 前 12 位，不和任务上的 `AGENT_VERSION` 绑死。
- `agent_releases.prompt_set` 是给人看的快照，不是运行配置。
- `obs_llm_payloads.span_id` 主键，任务合并仍走 `task_id + parent_id + start_ts`。
- output 仍在 `obs_llm_spans`，不在 payload 再存一份。
- input 默认截断 32KB（`LLM_PAYLOAD_MAX_CHARS`）。

独立写入：`insert_prompt.py` 插模板；`record_release.py` 记发布。代码 registry 只做空库种子。

---

## 4. Redis 任务态

进程内 `TaskManager` 仍是本进程热路径（尤其 SSE 队列）。旁路双写 Redis：

- Key：`task:status:{task_id}`
- 进度（current_node）：只写 Redis（PG 无此列）
- 终态：先写 PG `result_payload` / obs 终态，成功后再写 Redis，然后 TTL
- 读：本进程内存 → Redis → PG；PG 命中回填 Redis
- Redis 不可用：打日志，降级内存 + PG，不打断任务

本阶段 **SSE 仍绑执行进程**。多实例不断流要等事件总线（见下一节），不是这批的范围。

---

## 5. 消息队列到底有没有必要？

**分两件事，结论不同。**

### 5.1 观测 / LLM 入库：现在没有必要上 Kafka

已经有 `_SpanWriteBuffer`（32 条 / 100ms / `end_task` 强制 flush）。消费方只有 PostgreSQL，扇出 = 1。

**乱序可以接受。** start/end 都走 `INSERT ... ON CONFLICT DO UPDATE`：

- end 先到：直接插入完整行（含 output）
- start 后到：只补 identity 字段，不覆盖已有 `output` / payload.`input` / 非 `running` 终态
- 空值不盖有值：`output` 或 `input` 已经有数据时，晚到的空字符串或 `running` 占位丢掉

所以「终态已可见但 span 未到」只是短暂窗口，增量查询 `since_ts` 会把晚到的行补上。不需要用 Kafka 的分区顺序来保证入库。

入库洪峰来自一次规划里上百次 span 写，不是用户 QPS。进程内批量写对症。以后若真把观测写入独立消费者，仍然用同一套 upsert，不必先上 Kafka。

### 5.2 任务调度：现在没有必要，以后可能有

当前是 `submit_chat` → `asyncio.create_task` → 内存任务表 + 进程内 Semaphore。这在 **单 backend 实例、可接受重启丢进行中任务、靠 checkpoint 续跑** 时够用。

需要队列的信号（任一出现再做，不要提前上）：

1. 要跑 **多个 backend / worker**，提交和执行不在同一进程
2. 进程崩溃后，**尚未开始或排队中的任务**必须自动再投（checkpoint 只救「执行到一半」）
3. 提交洪峰明显超过 `TASK_CONCURRENCY`，需要积压而不是把请求堵在 HTTP 里
4. 多实例下 SSE 必须不断流（事件要出进程）

到那一步，优先 **Redis Streams**（消费组 + ACK + 与任务态共用一个 Redis），而不是 Kafka。

Kafka 适合多消费者、长时间留存、十万级事件/秒。这里每版本几百到几千任务，Docker 配额也紧，Kafka/KRaft 运维成本高于收益。`docs/mq-design.md` 的选型仍然成立。

### 5.3 和「削峰入库」的关系

「削峰」若指 **别把 span 同步写打爆 Postgres**：写缓冲已经做了。  
「削峰」若指 **别让突发用户请求打爆 LLM / 工作流**：那是任务队列 + 并发上限，不是 Kafka 写库。

本阶段用 Redis 存运行态 + Semaphore 限流，已经覆盖第二类的单机形态。队列是扩容时的下一步，不是现在的前置。

---

## 6. 本阶段落地清单

1. PG：`prompt_versions(agent,usage,version,content)`、`agent_releases`、`obs_llm_payloads`、`obs_tasks.result_payload`、增量索引
2. 提示词 registry 作种子；运行时从库取最新；LLM 调用自动记 input
3. Redis 任务快照双写，结束后 TTL；结果落 PG
4. `GET /tasks/{task_id}/events?since_ts=`
5. compose 增加 Redis；`REDIS_URL` 未配置则跳过 Redis
6. **不上** Kafka / RabbitMQ / Redis Streams 调度

后续可调：SSE 改 Pub/Sub、再评估 Redis Streams 调度。
