"""测试价格提取修复效果"""
import asyncio
import json
import sys
import os
import logging
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
logging.basicConfig(level=logging.INFO, format="%(message)s")

from tools.mcp_tools import get_mcp_manager
from agent_nodes.transport import _parse_transport_brief, _quick_extract_train_price, _extract_transport_cost


async def main():
    print("=" * 70)
    print("测试价格提取修复效果（上海→深圳）")
    print("=" * 70)

    manager = await get_mcp_manager()
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

    result = await manager.call_tool("12306 Server", "get-station-code-of-citys", citys="上海|深圳")
    codes = json.loads(result) if isinstance(result, str) else result
    def _extract_code(val):
        if isinstance(val, list) and val:
            return val[0].get("station_code") or val[0].get("code")
        if isinstance(val, dict):
            return val.get("station_code") or val.get("code")
        return None
    from_code = _extract_code(codes.get("上海"))
    to_code = _extract_code(codes.get("深圳"))

    raw_text = await manager.call_tool(
        "12306 Server", "get-tickets",
        date=tomorrow, fromStation=from_code, toStation=to_code
    )

    brief = _parse_transport_brief(raw_text, "train", max_items=3)
    print(f"\n_parse_transport_brief: {brief}")

    price = _quick_extract_train_price(raw_text)
    print(f"_quick_extract_train_price: {price} 元")

    print("\n调用 _extract_transport_cost (2人):")
    cost = await _extract_transport_cost(raw_text, "2人去深圳玩", "上海", "深圳", "train")
    print(f"_extract_transport_cost 返回: {cost} 元")

    print("\n调用 _extract_transport_cost (1人):")
    cost1 = await _extract_transport_cost(raw_text, "去深圳玩", "上海", "深圳", "train")
    print(f"_extract_transport_cost 返回: {cost1} 元")

    await manager.cleanup()
    print("\n✅ 测试完成")


if __name__ == "__main__":
    asyncio.run(main())
