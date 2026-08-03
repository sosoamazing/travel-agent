"""
Executor Agent - 双模式架构
- 简单模式：ReAct循环，LLM自主决策
- 复杂模式：Plan-then-Execute，先列计划再执行
使用自己的上下文：executor_context
"""
from typing import Dict, Any, List
import json
import logging
import asyncio
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage

from config.settings import (
    QWEN3_MODEL, QWEN3_API_BASE, DASHSCOPE_API_KEY, QWEN3_TEMPERATURE,
    R1_MODEL, DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, R1_TEMPERATURE
)
from config.prompts import REACT_THOUGHT_PROMPT
from graph.state import GlobalState
from tools.rag_tool import query_travel_knowledge
from tools.tool_registry import get_tools_description_for_llm, get_tool_by_name

logger = logging.getLogger(__name__)

# ── 特殊工具：无法通过注册表自动映射，需要自定义逻辑 ──
_SPECIAL_TOOLS = {"rag_search", "train_query", "r1_analysis", "final_answer"}


async def execute_tool(tool_name: str, params: Dict, manager) -> Any:
    """
    执行单个工具，支持两种路径：
    - 特殊工具（rag_search/train_query 等）：走自定义逻辑
    - 标准 MCP 工具：通过 tool_registry 查找 server/mcp_tool_name，自动调用
    """
    try:
        # ── 特殊工具：自定义逻辑 ──
        if tool_name == "rag_search":
            query = params.get("query", "")
            return await query_travel_knowledge(query)
        
        if tool_name == "train_query":
            from tools.mcp_tools import get_mcp_manager
            mgr = await get_mcp_manager()
            
            from_city = params.get("from", "")
            to_city = params.get("to", "")
            travel_date = params.get("date", "")

            station_result = await mgr.call_tool(
                "12306 Server",
                "get-station-code-of-citys",
                citys=f"{from_city},{to_city}"
            )
            
            from_code = None
            to_code = None
            if station_result and "error" not in str(station_result).lower():
                codes_data = json.loads(station_result) if isinstance(station_result, str) else station_result
                if isinstance(codes_data, dict):
                    for city in [from_city, to_city]:
                        if city in codes_data and isinstance(codes_data[city], list) and len(codes_data[city]) > 0:
                            code = codes_data[city][0].get('station_code') or codes_data[city][0].get('code')
                            if city == from_city:
                                from_code = code
                            else:
                                to_code = code
            
            if from_code and to_code:
                return await mgr.call_tool(
                    "12306 Server",
                    "get-tickets",
                    fromStation=from_code,
                    toStation=to_code,
                    date=travel_date
                )
            return "无法获取站点代码"

        # ── 标准 MCP 工具：从注册表查找映射，自动调用 ──
        tool_def = get_tool_by_name(tool_name)
        if tool_def and tool_def.server_name and tool_def.mcp_tool_name:
            return await manager.call_tool(
                tool_def.server_name,
                tool_def.mcp_tool_name,
                **params
            )
        
        return f"Unknown tool: {tool_name}"
    
    except Exception as e:
        return f"Tool execution failed: {str(e)}"


