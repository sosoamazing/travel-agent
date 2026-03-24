"""
Executor Agent - 双模式架构
- 简单模式：ReAct循环，LLM自主决策
- 复杂模式：Plan-then-Execute，先列计划再执行
使用自己的上下文：executor_context
"""
from typing import Dict, Any, List
import json
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
from tools.tool_registry import get_tools_description_for_llm


async def execute_tool(tool_name: str, params: Dict, manager, 
                  destination: str, origin: str, travel_date: str) -> Any:
    """执行单个工具"""
    try:
        if tool_name == "rag_search":
            query = params.get("query", destination)
            return await query_travel_knowledge(query)
        
        elif tool_name == "gaode_weather":
            city = params.get("city", destination)
            return await manager.call_tool("Gaode Server", "maps_weather", city=city)
        
        elif tool_name == "gaode_poi_search":
            keywords = params.get("keywords", f"{destination} 景点")
            city = params.get("city", destination)
            return await manager.call_tool("Gaode Server", "maps_text_search", keywords=keywords, city=city)
        
        elif tool_name == "gaode_hotel_search":
            keywords = params.get("keywords", f"{destination} 酒店")
            city = params.get("city", destination)
            return await manager.call_tool("Gaode Server", "maps_text_search", keywords=keywords, city=city)
        
        elif tool_name == "lucky_day":
            date = params.get("date", travel_date)
            return await manager.call_tool("bazi Server", "getChineseCalendar", date=date)
        
        elif tool_name == "train_query":
            from tools.mcp_tools import get_mcp_manager
            mgr = await get_mcp_manager()
            
            station_result = await mgr.call_tool(
                "12306 Server",
                "get-station-code-of-citys",
                citys=f"{origin},{destination}"
            )
            
            from_code = None
            to_code = None
            if station_result and "error" not in str(station_result).lower():
                codes_data = json.loads(station_result) if isinstance(station_result, str) else station_result
                if isinstance(codes_data, dict):
                    for city in [origin, destination]:
                        if city in codes_data and isinstance(codes_data[city], list) and len(codes_data[city]) > 0:
                            code = codes_data[city][0].get('station_code') or codes_data[city][0].get('code')
                            if city == origin:
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
    print(f"\n{'='*60}")
    print("🔄 【简单模式】ReAct循环开始")
    print(f"{'='*60}")
    
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
    
    print(f"\n📋 配置信息:")
    print(f"  最大迭代次数: {max_iterations} (仅作安全限制)")
    print(f"  LLM可随时判断信息充分并提前结束")
    
    while iteration_count < max_iterations:
        iteration_count += 1
        print(f"\n{'='*60}")
        print(f"🔄 ReAct迭代 {iteration_count}/{max_iterations}")
        print(f"{'='*60}")
        
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
            
            print(f"\n🤖 LLM响应:")
            print(content[:500])
            
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
            
            print(f"\n💡 思考: {thought}")
            print(f"🎯 决定行动: {action}")
            print(f"📝 行动参数: {action_input}")
            print(f"➡️  继续循环: {should_continue}")
            
            # 关键逻辑：只要LLM判断信息充分，立即结束循环
            # 不需要等待走完所有迭代次数
            if action == "final_answer" or not should_continue:
                print(f"\n✅ LLM判断信息已充分，提前结束ReAct循环")
                print(f"   已执行迭代: {iteration_count}/{max_iterations}")
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
                            print(f"⚠️  工具 {action} 已成功执行过，跳过重复调用")
                            tool_already_executed = True
                            # 使用之前的结果
                            observation = prev_result
                            break
            
            if not tool_already_executed:
                print(f"\n🔧 执行工具: {action}")
                observation = await execute_tool(
                    action, action_input, manager,
                    destination, origin, travel_date
                )
                
                print(f"✅ 工具执行完成")
                
                tool_results.append({
                    "tool": action,
                    "result": observation,
                    "iteration": iteration_count
                })
                
                if action == "rag_search":
                    rag_results_history.append(str(observation))
                
                collected_info[action] = observation
            
        except Exception as e:
            print(f"❌ ReAct迭代异常: {e}")
            import traceback
            traceback.print_exc()
            break
    
    print(f"\n{'='*60}")
    print(f"✅ ReAct循环结束 (实际执行: {iteration_count}次)")
    print(f"{'='*60}")
    
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
    print(f"\n{'='*60}")
    print("📋 【复杂模式】Plan-then-Execute开始")
    print(f"{'='*60}")
    
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

