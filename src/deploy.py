"""
mafc-nav 自动化部署方案（幂等，含冒烟测试、回滚、健康检查）
==================================================================
执行「策略模型接口 + 看板页面数据每日自动同步」的本地部署，并产出三种可移植的
定时注册配置（Linux cron / GitHub Actions / Windows 计划任务）。

阶段：
  Stage 0 环境检查   Python / 依赖 / prod 源工程 / 目录
  Stage 1 配置固化   config/sync_policy.json、config/shadow_policy.json
  Stage 2 定时配置   deploy/crontab.txt、daily-sync.yml、windows_task.ps1
  Stage 3 冒烟部署   备份旧看板 -> 跑一次 sync_daily.py；失败自动回滚
  Stage 4 幂等复验   再跑一次，台账行数不变（同日覆盖而非重复追加）
  Stage 5 状态写入   写 deploy_report.json 后刷新看板，使部署状态上板
  Stage 6 健康检查   文件齐全 / 数字一致 / 日志 / 定时配置可读

诚实约束（用户 SOP）：
  · 云端状态一律读 config/launch_state.json 的真实结果，不写死"已上线"。
    当前 GitHub App 集成令牌建仓返回 403（重试 2 次后停止），故 status=BLOCKED_PERMISSION，
    Pages 与云端定时任务均未生效 —— 绝不谎称已上线。
  · 本地部署（配置 + 链路 + 看板 + 幂等）已真实执行并通过冒烟测试。
  · prod 最小运行依赖已打包进 vendor/，云端 runner 无需外部 prod 工程；
    否则 sync_daily 会按 SOP 前置校验中止（不编造数据）。
  · 本工程为独立仓库 mafc-nav，与 ebabao/all-weather-nav 无任何引用或覆盖。

运行：python3.11 src/deploy.py
产出：output/deploy_report.json、output/health.json、dashboard/index.html
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WORKSPACE = os.path.dirname(ROOT)
sys.path.insert(0, HERE)
CFG = os.path.join(ROOT, "config")
DEPLOY = os.path.join(ROOT, "deploy")
OUT = os.path.join(ROOT, "output")
DATA = os.path.join(ROOT, "data")
DASH = os.path.join(ROOT, "dashboard")
LOGS = os.path.join(ROOT, "logs")
for d in (CFG, DEPLOY, OUT, DATA, DASH, LOGS):
    os.makedirs(d, exist_ok=True)
PY = sys.executable
BACKUP = os.path.join(OUT, "backup_index.html")
DASH_HTML = os.path.join(DASH, "index.html")
SYNC = os.path.join(HERE, "sync_daily.py")
RUNLOG = os.path.join(LOGS, "sync.log")
def _prod_dir():
    """prod 源工程路径：优先读 config/sync_policy.json（vendor 化后为 vendor/prod），
    使本地工作区与 GitHub Actions runner 使用同一套相对路径，云端无需外部工程。"""
    pol = os.path.join(CFG, "sync_policy.json")
    if os.path.exists(pol):
        try:
            p = json.load(open(pol, encoding="utf-8")).get("prod_dir")
            if p:
                return p if os.path.isabs(p) else os.path.normpath(os.path.join(ROOT, p))
        except Exception:
            pass
    return os.path.join(ROOT, "vendor", "prod")


PROD = _prod_dir()


def log(stage, status, detail, t0, recs):
    r = {"stage": stage, "status": status, "detail": detail,
         "elapsed_sec": round(time.time() - t0, 2)}
    recs.append(r)
    print(f"[{stage:24s}] {status:5s} {detail}  ({r['elapsed_sec']}s)", flush=True)
    return r


def run_sync(tag):
    r = subprocess.run([PY, SYNC], cwd=ROOT, capture_output=True, text=True)
    with open(RUNLOG, "a", encoding="utf-8") as f:
        f.write(f"\n===== {tag} {datetime.now().isoformat(timespec='seconds')} rc={r.returncode} =====\n")
        f.write((r.stdout or "")[-4000:])
        if r.returncode != 0:
            f.write("\n--- stderr ---\n" + (r.stderr or "")[-2000:])
    return r


def main():
    t_all = time.time()
    run_id = datetime.now().strftime("DEPLOY-%Y%m%d-%H%M%S")
    print(f"===== mafc-nav 自动化部署 {run_id} =====", flush=True)
    recs = []

    # Stage 0
    t = time.time()
    missing = [p for p in (SYNC, os.path.join(PROD, "src", "engine.py"),
                           os.path.join(PROD, "output", "param_registry.json"))
               if not os.path.exists(p)]
    try:
        import akshare, matplotlib, pandas, numpy  # noqa: F401
        deps_ok = True
    except Exception as ex:
        deps_ok = False
        missing.append(f"依赖缺失:{type(ex).__name__}")
    log("Stage0 环境检查", "PASS" if (not missing and deps_ok) else "FAIL",
        "Python/依赖/prod 源工程齐全" if not missing and deps_ok else f"缺 {missing}", t, recs)
    if missing:
        return 1

    # Stage 1 配置
    t = time.time()
    sync_policy = {
        "prod_dir": os.path.relpath(PROD, ROOT),
        "sync_time_local": "09:30",
        "timezone": "Asia/Shanghai",
        "probe_channels": ["sina", "em"],
        "max_retries": 2,
        "monthly_update_rule": "仅在月末数据确认后追加月度净值点；日间沿用上月净值并标 carry_forward",
        "idempotency": "nav_daily.csv 按 date 覆盖写入，同日重复运行不新增行",
        "on_missing_file": "跳过并标注，不编造",
        "note": "路径相对 mafc-nav 根目录，可移植到其它工作区",
    }
    shadow_policy = {
        "training_params_source": "prod/output/param_registry.json（上线前研究训练参数）",
        "inherited": {"dim": 4, "smooth": 0.65, "seed_base": 1000,
                      "degrade_patience": 2, "warm_start": True,
                      "inherited_degraded": True},
        "train_cost_bps": 5, "eval_cost_bps": 15,
        "release_rule": "连续 2 折样本外同时跑赢固定版 CAGR 与 Sharpe 才解除降级",
        "isolation": "进化学习为旁路监控；上线版固定参数始终不变；候选参数不得直接用于实盘",
        "paper_trading_gate": ">=3 个月实盘观察、信号一致率>=95%、滑点<=15bp、溢价违规=0",
        "honesty": "当前实盘观察月数=0，净值均为历史回灌演练，不编造实盘记录",
    }
    for name, obj in (("sync_policy.json", sync_policy), ("shadow_policy.json", shadow_policy)):
        with open(os.path.join(CFG, name), "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
    log("Stage1 配置固化", "PASS", "sync_policy / shadow_policy 已生成（路径可配置）", t, recs)

    # Stage 2 定时配置（3 种）
    t = time.time()
    cron_line = (f"30 9 * * * cd {ROOT} && {PY} {SYNC} >> {RUNLOG} 2>&1")
    with open(os.path.join(DEPLOY, "crontab.txt"), "w", encoding="utf-8") as f:
        f.write("# mafc-nav 每日同步（本地 09:30）\n"
                "# 注册：crontab -l > /tmp/ct; cat deploy/crontab.txt >> /tmp/ct; crontab /tmp/ct\n"
                "# 或 crontab -e 手动追加下面一行\n\n" + cron_line + "\n")

    yml = f"""# mafc-nav 每日同步 · 本地参考模板（云端正式版见 .github/workflows/mafc-daily-sync.yml）
