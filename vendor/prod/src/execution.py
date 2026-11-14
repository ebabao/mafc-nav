"""
P0-1 / P0-2 整改执行层：配置驱动的「唯一实盘决策入口」
========================================================
体检第 2/7/9 关指出：回测按 5bp 单边、无 QDII 溢价过滤、无回撤熔断，实盘会漏水。
本模块把这三项整改做成可配置、可回测、可审计的执行层，且**零侵入引擎**：

  1) 成本口径：通过 contextmanager 临时切换模块级 E.COST（严格 finally 恢复），
     取值来自 config/cost_policy.json；research 口径恒 5bp 保持不变，历史数字不被改写。
  2) QDII 溢价硬过滤：compute_signals() 的 _regime 数组按「t 月末可得溢价」屏蔽后，
     经引擎原生 signals= 接口注入 —— t+1 月不建股票仓（由负相关利率债/现金承接）。
     溢价序列为 resample("ME").last() 的月末值，用 t 值决策 t+1，无前视。
  3) 回撤熔断：上线参数显式启用引擎原生开关 dd_thr=0.15 / brake=0.60
     （体检固化的 params 里 dd_thr=null，即未启用；acceptance 却写"实盘默认启用" —— 本模块补齐该 gap）。

零侵入的意义：regression.py 的 10 项 GO 断言逐位可复现，E.COST 每次调用后恢复 0.0005。
任何实盘决策必须经本模块，禁止直接调用 E.run()（那会绕过全部成本/溢价/熔断约束）。

运行：python3.11 prod/src/execution.py   打印各情景对比
产出：prod/output/live_vs_research.csv、prod/output/execution_scenarios.json
"""
from __future__ import annotations

import json
import os
import sys
from contextlib import contextmanager
from datetime import datetime

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine as E

HERE = os.path.dirname(os.path.abspath(__file__))
PROD = os.path.dirname(HERE)
QB = os.path.dirname(PROD)                      # /workspace/quant_backtest
OUT = os.path.join(PROD, "output")
CFG = os.path.join(PROD, "config")
PREM_PATH = os.path.join(QB, "data", "premium_monthly.csv")
EXAM_PATH = os.path.join(OUT, "exam_results.json")
os.makedirs(OUT, exist_ok=True)

PREMIUM_THRESHOLD = 0.02                        # P0-2：QDII 溢价 > 2% 不建仓
BRAKE_DD = 0.15                                 # P0-1：组合回撤 > 15% 自动降仓
BRAKE_SCALE = 0.60


