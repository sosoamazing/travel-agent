"""
Context Compression Tool
上下文压缩工具 - 所有Agent都可以调用
"""
from typing import List, Any, Dict, Optional
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage, SystemMessage
from config.settings import DASHSCOPE_API_KEY, QWEN3_MODEL, QWEN3_API_BASE, QWEN3_TEMPERATURE
from langchain_openai import ChatOpenAI
import json

llm = ChatOpenAI(
    model=QWEN3_MODEL,
    api_key=DASHSCOPE_API_KEY,
    base_url=QWEN3_API_BASE,
    temperature=QWEN3_TEMPERATURE,
)

CONTEXT_COMPRESSION_PROMPT = """你是一个对话摘要专家。请将以下对话历史压缩成一个简洁的摘要，保留关键信息。

对话历史：
{conversation_history}

请输出JSON格式：
{{
  "summary": "对话摘要，包含关键的用户需求、偏好、已完成的查询等",
  "key_points": ["关键点1", "关键点2", "关键点3"],
  "user_preferences": {{
    "interests": ["兴趣1", "兴趣2"],
    "dislikes": ["不喜欢1", "不喜欢2"]
  }},
  "completed_queries": ["已完成的查询1", "已完成的查询2"],
  "pending_items": ["待处理事项1", "待处理事项2"]
}}

规则：
- 摘要要简洁，不超过500字
- key_points 要包含最重要的信息（目的地、时间、预算等）
- user_preferences 要提取用户的兴趣和不喜欢的事物
- completed_queries 要列出已经完成的查询
- pending_items 要列出还需要做的事情
- 用中文输出
"""


class ContextCompressor:
    """上下文压缩器"""
    
    def __init__(self, max_messages: int = 10, compress_threshold: int = 15):
        """
        初始化上下文压缩器
        
        Args:
            max_messages: 保留的最近消息数
            compress_threshold: 触发压缩的消息数阈值
        """
        self.max_messages = max_messages
        self.compress_threshold = compress_threshold
    
    def should_compress(self, messages: List[BaseMessage]) -> bool:
        """判断是否需要压缩"""
        return len(messages) >= self.compress_threshold
    
    def _format_messages(self, messages: List[BaseMessage]) -> str:
        """格式化消息为文本"""
        formatted = []
        for msg in messages[-self.compress_threshold:]:
            role = "用户" if isinstance(msg, HumanMessage) else "助手"
            content = msg.content[:200] if len(msg.content) > 200 else msg.content
            formatted.append(f"{role}: {content}")
        return "\n".join(formatted)
    
    async def compress(self, messages: List[BaseMessage]) -> Dict[str, Any]:
        """
        压缩对话历史
        
        Args:
            messages: 原始消息列表
            
        Returns:
            压缩结果，包含summary等
        """
        if not self.should_compress(messages):
            return {
                "compressed": False,
                "messages": messages,
                "summary": None
            }
        
        print(f"\n{'='*60}")
        print("📦 [Context Compressor] 开始压缩上下文...")
        print(f"  原始消息数: {len(messages)}")
        print(f"{'='*60}")
        
        conversation_text = self._format_messages(messages)
        
        prompt = CONTEXT_COMPRESSION_PROMPT.format(
            conversation_history=conversation_text
        )
        
        try:
            response = await llm.ainvoke([
                SystemMessage(content="你是一个对话摘要专家。"),
                HumanMessage(content=prompt)
            ])
            
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
            
            content = content.strip()
            
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
            
            result = json.loads(content)
            
            print(f"\n✅ 压缩成功:")
            print(f"  摘要: {result.get('summary', '')[:100]}...")
            print(f"  关键点: {len(result.get('key_points', []))}个")
            print(f"{'='*60}\n")
            
            return {
                "compressed": True,
                "summary": result.get("summary", ""),
                "key_points": result.get("key_points", []),
                "user_preferences": result.get("user_preferences", {"interests": [], "dislikes": []}),
                "completed_queries": result.get("completed_queries", []),
                "pending_items": result.get("pending_items", []),
                "retained_messages": messages[-self.max_messages:]
            }
            
        except Exception as e:
            print(f"❌ 压缩失败: {e}")
            import traceback
            traceback.print_exc()
            
            return {
                "compressed": True,
                "summary": "上下文过长，已保留最近消息",
                "key_points": [],
                "user_preferences": {"interests": [], "dislikes": []},
                "completed_queries": [],
                "pending_items": [],
                "retained_messages": messages[-self.max_messages:]
            }
    
    def build_context(self, compression_result: Dict[str, Any]) -> List[BaseMessage]:
        """
        根据压缩结果构建上下文
        
        Args:
            compression_result: compress()的返回结果
            
        Returns:
            构建好的消息列表
        """
        if not compression_result.get("compressed", False):
            return compression_result.get("messages", [])
        
        context_messages = []
        
        summary = compression_result.get("summary", "")
        if summary:
            context_messages.append(SystemMessage(
                content=f"【对话摘要】\n{summary}"
            ))
        
        key_points = compression_result.get("key_points", [])
        if key_points:
            key_points_text = "\n".join([f"- {point}" for point in key_points])
            context_messages.append(SystemMessage(
                content=f"【关键信息】\n{key_points_text}"
            ))
        
        retained = compression_result.get("retained_messages", [])
        context_messages.extend(retained)
        
        return context_messages


_context_compressor = None


def get_context_compressor() -> ContextCompressor:
    """获取全局上下文压缩器实例"""
    global _context_compressor
    if _context_compressor is None:
        _context_compressor = ContextCompressor()
    return _context_compressor
