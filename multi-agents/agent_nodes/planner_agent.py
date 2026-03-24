"""
Planner Agent - 负责制定详细的执行计划
使用自己的上下文：planner_context
"""
from typing import Dict, Any
from datetime import datetime
import json
from pydantic import BaseModel, Field
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
from config.settings import QWEN3_MODEL, QWEN3_API_BASE, DASHSCOPE_API_KEY, QWEN3_TEMPERATURE
from config.prompts import PLANNER_SYSTEM_PROMPT
from graph.state import GlobalState


class TravelPlanExtraction(BaseModel):
    """提取的旅行计划信息"""
    destination: str = Field(description="Destination city in Chinese")
    origin: str = Field(description="Origin city in Chinese")
    travel_days: int = Field(description="Number of travel days")
    budget: float = Field(description="Budget in yuan")
    travel_date: str = Field(description="Departure date in YYYY-MM-DD format")
    preferences: list[str] = Field(description="Travel preferences")
    needs_deep_analysis: bool = Field(default=False)
    tools_needed: list[str] = Field(default_factory=lambda: ["旅游攻略检索", "12306查询"])


def detect_multi_destination(user_query: str, extraction: dict) -> dict:
    """检测是否为多目的地场景（排除往返/回程误判）
    
    Args:
        user_query: 用户原始查询
        extraction: Planner提取的结果
    
    Returns:
        dict: {
            'is_multi_destination': bool,
            'detected_keywords': List[str],
            'raw_destination_text': str
        }
    """
    roundtrip_keywords = ["往返", "来回", "回程", "返程", "返回"]
    if any(kw in user_query for kw in roundtrip_keywords):
        return {
            'is_multi_destination': False,
            'detected_keywords': [],
            'raw_destination_text': extraction.get('destination', ''),
            'detection_method': 'roundtrip_excluded'
        }
    
    multi_dest_keywords = [
        "再去", "然后去", "接着去", "顺便去",
        "再到", "然后到", "接着到",
        "再去看看", "再看看",
        "之后去", "之后到"
    ]
    detected_keywords = [kw for kw in multi_dest_keywords if kw in user_query]
    if detected_keywords:
        return {
            'is_multi_destination': True,
            'detected_keywords': detected_keywords,
            'raw_destination_text': extraction.get('destination', ''),
            'detection_method': 'keyword'
        }
    
    destination = extraction.get('destination', '') or ''
    origin = extraction.get('origin', '') or ''
    norm = destination.replace(',', '，').replace('、', '，')
    cities = [c.strip() for c in norm.split('，') if c.strip()]
    unique_cities = []
    for c in cities:
        if c not in unique_cities:
            unique_cities.append(c)
    
    if len(unique_cities) >= 3:
        return {
            'is_multi_destination': True,
            'detected_keywords': [],
            'raw_destination_text': destination,
            'detection_method': 'comma_separated_3plus'
        }
    
    if len(unique_cities) == 2:
        if origin and origin in unique_cities:
            return {
                'is_multi_destination': False,
                'detected_keywords': [],
                'raw_destination_text': destination,
                'detection_method': 'origin_pair_excluded'
            }
        return {
            'is_multi_destination': True,
            'detected_keywords': [],
            'raw_destination_text': destination,
            'detection_method': 'comma_separated_2'
        }
    
    return {
        'is_multi_destination': False,
        'detected_keywords': [],
        'raw_destination_text': destination
    }


