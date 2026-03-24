"""
用户档案管理模块
- 加载、保存、更新用户偏好
- JSON 文件持久化
- 异步处理
"""
import json
import os
import asyncio
from datetime import datetime
from typing import Dict, Any, List, Optional
from pathlib import Path
from config.settings import PROJECT_ROOT


class UserProfileManager:
    """用户档案管理器"""
    
    def __init__(self, storage_dir: Optional[Path] = None):
        self.storage_dir = storage_dir or (PROJECT_ROOT / "data" / "user_profiles")
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self._current_user_id = "default_user"  # 默认用户
    
    def _get_profile_path(self, user_id: str) -> Path:
        """获取用户档案文件路径"""
        return self.storage_dir / f"{user_id}.json"
    
    def _create_default_profile(self) -> Dict[str, Any]:
        """创建默认用户档案"""
        return {
            "user_id": self._current_user_id,
            "travel_style": [],
            "destination_types": [],
            "budget_level": "舒适型",
            "max_daily_budget": None,
            "hotel_preference": [],
            "room_type_preference": None,
            "transport_priority": ["性价比", "时间"],
            "preferred_transport": [],
            "dietary_restrictions": [],
            "cuisine_preference": [],
            "liked_activities": [],
            "disliked_activities": [],
            "travel_season_preference": [],
            "daily_schedule_preference": "随性",
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
            "query_count": 0
        }
    
    def load_profile(self, user_id: Optional[str] = None) -> Dict[str, Any]:
        """同步加载用户档案"""
        user_id = user_id or self._current_user_id
        profile_path = self._get_profile_path(user_id)
        
        if profile_path.exists():
            try:
                with open(profile_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                print(f"⚠️ 加载用户档案失败，使用默认档案: {e}")
        
        return self._create_default_profile()
    
    async def load_profile_async(self, user_id: Optional[str] = None) -> Dict[str, Any]:
        """异步加载用户档案"""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.load_profile, user_id)
    
    def save_profile(self, profile: Dict[str, Any], user_id: Optional[str] = None):
        """同步保存用户档案"""
        user_id = user_id or self._current_user_id
        profile_path = self._get_profile_path(user_id)
        
        profile["updated_at"] = datetime.now().isoformat()
        
        try:
            with open(profile_path, 'w', encoding='utf-8') as f:
                json.dump(profile, f, ensure_ascii=False, indent=2)
            print(f"✅ 用户档案已保存: {user_id}")
        except Exception as e:
            print(f"❌ 保存用户档案失败: {e}")
    
    async def save_profile_async(self, profile: Dict[str, Any], user_id: Optional[str] = None):
        """异步保存用户档案"""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self.save_profile, profile, user_id)
    
    def update_profile(self, updates: Dict[str, Any], user_id: Optional[str] = None) -> Dict[str, Any]:
        """同步更新用户档案"""
        profile = self.load_profile(user_id)
        
        for key, value in updates.items():
            if key in profile:
                if isinstance(value, list) and isinstance(profile[key], list):
                    for item in value:
                        if item not in profile[key]:
                            profile[key].append(item)
                else:
                    profile[key] = value
        
        profile["query_count"] = profile.get("query_count", 0) + 1
        self.save_profile(profile, user_id)
        return profile
    
    async def update_profile_async(self, updates: Dict[str, Any], user_id: Optional[str] = None) -> Dict[str, Any]:
        """异步更新用户档案"""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.update_profile, updates, user_id)
    
    def format_profile_for_prompt(self, user_id: Optional[str] = None) -> str:
        """将用户档案格式化为提示词字符串"""
        profile = self.load_profile(user_id)
        
        lines = ["========== 用户偏好（请根据这些偏好调整推荐） =========="]
        
        if profile.get("travel_style"):
            lines.append(f"- 旅行风格：{', '.join(profile['travel_style'])}")
        
        if profile.get("budget_level"):
            lines.append(f"- 预算水平：{profile['budget_level']}")
        
        if profile.get("max_daily_budget"):
            lines.append(f"- 每日预算上限：{profile['max_daily_budget']}元")
        
        if profile.get("hotel_preference"):
            lines.append(f"- 住宿偏好：{', '.join(profile['hotel_preference'])}")
        
        if profile.get("dietary_restrictions"):
            lines.append(f"- 饮食禁忌：{', '.join(profile['dietary_restrictions'])}")
        
        if profile.get("cuisine_preference"):
            lines.append(f"- 菜系偏好：{', '.join(profile['cuisine_preference'])}")
        
        if profile.get("liked_activities"):
            lines.append(f"- 喜欢的活动：{', '.join(profile['liked_activities'])}")
        
        if profile.get("disliked_activities"):
            lines.append(f"- 不喜欢的活动：{', '.join(profile['disliked_activities'])}")
        
        if profile.get("transport_priority"):
            lines.append(f"- 交通优先级：{', '.join(profile['transport_priority'])}")
        
        lines.append("=" * 50)
        
        return "\n".join(lines)


_profile_manager = None


def get_profile_manager() -> UserProfileManager:
    """获取全局用户档案管理器实例"""
    global _profile_manager
    if _profile_manager is None:
        _profile_manager = UserProfileManager()
    return _profile_manager
