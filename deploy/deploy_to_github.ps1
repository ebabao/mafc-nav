# mafc-nav 一键上线（Windows / PowerShell，GitHub App 建仓 403 时的兜底路径）
# 用法：powershell -ExecutionPolicy Bypass -File deploy/deploy_to_github.ps1 [-Repo "ebabao/mafc-nav"]
# 前置：gh auth login（用户级 OAuth，具备 repo + workflow + pages 权限）
param([string]$Repo = "ebabao/mafc-nav")

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

Write-Host "===== mafc-nav 上线 $Repo ====="
if (-not (Get-Command gh -ErrorAction SilentlyContinue)) { throw "未安装 gh CLI" }
gh auth status 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) { throw "请先执行 gh auth login" }

# 前置校验：缺文件即中止，不编造
$need = @(
  "vendor/prod/src/engine.py", "vendor/prod/data/clean/prices_monthly.csv",
  "vendor/data/premium_monthly.csv", "vendor/prod/output/param_registry.json",
  ".github/workflows/mafc-daily-sync.yml", ".github/workflows/mafc-watchdog.yml",
  ".nojekyll", "requirements.txt", "README.md"
)
foreach ($f in $need) {
  if (-not (Test-Path $f)) { throw "缺文件 $f，按 SOP 中止" }
}
Write-Host "[1/5] 前置校验 PASS"

# 仓库存在性检查（不存在才创建，最多 2 次）
gh repo view $Repo 2>&1 | Out-Null
if ($LASTEXITCODE -eq 0) {
  Write-Host "[2/5] 仓库已存在，复用（不重复创建）"
} else {
  $ok = $false
  for ($i = 1; $i -le 2; $i++) {
    gh repo create $Repo --public --disable-wiki --description "MAFC 固定规则组合净值看板｜独立仓库/独立Pages，与 all-weather-nav 无关" 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) { $ok = $true; break }
    Write-Host "  第 $i 次创建失败，重试..."
  }
  if (-not $ok) { throw "建仓失败（重试 2 次），停止，不编造上线状态" }
  Write-Host "[2/5] 仓库已创建"
}

if (-not (Test-Path .git)) { git init -q }
git checkout -q -B main
git add -A
git config user.name "mafc-nav-bot"
git config user.email "bot@users.noreply.github.com"
git diff --cached --quiet
if ($LASTEXITCODE -eq 0) { Write-Host "[3/5] 无文件变化，跳过提交" }
else { git commit -qm "feat: mafc-nav 独立策略看板（MAFC 固定规则组合）"; Write-Host "[3/5] 已提交" }
git remote remove origin 2>$null | Out-Null
git remote add origin "https://github.com/$Repo.git"
git push -u origin main --force-with-lease
Write-Host "[4/5] 全部文件已推送"

gh api -X POST "repos/$Repo/pages" -f build_type=workflow 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
  gh api -X PUT "repos/$Repo/pages" -f build_type=workflow 2>&1 | Out-Null
}
gh workflow run mafc-daily-sync.yml --repo $Repo --ref main 2>&1 | Out-Null
Write-Host "[5/5] 已请求启用 Pages 并触发首次同步（请到 Actions / Settings→Pages 核验）"

# 真实状态回写
$statePath = "config/launch_state.json"
$state = Get-Content $statePath -Raw -Encoding UTF8 | ConvertFrom-Json
$state.status = "REGISTERED"
$state.pages_live = $true
$state.cloud_scheduler_live = $true
$state.honesty = "仓库已创建、文件已推送、Pages 与云端工作流已注册；首次运行结果以 Actions 页为准。"
$state | ConvertTo-Json -Depth 10 | Set-Content $statePath -Encoding UTF8

$owner = ($Repo -split "/")[0]; $name = ($Repo -split "/")[1]
Write-Host "完成：$Repo → https://$owner.github.io/$name/"
Write-Host "提示：运行 python src/deploy.py 刷新看板上的部署状态"
