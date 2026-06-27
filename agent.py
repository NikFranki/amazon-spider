#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Amazon MX 畅销榜 Agent

每日自动执行：
  1. 运行爬虫，抓取宠物狗类目前 100 名
  2. 与上次数据对比，检测新上榜 / 排名变化 / 价格变动
  3. 更新 output/history.json（前端页面数据源）
  4. 通过 Telegram Bot 推送每日摘要

环境变量：
  TELEGRAM_BOT_TOKEN   Telegram Bot Token
  TELEGRAM_CHAT_ID     接收通知的 Chat ID（个人或群组）

用法：
  python3 agent.py
  TELEGRAM_BOT_TOKEN=xxx TELEGRAM_CHAT_ID=yyy python3 agent.py
"""

import json
import os
import sys
import urllib.request
from datetime import date

from dotenv import load_dotenv
load_dotenv()
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from amazon_mx_bestsellers_spider_of_dog import scrape, save, DEFAULT_URL

OUTPUT_DIR = Path(__file__).parent / "output"
HISTORY_FILE = OUTPUT_DIR / "history.json"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


# ---------------------------------------------------------------- 数据加载

def _sorted_data_files(exclude=None):
    files = sorted(OUTPUT_DIR.glob("bestsellers_*.json"), key=lambda p: p.name)
    return [p for p in files if p != exclude]


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------- 变化检测

def detect_changes(today_data, prev_data):
    """对比今日与上次榜单，返回三类变化列表。"""
    if not prev_data:
        return {"new": [], "rank_up": [], "price_change": []}

    today = {r["ASIN编号"]: r for r in today_data["商品列表"]}
    prev  = {r["ASIN编号"]: r for r in prev_data["商品列表"]}

    # 新上榜
    new_entries = sorted(
        [today[a] for a in set(today) - set(prev)],
        key=lambda r: int(r["排名"])
    )

    # 排名上升（排名数字变小 = 上升）
    rank_up = []
    for asin, item in today.items():
        if asin not in prev:
            continue
        delta = int(prev[asin]["排名"]) - int(item["排名"])
        if delta > 0:
            rank_up.append({**item, "_delta": delta, "_prev_rank": int(prev[asin]["排名"])})
    rank_up.sort(key=lambda r: r["_delta"], reverse=True)

    # 价格变动（超过 2%）
    price_change = []
    for asin, item in today.items():
        if asin not in prev:
            continue
        try:
            curr = float(item["价格(墨西哥比索)"] or 0)
            old  = float(prev[asin]["价格(墨西哥比索)"] or 0)
            if curr and old and abs(curr - old) / old > 0.02:
                pct = (curr - old) / old * 100
                price_change.append({**item, "_prev_price": old, "_pct": pct})
        except (ValueError, ZeroDivisionError):
            pass
    price_change.sort(key=lambda r: abs(r["_pct"]), reverse=True)

    return {"new": new_entries, "rank_up": rank_up, "price_change": price_change}


# ---------------------------------------------------------------- 历史数据维护

def update_history(today_data):
    """将今日数据追加进 history.json，供前端页面读取。"""
    today_str = date.today().isoformat()

    if HISTORY_FILE.exists():
        history = load_json(HISTORY_FILE)
    else:
        history = {"last_updated": "", "dates": [], "products": {}}

    if today_str not in history["dates"]:
        history["dates"].append(today_str)
        history["dates"].sort()

    for item in today_data["商品列表"]:
        asin = item["ASIN编号"]
        if asin not in history["products"]:
            history["products"][asin] = {"title": "", "brand": "", "history": []}

        entry_map = {e["date"]: e for e in history["products"][asin]["history"]}
        entry_map[today_str] = {
            "date":          today_str,
            "rank":          int(item["排名"]),
            "price":         float(item["价格(墨西哥比索)"] or 0),
            "monthly_sales": int(item["月销量"] or 0),
            "rating":        float(item["评分"] or 0),
            "reviews":       int(item["评论数"] or 0),
        }
        history["products"][asin]["history"] = sorted(entry_map.values(), key=lambda e: e["date"])
        history["products"][asin]["title"] = item["商品名称"]
        history["products"][asin]["brand"] = item["品牌"]

    # 同时保存最新一天的完整榜单（前端默认视图用）
    history["latest"] = today_data["商品列表"]
    history["last_updated"] = today_str

    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)

    print(f"[history] 已更新，共 {len(history['products'])} 个商品，{len(history['dates'])} 天数据")


# ---------------------------------------------------------------- Telegram 通知

def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[telegram] 未配置 TOKEN/CHAT_ID，跳过通知", file=sys.stderr)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    body = json.dumps({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            print(f"[telegram] 发送成功 HTTP {resp.status}")
    except Exception as e:
        print(f"[telegram] 发送失败: {e}", file=sys.stderr)


def build_message(changes, today_data):
    today_str = date.today().isoformat()
    lines = [
        f"*🐾 Amazon MX 宠物狗畅销榜 · {today_str}*",
        f"共 {today_data['商品总数']} 款商品\n",
    ]

    if changes["new"]:
        lines.append(f"*🆕 新上榜 {len(changes['new'])} 款*")
        for r in changes["new"][:5]:
            lines.append(f"  #{r['排名']} {r['商品名称'][:28]}… MXN {r['价格(墨西哥比索)']}")
        if len(changes["new"]) > 5:
            lines.append(f"  …还有 {len(changes['new']) - 5} 款")
        lines.append("")

    if changes["rank_up"]:
        lines.append(f"*📈 排名上升 TOP5*")
        for r in changes["rank_up"][:5]:
            lines.append(f"  ↑{r['_delta']}  #{r['_prev_rank']}→#{r['排名']}  {r['商品名称'][:25]}…")
        lines.append("")

    if changes["price_change"]:
        lines.append(f"*💰 价格变动*")
        for r in changes["price_change"][:5]:
            arrow = "↑" if r["_pct"] > 0 else "↓"
            lines.append(f"  {arrow}{abs(r['_pct']):.1f}%  MXN {r['_prev_price']:.0f}→{r['价格(墨西哥比索)']}  {r['商品名称'][:22]}…")
        lines.append("")

    if not any(v for v in changes.values()):
        lines.append("_今日暂无显著变化_")

    return "\n".join(lines)


# ---------------------------------------------------------------- 主流程

def main():
    OUTPUT_DIR.mkdir(exist_ok=True)

    print("[agent] 启动爬虫...")
    try:
        category, items = scrape(DEFAULT_URL, pages=2, top=100)
    except Exception as e:
        msg = f"❌ 爬虫异常：{e}"
        print(msg, file=sys.stderr)
        send_telegram(msg)
        sys.exit(1)

    if not items:
        msg = "❌ 爬虫未抓到数据，页面结构可能已变化"
        print(msg, file=sys.stderr)
        send_telegram(msg)
        sys.exit(1)

    _, json_path = save(items, category, DEFAULT_URL, str(OUTPUT_DIR))
    today_path = Path(json_path)
    today_data = load_json(today_path)
    print(f"[agent] 保存完成: {today_path.name}")

    prev_files = _sorted_data_files(exclude=today_path)
    prev_data = load_json(prev_files[-1]) if prev_files else None

    changes = detect_changes(today_data, prev_data)
    print(
        f"[agent] 变化检测 — 新上榜: {len(changes['new'])}  "
        f"排名上升: {len(changes['rank_up'])}  "
        f"价格变动: {len(changes['price_change'])}"
    )

    update_history(today_data)
    send_telegram(build_message(changes, today_data))
    print("[agent] 完成")


if __name__ == "__main__":
    main()