def load_json(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ---------------- 上线参数（来源：体检固化的真实参数，不手抄） ----------------
def live_params(enable_brake: bool = True) -> E.SatParams:
    """从 exam_results.json 的 params 构造；整改项仅覆盖 dd_thr（启用熔断）。"""
    base = load_json(EXAM_PATH)["params"] if os.path.exists(EXAM_PATH) else None
    if base is None:                            # 兜底：与体检 params 逐字段一致
        base = dict(ma_fast=5, ma_slow=12, mom_lb=12, rs_lb=12, w_eq=1.0,
                    w_gold=0.4, w_cnbond=0.0, g_thr=0.0, b_thr=0.0,
                    use_g_ma=True, use_b_ma=False, bear_bond=1.0,
                    bond_as_cash=True, gold_only_in_bull=True, max_lev=1.2,
                    vol_target=None, dd_thr=None, brake=0.6, budget_split=False)
    p = E.SatParams(**base)
    if enable_brake:
        p.dd_thr, p.brake = BRAKE_DD, BRAKE_SCALE
    else:
        p.dd_thr = None
    return p


# ---------------- 真实溢价序列（对齐主表月份，缺失填 0 并记覆盖度） ----------------
def premium_series(path: str = PREM_PATH):
    """返回 (长度N的 prem_max 数组, 覆盖度字典)。prem_max = 两只 QDII ETF 中较高者（保守口径）。"""
    if not os.path.exists(path):
        raise FileNotFoundError(f"缺真实溢价数据：{path}（禁止编造，本模块拒绝运行）")
    prem = pd.read_csv(path, parse_dates=["month"])
    prem["ym"] = prem["month"].dt.strftime("%Y-%m")
    mp = dict(zip(prem["ym"], prem["prem_max"].astype(float)))
    yms = E.DATES.dt.strftime("%Y-%m").tolist()
    arr = np.array([float(mp.get(y, 0.0)) for y in yms])
    cov = {"source": os.path.relpath(path, PROD), "proxy_not_iopv": True,
           "from": prem["ym"].iloc[0], "to": prem["ym"].iloc[-1],
           "n_real_months": int(len(prem)), "n_aligned_in_backtest": int(sum(y in mp for y in yms)),
           "n_total_backtest_months": len(yms),
           "missing_filled_zero": True,
           "note": "2014-03 前无真实溢价数据，按 0 处理（过滤不生效）；估算代理非官方 IOPV"}
    return arr, cov


def guard_signals(p: E.SatParams, prem_arr: np.ndarray, thr: float = PREMIUM_THRESHOLD):
    """把高溢价月份的股票择时 regime 置 0，经 signals= 注入引擎。"""
    sig = E.compute_signals(p)
    reg = np.array(sig["_regime"], dtype=float, copy=True)
    blocked = prem_arr > thr
    reg[blocked] = 0.0
    sig2 = dict(sig)                            # 浅拷贝：其余因子数组不改动
    sig2["_regime"] = reg
    return sig2, blocked


@contextmanager
def cost_context(bps: float):
    """临时切换成本口径，严格保证恢复（验收断言会复核 E.COST == 0.0005）。"""
    saved = E.COST
    E.COST = float(bps) / 10000.0
    try:
        yield E.COST
    finally:
        E.COST = saved


# ---------------- 单情景回测 ----------------
def run_scenario(start: int, end: int, label: str, cost_bps: float,
                 premium_on: bool = False, brake_on: bool = False) -> dict:
    p = live_params(enable_brake=brake_on)
    sig2, blocked = None, None
    if premium_on:
        arr, _ = premium_series()
        sig2, blocked = guard_signals(p, arr)
    with cost_context(cost_bps):
        r = E.run(start, end, p, signals=sig2, collect=True)
    m = E.metrics(r["equity"], r["exposure"], r["turnover"])
    snap = r["snapshot"]
    out = {"label": label, "cost_bps_one_way": cost_bps, "premium_filter": bool(premium_on),
           "brake": bool(brake_on), "dd_thr": p.dd_thr,
           "period": f"{r['months'][0]}~{r['months'][-1]}", "n_months": m["n_months"],
           **{k: round(float(v), 6) for k, v in m.items()},
           "brake_triggered_months": int((snap["scale"] < 0.999).sum()),
           "cost_rate_used": float(cost_bps) / 10000.0}
    if premium_on and blocked is not None:
        sl = slice(start, end)
        out["premium_blocked_months"] = int(blocked[sl].sum())
        out["premium_blocked_pct"] = round(float(blocked[sl].mean()), 4)
        # 只统计「本来要建股票仓、被过滤拦下」的月份（真实有效拦截）
        orig_reg = E.compute_signals(p)["_regime"][start:end]
        orig_on = np.isfinite(orig_reg) & (orig_reg > 0.5)
        out["effective_blocks"] = int((blocked[sl] & orig_on).sum())
        out["stock_month_share_after_filter"] = round(
            float((snap["regime"] == 1).mean()), 4) if "regime" in snap else None
    return out


# ---------------- 四套情景 + 双均线基准对比 ----------------
def compare() -> dict:
    s, e = E.month_index("2007-11"), E.month_index("2026-11")
    s2 = E.month_index("2014-03")               # 溢价数据覆盖起点（子区间单独评估）
    scenarios = [
        ("S0 研究基线（5bp / 无过滤 / 无熔断）", 5, False, False),
        ("S1 实盘口径（15bp）", 15, False, False),
        ("S2 + QDII溢价>2%不建仓", 15, True, False),
        ("S3 上线版（+回撤熔断15%）", 15, True, True),
        ("S4 压力口径（30bp 全约束）", 30, True, True),
    ]
    rows = []
    for lab, bps, pf, bk in scenarios:
        rows.append(run_scenario(s, e, lab, bps, pf, bk))
        print(f"  {lab:36s} CAGR {rows[-1]['cagr']*100:6.2f}%  MDD {rows[-1]['mdd']*100:7.2f}%  "
              f"Sharpe {rows[-1]['sharpe']:.3f}  拦截 {rows[-1].get('effective_blocks','--')}", flush=True)

    # 子区间（溢价数据覆盖期）：避免过滤效果被 2014-03 前的空数据稀释
    sub = []
    for lab, bps, pf, bk in scenarios:
        sub.append(run_scenario(s2, e, lab, bps, pf, bk))

    # 双均线单因子：同口径对比，确认整改后相对基准仍成立
    bench = []
    for bps, tag in ((5, "research 5bp"), (15, "live 15bp")):
        with cost_context(bps):
            r = E.run_single_ma(s, e)
        m = E.metrics(r["equity"], r["exposure"], r["turnover"])
        bench.append({"label": f"双均线(5/12) {tag}", "cost_bps_one_way": bps,
                      **{k: round(float(v), 6) for k, v in m.items()}})

    premium_arr, cov = premium_series()
    i0 = E.month_index(cov["from"])
    real = premium_arr[i0:]                  # 只统计真实数据覆盖期，避免 2014-03 前的 0 稀释分布
    result = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "engine_cost_restored": E.COST == 0.0005,
        "premium_threshold": PREMIUM_THRESHOLD,
        "premium_coverage": cov,
        "brake": {"dd_thr": BRAKE_DD, "brake": BRAKE_SCALE,
                  "note": "条件触发的熔断（非体检瀑布中证伪的常态机械降仓）"},
        "main_period": f"2007-11~2026-11", "sub_period": "2014-03~2026-11",
        "scenarios": rows, "sub_scenarios": sub, "benchmarks": bench,
        "premium_series_stats": {
            "mean": round(float(real.mean()), 6),
            "max": round(float(real.max()), 6),
            "p95": round(float(np.percentile(real, 95)), 6),
            "months_above_2pct": int((real > PREMIUM_THRESHOLD).sum()),
            "stat_window": f"{cov['from']}~{cov['to']}（真实数据覆盖期）",
            "note": "months_above_2pct 为溢价超门槛的全部月份；其中仅原本处于股票多头的月份构成 effective_blocks",
        },
    }
    df = pd.DataFrame([{k: v for k, v in r.items() if not isinstance(v, (list, dict))}
                       for r in rows])
    df.to_csv(os.path.join(OUT, "live_vs_research.csv"), index=False)
    with open(os.path.join(OUT, "execution_scenarios.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)
    return result


if __name__ == "__main__":
    print("=== 整改执行层：研究口径 → 实盘口径 衰减链 ===", flush=True)
    r = compare()
    s3 = next(x for x in r["scenarios"] if x["label"].startswith("S3"))
    s0 = r["scenarios"][0]
    print(f"\n上线版 S3: CAGR {s3['cagr']*100:.2f}% / MDD {s3['mdd']*100:.2f}% / "
          f"Sharpe {s3['sharpe']:.3f}；相对 S0 衰减 {(s3['cagr']-s0['cagr'])*100:+.2f}pp")
    print(f"E.COST 已恢复 = {r['engine_cost_restored']}（应为 True）")