async def react_loop(state: GlobalState, planner_context: Dict, executor_context: Dict) -> Dict[str, Any]:
    """
    ReAct循环 - 简单模式：LLM自主决策
    
    核心逻辑：
    - 设置最大迭代次数作为安全限制
    - LLM每轮决定：继续收集信息 或 结束并生成答案
    - 只要LLM判断信息已充分，立即结束循环
    """
    logger.debug("=" * 60)
    logger.info("🔄 【简单模式】ReAct循环开始")
    logger.debug("=" * 60)
    
    tool_results = executor_context.get("tool_results", [])
    rag_results_history = executor_context.get("rag_results_history", [])
    collected_info = executor_context.get("collected_info", {})
    
    destination = planner_context.get("destination", "")
    origin = planner_context.get("origin", "")
    travel_days = planner_context.get("travel_days", 0)
    budget = planner_context.get("budget", 0)
    travel_date = planner_context.get("travel_date", "")
    preferences = planner_context.get("preferences", [])
    user_query = state.get("user_query", "")
    
    from tools.mcp_tools import get_mcp_manager
    manager = await get_mcp_manager()
    
    qwen3_llm = ChatOpenAI(
        model=QWEN3_MODEL,
        base_url=QWEN3_API_BASE,
        api_key=DASHSCOPE_API_KEY,
        temperature=QWEN3_TEMPERATURE
    )
    
    # 最大迭代次数仅作为安全限制，防止死循环
    # LLM可以在任何时候决定提前结束
    max_iterations = 8
    iteration_count = 0
    
    logger.info("📋 配置信息:")
    logger.info(f"  最大迭代次数: {max_iterations} (仅作安全限制)")
    logger.info("  LLM可随时判断信息充分并提前结束")
    
    while iteration_count < max_iterations:
        iteration_count += 1
        logger.debug("=" * 60)
        logger.info(f"🔄 ReAct迭代 {iteration_count}/{max_iterations}")
        logger.debug("=" * 60)
        
        # 构建已收集信息
        collected_info_str = []
        
        # 显示已执行的工具列表（防止重复调用）
        if tool_results:
            collected_info_str.append("📋 已执行的工具：")
            executed_tools = set()
            for result in tool_results:
                tool_name = result.get("tool", "")
                if tool_name not in executed_tools:
                    executed_tools.add(tool_name)
                    collected_info_str.append(f"  • {tool_name}")
        
        # 显示RAG检索结果
        if rag_results_history:
            collected_info_str.append("\n📖 RAG检索结果：")
            for i, rag_result in enumerate(rag_results_history[-2:], 1):  # 只显示最近2个结果
                # 截取过长的内容
                truncated = rag_result[:300] + "..." if len(rag_result) > 300 else rag_result
                collected_info_str.append(f"  [结果{i}]: {truncated}")
        
        # 显示工具返回结果
        if tool_results:
            collected_info_str.append("\n🔧 工具返回结果：")
            for result in tool_results[-3:]:  # 只显示最近3个结果
                tool_name = result.get("tool", "")
                tool_result = str(result.get("result", ""))
                # 截取过长的内容
                truncated = tool_result[:500] + "..." if len(tool_result) > 500 else tool_result
                collected_info_str.append(f"  [{tool_name}]: {truncated}")
        
        if not collected_info_str:
            collected_info_str = "暂无信息"
        else:
            collected_info_str = "\n".join(collected_info_str)
        
        # 获取工具描述
        tools_desc = get_tools_description_for_llm()
        
        prompt = REACT_THOUGHT_PROMPT.format(
            user_query=user_query,
            destination=destination,
            origin=origin,
            travel_days=travel_days,
            budget=budget,
            travel_date=travel_date,
            preferences=preferences,
            collected_info=collected_info_str,
            iteration_count=iteration_count,
            max_iterations=max_iterations,
            available_tools=tools_desc
        )
        
        try:
            response = await qwen3_llm.ainvoke([HumanMessage(content=prompt)])
            content = response.content.strip()
            
            logger.info("🤖 LLM响应:")
            logger.debug(content[:500])
            
            if "```json" in content:
                start = content.find("```json") + 7
                end = content.find("```", start)
                if end != -1:
                    content = content[start:end]
            elif "```" in content:
                start = content.find("```") + 3
                end = content.find("```", start)
                if end != -1:
                    content = content[start:end]
            
            if content.startswith("{"):
                brace_count = 0
                for i, char in enumerate(content):
                    if char == '{':
                        brace_count += 1
                    elif char == '}':
                        brace_count -= 1
                        if brace_count == 0:
                            content = content[:i+1]
                            break
            
            decision = json.loads(content.strip())
            
            thought = decision.get("thought", "")
            action = decision.get("action", "")
            action_input = decision.get("action_input", {})
            should_continue = decision.get("continue", True)
            
            logger.info(f"💡 思考: {thought}")
            logger.info(f"🎯 决定行动: {action}")
            logger.info(f"📝 行动参数: {action_input}")
            logger.info(f"➡️  继续循环: {should_continue}")
            
            # 关键逻辑：只要LLM判断信息充分，立即结束循环
            # 不需要等待走完所有迭代次数
            if action == "final_answer" or not should_continue:
                logger.info("✅ LLM判断信息已充分，提前结束ReAct循环")
                logger.info(f"   已执行迭代: {iteration_count}/{max_iterations}")
                break
            
            # 🚨 额外保护：检查该工具是否已成功执行过（仅对非rag_search工具）
            tool_already_executed = False
            
            # 只对 MCP 工具（非 rag_search 做重复调用检查
            if action != "rag_search":
                for result in tool_results:
                    if result.get("tool") == action:
                        # 检查之前的执行是否成功（没有"失败"、"error"等关键词）
                        prev_result = str(result.get("result", ""))
                        if "失败" not in prev_result.lower() and "error" not in prev_result.lower() and "Tool execution failed" not in prev_result:
                            logger.warning(f"⚠️  工具 {action} 已成功执行过，跳过重复调用")
                            tool_already_executed = True
                            # 使用之前的结果
                            observation = prev_result
                            break
            
            if not tool_already_executed:
                logger.info(f"🔧 执行工具: {action}")
                observation = await execute_tool(
                    action, action_input, manager
                )
                
                logger.info("✅ 工具执行完成")
                
                tool_results.append({
                    "tool": action,
                    "result": observation,
                    "iteration": iteration_count
                })
                
                if action == "rag_search":
                    rag_results_history.append(str(observation))
                
                collected_info[action] = observation
            
        except Exception as e:
            logger.error(f"❌ ReAct迭代异常: {e}")
            import traceback
            logger.error(traceback.format_exc())
            break
    
    logger.debug("=" * 60)
    logger.info(f"✅ ReAct循环结束 (实际执行: {iteration_count}次)")
    logger.debug("=" * 60)
    
    # 更新自己的上下文
    executor_context["tool_results"] = tool_results
    executor_context["rag_results_history"] = rag_results_history
    executor_context["collected_info"] = collected_info
    
    return executor_context


