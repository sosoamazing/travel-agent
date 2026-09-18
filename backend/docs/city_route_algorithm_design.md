# 城内路线算法化与智能迭代设计计划

> 状态：设计中（待审阅）
> 目标：把城内路线规划从「LLM 生成式」重构为「算法定路线/成本 + LLM 只在决策点介入」，并配合 ReAct 式有限动作迭代收敛预算。
> 约束：不改变 graph 拓扑；保持全城市并发结构（`plan_all_cities_concurrent`）；预算超支永远是硬判断；`budget_fail` 兜底保留。
> 前置依赖：PostgreSQL（psycopg3，`async_db_connection()`）已有；高德 POI/地理编码已可用。

---

## 1. 背景与动机

### 1.1 现状问题

当前城内路线规划 `_plan_city_route` 是**纯 LLM 生成式**：

- 把候选景点、预算、偏好一次性丢给 LLM，让它输出完整 `_CityRoutePlan`（selected_attractions + days + total_ticket_cost + inter_attraction_transport_cost + daily_starting_point）。
- 超预算时盲重试：`_run_one_city_inner` 里 `for replan_i in range(total_replan)`，第 2/3 次只是重复"请删减昂贵景点、缩短距离"的提示，**没有真实工具反馈**（不知道实际酒店价、实际间距），经常第 3 次仍超预算。
- 景点坐标经高德 POI + `resolve_geo_location` 补全，但仅存进程内 `_geo_cache`，重启即丢，跨会话无法复用。

### 1.2 目标

1. **路线生成算法化**：距离/成本/几何中心由算法确定性计算，不依赖 LLM 逐字段生成。
2. **LLM 收敛到决策点**：只做"选哪些景点"（结合偏好）+ 超预算时的有限修改动作（换景点 / 移酒店中心），不再生成整个方案。
3. **预算双层判断**：预算超支 = 硬判断（规则）；失败/收敛 = 软判断（模型自主决定是否继续迭代）。
4. **地理数据持久化**：景点经纬度入 PostgreSQL 轻量缓存，跨会话复用省 API 调用。

---

## 2. 总体架构

```text
城内单城子图（_run_one_city_inner）
────────────────────────────────────────────────
Step A  景点提取（已有，LLM）→ 候选景点列表（含经纬度，来自高德POI/地理编码）
                          │
                          ▼  (候选景点已带 location + ticket_price + visit_hours)
Step B  LLM 选点          ── 从候选里按偏好/预算选出 N 个游玩景点（软决策点①）
                          │
                          ▼
Step C  算法定路线         ── 算法：每日时间预算内贪心排布 + 距离×系数算市内交通费
                          │      + 几何中心/搜索半径（已有 _geometric_center）
                          │
                          ▼
Step D  LLM 排每日顺序     ── 仅对选定景点，按偏好/开放时间/午饭 排进每天（软决策点②）
                          │
                          ▼
Step E  预算硬校验         ── cost > per_city 预算 ?  ← 硬判断（规则）
                          │
              超支 ────────┤  ── 触发迭代（见 §4）
                          ▼
Step F  酒店检索 + LLM 选  ── 以路线几何中心周边搜酒店 → LLM 选（已有，保持）
                          │
                          ▼
Step G  汇总 per_city_budget / 写回工作记忆（已有）
────────────────────────────────────────────────
```

核心变化：Step B/C/D 取代原 `_plan_city_route` 的 LLM 全量生成；Step E 的硬校验 + 迭代取代原 `for replan_i` 盲重试。

---

## 3. 关键设计

### 3.1 景点坐标持久化（PostgreSQL 轻量缓存）

现状：`_geo_cache`（进程内存）+ `resolve_geo_location()` 每次调用高德 maps_geo。

设计：新建轻量 KV 表 `geo_attractions`：

```sql
CREATE TABLE IF NOT EXISTS geo_attractions (
    city       TEXT NOT NULL,
    name       TEXT NOT NULL,
    location   TEXT NOT NULL,        -- "lng,lat"
    address    TEXT DEFAULT '',
    ticket_hint REAL DEFAULT 0,      -- 可选：门票价粗略记忆，便于后续复用
    visit_hint REAL DEFAULT 1.5,     -- 可选：建议游玩时长记忆
    resolved_at TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (city, name)
);
```

