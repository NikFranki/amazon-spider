# amazon-spider

亚马逊墨西哥站畅销榜（Los más vendidos）选品爬虫，仅依赖 Python 3 标准库。

两阶段抓取：先从榜单页（2 页）拿到前 100 名的 ASIN + 排名（含懒加载的 31-50 名），
再逐个抓商品详情页补全所有选品维度。

## 用法

```bash
# 默认抓「宠物用品 - 狗狗」类目前 100 名（完整跑一次约 10-15 分钟）
python3 amazon_mx_bestsellers_spider_of_dog.py

# 抓任意类目的畅销榜页面
python3 amazon_mx_bestsellers_spider_of_dog.py "https://www.amazon.com.mx/gp/bestsellers/<类目>/<类目ID>/"

# 只抓前 50 名；跳过卖家所在地查询（更快）；跳过翻译
python3 amazon_mx_bestsellers_spider_of_dog.py <URL> --top 50 --no-seller-info --no-translate

# 指定输出目录 / 调整销量推算用的评论率
python3 amazon_mx_bestsellers_spider_of_dog.py <URL> -o ./data --review-rate 0.02
```

## 输出

输出到 `./output/`（可用 `-o` 修改），文件名格式 `bestsellers_<类目ID>_<时间戳>_of_dog.csv/.json`。

| 字段 | 说明 |
| --- | --- |
| 排名 / ASIN编号 / 商品名称 / 品牌 | 商品名称默认翻译为中文 |
| 评分 / 评论数 / 价格(墨西哥比索) | 来自详情页，榜单页数据兜底 |
| 月销量估算 / 销量依据 | 优先亚马逊官方「上月购买量」徽章；否则按 评论数 ÷ 在售月数 ÷ 评论率(默认2.5%) 推算 |
| 上架时间 / 在售月数 | 详情页「Producto en Amazon.com.mx desde」 |
| 变体数量 | twister 变体 JSON（颜色/尺码组合数），无变体为 1 |
| 卖家 / 卖家类型 / 卖家所在地 | 类型：亚马逊自营 / 品牌方(国别) / 第三方(国别)；所在地来自卖家主页营业地址 |
| 配送方式 | 亚马逊自营配送 / FBA / FBM（基于卖家链接 isAmazonFulfilled 标志 + 发货方） |
| 完整详情 | 详情页抓取失败时为 False（字段由榜单页兜底） |

## 已知限制

- 请求量约 100 个详情页 + 去重后的第三方卖家页，带 1.2-2.8s 随机延迟；
  请控制运行频率，避免触发风控（命中验证码会自动退避重试）。
- 月销量为估算值：评论率因类目而异（1%-3% 常见），可用 `--review-rate` 调整；
  上架时间缺失时按默认 24 个月在售推算，会在「销量依据」列标注。
- 卖家类型中「品牌方」按 卖家名≈品牌名 判断；卖家所在地依赖亚马逊卖家主页的
  「Dirección」营业地址，个别卖家页无地址时为空。
- 解析基于当前页面 HTML 结构，亚马逊改版后需更新对应正则
  （榜单页：`parse_recs_list` / `parse_rendered_items`；详情页：`parse_detail_page`；
  卖家页：`fetch_seller_country`）。