请制定详细的查询计划，输出JSON格式：
{{
  "query_plan": [
    {{
      "tool": "工具名",
      "params": {{"参数名": "参数值"}},
      "description": "这一步的目的"
    }}
  ]
}}

可用工具：rag_search, gaode_weather, gaode_hotel_search, lucky_day, train_query

建议包含：rag_search + train_query + gaode_hotel_search + gaode_weather + lucky_day
"""
    
    try:
        print(f"\n🧠 DeepSeek R1开始制定计划...")
        response = await r1_llm.ainvoke([HumanMessage(content=problem)])
        content = response.content.strip()
        
        print(f"\n📋 R1返回原始内容:")
        print(content[:500])
        
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
        
        print(f"\n✅ 计划制定完成，共 {len(query_plan)} 步")
        print(f"   📝 说明：将完整执行所有计划步骤（预先规划好的）")
        for i, step in enumerate(query_plan):
            print(f"  步骤 {i+1}: {step.get('tool')} - {step.get('description')}")
        
        failed_steps = []
        
        for i, step in enumerate(query_plan):
            tool_name = step.get("tool", "")
            params = step.get("params", {})
            description = step.get("description", "")
            
            print(f"\n{'='*60}")
            print(f"📋 执行计划步骤 {i+1}/{len(query_plan)}: {tool_name}")
            print(f"{'='*60}")
            print(f"  描述: {description}")
            print(f"  参数: {params}")
            
            try:
                observation = await execute_tool(
                    tool_name, params, manager,
                    destination, origin, travel_date
                )
                
                print(f"✅ 工具执行完成")
                
                tool_results.append({
                    "tool": tool_name,
                    "result": observation,
                    "step": description,
                    "success": True
                })
                
                if tool_name == "rag_search":
                    rag_results_history.append(str(observation))
                
                collected_info[tool_name] = observation
                
            except Exception as step_error:
                print(f"⚠️ 步骤执行失败: {step_error}")
                print(f"   继续执行后续步骤...")
                
                failed_steps.append({
                    "step": i + 1,
                    "tool": tool_name,
                    "error": str(step_error)
                })
                
                tool_results.append({
                    "tool": tool_name,
                    "result": f"工具执行失败: {str(step_error)}",
                    "step": description,
                    "success": False
                })
        
        if failed_steps:
            print(f"\n⚠️ 部分步骤执行失败 ({len(failed_steps)}/{len(query_plan)}):")
            for failed in failed_steps:
                print(f"  步骤 {failed['step']}: {failed['tool']} - {failed['error']}")
        
        print(f"\n✅ Plan-then-Execute 执行完成")
        print(f"   成功步骤: {len(query_plan) - len(failed_steps)}/{len(query_plan)}")
        
    except Exception as e:
        print(f"❌ Plan-then-Execute异常: {e}")
        import traceback
        traceback.print_exc()
    
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
    print(f"\n{'='*60}")
    print("▶️ Executor Agent 开始执行")
    print(f"{'='*60}")
    
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
    
    print(f"📊 状态信息:")
    print(f"  query_mode: {query_mode}")
    print(f"  needs_deep_analysis: {needs_deep_analysis}")
    
    if query_mode == "simple":
        print(f"\n✅ 【简单模式】ReAct循环，LLM自主决策")
        executor_context = await react_loop(state, planner_context, executor_context)
    else:
        print(f"\n✅ 【复杂模式】Plan-then-Execute，先列计划再执行")
        executor_context = await plan_then_execute(state, planner_context, executor_context)
    
    print(f"\n✅ Executor Agent 执行完成")
    print(f"  工具执行结果数: {len(executor_context.get('tool_results', []))}")
    print(f"  RAG结果数: {len(executor_context.get('rag_results_history', []))}")
    print(f"  下一步: summarizer")
    print(f"{'='*60}\n")
    
    return {
        "executor_context": executor_context,
        "current_agent": "executor",
        "next_agent": "summarizer"
    }
