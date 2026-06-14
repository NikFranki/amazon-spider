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

---

## Agent 自动化部署流程

每天自动抓取 → 变化检测 → 推送 GitHub → Telegram 通知 → 前端页面展示。

### 整体架构

```
GitHub Actions (每天 UTC 15:00 = 墨西哥城 09:00)
        ↓
  agent.py 运行爬虫 + 变化检测
        ↓
  更新 output/history.json（前端数据源）
        ↓
  git commit + push 到 GitHub
        ↓
  Telegram Bot 推送每日摘要
        ↓
  GitHub Pages 前端页面（web/index.html）自动读取最新数据
```

---

### 第一步：创建 Telegram Bot

1. 打开 Telegram，搜索 **@BotFather**
2. 发送 `/newbot`，按提示取一个 bot 名称（任意）
3. BotFather 会返回一串 **Token**，格式如 `1234567890:AAFxxxxxxxxxxxxxxxx`，保存好
4. 找到刚创建的 bot，给它发任意一条消息（如 `hello`）
5. 浏览器访问以下地址获取你的 `chat_id`：
   ```
   https://api.telegram.org/bot<你的TOKEN>/getUpdates
   ```
   返回 JSON 中 `result[0].message.chat.id` 的值就是 **Chat ID**（纯数字）

> ⚠️ Token 和 Chat ID 都不要提交到代码里，只存在 GitHub Secrets 中。

---

### 第二步：配置 GitHub Secrets

进入仓库页面 → **Settings → Secrets and variables → Actions → New repository secret**，添加两条：

| Secret 名称 | 值 |
|---|---|
| `TELEGRAM_BOT_TOKEN` | BotFather 给的 Token |
| `TELEGRAM_CHAT_ID` | getUpdates 拿到的 chat.id |

---

### 第三步：启用 GitHub Pages（前端页面）

1. 仓库页面 → **Settings → Pages**
2. Source 选 **Deploy from a branch**
3. Branch 选 `main`，目录选 `/web`
4. 保存后等约 1 分钟，页面地址为：
   ```
   https://<你的用户名>.github.io/<仓库名>/
   ```

> 前端页面从 `output/history.json` 读取数据。首次部署后需等 agent 跑完一次才有数据。

---

### 第四步：手动触发测试

1. 进入仓库 → **Actions → Daily Amazon MX Spider**
2. 点击 **Run workflow** 手动触发一次
3. 等待约 15 分钟（爬虫全量抓取耗时），检查：
   - Actions 日志是否显示绿色 ✓
   - `output/` 目录是否新增了 CSV + JSON 文件
   - `output/history.json` 是否已生成
   - Telegram 是否收到推送消息
   - 前端页面是否显示数据

---

### 定时调度说明

`.github/workflows/daily_spider.yml` 中的 cron 配置：

```yaml
schedule:
  - cron: '0 15 * * *'   # UTC 15:00 = 墨西哥城 09:00 (CST, UTC-6)
```

如需修改时间，注意 GitHub Actions 使用 UTC 时区，墨西哥城比 UTC 慢 6 小时（冬令时）或 5 小时（夏令时）。

---

### 每日 Telegram 通知格式示例

```
🐾 Amazon MX 宠物狗畅销榜 · 2026-06-15
共 100 款商品

🆕 新上榜 3 款
  #47 Collar táctico ajustable para perro… MXN 188.0
  ...

📈 排名上升 TOP5
  ↑12  #35→#23  MESVIER Arnés para Perro…
  ...

💰 价格变动
  ↓8.3%  MXN 299→274  Cama para Perro…
  ...
```

---

### 文件说明

| 文件 | 用途 |
|---|---|
| `amazon_mx_bestsellers_spider_of_dog.py` | 核心爬虫，可单独运行 |
| `agent.py` | Agent 调度脚本，供 GitHub Actions 调用 |
| `.github/workflows/daily_spider.yml` | GitHub Actions 定时任务配置 |
| `web/index.html` | 前端可视化页面（表格 + sparkline 趋势图） |
| `output/history.json` | 所有历史数据聚合文件，前端数据源 |
| `output/bestsellers_*.csv` | 每次抓取的原始 CSV，可直接用 Excel 打开 |
| `output/bestsellers_*.json` | 每次抓取的原始 JSON |

---

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
