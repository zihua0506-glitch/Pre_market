import requests
import json
import sys

# Windows 控制台默认 GBK，遇到 emoji / 部分中文符号会崩，强制 UTF-8
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

GAMMA_API = "https://gamma-api.polymarket.com"


def search_shanghai_weather_markets(keyword="Shanghai", max_pages=10):
    """
    搜索 Polymarket 上关于上海天气的市场，并提取 Token ID。

    注意：gamma-api 的 /markets 端点不支持全文搜索（query 参数被静默忽略，
    只会返回默认排序的市场）。真正的搜索端点是 /public-search，返回 events，
    每个 event 下挂多个 markets（不同温度阈值各一个），每个 market 才有 clobTokenIds。
    """
    all_markets = []

    for page in range(1, max_pages + 1):
        params = {
            "q": keyword,
            "limit_per_type": 50,
            "page": page,
        }
        try:
            resp = requests.get(f"{GAMMA_API}/public-search", params=params, timeout=15)
            resp.raise_for_status()
        except requests.exceptions.RequestException as e:
            print(f"请求 API 失败 (page={page}): {e}")
            break

        data = resp.json()
        events = data.get("events", [])
        if not events:
            break

        for ev in events:
            ev_title = ev.get("title", "") or ""
            # 过滤出天气相关（避免 "Shanghai Composite Index" 之类的非天气结果）
            if not any(k in ev_title.lower() for k in ("temperature", "weather", "rain", "snow", "temp")):
                continue

            for m in ev.get("markets", []):
                tokens_raw = m.get("clobTokenIds") or "[]"
                try:
                    tokens = json.loads(tokens_raw) if isinstance(tokens_raw, str) else tokens_raw
                except json.JSONDecodeError:
                    tokens = []

                all_markets.append({
                    "event_title": ev_title,
                    "question": m.get("question"),
                    "yes_token": tokens[0] if len(tokens) > 0 else None,
                    "no_token": tokens[1] if len(tokens) > 1 else None,
                    "start_date": m.get("startDate"),
                    "end_date": m.get("endDate"),
                    "closed": m.get("closed"),
                    "condition_id": m.get("conditionId"),
                })

        if not data.get("pagination", {}).get("hasMore"):
            break

    return all_markets


if __name__ == "__main__":
    keyword = "Shanghai"
    print(f"正在 Polymarket 搜索与 '{keyword}' 相关的天气市场...\n")

    markets = search_shanghai_weather_markets(keyword)

    if not markets:
        print("未找到相关的天气市场。")
        sys.exit(0)

    print(f"=== 共找到 {len(markets)} 个天气类 market ===\n")
    for m in markets:
        date_tag = (m["start_date"] or "")[:10] or "?"
        status = "已结算" if m["closed"] else "活跃"
        print(f"[{date_tag}] [{status}] {m['question']}")
        print(f"   Event : {m['event_title']}")
        print(f"   YES   : {m['yes_token']}")
        print(f"   NO    : {m['no_token']}")
        print(f"   Range : {m['start_date']}  ->  {m['end_date']}")
        print("-" * 70)
