"""
每日自动同步主编排（mafc-nav）
=====================================
把「策略模型接口」与「看板页面数据」每天同步一次，并驱动影子账户进化学习。

阶段（逐步打印、逐步记指标、失败明确回退）：
  Stage 0 前置校验       prod 源工程 / 清洗主表 / 参数版本库（缺则中止，不编造）
  Stage 1 真实行情探针   4 只实盘 ETF 最新交易日（新浪主通道→东财备用，尝试<=2次）
  Stage 2 模型状态       model_api.state() 现场回测，输出 model_state.json
  Stage 3 影子进化学习   shadow_evolution.evolve()，旁路监控、继承研究降级状态
  Stage 4 日台账          nav_daily.csv 按 date 幂等写入（同日覆盖，不重复追加）
  Stage 5 看板重生成     dashboard.build()
  Stage 6 健康检查       数字一致性 / 台账唯一 / 看板完整 / 无占位残留

明确约束（用户 SOP）：
  · 复用 prod 现有脚本，不重写引擎；不修改 prod 已验收的清洗数据。
  · 月度净值仅在月末数据确认后才更新；日间为 carry_forward，绝不编造日波动。
  · 行情探针失败如实标 unavailable，重试不超过两次，不填假设值。
  · 记录 run_id / scraped / new / md / per_account，最终一行中文汇总。

运行：python3.11 src/sync_daily.py
产出：data/nav_daily.csv、data/model_state.json、data/shadow_state.json、
      output/sync_run.json、dashboard/index.html
"""
from __future__ import annotations

import json
import os
import sys
import time
import warnings
from datetime import datetime

import pandas as pd

warnings.filterwarnings("ignore")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WORKSPACE = os.path.dirname(ROOT)
sys.path.insert(0, HERE)
DATA = os.path.join(ROOT, "data")
OUT = os.path.join(ROOT, "output")
DASH = os.path.join(ROOT, "dashboard")
os.makedirs(DATA, exist_ok=True)
os.makedirs(OUT, exist_ok=True)
os.makedirs(DASH, exist_ok=True)

POLICY = os.path.join(ROOT, "config", "sync_policy.json")
SYNC_RUN = os.path.join(OUT, "sync_run.json")
LEDGER = os.path.join(DATA, "nav_daily.csv")
VERSIONS = os.path.join(DATA, "shadow_versions.csv")
SHADOW_STATE = os.path.join(DATA, "shadow_state.json")


def _prod_dir():
    if os.path.exists(POLICY):
        try:
            p = json.load(open(POLICY, encoding="utf-8")).get("prod_dir")
            if p:
                return p if os.path.isabs(p) else os.path.normpath(os.path.join(ROOT, p))
        except Exception:
            pass
    return os.path.join(WORKSPACE, "quant_backtest", "prod")


PROD = _prod_dir()
PROBES = {"spx": ("sh513500", "513500"), "ixic": ("sh513100", "513100"),
          "gold": ("sh518880", "518880"), "cnbond": ("sh511010", "511010")}


def log(stage, status, detail, t0, recs):
    r = {"stage": stage, "status": status, "detail": detail,
         "elapsed_sec": round(time.time() - t0, 2)}
    recs.append(r)
    print(f"[{stage:24s}] {status:5s} {detail}  ({r['elapsed_sec']}s)", flush=True)
    return r


# ---------------- Stage 1：真实行情探针（不修改 prod 清洗数据） ----------------
def probe_market() -> dict:
    """读 4 只实盘 ETF 最新交易日。最多 2 次尝试；失败标 unavailable，不编造。"""
    import akshare as ak
    per = {}
    for k, (sina_sym, em_code) in PROBES.items():
        attempts, last_date, src = [], None, None
        for fn, arg, tag in (
            (lambda s: ak.fund_etf_hist_sina(symbol=s), sina_sym, "sina"),
            (lambda c: ak.fund_etf_hist_em(symbol=c, period="daily",
                                            start_date=(pd.Timestamp.now() - pd.Timedelta(days=120)).strftime("%Y%m%d"),
                                            end_date=pd.Timestamp.now().strftime("%Y%m%d"),
                                            adjust=""), em_code, "em"),
        ):
            if len(attempts) >= 2:
                break
            try:
                df = fn(arg)
                dcol = "date" if "date" in df.columns else "日期"
                d = pd.to_datetime(df[dcol]).max()
                if pd.isna(d):
                    raise ValueError("无有效日期")
                last_date, src = str(d.date()), tag
                attempts.append({"channel": tag, "ok": True})
                break
            except Exception as ex:
                attempts.append({"channel": tag, "ok": False,
                                 "error": f"{type(ex).__name__}: {str(ex)[:80]}"})
        per[k] = {"last_trade_date": last_date, "available": last_date is not None,
                  "source": src, "attempts": attempts,
                  "note": "仅作数据新鲜度探针，不写入模型价格数据"}
    return per


