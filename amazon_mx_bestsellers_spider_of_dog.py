#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
亚马逊墨西哥站畅销榜（Los más vendidos）选品爬虫

两阶段抓取：
  1. 榜单页（最多 2 页 = 前 100 名）：拿到完整的 ASIN + 排名（含懒加载的 31-50 名）；
  2. 逐个抓商品详情页 /dp/<ASIN>：补全标题、评分、评论数、价格，并解析选品维度——
     月销量（优先亚马逊官方"上月购买量"，否则按评论数推算）、品牌、卖家及其所在地
     （亚马逊自营/品牌方/第三方中国卖家）、上架时间、变体数量、配送方式（FBA/FBM）。

仅使用 Python 标准库，无第三方依赖。
标题翻译使用 Google Translate 免费 endpoint，无需 API Key。

用法:
    python3 amazon_mx_bestsellers_spider_of_dog.py                      # 默认抓宠物用品-狗狗类目前100名
    python3 amazon_mx_bestsellers_spider_of_dog.py <畅销榜URL>           # 抓指定类目
    python3 amazon_mx_bestsellers_spider_of_dog.py <URL> --top 50       # 只抓前50名
    python3 amazon_mx_bestsellers_spider_of_dog.py <URL> --no-seller-info  # 跳过卖家所在地查询（更快）
    python3 amazon_mx_bestsellers_spider_of_dog.py <URL> --no-translate   # 跳过翻译
    python3 amazon_mx_bestsellers_spider_of_dog.py <URL> -o ./data        # 指定输出目录

说明:
    全量抓取约发起 100 个详情页请求 + 若干卖家页请求，带随机延迟，
    完整跑一次约 8-15 分钟。单个商品抓取失败不会中断整体，
    对应行会标注 完整详情=False。
