"""
测试：搜索"西工大长安校区"周围的酒店，看完整输出

验证：
1. 高德 maps_text_search 返回的原始结构（确认是否含房价）
2. _extract_hotels 提取后的酒店列表（含 LLM 估价的价格）
3. 模拟 hotel_search_node 的选定逻辑
"""
import asyncio
import os
import sys
import json
from pathlib import Path

# 确保能 import backend 包
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from config.settings import setup_logging
setup_logging()

import logging
logger = logging.getLogger(__name__)


async def main():
    # 1) 直接调用高德 maps_text_search，打印原始返回
    from tools.mcp_tools import get_mcp_manager
    mgr = await get_mcp_manager()

    print("\n" + "=" * 70)
    print("[1] 调用 Gaode maps_text_search（keywords='西工大长安校区 酒店', city='西安'）")
    print("=" * 70)
    raw = await mgr.call_tool(
        "Gaode Server", "maps_text_search",
        keywords="西工大长安校区 酒店", city="西安"
    )
    print(f"[返回类型] {type(raw).__name__}, 长度={len(raw) if hasattr(raw, '__len__') else 'N/A'}")
    print("[原始返回内容]（前 3000 字）:")
    print(str(raw)[:3000])

    # 解析原始 JSON 看看是否含价格字段
    print("\n[原始返回字段分析]:")
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
        if isinstance(data, dict):
            print(f"  顶层 keys: {list(data.keys())}")
            # 高德 MCP 把结果包在 return 里
            ret = data.get("return") or data.get("data") or []
            if isinstance(ret, list) and ret:
                print(f"  返回条目数: {len(ret)}")
                print(f"  第一条所有字段: {list(ret[0].keys())}")
                # 检查是否含价格相关字段
                first = ret[0]
                price_keys = [k for k in first.keys() if any(p in k.lower() for p in ["price", "fee", "cost", "money"])]
                print(f"  价格相关字段: {price_keys or '(无)'}")
                print(f"  第一条完整内容: {json.dumps(first, ensure_ascii=False, indent=2)}")
    except Exception as e:
        print(f"  解析失败: {e}")

    # 2) 调用项目内的 _extract_hotels
    print("\n" + "=" * 70)
    print("[2] 调用 _extract_hotels（POI 解析 + DeepSeek-Flash 估价 + 字段拼接）")
    print("=" * 70)
    from agent_nodes.city_planning import _extract_hotels
    hotels = await _extract_hotels(
        hotel_raw=raw,
        city="西安",
        planner_context={
            "preferences": [],
            "travel_date": "2026-10-01",  # 国庆节，验证旺季价格上浮
            "travel_days": 3,
        },
        user_query="西工大长安校区附近的酒店",
        max_n=5,
    )
    print(f"\n[提取到 {len(hotels)} 个酒店]")
    for i, h in enumerate(hotels, 1):
        print(f"  [{i}] {h.get('name', '?')} | 价格={h.get('price_per_night', 0):.0f}元/晚 | 地址={h.get('address', '')}")

    print("\n[拼接后的完整 POI JSON 字段（第 1 个酒店）]:")
    if hotels:
        print(json.dumps(hotels[0], ensure_ascii=False, indent=2))

    # 3) 模拟 hotel_search_node 的选定逻辑（取最便宜）
    print("\n" + "=" * 70)
    print("[3] 模拟 hotel_search_node 选定逻辑")
    print("=" * 70)
    if not hotels:
        print("(无候选酒店)")
        return

    cheapest_hotel = {}
    cheapest = 0.0
    for h in hotels:
        p = float(h.get("price_per_night", 0) or 0)
        if not cheapest_hotel or p < cheapest:
            cheapest_hotel = h
            cheapest = p

    nights = 3  # 假设住 3 晚
    hotel_cost = round(cheapest * nights, 2)
    print(f"\n候选酒店价格列表: {[h.get('price_per_night', 0) for h in hotels]}")
    print(f"✅ 最终选定: {cheapest_hotel.get('name', '?')}")
    print(f"   {cheapest:.0f}/晚 × {nights}晚 = {hotel_cost:.0f}元")


if __name__ == "__main__":
    asyncio.run(main())
