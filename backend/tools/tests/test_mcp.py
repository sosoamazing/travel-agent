"""
MCP 服务器连通性简单测试（无需 Streamlit）
用法: cd backend && python tools/tests/test_mcp.py
"""
import asyncio
import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from config.settings import setup_logging
setup_logging()


async def main():
    from tools.mcp_tools import get_mcp_manager

    print("=" * 60)
    print("MCP 服务器连通性测试")
    print("=" * 60)

    # 1. 初始化
    print("\n[1] 初始化 MCP Manager ...")
    try:
        mgr = await get_mcp_manager()
    except Exception as e:
        print(f"  初始化失败: {e}")
        return

    connected = list(mgr.mcp_servers.keys())
    print(f"   已连接服务器: {connected}")

    # 2. 列出各服务器工具
    print("\n[2] 各服务器可用工具:")
    for name in connected:
        try:
            tools = await mgr.list_tools(name)
            print(f"   [{name}] ({len(tools)} 个): {tools[:8]}{'...' if len(tools)>8 else ''}")
        except Exception as e:
            print(f"   [{name}] list_tools 失败: {e}")

    # 3. 试调一个工具
    print("\n[3] 试调 gaode_weather (杭州):")
    try:
        result = await mgr.call_tool("Gaode Server", "maps_weather", city="杭州")
        print(f"   结果(前500字): {result[:500]}")
        print("   ✅ 调用成功")
    except Exception as e:
        print(f"   ❌ 调用失败: {e}")

    # 4. 试调 12306
    print("\n[4] 试调 12306 get-current-date:")
    try:
        result = await mgr.call_tool("12306 Server", "get-current-date")
        print(f"   结果: {result[:200]}")
        print("   ✅ 调用成功")
    except Exception as e:
        print(f"   ❌ 调用失败: {e}")

    # 5. 试调 bazi Server lucky_day
    print("\n[5] 试调 bazi Server getChineseCalendar (今天日期):")
    try:
        from datetime import date
        today = date.today().isoformat()
        result = await mgr.call_tool("bazi Server", "getChineseCalendar", date=today)
        print(f"   结果(前500字): {result[:500]}")
        print("   ✅ 调用成功")
    except Exception as e:
        print(f"   ❌ 调用失败: {e}")

    # 6. 试调 gaode_driving (上海→杭州)
    print("\n[6] 试调 上海→杭州 驾车距离:")
    try:
        # 先 geocode
        import json
        geo1 = await mgr.call_tool("Gaode Server", "maps_geo", address="上海")
        geo2 = await mgr.call_tool("Gaode Server", "maps_geo", address="杭州")

        def _extract_loc(raw):
            """兼容高德新结构 {"return": [{"location": "..."}]} 和旧顶层 {"location": "..."}"""
            data = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(data, dict):
                ret = data.get("return")
                if isinstance(ret, list) and ret:
                    return ret[0].get("location", "")
                return data.get("location", "")
            return ""

        loc1 = _extract_loc(geo1)
        loc2 = _extract_loc(geo2)
        print(f"   上海坐标: {loc1}, 杭州坐标: {loc2}")
        route = await mgr.call_tool("Gaode Server", "maps_direction_driving",
                                     origin=loc1, destination=loc2)
        print(f"   驾车路线(前500字): {route[:500]}")
        print("   ✅ 调用成功")
    except Exception as e:
        print(f"   ❌ 调用失败: {e}")

    # 7. 清理
    print(f"\n{'='*60}")
    print("测试完成")
    print(f"{'='*60}")


if __name__ == "__main__":
    asyncio.run(main())