流程：
1. 景点提取后，对每个候选景点**先查库**（`SELECT location FROM geo_attractions WHERE city=? AND name=?`）。
2. 未命中 → `resolve_geo_location()` 调高德 → **回写库**。
3. 命中 → 直接复用坐标，省一次高德调用。

说明：
- 只做 KV 缓存，不做关系建模/复杂索引。
- 用 `async_db_connection()`（psycopg3 dict_row），复用现有 DB 访问路径。
- 可选升级：若想跨会话复用"门票/时长"，把 `ticket_hint/visit_hint` 一并缓存（当前为可选，第一期可只存坐标）。
- **约束**：表中数据是"搜索记忆"非权威数据，坐标可能随地图更新过期，建议保留 `resolved_at` 供后续 TTL 刷新。

### 3.2 路线算法化（含自驾费用模型）

#### 3.2.1 数据结构：费用系数表

原启发式 `_estimate_transport_cost_from_distance` 是单一阈值分段。改为**按出行模式分档**，由 `planner_context.is_self_driving` 选择：

```python
TRANSPORT_TIERS = {
    "default": [   # 步行/公交/打车 混合（现状默认）
        # (距离阈值km, 单位费用规则)
        (2.5,  ("walk",   0.0)),      # <2.5km 步行，0元
        (20,   ("bus",    5.0)),      # <20km  公交/地铁，5元固定
        (None, ("taxi",   2.5)),      # >=20km 打车，2.5元/km
    ],
    "self_driving": [   # 自驾
        # 自驾按里程油费+停车估算，单位低、容忍远距离
        (30,   ("drive", 0.8)),       # 油费约 0.8元/km（含折旧摊薄）
        (None, ("drive", 0.8)),       # 自驾无距离硬上限，费率恒定
    ],
}
```

说明：
- `default` 档：市内距离通常很短，多数段落在步行/公交档 → 城内交通费天然很低，正是用户预期的"城内交通费用一般都很低"。
- `self_driving` 档：费率恒定（油费+停车摊薄约 0.8 元/km），无需高德精确距离，用 Haversine `_geo_distance_km` 近似即可；自驾可容忍更远距离 → 距离阈值放宽。
- 第一期：先落地 `default` 档 + `is_self_driving` 参数与系数表结构；`self_driving` 具体费率写进文档供审阅，可一键开启。

> ⚠️ 自驾费率（0.8 元/km）为**估算初始值**，需结合本地油价/停车费校准，文档审阅重点。

#### 3.2.2 算法排布每日路线

输入：选定景点集合（带坐标/时长/门票）+ 出发地坐标 + 建议游玩天数 + 出行模式。

算法（贪心/最邻近）：
1. 用 `_geometric_center` 定位区域中心，作为每日出发参考点（已有）。
2. 每日维护"时间预算"（可用游玩时长，如 8h），当天优先塞入**距离当前点最近**且未访问、且塞得下的景点。
3. 顺序沿"由近到远"自然展开，避免折返。
4. 每日交通费 = 该日相邻景点间距离 × 对应模式系数，累加得 `inter_attraction_transport_cost`。
5. 总门票 = 选定景点 `ticket_price` 求和 → `total_ticket_cost`。
6. 输出 `hotel_center` / `hotel_center_radius`（复用现有 `_geometric_center` 逻辑）。

这套算法保证：**成本可精确预算、可复现、不依赖 LLM 逐段编造距离**。

#### 3.2.3 LLM 的角色收缩

- **决策点① 选点**：LLM 从候选景点（含价格/时长/坐标）按用户偏好选 N 个游玩景点。比现在"生成整方案"便宜得多，且可给出选择理由。
- **决策点② 排每日顺序**：LLM 只对"已选定"景点，按开放时间/午餐节奏/偏好排序成每日顺序。行程有人味，但成本/距离已由算法锁定。

两者皆可选步骤：若 LLM 选点/排序失败，算法有规则兜底（贪心全选最优先 + 就近排布），链路不中断。

---

## 4. 预算双层判断与 ReAct 迭代

### 4.1 双层判断

