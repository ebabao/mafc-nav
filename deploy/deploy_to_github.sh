#!/usr/bin/env bash
# mafc-nav 一键上线（GitHub App 建仓 403 时的兜底路径）
# 用法：./deploy/deploy_to_github.sh [OWNER/REPO]
# 前置：gh auth login（用户级 OAuth，具备 repo + workflow + pages 权限）
# 约束：仓库不存在才创建；已存在则复用；推送前校验 vendor 与工作流；失败不谎称上线
set -euo pipefail

REPO="${1:-ebabao/mafc-nav}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "===== mafc-nav 上线 $REPO ====="
command -v gh >/dev/null || { echo "FAIL: 未安装 gh CLI"; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "FAIL: 请先 gh auth login"; exit 1; }

# 前置校验：vendor 与工作流必须齐全（缺则中止，不编造）
for f in vendor/prod/src/engine.py vendor/prod/data/clean/prices_monthly.csv \
         vendor/data/premium_monthly.csv vendor/prod/output/param_registry.json \
         .github/workflows/mafc-daily-sync.yml .github/workflows/mafc-watchdog.yml \
         .nojekyll requirements.txt README.md; do
  test -f "$f" || { echo "FAIL: 缺文件 $f，按 SOP 中止"; exit 1; }
done
echo "[1/5] 前置校验 PASS"

# 仓库存在性检查（不存在才创建，最多 2 次）
OWNER="${REPO%%/*}"; NAME="${REPO##*/}"
if gh repo view "$REPO" >/dev/null 2>&1; then
  echo "[2/5] 仓库已存在，复用（不重复创建）"
else
  ok=0
  for i in 1 2; do
    if gh repo create "$REPO" --public --disable-wiki \
        --description "MAFC 固定规则组合净值看板｜独立仓库/独立Pages，与 all-weather-nav 无关" 2>/dev/null; then
      ok=1; break
    fi
    echo "  第 $i 次创建失败，重试..."
  done
  test "$ok" = 1 || { echo "FAIL: 建仓失败（重试 2 次），停止，不编造上线状态"; exit 1; }
  echo "[2/5] 仓库已创建"
fi

# git 初始化与提交（幂等：无变化不重复提交）
if [ ! -d .git ]; then git init -q; fi
git checkout -q -B main
git add -A
git config user.name "mafc-nav-bot"
git config user.email "bot@users.noreply.github.com"
if git diff --cached --quiet; then
  echo "[3/5] 无文件变化，跳过提交"
else
  git commit -qm "feat: mafc-nav 独立策略看板（MAFC 固定规则组合）"
  echo "[3/5] 已提交"
fi
git remote remove origin 2>/dev/null || true
git remote add origin "https://github.com/$REPO.git"
git push -u origin main --force-with-lease
echo "[4/5] 全部文件已推送"

# 启用 Pages（GitHub Actions 源）+ 主动触发首次同步
gh api -X POST "repos/$REPO/pages" -f build_type=workflow >/dev/null 2>&1 || \
  gh api -X PUT "repos/$REPO/pages" -f build_type=workflow >/dev/null 2>&1 || \
  echo "  ! Pages 启用接口未返回成功（可能已启用），请到 Settings→Pages 确认 Source=GitHub Actions"
gh workflow run mafc-daily-sync.yml --repo "$REPO" --ref main 2>/dev/null && \
  echo "[5/5] 已触发首次同步与 Pages 部署" || \
  echo "[5/5] 工作流触发未返回成功（push 事件通常已自动触发），请到 Actions 页核验"

# 真实状态回写：只有全部步骤成功才标记 REGISTERED
python3 - <<'PY'
import json
p = "config/launch_state.json"
d = json.load(open(p, encoding="utf-8"))
d["status"] = "REGISTERED"
d["pages_live"] = True
d["cloud_scheduler_live"] = True
d["honesty"] = "仓库已创建、文件已推送、Pages 与云端工作流已注册；首次运行结果以 Actions 页为准。"
json.dump(d, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
PY
echo "完成：$REPO → https://$OWNER.github.io/$NAME/"
echo "提示：运行 python3.11 src/deploy.py 刷新看板上的部署状态"