async def plan_then_execute(state: GlobalState, planner_context: Dict, executor_context: Dict) -> Dict[str, Any]:
    """
    Plan-then-Execute - 复杂模式：先列计划再执行
    
    核心逻辑：
    - 先由 DeepSeek R1 制定详细的查询计划
    - 然后按计划依次执行每个步骤
    - 增加容错机制：某个工具失败不影响后续步骤
    - 完整执行完所有计划步骤（因为是预先规划好的）
    """
    logger.debug("=" * 60)
    logger.info("📋 【复杂模式】Plan-then-Execute开始")
    logger.debug("=" * 60)
    
    destination = planner_context.get("destination", "")
    origin = planner_context.get("origin", "")
    travel_days = planner_context.get("travel_days", 0)
    budget = planner_context.get("budget", 0)
    travel_date = planner_context.get("travel_date", "")
    preferences = planner_context.get("preferences", [])
    user_query = state.get("user_query", "")
    
    tool_results = executor_context.get("tool_results", [])
    rag_results_history = executor_context.get("rag_results_history", [])
    collected_info = executor_context.get("collected_info", {})
    
    from tools.mcp_tools import get_mcp_manager
    manager = await get_mcp_manager()
    
    r1_llm = ChatOpenAI(
        model=R1_MODEL,
        base_url=DEEPSEEK_BASE_URL,
        api_key=DEEPSEEK_API_KEY,
        temperature=R1_TEMPERATURE
    )
    
    tools_desc = get_tools_description_for_llm()
    
    problem = f"""
用户的旅行需求：
最新查询：{user_query}

已提取的信息：
- 目的地：{destination}
- 出发地：{origin}
- 总天数：{travel_days}
- 总预算：{budget}元
- 出发日期：{travel_date}
- 偏好：{', '.join(preferences) if preferences else '无'}

请制定一个带依赖关系的查询计划（DAG），每个步骤包含输入/输出共享控制，输出严格JSON格式：
{{
  "query_plan": [
    {{
      "id": "step1",
      "tool": "gaode_geo",
      "params": {{"address": "西安"}},
      "description": "获取西安经纬度坐标",
      "depends_on": [],
      "input_from_shared": [],
      "output_to_shared": [1]
    }},
    {{
      "id": "step2",
      "tool": "gaode_around_search",
      "params": {{"keywords": "餐厅", "location": "step1.location"}},
      "description": "搜索坐标附近的餐厅",
      "depends_on": ["step1"],
      "input_from_shared": [1],
      "output_to_shared": []
    }}
  ]
}}

规则说明：
1. ID必须是唯一标识符，推荐step1, step2, ...
2. depends_on：必须在当前步骤之前完成的步骤id列表，无依赖填[]

3. 共享字典机制：
   - 每批次工具执行完成后，结果自动存入全局共享字典
   - 始终存入 sid → 工具完整返回结果
   - output_to_shared 控制展开哪些字段为 sid.字段名

4. input_from_shared（输入位置数组）：
   - params 的 key 按字母序排序后得到位置索引
   - 例：params={{"bb":1,"aa":2}} → 排序后 [aa, bb]，aa是位置0，bb是位置1
   - input_from_shared=[1] 表示位置1（bb）的值是共享字典的key，执行时查表替换
   - input_from_shared=[] 表示所有参数直接传递
   - 位置的值作为key查共享字典，如 key="step1.location" → 取到对应坐标

5. output_to_shared（输出位置数组）：
   - 工具返回JSON对象的字段按字母序排序
   - output_to_shared=[1] 表示展开第1个字段（按字母序第2个）为 sid.字段名
   - output_to_shared=[] 表示不展开任何字段（仍会存 sid → 完整结果）
   - 不填此字段：默认展开所有字段
   - 例：gaode_geo 返回 {{"city":"西安","location":"108.9,34.2","status":"ok"}}
     → 字段排序: [city, location, status]
     → output_to_shared=[1] → 展开 step1.location = "108.9,34.2"
     → output_to_shared=[0,2] → 展开 step1.city 和 step1.status

6. 后续步骤引用时用 "sid.字段名" 作为key（如 "step1.location"）

可用工具及参数说明：
{tools_desc}

建议必须包含：rag_search + train_query + gaode_hotel_search + gaode_weather + lucky_day
"""
    
    try:
        logger.info("🧠 DeepSeek R1开始制定计划...")
        response = await r1_llm.ainvoke([HumanMessage(content=problem)])
        content = response.content.strip()
        
        logger.info("📋 R1返回原始内容:")
        logger.debug(content[:500])
        
        if "```json" in content:
            start = content.find("```json") + 7
            end = content.find("```", start)
            if end != -1:
                content = content[start:end]
        elif "```" in content:
            start = content.find("```") + 3
            end = content.find("```", start)
            if end != -1:
                content = content[start:end]
        
        plan_data = json.loads(content.strip())
        query_plan = plan_data.get("query_plan", [])
        
        logger.info(f"✅ 计划制定完成，共 {len(query_plan)} 步")
        for step in query_plan:
            deps = step.get("depends_on", [])
            dep_str = f" → 依赖 {deps}" if deps else ""
            logger.info(f"  {step.get('id', '?')}: {step.get('tool')} - {step.get('description')}{dep_str}")
        
        # ── 拓扑排序分层并行执行引擎（共享字典 + 位掩码参数解析）──
        shared_dict: Dict[str, Any] = {}    # 跨步骤共享字典，每批次自动存入
        step_index: Dict[str, int] = {}      # step_id → 步骤序号（用于日志）
        in_degree: Dict[str, int] = {}       # step_id → 未完成的前置依赖数
        dependents: Dict[str, list] = {}     # step_id → 依赖它的步骤id列表
        step_map: Dict[str, Dict] = {}       # step_id → 完整step定义
        
        for idx, step in enumerate(query_plan):
            sid = step.get("id", f"step{idx+1}")
            step["id"] = sid  # 保证id存在
            step_index[sid] = idx
            step_map[sid] = step
            deps = step.get("depends_on", [])
            in_degree[sid] = len(deps)
            for dep in deps:
                dependents.setdefault(dep, []).append(sid)
        
        def resolve_input(params: Dict, from_shared: list, shared: Dict[str, Any]) -> Dict:
            """
            根据位置数组从共享字典解析输入参数：
            - params 的 key 按字母序排序
            - from_shared = [i, j, ...] 表示第i个、第j个参数的值是共享字典的key，查表替换
            """
            if not from_shared or not params:
                return dict(params)
            
            ordered_keys = sorted(params.keys())
            resolved = {}
            for i, key in enumerate(ordered_keys):
                if i in from_shared:
                    lookup_key = str(params[key])
                    resolved[key] = shared.get(lookup_key, params[key])
                else:
                    resolved[key] = params[key]
            return resolved
        
        def store_output(sid: str, result: Any, to_shared: list, shared: Dict):
            """
            根据位置数组将输出存入共享字典：
            - 始终存入 sid → 完整结果
            - to_shared = [i, j, ...] 控制展开第i个、第j个字段为 sid.field
            - to_shared 为空列表 []: 只存 sid，不展开
            - to_shared 为 None: 展开所有字段
            """
            shared[sid] = result
            
            if to_shared is not None and len(to_shared) == 0:
                return
            
            try:
                parsed = json.loads(str(result)) if isinstance(result, str) else result
            except (json.JSONDecodeError, TypeError):
                return
            
            if not isinstance(parsed, dict):
                return
            
            ordered_fields = sorted(parsed.keys())
            
            if to_shared is None:
                for k, v in parsed.items():
                    shared[f"{sid}.{k}"] = v
            else:
                for idx in to_shared:
                    if 0 <= idx < len(ordered_fields):
                        field = ordered_fields[idx]
                        shared[f"{sid}.{field}"] = parsed[field]
        
        async def run_step(sid: str):
            """执行单个步骤（内部使用，带共享字典参数解析）"""
            step = step_map[sid]
            tool_name = step["tool"]
            from_shared = step.get("input_from_shared", [])
            to_shared = step.get("output_to_shared", None)
            params = resolve_input(step.get("params", {}), from_shared, shared_dict)
            description = step.get("description", "")
            idx = step_index.get(sid, 0)
            in_str = f" in{from_shared}" if from_shared else ""
            out_str = f" out{to_shared}" if to_shared is not None else ""
            logger.info(f"▶ 步骤 {idx+1}/{len(query_plan)} [{sid}]: {tool_name}{in_str}{out_str} - {description}")
            try:
                result = await execute_tool(tool_name, params, manager)
                logger.info(f"✅ [{sid}] 完成: {tool_name}")
                return (sid, tool_name, description, to_shared, result, None)
            except Exception as e:
                logger.warning(f"⚠️ [{sid}] 失败: {tool_name} - {e}")
                return (sid, tool_name, description, to_shared, None, str(e))
        
        pending = set(step_map.keys())
        failed_steps = []
        batch_num = 0
        
        while pending:
            # 收集当前入度为 0 的步骤
            ready = [sid for sid in pending if in_degree.get(sid, 0) == 0]
            if not ready:
                logger.warning(f"⚠️ 存在循环依赖，剩余 {len(pending)} 步无法调度")
                for sid in pending:
                    failed_steps.append({
                        "step": step_index.get(sid, 0)+1,
                        "tool": step_map[sid].get("tool", "?"),
                        "error": "循环依赖"
                    })
                break
            
            batch_num += 1
            logger.info(f"🚀 第 {batch_num} 批并发: {[step_map[s]['tool'] for s in ready]}")
            
            batch_results = await asyncio.gather(*[run_step(sid) for sid in ready])
            
            # ── 本批次完成后，按 output_to_shared 存入共享字典 ──
            for sid, tool_name, description, to_shared, observation, error in batch_results:
                if error:
                    failed_steps.append({
                        "step": step_index.get(sid, 0)+1,
                        "tool": tool_name,
                        "error": error
                    })
                else:
                    store_output(sid, observation, to_shared, shared_dict)
                    tool_results.append({
                        "tool": tool_name,
                        "result": observation,
                        "step": description,
                        "success": True
                    })
                    if tool_name == "rag_search":
                        rag_results_history.append(str(observation))
                    collected_info[tool_name] = observation
                
                # 标记完成，减少依赖它的步骤的入度
                pending.discard(sid)
                for dep in dependents.get(sid, []):
                    in_degree[dep] = max(0, in_degree[dep] - 1)
        
        if failed_steps:
            logger.warning(f"⚠️ 部分步骤执行失败 ({len(failed_steps)}/{len(query_plan)}):")
            for failed in failed_steps:
                logger.warning(f"  步骤 {failed['step']}: {failed['tool']} - {failed['error']}")
        
        logger.info("✅ Plan-then-Execute 执行完成")
        logger.info(f"   成功步骤: {len(query_plan) - len(failed_steps)}/{len(query_plan)}")
        
    except Exception as e:
        logger.error(f"❌ Plan-then-Execute异常: {e}")
        import traceback
        logger.error(traceback.format_exc())
    
    # 更新自己的上下文
    executor_context["tool_results"] = tool_results
    executor_context["rag_results_history"] = rag_results_history
    executor_context["collected_info"] = collected_info
    
    return executor_context