"""

import argparse
import csv
import gzip
import html
import io
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

DEFAULT_URL = (
    "https://www.amazon.com.mx/gp/bestsellers/pet-supplies/12478521011/"
)

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
]

MAX_RETRIES = 4
RETRY_BACKOFF = 3.0          # 秒，指数退避基数
PAGE_DELAY = (2.0, 5.0)      # 榜单翻页间随机延迟
DETAIL_DELAY = (1.2, 2.8)    # 详情页之间随机延迟，避免触发风控
REVIEW_RATE = 0.012          # 评论率假设：墨西哥/LATAM 市场约 1~1.5%（低于美国的 2.5%）
DEFAULT_MONTHS = 24          # 拿不到上架时间时，推算销量假设的默认在售月数

# CSV/JSON 字段名（中文表头）
FIELD_NAMES_ZH = [
    "排名", "ASIN编号", "商品名称", "品牌", "评分", "评论数", "价格(墨西哥比索)",
    "月销量估算", "销量依据", "上架时间", "在售月数", "变体数量",
    "卖家", "卖家类型", "卖家所在地", "配送方式", "商品链接", "完整详情",
]
FIELD_NAMES_EN = [
    "rank", "asin", "title", "brand", "rating", "reviews", "price_mxn",
    "monthly_sales_est", "sales_basis", "listed_date", "months_listed", "variant_count",
    "seller", "seller_type", "seller_country", "fulfillment", "url", "detail",
]
FIELD_MAP = dict(zip(FIELD_NAMES_EN, FIELD_NAMES_ZH))

SPANISH_MONTHS = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}

COUNTRY_ZH = {
    "CN": "中国", "HK": "中国香港", "TW": "中国台湾", "US": "美国", "MX": "墨西哥",
    "JP": "日本", "KR": "韩国", "DE": "德国", "GB": "英国", "FR": "法国",
    "ES": "西班牙", "IT": "意大利", "CA": "加拿大", "IN": "印度", "VN": "越南",
}


def strip_tags(s):
    return html.unescape(re.sub(r"<[^>]+>", " ", s)).strip()


def translate_to_zh(text):
    """调用 Google Translate 免费 endpoint 将文本翻译为中文，无需 API Key。"""
    if not text:
        return text
    api = "https://translate.googleapis.com/translate_a/single"
    params = urllib.parse.urlencode({
        "client": "gtx", "sl": "auto", "tl": "zh-CN", "dt": "t", "q": text
    })
    req = urllib.request.Request(
        f"{api}?{params}",
        headers={"User-Agent": "Mozilla/5.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return "".join(seg[0] for seg in data[0] if seg[0])
    except Exception as e:
        print(f"  翻译失败 ({e})，保留原文", file=sys.stderr)
        return text


def fetch(url, max_retries=MAX_RETRIES):
    """带重试和浏览器请求头抓取页面，返回 HTML 文本。"""
    last_err = None
    for attempt in range(1, max_retries + 1):
        req = urllib.request.Request(url, headers={
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "es-MX,es;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip",
            "Connection": "keep-alive",
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
                text = raw.decode("utf-8", errors="replace")
                if "api-services-support@amazon.com" in text and "captcha" in text.lower():
                    raise RuntimeError("命中亚马逊验证码页面")
                return text
        except (urllib.error.HTTPError, urllib.error.URLError, RuntimeError) as e:
            last_err = e
            wait = RETRY_BACKOFF * (2 ** (attempt - 1)) + random.uniform(0, 2)
            print(f"  请求失败 ({e})，{wait:.1f}s 后重试 {attempt}/{max_retries} ...",
                  file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"抓取失败，已重试 {max_retries} 次: {last_err}")


# ---------------------------------------------------------------- 榜单页解析

def parse_category(src):
    """从 <title> 提取类目名称。"""
    m = re.search(r"<title>Amazon\.com\.mx Los más vendidos: "
                  r"Los productos más populares en ([^<]+)</title>", src)
    return m.group(1).strip() if m else ""


def parse_recs_list(src):
    """解析 data-client-recs-list 属性，得到完整的 ASIN→排名 映射（含懒加载项）。"""
    ranks = {}
    matches = list(re.finditer(r'data-client-recs-list="([^"]+)"', src))
    if not matches:
        print("  [警告] 未找到 data-client-recs-list 属性，亚马逊页面结构可能已变化，"
              "排名数据可能不完整。", file=sys.stderr)
    for m in matches:
        try:
            items = json.loads(html.unescape(m.group(1)))
        except json.JSONDecodeError:
            continue
        for it in items:
            asin = it.get("id")
            rank = it.get("metadataMap", {}).get("render.zg.rank")
            if asin and rank:
                ranks[asin] = int(rank)
    if matches and not ranks:
        print("  [警告] data-client-recs-list 属性存在但解析不到有效排名，"
              "JSON 格式可能已变化。", file=sys.stderr)
    return ranks


def parse_rendered_items(src):
    """解析榜单页服务端渲染的商品卡片（每页前 30 个），作为详情页失败时的兜底数据。"""
    details = {}
    chunks = re.split(r'id="p13n-asin-index-\d+"', src)
    for c in chunks[1:]:
        asin_m = (re.search(r'data-asin="([A-Z0-9]{10})"', c)
                  or re.search(r"/dp/([A-Z0-9]{10})/", c))
        if not asin_m:
            continue
        asin = asin_m.group(1)
        title_m = re.search(r'p13n-sc-css-line-clamp-\d_[^"]*">([^<]+)</div>', c)
        rating_m = re.search(r'aria-label="([\d.]+) de 5 estrellas, ([\d,]+)', c)
        price_m = (re.search(r'p13n-sc-price[^"]*">\s*\$\s*([\d,]+\.?\d*)', c)
                   or re.search(r"\$([\d,]+\.\d{2})", c))
        details[asin] = {
            "title": html.unescape(title_m.group(1)).strip() if title_m else "",
            "rating": float(rating_m.group(1)) if rating_m else None,
            "reviews": int(rating_m.group(2).replace(",", "")) if rating_m else None,
            "price_mxn": float(price_m.group(1).replace(",", "")) if price_m else None,
        }
    return details


# ---------------------------------------------------------------- 详情页解析

def parse_spanish_date(text):
    """解析西语日期（'23 marzo 2021' / '23 de marzo de 2021'），返回 (ISO日期, 在售月数)。"""
    m = re.search(r"(\d{1,2})\s*(?:de\s+)?([a-zA-Záéíóú]+)\s*(?:de\s+)?(\d{4})", text)
    if not m:
        return "", None
    month = SPANISH_MONTHS.get(m.group(2).lower())
    if not month:
        return "", None
    try:
        d = datetime(int(m.group(3)), month, int(m.group(1)))
    except ValueError:
        return "", None
    months = max(1, round((datetime.now() - d).days / 30.4))
    return d.strftime("%Y-%m-%d"), months


def parse_bought_last_month(src):
    """解析亚马逊官方'上月购买量'徽章（'Más de 1 mil compras el mes pasado'），返回 int 或 None。"""
    m = re.search(r"([\d.,]+)\s*(mil|k)?\s*\+?\s*compras? el mes pasado", src, re.I)
    if not m:
        return None
    try:
        num = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    if m.group(2):
        num *= 1000
    return int(num)


def parse_variant_count(src):
    """从 twister 内联 JSON 解析变体数量（颜色/尺码组合数），无变体返回 1。"""
    m = re.search(r'"dimensionValuesDisplayData"\s*:\s*(\{[^{}]*\})', src)
    if m:
        try:
            return max(1, len(json.loads(m.group(1))))
        except json.JSONDecodeError:
            pass
    m = re.search(r'"num_total_variations"\s*:\s*(\d+)', src)
    if m:
        return max(1, int(m.group(1)))
    return 1


def parse_listed_date(src):
    """从详情页 detail bullets / 产品信息表解析上架时间。"""
    m = (re.search(r"Producto en Amazon[^<]{0,40}desde.{0,300}?<span>([^<]+)</span>",
                   src, re.S)
         or re.search(r"Fecha en que est[aá] disponible[^<]{0,60}</th>\s*"
                      r"<td[^>]*>\s*([^<]+)<", src, re.S))
    return parse_spanish_date(m.group(1)) if m else ("", None)


def parse_buybox_seller(src):
    """解析 buybox 的卖家名、卖家ID、发货方，返回 (seller, seller_id, ships_from, is_amazon)。"""
    seller, seller_id, ships_from, is_amazon = "", "", "", False

    # 新版 ODF 块：desktop-merchant-info（卖家）/ desktop-fulfiller-info（发货方）
    m = re.search(r'offer-display-feature-name="desktop-merchant-info".{0,2000}?'
                  r'offer-display-feature-text-message">\s*([^<]+?)\s*<', src, re.S)
    if m:
        seller = html.unescape(m.group(1)).strip()
    m = re.search(r'offer-display-feature-name="desktop-fulfiller-info".{0,2000}?'
                  r'offer-display-feature-text-message">\s*([^<]+?)\s*<', src, re.S)
    if m:
        ships_from = html.unescape(m.group(1)).strip()

    # 手风琴摘要：'Enviado por: X / Vendido por: Y'
    if not seller:
        m = re.search(r'Vendido por:?\s*</span>\s*<span[^>]*>\s*([^<]+?)\s*</span>', src)
        if m:
            seller = html.unescape(m.group(1)).strip()
    if not ships_from:
        m = re.search(r'Enviado por:?\s*</span>\s*<span[^>]*>\s*([^<]+?)\s*</span>', src)
        if m:
            ships_from = html.unescape(m.group(1)).strip()

    # 老式布局兜底：sellerProfileTriggerId / tabular buybox / merchant-info
    if not seller:
        m = re.search(r'id="sellerProfileTriggerId"[^>]*>([^<]+)<', src)
        if m:
            seller = html.unescape(m.group(1)).strip()
    for attr, key in (("Vendido por", "seller"), ("Env[ií]o", "ships")):
        bm = re.search(r'tabular-attribute-name="%s"(.{0,800}?)</div>\s*</div>' % attr,
                       src, re.S)
        if bm:
            val = strip_tags(bm.group(1))
            if key == "seller" and not seller:
                seller = val
            elif key == "ships" and not ships_from:
                ships_from = val
    if not seller:
        mm = re.search(r'id="merchant-info"[^>]*>(.{0,500}?)</div>', src, re.S)
        if mm:
            info = strip_tags(mm.group(1))
            sm = re.search(r"[Vv]endido (?:y enviado )?por\s+(.+?)(?:\s+y\s|\.|$)", info)
            if sm:
                seller = sm.group(1).strip()
            if "enviado por Amazon" in info or "Envío de Amazon" in info:
                ships_from = ships_from or "Amazon"

    # 第三方卖家主页链接里的 seller id（自营无此链接）；顺带读取 FBA 标志
    amazon_fulfilled = None
    id_m = re.search(r'href=["\'][^"\']*?[?&](?:amp;)?seller=([A-Z0-9]{10,21})[^"\']*',
                     src)
    if id_m:
        seller_id = id_m.group(1)
        af_m = re.search(r"isAmazonFulfilled=([01])", id_m.group(0))
        if af_m:
            amazon_fulfilled = af_m.group(1) == "1"

    if re.match(r"^Amazon\b", seller, re.I):
        is_amazon = True
        seller_id = ""
    return seller, seller_id, ships_from, is_amazon, amazon_fulfilled


def parse_detail_page(src):
    """解析商品详情页，返回各维度字段 dict。"""
    d = {}
    m = re.search(r'id="productTitle"[^>]*>([^<]+)<', src)
    d["title"] = html.unescape(m.group(1)).strip() if m else ""

    m = re.search(r'class="a-icon-alt">([\d.]+) de 5 estrellas', src)
    d["rating"] = float(m.group(1)) if m else None

    m = (re.search(r'id="acrCustomerReviewText"[^>]*aria-label="([\d,.]+)', src)
         or re.search(r'id="acrCustomerReviewText"[^>]*>\s*\(?\s*([\d,.]+)', src))
    d["reviews"] = int(re.sub(r"[,.]", "", m.group(1))) if m else None

    m = (re.search(r'"priceAmount"\s*:\s*([\d.]+)', src)
         or re.search(r'id="apex_desktop".{0,4000}?class="a-offscreen">\s*\$\s*'
                      r'([\d,]+\.?\d*)', src, re.S)
         or re.search(r'class="a-offscreen">\s*\$\s*([\d,]+\.?\d*)', src))
    d["price_mxn"] = float(m.group(1).replace(",", "")) if m else None

    m = re.search(r'<a[^>]*\bid="bylineInfo"[^>]*>\s*'
                  r'(?:Visita la [Tt]ienda de\s*|Marca:\s*)?([^<]+)<', src)
    d["brand"] = html.unescape(m.group(1)).strip() if m else ""

    d["bought_last_month"] = parse_bought_last_month(src)
    d["variant_count"] = parse_variant_count(src)
    d["listed_date"], d["months_listed"] = parse_listed_date(src)
    (d["seller"], d["seller_id"], d["ships_from"],
     d["is_amazon"], d["amazon_fulfilled"]) = parse_buybox_seller(src)
    return d


# ---------------------------------------------------------------- 卖家所在地

def fetch_seller_country(seller_id, cache):
    """抓卖家主页解析'Dirección comercial'营业地址，返回国家二字码（缓存去重）。"""
    if not seller_id:
        return ""
    if seller_id in cache:
        return cache[seller_id]
    url = f"https://www.amazon.com.mx/sp?ie=UTF8&seller={seller_id}"
    country = ""
    try:
        src = fetch(url, max_retries=2)
        m = re.search(r"Direcci[oó]n(?:\s+comercial)?\s*:(.{0,3000})", src, re.S)
        if m:
            codes = re.findall(r"<span>\s*([A-Z]{2})\s*</span>", m.group(1))
            if codes:
                country = codes[-1]
    except Exception as e:
        print(f"  卖家页抓取失败 ({seller_id}): {e}", file=sys.stderr)
    cache[seller_id] = country
    return country


def classify_seller(seller, brand, is_amazon, country):
    """卖家类型：亚马逊自营 / 品牌方 / 第三方(国别)。"""
    if is_amazon:
        return "亚马逊自营"
    if seller and brand:
        norm = lambda s: re.sub(r"[^a-z0-9一-鿿]", "", s.lower())
        a, b = norm(seller), norm(brand)
        if a and b and (a in b or b in a):
            return f"品牌方({COUNTRY_ZH.get(country, country)})" if country else "品牌方"
    if country in ("CN", "HK"):
        return "第三方(中国)"
    if country:
        return f"第三方({COUNTRY_ZH.get(country, country)})"
    return "第三方" if seller else ""


def classify_fulfillment(is_amazon, ships_from, amazon_fulfilled):
    if is_amazon:
        return "亚马逊自营配送"
    if amazon_fulfilled or re.search(r"\bAmazon\b", ships_from or "", re.I):
        return "FBA"
    if amazon_fulfilled is False or ships_from:
        return "FBM"
    return ""


def estimate_monthly_sales(bought, reviews, months, review_rate):
    """月销量估算：优先亚马逊官方'上月购买量'，否则按 评论数/在售月数/评论率 推算。"""
    if bought:
        return bought, "亚马逊官方(上月购买量)"
    if reviews:
        if months:
            return (round(reviews / months / review_rate),
                    f"评论数推算(评论率{review_rate:.1%})")
        return (round(reviews / DEFAULT_MONTHS / review_rate),
                f"评论数推算(评论率{review_rate:.1%}, 按默认{DEFAULT_MONTHS}个月在售)")
    return None, ""


# ---------------------------------------------------------------- 主流程

def scrape(base_url, pages, top, do_translate=True, do_seller=True,
           review_rate=REVIEW_RATE):
    """抓榜单页 + 逐个详情页，返回 (类目名, 按排名排序的商品列表)。"""
    # 阶段 1：榜单页 → ASIN + 排名（含懒加载项），渲染卡片数据留作兜底
    category = ""
    ranks = {}
    fallback = {}
    for pg in range(1, pages + 1):
        sep = "&" if "?" in base_url else "?"
        url = base_url if pg == 1 else f"{base_url}{sep}pg={pg}"
        print(f"[榜单] 抓取第 {pg} 页: {url}")
        src = fetch(url)
        if not category:
            category = parse_category(src)
        ranks.update(parse_recs_list(src))
        fallback.update(parse_rendered_items(src))
        if pg < pages:
            time.sleep(random.uniform(*PAGE_DELAY))
    print(f"[榜单] 共 {len(ranks)} 个排名条目")

    ordered = sorted(ranks.items(), key=lambda kv: kv[1])[:top]

    # 阶段 2：逐个抓详情页
    items = []
    total = len(ordered)
    for i, (asin, rank) in enumerate(ordered, 1):
        url = f"https://www.amazon.com.mx/dp/{asin}"
        try:
            d = parse_detail_page(fetch(url))
            ok = bool(d["title"])
        except Exception as e:
            print(f"  详情页抓取失败 ({asin}): {e}", file=sys.stderr)
            d, ok = {}, False
        fb = fallback.get(asin, {})
        item = {
            "rank": rank,
            "asin": asin,
            "title": d.get("title") or fb.get("title", ""),
            "brand": d.get("brand", ""),
            "rating": d.get("rating") if d.get("rating") is not None else fb.get("rating"),
            "reviews": d.get("reviews") if d.get("reviews") is not None else fb.get("reviews"),
            "price_mxn": d.get("price_mxn") if d.get("price_mxn") is not None else fb.get("price_mxn"),
            "listed_date": d.get("listed_date", ""),
            "months_listed": d.get("months_listed"),
            "variant_count": d.get("variant_count"),
            "seller": d.get("seller", ""),
            "seller_id": d.get("seller_id", ""),
            "ships_from": d.get("ships_from", ""),
            "is_amazon": d.get("is_amazon", False),
            "amazon_fulfilled": d.get("amazon_fulfilled"),
            "bought_last_month": d.get("bought_last_month"),
            "url": url,
            "detail": ok,
        }
        items.append(item)
        print(f"[详情 {i}/{total}] 第{rank}名 {asin} "
              f"{'OK' if ok else '失败(用榜单兜底)'} 卖家={item['seller'] or '-'}")
        if i < total:
            time.sleep(random.uniform(*DETAIL_DELAY))

    # 阶段 3：卖家所在地（按 seller_id 去重）
    seller_cache = {}
    if do_seller:
        unique = {it["seller_id"] for it in items if it["seller_id"] and not it["is_amazon"]}
        print(f"[卖家] 共 {len(unique)} 个独立第三方卖家，查询营业地址 ...")
        for j, sid in enumerate(sorted(unique), 1):
            c = fetch_seller_country(sid, seller_cache)
            print(f"[卖家 {j}/{len(unique)}] {sid} → {c or '未知'}")
            time.sleep(random.uniform(*DETAIL_DELAY))

    # 阶段 4：派生字段 + 翻译
    for it in items:
        country = seller_cache.get(it["seller_id"], "")
        it["seller_country"] = COUNTRY_ZH.get(country, country)
        it["seller_type"] = classify_seller(it["seller"], it["brand"],
                                            it["is_amazon"], country)
        it["fulfillment"] = classify_fulfillment(it["is_amazon"], it["ships_from"],
                                                 it["amazon_fulfilled"])
        it["monthly_sales_est"], it["sales_basis"] = estimate_monthly_sales(
            it["bought_last_month"], it["reviews"], it["months_listed"], review_rate)
        if do_translate and it["title"]:
            it["title"] = translate_to_zh(it["title"])
        # 清理内部中间字段
        for k in ("seller_id", "ships_from", "is_amazon", "amazon_fulfilled",
                  "bought_last_month"):
            it.pop(k, None)

    return category, items


def save(items, category, base_url, out_dir):
    """保存为 CSV 和 JSON，文件名带类目 ID 和时间戳。"""
    os.makedirs(out_dir, exist_ok=True)
    cat_id_m = re.search(r"/bestsellers/[^/]+/(\d+)", base_url)
    cat_id = cat_id_m.group(1) if cat_id_m else "root"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = os.path.join(out_dir, f"bestsellers_{cat_id}_{stamp}_of_dog")

    # 将内部英文 key 转为中文表头后写入 CSV
    zh_items = [{FIELD_MAP[k]: item.get(k) for k in FIELD_NAMES_EN} for item in items]
    with open(base + ".csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELD_NAMES_ZH)
        w.writeheader()
        w.writerows(zh_items)

    # JSON 同样使用中文 key
    with open(base + ".json", "w", encoding="utf-8") as f:
        json.dump({
            "来源": base_url,
            "类目": category,
            "抓取时间": datetime.now().isoformat(),
            "商品总数": len(items),
            "商品列表": zh_items,
        }, f, ensure_ascii=False, indent=2)

    return base + ".csv", base + ".json"


def main():
    ap = argparse.ArgumentParser(description="亚马逊墨西哥站畅销榜选品爬虫")
    ap.add_argument("url", nargs="?", default=DEFAULT_URL,
                    help="畅销榜页面 URL（默认: 宠物用品-狗狗类目）")
    ap.add_argument("--pages", type=int, default=2, choices=[1, 2],
                    help="榜单页数，每页 50 名（默认 2 = 前 100 名）")
    ap.add_argument("--top", type=int, default=100,
                    help="最多抓取的商品数（默认 100）")
    ap.add_argument("-o", "--out", default="./output", help="输出目录（默认 ./output）")
    ap.add_argument("--no-translate", action="store_true",
                    help="跳过标题翻译，保留西班牙语原文")
    ap.add_argument("--no-seller-info", action="store_true",
                    help="跳过卖家营业地址查询（少抓几十个页面，更快）")
    ap.add_argument("--review-rate", type=float, default=REVIEW_RATE,
                    help=f"销量推算用的评论率（默认 {REVIEW_RATE} 即 2.5%%）")
    args = ap.parse_args()

    category, items = scrape(args.url, args.pages, args.top,
                             do_translate=not args.no_translate,
                             do_seller=not args.no_seller_info,
                             review_rate=args.review_rate)
    if not items:
        print("未解析到任何商品，页面结构可能已变化。", file=sys.stderr)
        sys.exit(1)

    csv_path, json_path = save(items, category, args.url, args.out)
    full = sum(1 for i in items if i["detail"])
    print(f"\n类目: {category}")
    print(f"共抓取 {len(items)} 个商品（{full} 个含完整详情，"
          f"{len(items) - full} 个详情页抓取失败）")
    print(f"标题翻译: {'已开启（中文）' if not args.no_translate else '已跳过（西班牙语原文）'}")
    print(f"已保存:\n  {csv_path}\n  {json_path}")


if __name__ == "__main__":
    main()
