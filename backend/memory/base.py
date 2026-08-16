"""记忆系统基础数据结构：工作记忆快照 / 情景记忆事件 / 记忆配置。"""
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta


# 城市规划状态
CITY_PENDING = "pending"    # 没规划
CITY_PARTIAL = "partial"    # 没规划完
CITY_DONE = "done"          # 规划完

# keep / replan 判定
FLAG_KEEP = "keep"
FLAG_REPLAN = "replan"

# 工作记忆默认 TTL（秒）
DEFAULT_TTL_SECONDS = 86400


def _now() -> str:
    return datetime.now().isoformat()


def _expires(ttl_seconds: int) -> str:
    return (datetime.now() + timedelta(seconds=ttl_seconds)).isoformat()


@dataclass
class WorkingCityState:
    """单个城市在工作记忆中的状态。"""
    city: str
    status: str = CITY_PENDING          # pending / partial / done
    spent: float = 0.0                  # 该城已花费（景点+酒店）
    locked: bool = False                # 是否锁定（预算分配时全额扣减）
    plan: Optional[Dict[str, Any]] = None   # 该城完整 city_plan（done 时非空）
    budget: Optional[Dict[str, Any]] = None # 该城预算份额（attractions/hotel/nights）


@dataclass
class WorkingMemorySnapshot:
    """工作记忆快照：一次规划会话的中间/最终状态。"""
    session_id: str
    user_id: str = "default_user"
    total_budget: float = 0.0
    transport_total: float = 0.0
    buffer_budget: float = 0.0
    transport_costs: Dict[str, float] = field(default_factory=dict)
    cities: Dict[str, WorkingCityState] = field(default_factory=dict)  # city -> 状态
    updated_at: str = field(default_factory=_now)
    ttl_seconds: int = DEFAULT_TTL_SECONDS

    @property
    def locked_spent(self) -> float:
        """locked=true 的城市已花费合计（预算分配时全额扣减）。"""
        return round(sum(c.spent for c in self.cities.values() if c.locked), 2)

    @property
    def locked_cities(self) -> List[str]:
        return [c.city for c in self.cities.values() if c.locked]

    @property
    def done_cities(self) -> List[str]:
        return [c.city for c in self.cities.values() if c.status == CITY_DONE]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "user_id": self.user_id,
            "total_budget": self.total_budget,
            "transport_total": self.transport_total,
            "buffer_budget": self.buffer_budget,
            "transport_costs": self.transport_costs,
            "cities": {k: asdict(v) for k, v in self.cities.items()},
            "updated_at": self.updated_at,
            "ttl_seconds": self.ttl_seconds,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WorkingMemorySnapshot":
        if not data:
            raise ValueError("empty snapshot")
        cities = {}
        for name, c in (data.get("cities") or {}).items():
            cities[name] = WorkingCityState(
                city=c.get("city", name),
                status=c.get("status", CITY_PENDING),
                spent=float(c.get("spent", 0) or 0),
                locked=bool(c.get("locked", False)),
                plan=c.get("plan"),
                budget=c.get("budget"),
            )
        return cls(
            session_id=data.get("session_id", ""),
            user_id=data.get("user_id", "default_user"),
            total_budget=float(data.get("total_budget", 0) or 0),
            transport_total=float(data.get("transport_total", 0) or 0),
            buffer_budget=float(data.get("buffer_budget", 0) or 0),
            transport_costs={k: float(v or 0) for k, v in (data.get("transport_costs") or {}).items()},
            cities=cities,
            updated_at=data.get("updated_at", _now()),
            ttl_seconds=int(data.get("ttl_seconds", DEFAULT_TTL_SECONDS)),
        )

    def is_expired(self) -> bool:
        """TTL 过期判断。"""
        try:
            updated = datetime.fromisoformat(self.updated_at)
            return datetime.now() - updated > timedelta(seconds=self.ttl_seconds)
        except Exception:
            return True


@dataclass
class TripEpisode:
    """情景记忆：一次已完成的旅行规划（episode）。"""
    user_id: str = "default_user"
    session_id: str = ""
    origin: str = ""
    destination: str = ""
    start_date: str = ""
    end_date: str = ""
    nights: int = 0
    total_budget: float = 0.0
    total_spent: float = 0.0
    transport_mode: str = ""
    transport_cost: float = 0.0
    hotels: List[Dict[str, Any]] = field(default_factory=list)
    attractions: List[Dict[str, Any]] = field(default_factory=list)
    feedback: str = ""
    satisfaction: Optional[int] = None
    summary: str = ""
    created_at: str = field(default_factory=_now)
