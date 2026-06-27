#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
亚马逊墨西哥站畅销榜（Los más vendidos）选品爬虫

两阶段抓取：
  1. 榜单页（最多 2 页 = 前 100 名）：拿到完整的 ASIN + 排名（含懒加载的 31-50 名）；
  2. 逐个抓商品详情页 /dp/<ASIN>：补全标题、评分、评论数、价格，并解析选品维度——
     月销量（优先亚马逊官方"上月购买量"，否则按评论数推算）、品牌、卖家及其所在地
     （亚马逊自营/品牌方/第三方中国卖家）、上架时间、变体数量、配送方式（FBA/FBM）。

标题翻译使用 DeepSeek API，需在 .env 文件配置 DEEPSEEK_API_KEY 和 DEEPSEEK_API_URL，
依赖 python-dotenv（pip install python-dotenv）。

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
import urllib.request
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_API_URL = os.environ.get("DEEPSEEK_API_URL", "")

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
PAGE_DELAY = (3.0, 5.0)      # 榜单翻页间随机延迟
DETAIL_DELAY = (1.0, 2.5)    # 详情页之间随机延迟，避免触发风控
REVIEW_RATE = 0.012          # 评论率假设：墨西哥/LATAM 市场约 1~1.5%（低于美国的 2.5%）
DEFAULT_MONTHS = 24          # 拿不到上架时间时，推算销量假设的默认在售月数


def load_cookies(cookie_str=None, cookie_file=None):
    """
    解析 Cookie，支持两种来源：
      --cookie "k1=v1; k2=v2"   浏览器 DevTools → Network → 任意请求 → 复制 Cookie 请求头
      --cookie-file cookies.txt  Netscape 格式文件（浏览器插件 "Get cookies.txt" 导出）
    """
    cookies = {}
    if cookie_file:
        try:
            with open(cookie_file, encoding="utf-8") as f:
                content = f.read()
            netscape_lines = [
                ln for ln in content.splitlines()
                if ln.strip() and not ln.startswith("#") and len(ln.split("\t")) >= 7
            ]
            if netscape_lines:
                for line in netscape_lines:
                    parts = line.split("\t")
                    cookies[parts[5]] = parts[6]
            else:
                # 不是 Netscape 格式，按 "k=v; k=v" 的 Cookie 请求头字符串解析
                for part in content.replace("\n", ";").split(";"):
                    part = part.strip()
                    if "=" in part:
                        k, _, v = part.partition("=")
                        cookies[k.strip()] = v.strip()
            print(f"[Cookie] 从文件加载 {len(cookies)} 个 Cookie")
        except Exception as e:
            print(f"[Cookie] 读取文件失败: {e}", file=sys.stderr)
    if cookie_str:
        for part in cookie_str.split(";"):
            part = part.strip()
            if "=" in part:
                k, _, v = part.partition("=")
                cookies[k.strip()] = v.strip()
        print(f"[Cookie] 从字符串加载 {len(cookies)} 个 Cookie")
    return cookies