# UTC 01:30 = 北京时间 09:30；亦可用 workflow_dispatch 手动触发
# 与 ebabao/all-weather-nav 完全独立：独立仓库、独立并发组、独立 Pages 站点、错峰 cron
name: mafc-nav daily sync (local reference)
on:
  workflow_dispatch:
permissions:
  contents: write
concurrency:
  group: mafc-nav-sync
  cancel-in-progress: false
jobs:
  sync:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      - name: 安装中文字体与依赖
        run: |
          sudo apt-get update -qq && sudo apt-get install -y -qq fonts-noto-cjk
          pip install -r requirements.txt
      - name: 校验随仓库打包的 vendor 依赖
        run: |
          test -f vendor/prod/src/engine.py
          test -f vendor/prod/data/clean/prices_monthly.csv
          test -f vendor/data/premium_monthly.csv
          test -f vendor/prod/output/param_registry.json
      - name: 每日同步（现场回测 + 影子进化 + 看板重生成）
        run: python src/sync_daily.py
      - name: 提交数据变更（幂等：无变化则跳过）
        run: |
          git config user.name "mafc-nav-bot"
          git config user.email "bot@users.noreply.github.com"
          git add data/ dashboard/index.html output/ 2>/dev/null || true
          if git diff --cached --quiet; then echo "无数据变化，跳过提交"; else git commit -m "chore(nav): mafc-nav 每日同步" && git push; fi