| 层 | 判断方 | 职责 | 不可协商性 |
|---|---|---|---|
| 硬判断 | 规则 | 预算是否超支（`cost > per_city 预算`，汇总后 `spent > total+buffer`） | **绝对红线，模型无豁免权** |
| 软判断 | 模型 | 是否继续迭代 / 是否收敛 / 是否放弃 | 只决定"过程"，不决定"结果" |

### 4.2 迭代循环（取代原 `for replan_i in range(total_replan)`）

```
for round_i in range(MAX_ROUND):        # MAX_ROUND=4~5，兜底防死循环
    # 软判断①：模型决定下一步动作（continue / stop）
    action = llm.判断(当前方案成本明细 + 超支差额 + 剩余削减空间)
    if action == stop:
        break                            # 模型认输/收敛，停止迭代（但仍走下方硬校验）

    # 软判断②：模型选择修改动作（有限动作空间，非全量重生成）
    move = llm.选择([换景点, 移酒店中心])
    if move == 换景点:
        删贵/换便宜候选 → 重新执行算法定路线（Step C）
    elif move == 移酒店中心:
        移动几何中心 → 重新周边搜酒店 → LLM 重选（Step F）

# 硬判断（无论模型做了什么）
if plan.cost > per_city 预算:
    标记超支 → 由并发汇总 / budget_fail 处理   # 模型无豁免权
else:
    完成
```

要点：
- `MAX_ROUND` 只是**安全阀**（防死循环），不是主机制；模型应在它之前主动收敛。
- 模型 `stop` 不等于"接受超支"，只是"停止迭代"，最终仍过硬校验。
- 每轮修改都是**增量**（换一个景点 / 移一次中心），比"从头重生成"收敛快、token 省。
- 收敛判据（可选优化）：若本轮削减幅度 < 5% 且仍超支，模型应倾向 `stop`，避免空转。

### 4.3 智能失败兜底

模型 `stop` 且硬校验未通过时：返回**"最优的一版 + 超支差额"**，交 `budget_fail_node`（现有）向用户说明并征询决策，不清空方案。这正是现有兜底行为，予以保留。

---

## 5. 需要你审阅/拍板的决策点

| # | 决策 | 我的建议 | 原因 |
|---|---|---|---|
| D1 | LLM 选点 + 算法排路线（方案2），还是全算法？ | **方案2（选点留 LLM）** | 你已定"先选景点比较好，毕竟是旅游"，选点需偏好判断，留 LLM；排布算法保证预算 |
| D2 | 自驾费率初始值 0.8 元/km | 文档先审阅，暂不硬编码开启 | 需结合本地油价/停车校准 |
| D3 | 每日时间预算默认多少（8h？） | 8h，可配 | 决定每天能塞几个景点 |
| D4 | 几何中心搜索半径范围 | 沿用现有 2~8km 自适应 | 已有成熟逻辑 |
| D5 | 地理缓存是否连带缓存门票/时长 | 第一期只存坐标 | 简化，门票/时长每次重估更准 |
| D6 | MAX_ROUND 取值 | 5 | 模型应早于它收敛 |

---

## 6. 落地步骤（后续）

1. 建 `geo_attractions` 表 + 读写封装（`geo_cache.py`）。
2. 重写 `_plan_city_route` → 拆成 `_select_attractions_llm` + `_route_by_algorithm` + `_order_days_llm`。
3. 重写 `_run_one_city_inner` 的超预算迭代为 §4.2 的双层循环。
4. 引入 `is_self_driving` 参数与双费用系数表。
5. 回归测试：现有 `backend/agent_nodes/tests/` 单测 + 全流程联调。
6. 按需更新 `budget_fail_node` 文案（说明"算法收敛后仍超支"）。

---

## 7. 风险与权衡

| 风险 | 缓解 |
|---|---|
| 算法路线"机械"缺个性 | LLM 保留排序决策点②，注入开放时间/偏好 |
| 自驾费率不准 | 文档审阅 + 参数化，后续校准 |
| 缓存坐标过期 | `resolved_at` + 可选 TTL 刷新 |
| 模型 `stop` 被滥用规避硬判断 | 硬判断永远独立于模型，`stop` 无豁免权 |
| 迭代轮内工具调用过多 | 优先复用已有数据（hotel/坐标缓存），仅换酒店/移中心时才重查 |
