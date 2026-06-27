#!/bin/bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

mkdir -p logs
exec >> logs/spider.log 2>&1

echo ""
echo "[$(date '+%Y-%m-%d %H:%M:%S')] ===== 开始运行 ====="

# 加载 .env（DEEPSEEK / TELEGRAM 凭证）
if [ -f ".env" ]; then
    set -a
    source .env
    set +a
fi

# 跑爬虫 + 变化检测 + 更新 history.json + 发 Telegram
"$PROJECT_DIR/venv/bin/python3" agent.py

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 爬虫完成，推送到 GitHub ..."

export PATH="/opt/homebrew/bin:/usr/bin:/bin:$PATH"

git add output/
if git diff --staged --quiet; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] output/ 无变化，跳过提交"
else
    git commit -m "data: 自动更新畅销榜数据 $(date +%Y-%m-%d)"
    git push
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 推送完成"
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] ===== 全部完成 ====="