async def executor_agent_node(state: GlobalState) -> Dict[str, Any]:
    """
    执行Agent节点 - 双模式选择
    使用自己的上下文：executor_context
    
    根据query_mode选择：
    - simple: ReAct循环，LLM自主决策
    - full: Plan-then-Execute，先列计划再执行
    """
    logger.debug("=" * 60)
    logger.info("▶️ Executor Agent 开始执行")
    logger.debug("=" * 60)
    
    # 从 Planner 的上下文中获取信息
    planner_context = state.get("planner_context") or {}
    needs_deep_analysis = planner_context.get("needs_deep_analysis", False) if planner_context else False
    query_mode = planner_context.get("query_mode", "full") if planner_context else "full"
    
    # 初始化或获取自己的上下文
    executor_context = state.get("executor_context") or {
        "tool_results": [],
        "rag_results_history": [],
        "collected_info": {}
    }
    
    logger.info("📊 状态信息:")
    logger.info(f"  query_mode: {query_mode}")
    logger.info(f"  needs_deep_analysis: {needs_deep_analysis}")
    
    if query_mode == "simple":
        logger.info("✅ 【简单模式】ReAct循环，LLM自主决策")
        executor_context = await react_loop(state, planner_context, executor_context)
    else:
        logger.info("✅ 【复杂模式】Plan-then-Execute，先列计划再执行")
        executor_context = await plan_then_execute(state, planner_context, executor_context)
    
    logger.info("✅ Executor Agent 执行完成")
    logger.info(f"  工具执行结果数: {len(executor_context.get('tool_results', []))}")
    logger.info(f"  RAG结果数: {len(executor_context.get('rag_results_history', []))}")
    logger.info("  下一步: summarizer")
    logger.debug("=" * 60)
    
    return {
        "executor_context": executor_context,
        "current_agent": "executor",
        "next_agent": "summarizer"
    }
