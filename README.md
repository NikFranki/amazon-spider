# amazon-spider

亚马逊墨西哥站畅销榜（Los más vendidos）选品爬虫。

两阶段抓取：先从榜单页（2 页）拿到前 100 名的 ASIN + 排名（含懒加载的 31-50 名），
再逐个抓商品详情页补全所有选品维度。

## 安装

**1. 创建并激活虚拟环境**

```bash
cd amazon-spider
python3 -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
```

**2. 安装依赖**

```bash
pip install python-dotenv
```

**3. 配置 `.env`**

在项目根目录创建 `.env`（参考下方说明填写）：

```
DEEPSEEK_API_KEY=你的key
DEEPSEEK_API_URL=https://api.deepseek.com/v1/chat/completions

TELEGRAM_BOT_TOKEN=你的token
TELEGRAM_CHAT_ID=你的chat_id
```

- `DEEPSEEK_*`：用于将西班牙语商品标题翻译为中文，未填写时自动跳过翻译
- `TELEGRAM_*`：用于每日推送摘要，未填写时跳过通知

> 每次开新终端都要先执行 `source venv/bin/activate` 激活虚拟环境。

在项目根目录创建 `.env`：

```
DEEPSEEK_API_KEY=你的key
DEEPSEEK_API_URL=DeepSeek 聊天接口地址
```

未配置时会自动禁用翻译，标题保留西班牙语原文（不影响其余抓取流程）。

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

# 使用 Netscape cookie 文件登录态抓取（用浏览器插件 Get cookies.txt LOCALLY 导出到
# amazon-spider/amazon.com.mx_cookies.txt），登录态下部分商品可拿到官方「上月购买量」
python3 amazon_mx_bestsellers_spider_of_dog.py --cookie-file amazon.com.mx_cookies.txt

# 也可以直接传浏览器 DevTools → Network 面板复制的 Cookie 请求头字符串
python3 amazon_mx_bestsellers_spider_of_dog.py --cookie "k1=v1; k2=v2"

# 不加翻译
python3 amazon_mx_bestsellers_spider_of_dog.py --cookie-file amazon.com.mx_cookies.txt --no-translate

# 指定页码和前几名
python3 amazon_mx_bestsellers_spider_of_dog.py --pages 1 --top 1 --cookie-file
  amazon.com.mx_cookies.txt
```

## 输出

输出到 `./output/`（可用 `-o` 修改），文件名格式 `bestsellers_<类目ID>_<时间戳>_of_dog.csv/.json`。

| 字段 | 说明 |
| --- | --- |
| 排名 / ASIN编号 / 商品名称 / 品牌 | 商品名称默认翻译为中文 |
| 评分 / 评论数 / 价格(墨西哥比索) | 来自详情页，榜单页数据兜底 |
| 月销量估算 / 销量依据 | 三级优先：① 亚马逊官方「上月购买量」徽章 ② BSR（畅销排名）对数插值推算 ③ 评论数 ÷ 在售月数 ÷ 评论率(默认1.2%) 兜底 |
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
本地 Mac（macOS launchd 每天 10:00 触发）
        ↓
  run_spider.sh
        ↓
  agent.py 运行爬虫 + 变化检测 + 更新 output/history.json
        ↓
  git commit + push 到 GitHub
        ↓
  Telegram Bot 推送每日摘要
        ↓
  GitHub Pages 前端页面自动读取最新 history.json
```

**为什么用本地机器而不是 GitHub Actions？**

GitHub Actions 运行在 Azure 云服务器上，IP 段是公开的，亚马逊会直接封锁。
本地住宅宽带 IP 不在封禁名单内，抓取成功率接近 100%。
GitHub 仍承担数据存储（output/）和前端展示（GitHub Pages）职责，不做抓取。

`.github/workflows/daily_spider.yml` 中的 `schedule` 已注释禁用，`workflow_dispatch` 保留手动触发入口备用。

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

> ⚠️ Token 和 Chat ID 不要提交到代码里，只写在本地 `.env` 文件中（已在 `.gitignore` 排除）。

---

### 第二步：配置 `.env`

将 Telegram 凭证和 DeepSeek API Key 写入项目根目录的 `.env`（`.gitignore` 已排除，不会提交）：

