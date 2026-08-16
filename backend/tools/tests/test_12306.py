"""
12306 MCP 工具全面测试脚本
用法: cd backend && python tools/tests/test_12306.py
"""
import asyncio
import sys
import os
import json
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from config.settings import setup_logging
setup_logging()


async def main():
    from tools.mcp_tools import get_mcp_manager

    print("=" * 60)
    print("12306 MCP 工具全面测试")
    print("=" * 60)

    # ── 初始化 ──
    print("\n[0] 初始化 MCP Manager ...")
    try:
        mgr = await get_mcp_manager()
    except Exception as e:
        print(f"  初始化失败: {e}")
        return

    if "12306 Server" not in mgr.mcp_servers:
        print("  ❌ 12306 Server 未连接，请检查 servers_config.json")
        return
    print("  ✅ 12306 Server 已连接")

    # ── 列出工具 ──
    print("\n[1] 12306 Server 可用工具:")
    try:
        tools = await mgr.list_tools("12306 Server")
        for t in tools:
            print(f"   - {t}")
    except Exception as e:
        print(f"   list_tools 失败: {e}")

    today = date.today().isoformat()
    tomorrow = (date.today() + timedelta(days=1)).isoformat()

    # ── 工具1: get-current-date ──
    print(f"\n[2] get-current-date:")
    try:
        result = await mgr.call_tool("12306 Server", "get-current-date")
        print(f"   结果: {result}")
        print("   ✅ 成功")
    except Exception as e:
        print(f"   ❌ 失败: {e}")

    # ── 工具2: get-stations-code-in-city ──
    print(f"\n[3] get-stations-code-in-city (西安):")
    try:
        result = await mgr.call_tool("12306 Server", "get-stations-code-in-city", city="西安")
        print(f"   结果(前300字): {result[:300]}")
        print("   ✅ 成功")
    except Exception as e:
        print(f"   ❌ 失败: {e}")

    # ── 工具3: get-station-code-of-citys ──
    print(f"\n[4] get-station-code-of-citys (北京|上海):")
    try:
        result = await mgr.call_tool("12306 Server", "get-station-code-of-citys", citys="北京|上海")
        print(f"   结果: {result}")
        print("   ✅ 成功")
    except Exception as e:
        print(f"   ❌ 失败: {e}")

    # ── 工具4: get-station-code-by-names ──
    print(f"\n[5] get-station-code-by-names (北京南|上海虹桥):")
    try:
        result = await mgr.call_tool("12306 Server", "get-station-code-by-names", stationNames="北京南|上海虹桥")
        print(f"   结果: {result}")
        print("   ✅ 成功")
    except Exception as e:
        print(f"   ❌ 失败: {e}")

    # ── 工具5: get-station-by-telecode ──
    print(f"\n[6] get-station-by-telecode (BJP = 北京):")
    try:
        result = await mgr.call_tool("12306 Server", "get-station-by-telecode", stationTelecode="BJP")
        print(f"   结果(前300字): {result[:300]}")
        print("   ✅ 成功")
    except Exception as e:
        print(f"   ❌ 失败: {e}")

    # ── 工具6: get-tickets（余票查询）──
    print(f"\n[7] get-tickets (北京→上海, 明天 {tomorrow}):")
    try:
        result = await mgr.call_tool("12306 Server", "get-tickets",
                                     date=tomorrow,
                                     fromStation="北京",
                                     toStation="上海",
                                     trainFilterFlags="G",
                                     format="text")
        # 打印前800字符
        print(f"   结果(前800字):\n{result[:800]}")
        print("   ✅ 成功")
    except Exception as e:
        print(f"   ❌ 失败: {e}")

    # ── 工具6b: get-tickets（CSV格式，限3条）──
    print(f"\n[8] get-tickets (西安→成都, CSV, 限3条):")
    try:
        result = await mgr.call_tool("12306 Server", "get-tickets",
                                     date=tomorrow,
                                     fromStation="西安",
                                     toStation="成都",
                                     format="csv",
                                     limitedNum=3)
        print(f"   结果:\n{result[:600]}")
        print("   ✅ 成功")
    except Exception as e:
        print(f"   ❌ 失败: {e}")

    # ── 工具7: get-interline-tickets（中转查询）──
    print(f"\n[9] get-interline-tickets (西安→拉萨, 中转, 限3条):")
    try:
        result = await mgr.call_tool("12306 Server", "get-interline-tickets",
                                     date=tomorrow,
                                     fromStation="西安",
                                     toStation="拉萨",
                                     limitedNum=3,
                                     format="text")
        print(f"   结果(前800字):\n{result[:800]}")
        print("   ✅ 成功")
    except Exception as e:
        print(f"   ❌ 失败: {e}")

    # ── 工具8: get-train-route-stations（经停站查询）──
    print(f"\n[10] get-train-route-stations (G1, 明天):")
    try:
        result = await mgr.call_tool("12306 Server", "get-train-route-stations",
                                     trainCode="G1",
                                     departDate=tomorrow,
                                     format="text")
        print(f"   结果(前600字):\n{result[:600]}")
        print("   ✅ 成功")
    except Exception as e:
        print(f"   ❌ 失败: {e}")

    # ── 汇总 ──
    print(f"\n{'='*60}")
    print("测试完成")
    print(f"{'='*60}")


if __name__ == "__main__":
    asyncio.run(main())