async def planner_agent_node(state: GlobalState) -> Dict[str, Any]:
    """
    规划Agent节点 - 意图识别
    使用自己的上下文：planner_context
    
    职责：
    1. 分析用户需求
    2. 提取关键信息（目的地、时间、预算等）
    3. 检测是否需要复杂推理（多目的地、复杂约束等）
    4. 识别查询模式（简单模式 vs 完整规划模式）
    5. 初始化自己的上下文，不预先指定plan_steps
    """
    print(f"\n{'='*60}")
    print("▶️ Planner Agent 开始执行（意图识别）")
    print(f"{'='*60}")
    
    user_query = state.get("user_query", "") or ""
    print(f"📝 用户查询: {user_query}")
    
    # 从全局状态读取对话历史（只需要用户当前查询 + 最近几轮来理解上下文）
    conversation_messages = state.get("messages") or []
    
    # 初始化或获取自己的上下文
    planner_context = state.get("planner_context") or {
        "destination": None,
        "origin": None,
        "travel_days": None,
        "budget": None,
        "travel_date": None,
        "preferences": None,
        "raw_destination_text": None,
        "needs_deep_analysis": False,
        "query_mode": None,
        "scenario_type": None,
        "tools_needed": None,
        "needs_clarification": False,
        "clarification_question": None
    }
    
    qwen3_llm = ChatOpenAI(
        model=QWEN3_MODEL,
        base_url=QWEN3_API_BASE,
        api_key=DASHSCOPE_API_KEY,
        temperature=QWEN3_TEMPERATURE
    )
    
    try:
        qwen3_structured = qwen3_llm.with_structured_output(TravelPlanExtraction)
    except Exception:
        qwen3_structured = None
    
    today = datetime.now().strftime("%Y-%m-%d")
    dynamic_prompt = PLANNER_SYSTEM_PROMPT.replace("{{TODAY}}", today)
    
    messages = [SystemMessage(content=dynamic_prompt)]
    for msg in conversation_messages:
        if isinstance(msg, (HumanMessage, AIMessage)):
            messages.append(msg)
        elif isinstance(msg, dict):
            if msg.get("type") == "human" or msg.get("role") == "user":
                messages.append(HumanMessage(content=msg.get("content", "")))
            elif msg.get("type") == "ai" or msg.get("role") == "assistant":
                messages.append(AIMessage(content=msg.get("content", "")))
    
    result = {}
    if qwen3_structured is not None:
        try:
            extraction = await qwen3_structured.ainvoke(messages)
            planner_context["destination"] = extraction.destination
            planner_context["origin"] = extraction.origin
            planner_context["travel_days"] = extraction.travel_days
            planner_context["budget"] = extraction.budget
            planner_context["travel_date"] = extraction.travel_date
            planner_context["preferences"] = extraction.preferences
            planner_context["needs_deep_analysis"] = extraction.needs_deep_analysis
            planner_context["tools_needed"] = extraction.tools_needed
            
            print(f"\n🔍 LLM提取结果:")
            print(f"  目的地: {planner_context.get('destination')}")
            print(f"  出发地: {planner_context.get('origin')}")
            print(f"  旅行天数: {planner_context.get('travel_days')}")
            print(f"  预算: {planner_context.get('budget')}")
            print(f"  出发日期: {planner_context.get('travel_date')}")
            print(f"  需要深度分析: {planner_context.get('needs_deep_analysis')}")
            
            # 首先基于关键词进行简单查询预检测
            simple_keywords = ["天气", "景点", "美食", "攻略", "推荐", "怎么样", "如何", "好玩", "哪里"]
            has_simple_keyword = any(kw in user_query for kw in simple_keywords)
            print(f"\n🔍 简单查询检测:")
            print(f"  包含简单关键词: {has_simple_keyword}")
            
            multi_dest_detection = detect_multi_destination(user_query, planner_context)
            if multi_dest_detection.get('is_multi_destination', False):
                planner_context['needs_deep_analysis'] = True
                planner_context['scenario_type'] = 'multi_destination'
                planner_context['raw_destination_text'] = multi_dest_detection.get('raw_destination_text', planner_context['destination'])
            else:
                planner_context['scenario_type'] = 'simple' if not planner_context.get('needs_deep_analysis', False) else 'complex'
            
            # 检测是否是简单查询（和原始项目逻辑一致 + 关键词检测）
            # 简单查询：只有目的地，没有旅行天数/预算/日期，或者包含简单查询关键词
            is_simple_query = (
                (planner_context['destination'] and 
                 not planner_context['travel_days'] and 
                 not planner_context['budget'] and 
                 not planner_context['travel_date']) or
                has_simple_keyword
            )
            
            print(f"  is_simple_query: {is_simple_query}")
            
            if is_simple_query:
                print(f"\n🔍 检测到简单查询模式")
                print(f"  ➡️ 下一步: executor（ReAct模式，LLM自主决策）")
                print(f"  ✅ 设置 query_mode 为 'simple'")
                
                # 如果没有提取到目的地，尝试从用户查询中提取
                destination_for_query = planner_context.get('destination', '')
                if not destination_for_query:
                    # 简单的城市提取逻辑
                    cities = ["北京", "上海", "广州", "深圳", "杭州", "南京", "苏州", "成都", "重庆", "武汉", "西安", "长沙", "青岛", "大连", "厦门", "三亚", "昆明", "丽江", "桂林", "黄山"]
                    for city in cities:
                        if city in user_query:
                            destination_for_query = city
                            planner_context['destination'] = city
                            break
                    print(f"  从查询中提取到目的地: {destination_for_query}")
                
                planner_context["query_mode"] = "simple"
                planner_context["needs_clarification"] = False
                planner_context["tools_needed"] = []
                
                return {
                    "planner_context": planner_context,
                    "current_agent": "planner",
                    "next_agent": "executor"
                }
            
            print(f"\n📋 检测到完整规划模式")
            
            # 完整规划模式：检查关键信息是否缺失
            missing_fields = []
            if not planner_context['destination']:
                missing_fields.append("目的地")
            # 如果有目的地但没有出发地，则需要询问（用于查询交通）
            if planner_context['destination'] and not planner_context['origin']:
                missing_fields.append("出发地")
            
            print(f"  缺失的字段: {missing_fields}")
            
            if missing_fields:
                clarification = f"请问您的{''.join(missing_fields)}是哪里？这样我才能为您查询具体的交通和行程信息。"
                print(f"  ❌ 需要澄清: {clarification}")
                planner_context["needs_clarification"] = True
                planner_context["clarification_question"] = clarification
                return {
                    "planner_context": planner_context,
                    "current_agent": "planner",
                    "next_agent": None,
                    "is_complete": True
                }
            
            print(f"  ✅ 信息完整，准备执行")
            print(f"  ➡️ 下一步: executor（Plan-then-Execute模式）")
            planner_context["query_mode"] = "full"
            planner_context["needs_clarification"] = False
            return {
                "planner_context": planner_context,
                "current_agent": "planner",
                "next_agent": "executor"
            }
        except Exception as e:
            pass
    
    # Fallback 路径
    response = await qwen3_llm.ainvoke(messages)
    try:
        content = response.content.strip()
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
        
        extraction = json.loads(content)
        planner_context["destination"] = extraction.get("destination", "")
        planner_context["origin"] = extraction.get("origin", "")
        planner_context["travel_days"] = extraction.get("travel_days", 0)
        planner_context["budget"] = extraction.get("budget", 0)
        planner_context["travel_date"] = extraction.get("travel_date", "")
        planner_context["preferences"] = extraction.get("preferences", [])
        planner_context["needs_deep_analysis"] = extraction.get("needs_deep_analysis", False)
        planner_context["tools_needed"] = extraction.get("tools_needed", [])
        
        # 首先基于关键词进行简单查询预检测（fallback路径也要有）
        simple_keywords = ["天气", "景点", "美食", "攻略", "推荐", "怎么样", "如何", "好玩", "哪里"]
        has_simple_keyword = any(kw in user_query for kw in simple_keywords)
        
        multi_dest_detection = detect_multi_destination(user_query, planner_context)
        if multi_dest_detection.get('is_multi_destination', False):
            planner_context['needs_deep_analysis'] = True
            planner_context['scenario_type'] = 'multi_destination'
            planner_context['raw_destination_text'] = multi_dest_detection.get('raw_destination_text', planner_context['destination'])
        else:
            planner_context['scenario_type'] = 'simple' if not planner_context.get('needs_deep_analysis', False) else 'complex'
        
        # 检测是否是简单查询（和前面一样）
        is_simple_query = (
            (planner_context['destination'] and 
             not planner_context['travel_days'] and 
             not planner_context['budget'] and 
             not planner_context['travel_date']) or
            has_simple_keyword
        )
        
        if is_simple_query:
            # 如果没有提取到目的地，尝试从用户查询中提取
            destination_for_query = planner_context.get('destination', '')
            if not destination_for_query:
                # 简单的城市提取逻辑
                cities = ["北京", "上海", "广州", "深圳", "杭州", "南京", "苏州", "成都", "重庆", "武汉", "西安", "长沙", "青岛", "大连", "厦门", "三亚", "昆明", "丽江", "桂林", "黄山"]
                for city in cities:
                    if city in user_query:
                        destination_for_query = city
                        planner_context['destination'] = city
                        break
            
            planner_context["query_mode"] = "simple"
            planner_context["needs_clarification"] = False
            planner_context["tools_needed"] = ["旅游攻略检索"]
            
            return {
                "planner_context": planner_context,
                "current_agent": "planner",
                "next_agent": "executor"
            }
        
        # 完整规划模式
        planner_context["query_mode"] = "full"
        return {
            "planner_context": planner_context,
            "current_agent": "planner",
            "next_agent": "executor"
        }
    except Exception:
        # 最后的异常处理路径也要检查简单查询
        simple_keywords = ["天气", "景点", "美食", "攻略", "推荐", "怎么样", "如何", "好玩", "哪里"]
        has_simple_keyword = any(kw in user_query for kw in simple_keywords)
        
        if has_simple_keyword:
            # 尝试从用户查询中提取城市
            destination_for_query = ""
            cities = ["北京", "上海", "广州", "深圳", "杭州", "南京", "苏州", "成都", "重庆", "武汉", "西安", "长沙", "青岛", "大连", "厦门", "三亚", "昆明", "丽江", "桂林", "黄山"]
            for city in cities:
                if city in user_query:
                    destination_for_query = city
                    planner_context["destination"] = city
                    break
            
            planner_context["query_mode"] = "simple"
            planner_context["needs_clarification"] = False
            planner_context["tools_needed"] = ["旅游攻略检索"]
            
            return {
                "planner_context": planner_context,
                "current_agent": "planner",
                "next_agent": "executor"
            }
        
        planner_context["needs_clarification"] = True
        planner_context["clarification_question"] = "请提供更多关于您旅行计划的信息，例如目的地、出行时间等。"
        return {
            "planner_context": planner_context,
            "current_agent": "planner",
            "next_agent": None,
            "is_complete": True
        }