```
DEEPSEEK_API_KEY=你的key
DEEPSEEK_API_URL=https://api.deepseek.com/v1/chat/completions

TELEGRAM_BOT_TOKEN=你的token
TELEGRAM_CHAT_ID=你的chat_id
```

`run_spider.sh` 启动时会 `source .env` 将这些变量注入环境，`agent.py` 通过 `os.environ.get` 读取。

---

### 第三步：启用 GitHub Pages（前端页面）

1. 仓库页面 → **Settings → Pages**
2. Source 选 **Deploy from a branch**
3. Branch 选 `main`，目录选 `/(根目录)`
4. 保存后等约 1 分钟，页面地址为：
   ```
   https://<你的用户名>.github.io/<仓库名>/
   ```

> 前端页面从 `output/history.json` 读取数据。首次部署后需等 agent 跑完一次才有数据。

---

### 第四步：本地定时调度（macOS launchd）

调度方案：`run_spider.sh` + launchd plist，每天本地时间 10:00 自动触发。

**`run_spider.sh` 做了什么：**

1. `source .env` 加载凭证到环境变量
2. 用 venv 里的 python3 跑 `agent.py`（爬虫 + 变化检测 + history.json + Telegram）
3. `git add output/ && git commit && git push` 推送到 GitHub
4. 所有输出追加到 `logs/spider.log`

**部署步骤：**

```bash
# 1. 给脚本加执行权限
chmod +x run_spider.sh

# 2. 把 plist 复制到 LaunchAgents 目录
cp com.franki.amazon-spider.plist ~/Library/LaunchAgents/

# 3. 加载（下次开机及每天 10:00 自动触发）
launchctl load ~/Library/LaunchAgents/com.franki.amazon-spider.plist

# 4. 立即手动触发一次验证
launchctl start com.franki.amazon-spider
```

**查看日志：**

```bash
tail -f logs/spider.log
```

日志每次运行约 20-30 KB，一年累计约 10 MB，一般不需要处理。如果想清空：

```bash
> logs/spider.log
```

**停用/重新加载：**

```bash
launchctl unload ~/Library/LaunchAgents/com.franki.amazon-spider.plist
launchctl load   ~/Library/LaunchAgents/com.franki.amazon-spider.plist
```

> 注意：电脑关机或睡眠时 launchd 不会触发，当天错过的任务不会补跑。如需保障每天必跑，保持电脑在 10:00 前开机即可。

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
| `agent.py` | 调度脚本：爬虫 + 变化检测 + history.json + Telegram |
| `run_spider.sh` | 本地定时任务入口：加载 .env → 跑 agent.py → git push |
| `com.franki.amazon-spider.plist` | macOS launchd 定时配置，复制到 ~/Library/LaunchAgents/ |
| `.github/workflows/daily_spider.yml` | GitHub Actions 配置（schedule 已禁用，保留手动触发） |
| `index.html` | 前端可视化页面（表格 + sparkline 趋势图） |
| `output/history.json` | 所有历史数据聚合文件，前端数据源 |
| `output/bestsellers_*.csv` | 每次抓取的原始 CSV，可直接用 Excel 打开 |
| `output/bestsellers_*.json` | 每次抓取的原始 JSON |
| `logs/spider.log` | 本地运行日志（.gitignore 排除） |

---

## 已知限制

- 请求量约 100 个详情页 + 去重后的第三方卖家页，带 2.5-5.0s 随机延迟；
  请控制运行频率，避免触发风控（命中验证码会自动退避重试）。
- 月销量为估算值（BSR 推算/评论率推算均非官方数据）：评论率因类目而异（1%-3% 常见），
  可用 `--review-rate` 调整；上架时间缺失时按默认 24 个月在售推算，会在「销量依据」列标注。
- 卖家类型中「品牌方」按 卖家名≈品牌名 判断；卖家所在地依赖亚马逊卖家主页的
  「Dirección」营业地址，个别卖家页无地址时为空。
- 解析基于当前页面 HTML 结构，亚马逊改版后需更新对应正则
  （榜单页：`parse_recs_list` / `parse_rendered_items`；详情页：`parse_detail_page`；
  卖家页：`fetch_seller_country`）。
