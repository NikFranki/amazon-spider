#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
抓取 Amazon MX 畅销榜的一级、二级分类链接

输出：output/category_links.csv
  A列: 一级分类链接（相对路径）
  B列: 二级分类链接（相对路径）

用法:
    python3 scrape_category_links.py
    python3 scrape_category_links.py --cookie-file amazon.com.mx_cookies.txt
"""

import argparse
import csv
import os
import random
import re
import time
import urllib.request
import urllib.error
import gzip
from html.parser import HTMLParser

BASE_URL = "https://www.amazon.com.mx"
ENTRY_PATH = "/gp/bestsellers/ref=zg_bs_unv_officeproduct_0_1"

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
]

MAX_RETRIES = 3
PAGE_DELAY = (2.0, 4.0)


def load_cookies(cookie_file):
    cookies = {}
    if not cookie_file or not os.path.exists(cookie_file):
        return cookies
    with open(cookie_file, encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7:
                cookies[parts[5]] = parts[6]
    print(f"[Cookie] 加载 {len(cookies)} 个")
    return cookies


def fetch(url, cookies=None, referer=None):
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "es-MX,es;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "keep-alive",
    }
    if referer:
        headers["Referer"] = referer
    if cookies:
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())

    for attempt in range(MAX_RETRIES):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
                return data.decode("utf-8", errors="replace")
        except Exception as e:
            wait = (attempt + 1) * 3 + random.uniform(0, 2)
            print(f"  [重试 {attempt+1}/{MAX_RETRIES}] {url} — {e}，等待 {wait:.1f}s")
            time.sleep(wait)
    print(f"  [失败] 无法抓取: {url}")
    return ""


def extract_nav_links(html, level):
    """
    从左侧导航提取指定层级的链接。
    level=0 → ref=zg_bs_nav_*_0  (一级)
    level=1 → ref=zg_bs_nav_*_1  (二级)
    """
    pattern = rf'href="(/gp/bestsellers/[^"]*ref=zg_bs_nav_[^"]*_{level}[^"]*)"'
    links = re.findall(pattern, html)
    # 去重保序
    seen = set()
    result = []
    for link in links:
        # 只保留路径部分（去掉 query string 之后的杂项不影响，但截断 ref= 之前的路径）
        path = link.split("?")[0] if "?" in link else link
        # 规范化：去掉末尾的 ref=... 只保留纯路径
        clean = re.sub(r'/ref=.*$', '', path)
        if clean not in seen:
            seen.add(clean)
            result.append(clean)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cookie-file", default="amazon.com.mx_cookies.txt")
    parser.add_argument("--output", default="output/category_links.csv")
    args = parser.parse_args()

    cookies = load_cookies(args.cookie_file)
    os.makedirs(os.path.dirname(args.output), exist_ok=True)

    # ── Step 1: 抓主页，提取所有一级分类链接 ──
    entry_url = BASE_URL + ENTRY_PATH
    print(f"[Step 1] 抓取入口页: {entry_url}")
    html = fetch(entry_url, cookies=cookies)
    if not html:
        print("入口页抓取失败，退出")
        return

    l1_links = extract_nav_links(html, level=0)
    print(f"  找到 {len(l1_links)} 个一级分类链接")

    # ── Step 2: 逐个一级页，抓二级链接 ──
    rows = []
    for i, l1 in enumerate(l1_links, 1):
        l1_url = BASE_URL + l1
        print(f"[Step 2] ({i}/{len(l1_links)}) {l1}")
        time.sleep(random.uniform(*PAGE_DELAY))

        l1_html = fetch(l1_url, cookies=cookies, referer=entry_url)
        if not l1_html:
            rows.append((l1, ""))
            continue

        l2_links = extract_nav_links(l1_html, level=1)
        print(f"  → {len(l2_links)} 个二级链接")

        if not l2_links:
            rows.append((l1, ""))
        else:
            for l2 in l2_links:
                rows.append((l1, l2))

    # ── Step 3: 写 CSV ──
    with open(args.output, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["一级分类链接", "二级分类链接"])
        writer.writerows(rows)

    print(f"\n[完成] 共 {len(rows)} 行，已保存到 {args.output}")


if __name__ == "__main__":
    main()