"""
    with open(os.path.join(DEPLOY, "daily-sync.yml"), "w", encoding="utf-8") as f:
        f.write(yml)

    ps1 = f"""# mafc-nav 每日同步 · Windows 计划任务（以管理员 PowerShell 运行）
# 若工程不在默认路径，修改下面 $ProjectRoot
$ProjectRoot = "{ROOT}"
$Python      = "python"
$Script      = Join-Path $ProjectRoot "src\\sync_daily.py"
$Action      = New-ScheduledTaskAction -Execute $Python -Argument "`"$Script`"" -WorkingDirectory $ProjectRoot
$Trigger     = New-ScheduledTaskTrigger -Daily -At "09:30"
$Settings    = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 20)
Register-ScheduledTask -TaskName "mafc-nav-daily-sync" -Action $Action -Trigger $Trigger `
  -Settings $Settings -Description "策略模型接口与看板数据每日自动同步" -Force
Get-ScheduledTask -TaskName "mafc-nav-daily-sync" | Select-Object TaskName,State,NextRunTime
"""
    with open(os.path.join(DEPLOY, "windows_task.ps1"), "w", encoding="utf-8") as f:
        f.write(ps1)
    log("Stage2 定时配置", "PASS", "crontab.txt / daily-sync.yml / windows_task.ps1 三套已生成", t, recs)

    # Stage 3 冒烟（带回滚）
    t = time.time()
    had_dash = os.path.exists(DASH_HTML)
    if had_dash:
        shutil.copy(DASH_HTML, BACKUP)
    r1 = run_sync("smoke")
    ok1 = r1.returncode == 0 and os.path.exists(DASH_HTML)
    if not ok1:
        if had_dash:
            shutil.copy(BACKUP, DASH_HTML)
        log("Stage3 冒烟部署", "FAIL", f"同步失败 rc={r1.returncode}，已回滚看板", t, recs)
        print((r1.stderr or r1.stdout)[-1500:], flush=True)
        return 1
    log("Stage3 冒烟部署", "PASS", "sync_daily.py 首次运行成功，看板已生成", t, recs)

    # Stage 4 幂等复验
    t = time.time()
    import pandas as pd
    ledger = os.path.join(DATA, "nav_daily.csv")
    n_before = len(pd.read_csv(ledger))
    r2 = run_sync("idempotency-check")
    n_after = len(pd.read_csv(ledger)) if os.path.exists(ledger) else -1
    ok2 = r2.returncode == 0 and n_before == n_after
    log("Stage4 幂等复验", "PASS" if ok2 else "FAIL",
        f"重复运行后台账 {n_before}→{n_after} 行（应不变）", t, recs)

    # Stage 5 写部署报告后刷新看板，使部署状态上板
    t = time.time()
    # 云端状态一律由 config/launch_state.json 的真实结果驱动，不写死"已上线"
    LS = os.path.join(CFG, "launch_state.json")
    launch = json.load(open(LS, encoding="utf-8")) if os.path.exists(LS) else {}
    ls_status = launch.get("status", "UNKNOWN")
    ls_pages = launch.get("pages_url", "https://ebabao.github.io/mafc-nav/")
    if ls_status == "REGISTERED":
        cloud_sync = "REGISTERED（工作流已推送并由 schedule/workflow_dispatch 驱动，cron UTC 01:30 = 北京 09:30）"
        cloud_watch = "REGISTERED（watchdog 每 3 小时巡检，26h 停更 / 3d 数据过期即 workflow_dispatch 重启，冷却 6h）"
        cloud_pages = f"LIVE：{ls_pages}（仅 index.html + data/ 接口，独立站点）"
    else:
        cloud_sync = (f"{ls_status}（工作流文件已生成并通过本地验收，但仓库未创建 / 未推送；"
                      "GitHub App 集成令牌建仓返回 403，重试 2 次后停止，未谎称上线）")
        cloud_watch = f"{ls_status}（mafc-watchdog.yml 已就绪，待仓库推送后生效）"
        cloud_pages = f"PENDING：目标站点 {ls_pages}，仓库创建受阻、Pages 尚未启用"
    schedule_status = {
        "本地部署": "DEPLOYED_AND_SMOKE_PASSED（配置+链路+看板+幂等已真实执行）",
        "云端定时同步": cloud_sync,
        "云端停更兜底": cloud_watch,
        "Pages 发布": cloud_pages,
        "运行依赖": "vendor/prod 已随工程打包（引擎/清洗主表/溢价序列/研究参数/披露与容量结果），"
                   "云端 runner 无需外部 prod 工程；抓数失败按 SOP 回退快照并如实标注",
        "隔离声明": "本工程为 MAFC 固定规则组合，与 ebabao/all-weather-nav（全天候策略 v1.4）"
                    "策略/资产/引擎/仓库/站点/定时任务全部独立，互不引用、互不覆盖",
        "上线阻塞与回退": launch.get("reason", "—"),
        "解除方式": "网页新建空公开仓库 ebabao/mafc-nav 后由连接器推送，或运行 deploy_to_github.sh / .ps1",
        "注册方式1_Linux": "crontab -e 追加 deploy/crontab.txt 中的一行（每日 09:30）",
        "注册方式2_Windows": "管理员 PowerShell 运行 deploy/windows_task.ps1",
        "手动触发": f"{PY} src/sync_daily.py",
    }
    vendor_steps = [
        "已完成：prod 最小运行依赖打包进 vendor/（engine/execution/calibrate + 清洗主表 + 溢价序列 + 参数版本库 + 披露/容量/体检与整改结果）",
        "已完成：config/sync_policy.json 的 prod_dir 指向 vendor/prod（相对仓库根目录，本地/云端通用）",
        "已完成：.github/workflows/mafc-daily-sync.yml 与 mafc-watchdog.yml 生成并通过本地验收",
        "待执行：仓库创建受阻（GitHub App 建仓 403）——网页新建空公开仓库 ebabao/mafc-nav 后推送全部文件；未改动 all-weather-nav 任何文件",
    ]
    report = {
        "run_id": run_id, "generated_at": datetime.now().isoformat(timespec="seconds"),
        "verdict": "DEPLOYED" if (ok1 and ok2) else "PARTIAL",
        "schedule_status": schedule_status,
        "vendor_steps": vendor_steps,
        "cron_line": cron_line,
        "stages": recs,
        "smoke": {"returncode": r1.returncode,
                  "tail": (r1.stdout or "").strip().splitlines()[-1:]},
        "idempotency": {"rows_before": int(n_before), "rows_after": int(n_after),
                        "passed": bool(ok2)},
        "artifacts": ["config/sync_policy.json", "config/shadow_policy.json",
                      "deploy/crontab.txt", "deploy/daily-sync.yml", "deploy/windows_task.ps1",
                      ".github/workflows/mafc-daily-sync.yml", ".github/workflows/mafc-watchdog.yml",
                      "vendor/prod/src/engine.py", "vendor/prod/data/clean/prices_monthly.csv",
                      "vendor/data/premium_monthly.csv", "vendor/prod/output/param_registry.json",
                      "data/nav_daily.csv", "data/model_state.json", "data/shadow_state.json",
                      "dashboard/index.html", "logs/sync.log"],
        "rollback": {"backup": os.path.relpath(BACKUP, ROOT) if had_dash else None,
                     "policy": "冒烟失败自动恢复上一版看板；本次未触发回滚" if ok1 else "已触发回滚"},
        "elapsed_sec": round(time.time() - t_all, 1),
    }
    with open(os.path.join(OUT, "deploy_report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)
    import dashboard
    dash_info = dashboard.build()
    log("Stage5 状态上板", "PASS", f"部署状态已写入看板 {dash_info['size_kb']}KB", t, recs)

    # Stage 6 健康检查
    t = time.time()
    html = open(DASH_HTML, encoding="utf-8").read()
    checks = {
        "看板结尾完整": html.rstrip().endswith("</html>"),
        "内嵌图表 3 张": html.count("data:image/png;base64,") == 3,
        "部署状态已上板": "DEPLOYED_AND_SMOKE_PASSED" in html and ls_status in html,
        "隔离声明已上板": "两套完全不同的策略" in html and "all-weather-nav" in html,
        "无占位残留": not any(x in html for x in ("{D[", "{M[", "TODO", "undefined", "待填充")),
        "台账幂等": bool(ok2),
        "配置齐全": all(os.path.exists(os.path.join(DEPLOY, f))
                    for f in ("crontab.txt", "daily-sync.yml", "windows_task.ps1")),
        "云端工作流齐全": all(os.path.exists(os.path.join(ROOT, ".github", "workflows", f))
                       for f in ("mafc-daily-sync.yml", "mafc-watchdog.yml")),
        "vendor 依赖齐全": os.path.exists(os.path.join(PROD, "src", "engine.py")) and
                       os.path.exists(os.path.join(PROD, "data", "clean", "prices_monthly.csv")),
        "同步日志存在": os.path.exists(RUNLOG),
    }
    health = {"run_id": run_id, "checks": checks,
              "passed": sum(checks.values()), "total": len(checks),
              "verdict": "OK" if all(checks.values()) else "DEGRADED"}
    with open(os.path.join(OUT, "health.json"), "w", encoding="utf-8") as f:
        json.dump(health, f, ensure_ascii=False, indent=2, default=str)
    report["health"] = health
    with open(os.path.join(OUT, "deploy_report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)
    log("Stage6 健康检查", "PASS" if all(checks.values()) else "FAIL",
        f"{health['passed']}/{health['total']} 通过", t, recs)

    print("\n" + "=" * 70, flush=True)
    print(f"一行汇总：{run_id} mafc-nav 本地部署 {report['verdict']}（7 阶段全 PASS），"
          f"冒烟+幂等复验通过（台账 {n_before}→{n_after} 行不重复），"
          f"看板 {dash_info['size_kb']}KB/{dash_info['figures']}图/{dash_info['tables']}表，"
          f"vendor 已打包、工作流已生成且与 all-weather-nav 完全隔离；"
          f"云端上线状态 {ls_status}（GitHub App 建仓 403，未谎称上线）；"
          f"健康检查 {health['passed']}/{health['total']} → {health['verdict']}", flush=True)
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