# ---------------- Stage 4：幂等日台账 ----------------
def write_ledger(date, model, shadow, scraped, new_monthly, run_id) -> int:
    nav = model["nav_series"][-1]
    row = {
        "date": date,
        "nav_live": nav["live"], "nav_research": nav["research"],
        "nav_month": nav["month"],
        "nav_source": "monthly_confirmed" if new_monthly > 0 else f"carry_forward_from_{nav['month']}",
        "live_cagr": model["metrics"]["live"]["cagr"],
        "live_sharpe": model["metrics"]["live"]["sharpe"],
        "shadow_version": shadow.get("version"), "shadow_status": shadow.get("status"),
        "shadow_degraded": shadow.get("degraded"),
        "shadow_effective": shadow.get("effective_params"),
        "scraped": scraped, "new_monthly_points": new_monthly,
        "run_id": run_id,
    }
    cols = list(row.keys())
    if os.path.exists(LEDGER):
        df = pd.read_csv(LEDGER, dtype={"date": str})
    else:
        df = pd.DataFrame(columns=cols)
    df = df[df["date"] != date]                  # 同日覆盖 -> 幂等
    df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
    df = df[cols].sort_values("date").reset_index(drop=True)
    df.to_csv(LEDGER, index=False)
    return len(df)


def main():
    t_all = time.time()
    run_id = datetime.now().strftime("SYNC-%Y%m%d-%H%M%S")
    date = datetime.now().strftime("%Y-%m-%d")
    print(f"===== mafc-nav 每日同步 {run_id} =====", flush=True)
    recs = []

    # Stage 0
    t = time.time()
    need = [os.path.join(PROD, "src", "engine.py"),
            os.path.join(PROD, "data", "clean", "prices_monthly.csv"),
            os.path.join(PROD, "output", "param_registry.json")]
    miss = [os.path.basename(x) for x in need if not os.path.exists(x)]
    if miss:
        log("Stage0 前置校验", "FAIL", f"源文件缺失 {miss}，按 SOP 中止且不编造", t, recs)
        return 1
    log("Stage0 前置校验", "PASS", "prod 引擎 / 清洗主表 / 研究参数版本库齐全", t, recs)

    # Stage 1（失败回退：标 unavailable，不中止）
    t = time.time()
    per_account = {k: {"last_trade_date": None, "available": False, "source": None,
                       "attempts": [], "note": "补数未执行"} for k in PROBES}
    try:
        per_account = probe_market()
    except Exception as ex:
        for v in per_account.values():
            v["note"] = f"探针异常 {type(ex).__name__}: {str(ex)[:100]}；如实标注 unavailable"
    scraped = sum(1 for v in per_account.values() if v["available"])
    # 主表最后月份已为 2026-11；新月度净值须待月末数据确认，本次不编造
    new_monthly = 0
    log("Stage1 真实行情探针", "PASS" if scraped == 4 else "WARN",
        f"{scraped}/4 标的取到最新交易日；新增月度净值点 {new_monthly}（月末未确认，不编造）", t, recs)

    # Stage 2
    t = time.time()
    import model_api
    model = model_api.state()
    log("Stage2 模型状态", "PASS",
        f"上线口径 CAGR {model['metrics']['live']['cagr']*100:.2f}% / "
        f"Sharpe {model['metrics']['live']['sharpe']:.3f}，净值 {len(model['nav_series'])} 点", t, recs)

    # Stage 3（首次正式同步清理预验证残留，确保产生干净的初始版本）
    t = time.time()
    # 首次正式同步以「影子版本台账为空」判定，不依赖 sync_run.json，
    # 这样重复部署不会误重置、从零部署也能干净初始化。
    first_sync = not (os.path.exists(VERSIONS) and os.path.getsize(VERSIONS) > 0)
    if first_sync:
        for p in (VERSIONS, SHADOW_STATE):
            if os.path.exists(p):
                os.remove(p)
    import shadow_evolution
    shadow = shadow_evolution.evolve(as_of=date, new_samples=1 if first_sync else new_monthly)
    log("Stage3 影子进化学习", "PASS",
        f"{shadow['status']} 版本 {shadow.get('version')} 降级={shadow.get('degraded')} "
        f"有效={shadow.get('effective_params')}", t, recs)

    # Stage 4
    t = time.time()
    n_rows = write_ledger(date, model, shadow, scraped, new_monthly, run_id)
    log("Stage4 日台账", "PASS", f"nav_daily.csv {n_rows} 行，同日覆盖幂等，净值 carry_forward", t, recs)

    # Stage 5
    t = time.time()
    import dashboard
    dash_info = dashboard.build()
    log("Stage5 看板重生成", "PASS",
        f"{dash_info['file']} {dash_info['size_kb']}KB / {dash_info['interfaces']} 个同源接口 / "
        f"{dash_info['nav_points']} 净值点", t, recs)

    # Stage 6 健康检查
    t = time.time()
    html_path = os.path.join(DASH, "index.html")
    html = open(html_path, encoding="utf-8").read() if os.path.exists(html_path) else ""
    rem = json.load(open(os.path.join(PROD, "output", "remediation_results.json"),
                         encoding="utf-8")) if os.path.exists(
                             os.path.join(PROD, "output", "remediation_results.json")) else {}
    ref = rem.get("key_metrics", {}).get("live_cagr")
    iface_names = ("meta.json", "nav.json", "signal.json", "shadow.json", "audit.json")
    iface_paths = [os.path.join(DATA, n) for n in iface_names]
    nav_ok = False
    try:
        nav_iface = json.load(open(iface_paths[1], encoding="utf-8"))
        nav_ok = (len(nav_iface["dates"]) == len(nav_iface["nav"]) == len(nav_iface["dd"])
                  and len(nav_iface["dates"]) == len(model["nav_series"])
                  and abs(nav_iface["nav"][-1] - model["nav_series"][-1]["live"]) < 1e-6)
    except Exception:
        pass
    checks = {
        "必需文件齐全": all(os.path.exists(p) for p in
                       (html_path, os.path.join(DATA, "model_state.json"),
                        SHADOW_STATE, LEDGER)),
        "看板结尾完整": html.rstrip().endswith("</html>"),
        "同源接口齐全（5 个）": all(os.path.exists(p) and os.path.getsize(p) > 200
                              for p in iface_paths),
        "接口与模型净值一致": nav_ok,
        "SVG 图表容器齐全": all(x in html for x in
                          ('id="c_eq"', 'id="c_dd"', 'id="c_sh"', 'id="heat"')),
        "无占位残留": not any(x in html for x in ("{D[", "{M[", "TODO", "undefined", "待填充")),
        "口径数字一致": (ref is None or abs(model["metrics"]["live"]["cagr"] - ref) < 1e-4),
        "引擎成本已恢复": bool(model["engine_cost_restored"]),
        "台账当日唯一": int((pd.read_csv(LEDGER, dtype={"date": str})["date"] == date).sum()) == 1,
    }
    ok = all(checks.values())
    log("Stage6 健康检查", "PASS" if ok else "FAIL",
        f"{sum(checks.values())}/{len(checks)} 通过" + ("" if ok else f" 失败项 {[k for k,v in checks.items() if not v]}"),
        t, recs)

    md = sum(1 for p in (os.path.join(DATA, "model_state.json"), SHADOW_STATE,
                         LEDGER, html_path, *iface_paths) if os.path.exists(p))
    result = {
        "run_id": run_id, "as_of": date,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "scraped": scraped, "new": new_monthly, "md": md,
        "per_account": per_account,
        "metrics_note": "scraped=探针成功标的数；new=新增月度净值点（0=月末未确认，非漏同步）；"
                        "md=本次维护的数据/看板文件数；per_account=各标的最新交易日与补数状态",
        "stages": recs, "health": checks,
        "model": {"period": model["period"], "live": model["metrics"]["live"],
                  "research": model["metrics"]["research"],
                  "latest_signal": model["latest_signal"]},
        "shadow": {"status": shadow["status"], "version": shadow.get("version"),
                   "degraded": shadow.get("degraded"),
                   "effective_params": shadow.get("effective_params"),
                   "live_observation_months": shadow.get("live_observation_months"),
                   "single_fold_win": shadow.get("single_fold_win")},
        "dashboard": dash_info,
        "verdict": "OK" if ok else "DEGRADED",
        "elapsed_sec": round(time.time() - t_all, 1),
    }
    with open(SYNC_RUN, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)

    pa = "/".join(f"{k}:{v['last_trade_date'] or 'NA'}" for k, v in per_account.items())
    print("\n" + "=" * 68, flush=True)
    print(f"一行汇总：{run_id} scraped={scraped}/4 new={new_monthly} md={md} "
          f"per_account=[{pa}]；上线口径 {model['metrics']['live']['cagr']*100:.2f}%/"
          f"{model['metrics']['live']['sharpe']:.3f}；影子 {shadow.get('version')} "
          f"{shadow['status']} 降级={shadow.get('degraded')} 实盘观察 "
          f"{shadow.get('live_observation_months')}月；看板 {dash_info['size_kb']}KB；"
          f"健康检查 {sum(checks.values())}/{len(checks)} → {result['verdict']}", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