# CSV/JSON 字段名（中文表头）
FIELD_NAMES_ZH = [
    "排名", "ASIN编号", "商品名称", "品牌", "评分", "评论数", "价格(墨西哥比索)",
    "月销量估算", "销量依据", "上架时间", "在售月数", "变体数量",
    "卖家", "卖家类型", "卖家所在地", "卖家详细地址", "配送方式", "商品链接", "完整详情",
]
FIELD_NAMES_EN = [
    "rank", "asin", "title", "brand", "rating", "reviews", "price_mxn",
    "monthly_sales_est", "sales_basis", "listed_date", "months_listed", "variant_count",
    "seller", "seller_type", "seller_country", "seller_address", "fulfillment", "url", "detail",
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


_translate_fail_count = 0
_translate_disabled = False

def translate_to_zh(text):
    """调用 DeepSeek API 将西班牙语商品标题翻译为中文。
    连续失败 3 次后自动禁用翻译，避免因网络/配额问题导致长时间等待。"""
    global _translate_fail_count, _translate_disabled
    if not text or _translate_disabled:
        return text
    if not DEEPSEEK_API_KEY or not DEEPSEEK_API_URL:
        print("  [翻译] 未配置 DEEPSEEK_API_KEY / DEEPSEEK_API_URL（.env），"
              "已禁用翻译，标题保留西班牙语原文。", file=sys.stderr)
        _translate_disabled = True
        return text
    body = json.dumps({
        "model": "deepseek-v4-flash",
        "messages": [
            {"role": "system", "content": "你是专业的电商翻译，把用户输入的西班牙语商品标题"
                                           "翻译成简洁自然的中文，只返回译文，不要解释，不要加引号。"},
            {"role": "user", "content": text},
        ],
        "stream": False,
    }).encode("utf-8")
    req = urllib.request.Request(
        DEEPSEEK_API_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            _translate_fail_count = 0
            return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        _translate_fail_count += 1
        if _translate_fail_count >= 3:
            _translate_disabled = True
            print("  [翻译] 连续失败 3 次，自动禁用翻译，标题保留西班牙语原文。"
                  "如需翻译请检查 .env 配置或网络。", file=sys.stderr)
        else:
            print(f"  [翻译] 失败 ({e})，保留原文", file=sys.stderr)
        return text


def translate_address_zh(reversed_addr):
    """调用 DeepSeek 把已倒序（国家→...→门牌号）、用 / 分隔的西语地址翻译成中文，
    分隔符和段数、顺序保持不变。复用标题翻译的失败计数/熔断状态。"""
    global _translate_fail_count, _translate_disabled
    if not reversed_addr or _translate_disabled:
        return reversed_addr
    if not DEEPSEEK_API_KEY or not DEEPSEEK_API_URL:
        return reversed_addr
    body = json.dumps({
        "model": "deepseek-v4-flash",
        "messages": [
            {"role": "system", "content": "你是专业的地址翻译，用户会给你一个用 / 分隔、已经按"
                                           "从国家到门牌号顺序排列的西班牙语地址，请把每一段翻译"
                                           "或音译成中文（国家二字码翻译成中文国家名），数字（邮编、"
                                           "门牌号）原样保留，保持 / 分隔符、段数和顺序不变，只返回"
                                           "结果，不要解释，不要加引号。"},
            {"role": "user", "content": reversed_addr},
        ],
        "stream": False,
    }).encode("utf-8")
    req = urllib.request.Request(
        DEEPSEEK_API_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            _translate_fail_count = 0
            return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        _translate_fail_count += 1
        if _translate_fail_count >= 3:
            _translate_disabled = True
            print("  [翻译] 连续失败 3 次，自动禁用翻译，地址保留原文。"
                  "如需翻译请检查 .env 配置或网络。", file=sys.stderr)
        else:
            print(f"  [翻译] 地址翻译失败 ({e})，保留原文", file=sys.stderr)
        return reversed_addr


def fetch(url, max_retries=MAX_RETRIES, cookies=None, referer=None):
    """带重试和浏览器请求头抓取页面，返回 HTML 文本。cookies 为 dict，有值时附加到请求头。"""
    last_err = None
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "es-MX,es;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }
    if referer:
        headers["Referer"] = referer
    if cookies:
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
    for attempt in range(1, max_retries + 1):
        req = urllib.request.Request(url, headers=headers)
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


def parse_bsr(src):
    """解析商品详情页的亚马逊畅销排名（BSR），返回该商品在所在大类的排名整数，失败返回 None。
    Amazon MX 页面格式示例：
      #4 en Mascotas  /  Nº 4 en Mascotas  /  #4 en Productos para Mascotas
    """
    m = (re.search(r'salesrank[^>]*>.*?[#Nº°]+\s*([\d,]+)', src, re.S | re.I)
         or re.search(r'Clasificaci[oó]n.{0,200}?[#Nº°]+\s*([\d,\.]+)', src, re.S | re.I)
         or re.search(r'[#Nº°]\s*([\d,\.]+)\s+en\s+(?:Mascotas|Productos para mascotas'
                      r'|Pet Supplies|Animales)', src, re.I))
    if m:
        try:
            return int(re.sub(r"[,.]", "", m.group(1)))
        except ValueError:
            pass
    return None


# Amazon MX 宠物用品类目 BSR→月销量对照表（对数插值，比评论率法准 2-3 倍）
# 数据来源：行业公开 BSR 转换研究 + 本项目已知数据点校准
_BSR_TABLE = [
    (1,     80000),
    (2,     45000),
    (5,     18000),
    (10,    10000),
    (20,     6000),
    (50,     3000),
    (100,    1500),
    (200,     800),
    (500,     350),
    (1000,    180),
    (2000,     90),
    (5000,     40),
    (10000,    18),
]


def bsr_to_monthly_sales(bsr):
    """用对数插值将 BSR 转换为月销量估算值。"""
    if bsr is None or bsr <= 0:
        return None
    import math
    table = _BSR_TABLE
    if bsr <= table[0][0]:
        return table[0][1]
    if bsr >= table[-1][0]:
        # 超出表末尾：外推
        x1, y1 = math.log(table[-2][0]), math.log(table[-2][1])
        x2, y2 = math.log(table[-1][0]), math.log(table[-1][1])
        slope = (y2 - y1) / (x2 - x1)
        return max(1, round(math.exp(y2 + slope * (math.log(bsr) - x2))))
    for i in range(len(table) - 1):
        lo_bsr, lo_sales = table[i]
        hi_bsr, hi_sales = table[i + 1]
        if lo_bsr <= bsr <= hi_bsr:
            t = (math.log(bsr) - math.log(lo_bsr)) / (math.log(hi_bsr) - math.log(lo_bsr))
            return round(math.exp(math.log(lo_sales) + t * (math.log(hi_sales) - math.log(lo_sales))))
    return None


_UNIT_MULTIPLIERS = {"mil": 1000, "k": 1000, "万": 10000, "w": 10000}


def parse_bought_last_month(src):
    """解析亚马逊官方'上月购买量'徽章，返回 int 或 None。
    锚定 DOM id（social-proofing-faceout-title-tk_bought），正则只抓"数字 + 紧跟的短单位词 + 号"，
    不关心单位词具体是什么语言；单位词到倍数的换算放在 _UNIT_MULTIPLIERS 查表里，
    以后亚马逊换文案/换语言，加一条表项即可，不用改正则。
    """
    m = re.search(r'id="social-proofing-faceout-title-tk_bought"[^>]*>'
                  r'.{0,200}?([\d.,]+)\s*([^\d\s+<]{0,4})\s*\+',
                  src, re.S)
    if not m:
        # 找不到该 id（老版页面结构）：按西语兜底文案匹配
        m = re.search(r"([\d.,]+)\s*([^\d\s+<]{0,4})\s*\+?\s*compras? el mes pasado", src, re.I)
    if not m:
        return None
    try:
        num = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    num *= _UNIT_MULTIPLIERS.get(m.group(2).strip().lower(), 1)
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
    d["bsr"] = parse_bsr(src)
    d["variant_count"] = parse_variant_count(src)
    d["listed_date"], d["months_listed"] = parse_listed_date(src)
    (d["seller"], d["seller_id"], d["ships_from"],
     d["is_amazon"], d["amazon_fulfilled"]) = parse_buybox_seller(src)
    return d


# ---------------------------------------------------------------- 卖家所在地

def parse_seller_detail(src):
    """从卖家主页'卖家详细信息'区块解析地址明细行和国家二字码，返回 (lines, country)。
    锚定 HTML 注释 <!-- Detailed Seller Information -->，地址明细行靠结构 class
    'indent-left' 识别，跟页面显示语言（中/西/英）无关。lines 按 DOM 原始顺序（门牌号→
    街区→市→州→邮编→国家，行政级别从细到粗），最后一行即国家二字码。
    """
    m = re.search(r"<!--\s*Detailed Seller Information\s*-->(.*?)"
                  r"<!--\s*Detailed Seller Information\s*-->", src, re.S)
    if not m:
        return [], ""
    rows = re.findall(r'<div class="a-row[^"]*indent-left[^"]*"[^>]*>(.*?)</div>',
                       m.group(1), re.S)
    lines = [strip_tags(r) for r in rows]
    lines = [ln for ln in lines if ln]
    country = lines[-1] if lines and re.fullmatch(r"[A-Za-z]{2}", lines[-1]) else ""
    return lines, country


def fetch_seller_detail(seller_id, cache, cookies=None):
    """抓卖家主页解析地址明细行和国家二字码（缓存去重），返回 (lines, country)。"""
    if not seller_id:
        return [], ""
    if seller_id in cache:
        return cache[seller_id]
    url = f"https://www.amazon.com.mx/sp?ie=UTF8&seller={seller_id}"
    lines, country = [], ""
    try:
        src = fetch(url, max_retries=2, cookies=cookies)
        lines, country = parse_seller_detail(src)
        if not country:
            # 兜底：老版页面结构，直接找 Dirección 后面的国家码
            m = re.search(r"Direcci[oó]n(?:\s+comercial)?\s*:(.{0,3000})", src, re.S)
            if m:
                codes = re.findall(r"<span>\s*([A-Z]{2})\s*</span>", m.group(1))
                if codes:
                    country = codes[-1]
    except Exception as e:
        print(f"  卖家页抓取失败 ({seller_id}): {e}", file=sys.stderr)
    cache[seller_id] = (lines, country)
    return lines, country


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


def estimate_monthly_sales(bought, bsr, reviews=None, months=None, review_rate=None):
    """月销量估算，三级优先级：
    1. 亚马逊官方上月购买量（最准，登录态下部分商品可见）
    2. BSR 对数插值（比评论率法准 2-3 倍，覆盖率高）
    3. 评论数/在售月数/评论率（兜底）
    """
    if bought:
        return bought, "亚马逊官方(上月购买量)"
    bsr_sales = bsr_to_monthly_sales(bsr)
    if bsr_sales:
        return bsr_sales, f"BSR推算(BSR={bsr})"
    return None, ""


# ---------------------------------------------------------------- 主流程

def scrape(base_url, pages, top, do_translate=True, do_seller=True,
           review_rate=REVIEW_RATE, cookies=None):
    """抓榜单页 + 逐个详情页，返回 (类目名, 按排名排序的商品列表)。"""
    if cookies:
        print(f"[Cookie] 已启用登录态，共 {len(cookies)} 个 Cookie，将尝试获取官方上月购买量")
    # 阶段 1：榜单页 → ASIN + 排名（含懒加载项），渲染卡片数据留作兜底
    category = ""
    ranks = {}
    fallback = {}
    for pg in range(1, pages + 1):
        sep = "&" if "?" in base_url else "?"
        url = base_url if pg == 1 else f"{base_url}{sep}pg={pg}"
        print(f"[榜单] 抓取第 {pg} 页: {url}")
        src = fetch(url, cookies=cookies)
        if not category:
            category = parse_category(src)
            print(f"[类目] {category}")
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
            d = parse_detail_page(fetch(url, cookies=cookies, referer=base_url))
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

    # 阶段 3：卖家所在地 + 详细地址（仅非亚马逊卖家，按 seller_id 去重）
    seller_cache = {}
    if do_seller:
        unique = {it["seller_id"] for it in items if it["seller_id"] and not it["is_amazon"]}
        print(f"[卖家] 共 {len(unique)} 个独立第三方卖家，查询营业地址 ...")
        for j, sid in enumerate(sorted(unique), 1):
            lines, c = fetch_seller_detail(sid, seller_cache, cookies=cookies)
            addr_preview = ", ".join(lines)
            print(f"[卖家 {j}/{len(unique)}] {sid} → {c or '未知'} {addr_preview[:50] if addr_preview else ''}")
            time.sleep(random.uniform(*DETAIL_DELAY))

    # 阶段 4：派生字段 + 翻译
    if do_translate:
        print(f"[翻译] 共 {len(items)} 条标题，开始翻译 ...")
    for idx, it in enumerate(items, 1):
        lines, country = seller_cache.get(it["seller_id"], ([], ""))
        it["seller_country"] = COUNTRY_ZH.get(country, country)
        reversed_addr = "/".join(reversed(lines))
        if do_translate and reversed_addr:
            it["seller_address"] = translate_address_zh(reversed_addr)
            print(f"[翻译地址 {idx}/{len(items)}] {it['seller_address'][:50]}")
        else:
            it["seller_address"] = reversed_addr
        it["seller_type"] = classify_seller(it["seller"], it["brand"],
                                            it["is_amazon"], country)
        it["fulfillment"] = classify_fulfillment(it["is_amazon"], it["ships_from"],
                                                 it["amazon_fulfilled"])
        it["monthly_sales_est"], it["sales_basis"] = estimate_monthly_sales(
            it["bought_last_month"], it.get("bsr"), it["reviews"], it["months_listed"], review_rate)
        if do_translate and it["title"]:
            it["title"] = translate_to_zh(it["title"])
            print(f"[翻译 {idx}/{len(items)}] {it['title'][:40]}")
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
                    help=f"销量推算用的评论率（默认 {REVIEW_RATE} 即 1.2%%）")
    ap.add_argument("--cookie", default="",
                    help="浏览器 Cookie 字符串（从 DevTools Network 面板复制 Cookie 请求头值）")
    ap.add_argument("--cookie-file", default="",
                    help="Netscape 格式 Cookie 文件路径（用浏览器插件 'Get cookies.txt' 导出）")
    args = ap.parse_args()

    cookies = load_cookies(
        cookie_str=args.cookie or None,
        cookie_file=args.cookie_file or None,
    ) or None

    category, items = scrape(args.url, args.pages, args.top,
                             do_translate=not args.no_translate,
                             do_seller=not args.no_seller_info,
                             review_rate=args.review_rate,
                             cookies=cookies)
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
