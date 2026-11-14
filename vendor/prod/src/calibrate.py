"""
ML 受限校准 + 严格 Walk-Forward + 参数版本库 + 自动降级
======================================================
严格遵循 engineering_feasibility.md 的实证结论：
  · 「每折全参数重训」样本外过拟合（ACM 报告 WF-A 收益持平、回撤恶化 6pct，WF-B 明显跑输）
  · 因此 ML 降级为「受限在线校准」：只进化 4 个低敏感参数 + 参数漂移平滑
  · 固定规则组合写死为核心引擎；进化版连续跑输固定版 -> 自动降级回固定参数
  · 训练流水线同时输出固定版与进化版 WF 对比，把「进化是否带来增量」做成硬门槛

进化维度（4 维，均为低敏感、有经济含义的参数；本版围绕中国利率债重标定）：
  g0 w_gold     黄金卫星权重        [0.00, 0.55]
  g1 bear_bond  空头利率债承接权重  [0.40, 1.20]  （核心机制，负相关对冲，让数据说话）
  g2 b_thr      利率债 12M 动量阈值 [0.00, 0.06]  （利率债动量中位数仅 3.6%，量级重标定）
  g3 w_cnbond   多头利率债小卫星    [0.00, 0.25]  （数据诊断显示叠加会稀释股票腿，大概率收敛~0）
固定不动（工程文档：约束自由度 + 风控红线不交给 ML）：
  ma_fast=5, ma_slow=12, mom_lb=12, rs_lb=12, w_eq=1.0,
  max_lev=1.20（杠杆硬上限）, bond_as_cash=True, gold_only_in_bull=True

适应度：以风险调整收益为主 + 稳健性（多子窗口）+ 尾部回撤软约束，
        刻意避免「回撤惩罚过重导致空仓钻空子」与「只追 CAGR 导致追杠杆」两个已踩过的坑。

运行：python3.11 prod/src/calibrate.py
产出：prod/output/cal_*.csv / prod/output/param_registry.json / 控制台指标
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import asdict

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine as E

HERE = os.path.dirname(os.path.abspath(__file__))
PROD = os.path.dirname(HERE)
OUT = os.path.join(PROD, "output")
os.makedirs(OUT, exist_ok=True)

DIM = 4
RNG_SEED = 20261113
BOUNDS = [("w_gold", 0.00, 0.55), ("bear_bond", 0.40, 1.20),
          ("b_thr", 0.00, 0.06), ("w_cnbond", 0.00, 0.25)]

# 固定版参数（人工设定，作为兜底基线与 ML 增量对照）= 利率债版定型结构
FIXED = E.SatParams(w_eq=1.0, w_gold=0.40, w_cnbond=0.0, b_thr=0.0,
                    bear_bond=1.0, bond_as_cash=True, gold_only_in_bull=True,
                    max_lev=1.20, budget_split=False)
SMOOTH = 0.65          # 参数漂移平滑：新参数 = 0.65*旧 + 0.35*GA最优


def decode(x: np.ndarray, base=None) -> E.SatParams:
    x = np.clip(np.asarray(x, float), 0, 1)
    b = base if base is not None else FIXED
    def g(i):
        lo, hi = BOUNDS[i][1], BOUNDS[i][2]
        return float(lo + x[i] * (hi - lo))
    return E.SatParams(
        ma_fast=5, ma_slow=12, mom_lb=12, rs_lb=12,
        w_eq=1.0,
        w_gold=g(0), bear_bond=g(1), b_thr=g(2), w_cnbond=g(3),
        max_lev=1.20,                       # 风控红线固定，不交给 ML
        use_g_ma=True, use_b_ma=False,      # 利率债 98.7% 时间 MA 多头，不叠加
        bond_as_cash=True, gold_only_in_bull=True, budget_split=False,
        vol_target=None, dd_thr=None,
    )


def subwindows(a: int, b: int):
    """训练期内 3 个重叠子窗口（全段 + 前 2/3 + 后 2/3），抑制对单段行情的过拟合。"""
    L = b - a + 1
    third = L // 3
    ws = [(a, b)]
    for w in [(a, a + 2 * third - 1), (a + third, b)]:
        if w[1] - w[0] + 1 >= 24 and w[1] < E.N:
            ws.append(w)
    return ws


def fitness_of(p: E.SatParams, windows) -> float:
    try:
        sig = E.compute_signals(p)
        cagrs, mdds, sharpes = [], [], []
        for (a, b) in windows:
            if b - a < 23:
                continue
            m = E.metrics(E.run(a, b, p, signals=sig, collect=False)["equity"])
            if not m:
                return -1e6
            cagrs.append(m["cagr"]); mdds.append(abs(m["mdd"])); sharpes.append(m["sharpe"])
        if not cagrs:
            return -1e6
        c, s, d, sh = (float(np.mean(cagrs)), float(np.std(cagrs)),
                       float(np.mean(mdds)), float(np.mean(sharpes)))
        # 收益为主 + 稳健性 + 夏普奖励 + 仅对 >20% 的尾部回撤做软惩罚
        val = 100 * (c - 0.6 * s) + 8 * sh - 20 * max(0.0, d - 0.20)
        return float(val) if np.isfinite(val) else -1e6
    except Exception:
        return -1e6


def ga(windows, pop_size=26, generations=16, seed=42, x0=None):
    rng = np.random.default_rng(seed)
    pop = rng.random((pop_size, DIM))
    if x0 is not None:                     # 温启动：上一折参数作为种群个体之一
        pop[0] = np.clip(x0, 0, 1)
    best_x, best_f = pop[0].copy(), -1e18
    hist = []
    for gen in range(generations):
        fs = np.array([fitness_of(decode(ind), windows) for ind in pop])
        order = np.argsort(fs)[::-1]
        if fs[order[0]] > best_f:
            best_f, best_x = float(fs[order[0]]), pop[order[0]].copy()
        hist.append({"gen": gen, "best": round(best_f, 4), "mean": round(float(np.mean(fs)), 4)})
        elite = pop[order[:3]].copy()
        new = [e.copy() for e in elite]
        sigma = 0.16 * (0.3 ** (gen / max(1, generations - 1))) + 0.02
        while len(new) < pop_size:
            c = rng.integers(0, pop_size, size=3); pa = pop[c[np.argmax(fs[c])]].copy()
            c = rng.integers(0, pop_size, size=3); pb = pop[c[np.argmax(fs[c])]].copy()
            alpha = rng.random()
            child = alpha * pa + (1 - alpha) * pb
            mut = rng.random(DIM) < 0.28
            child = np.where(mut, child + rng.normal(0, sigma, DIM), child)
            new.append(np.clip(child, 0, 1))
        pop = np.array(new)
    return best_x, best_f, hist


def build_folds(start, end, train_len, test_len):
    folds, a = [], start
    while a + train_len + 6 <= end:
        tr = (a, a + train_len - 1)
        te = (a + train_len, min(a + train_len + test_len - 1, end))
        if te[1] - te[0] < 5 or te[1] >= E.N:
            break
        folds.append({"train": tr, "test": te,
                      "train_label": f"{E.DATES.iloc[tr[0]].strftime('%Y-%m')}~{E.DATES.iloc[tr[1]].strftime('%Y-%m')}",
                      "test_label": f"{E.DATES.iloc[te[0]].strftime('%Y-%m')}~{E.DATES.iloc[te[1]].strftime('%Y-%m')}"})
        a += test_len
    return folds


def _concat(rets_list, init=1e6):
    r = np.concatenate(rets_list)
    return init * np.r_[1.0, np.cumprod(1 + r)]


def walk_forward(folds, tag, smooth=SMOOTH, degrade_patience=2):
    """严格 WF：每折只用训练期数据搜索参数；参数平滑；进化版连续跑输 -> 自动降级固定版。"""
    rows, params, rets_evo, rets_fixed, rets_ma = [], [], [], [], []
    prev_x, conv = None, None
    registry = []                  # 参数版本库
    consecutive_loss, degraded = 0, False
    for k, fd in enumerate(folds):
        t0 = time.time()
        wins = subwindows(*fd["train"])
        x_opt, sc, hist = ga(wins, seed=1000 + k, x0=prev_x)
        if conv is None:
            conv = hist
        # 参数平滑 + 降级判定
        x_used = x_opt if prev_x is None else smooth * prev_x + (1 - smooth) * x_opt
        prev_x = x_used
        p = decode(x_used)
        a, b = fd["test"]
        # 决策起点 a-1 使收益恰好覆盖 DATES[a..b]，相邻折严格连续
        r_e = E.run(a - 1, b, p)
        r_f = E.run(a - 1, b, FIXED)
        r_m = E.run_single_ma(a - 1, b)
        me = E.metrics(r_e["equity"], r_e["exposure"], r_e["turnover"])
        mf = E.metrics(r_f["equity"], r_f["exposure"], r_f["turnover"])
        mm = E.metrics(r_m["equity"])
        # 自动降级：以「风险调整收益(Sharpe)」为准，进化版连续 2 折不如固定版 -> 冻结为固定参数
        if me["sharpe"] < mf["sharpe"]:
            consecutive_loss += 1
        else:
            consecutive_loss = 0
        if consecutive_loss >= degrade_patience and not degraded:
            degraded = True
        p_effective = FIXED if degraded else p
        if degraded:                       # 降级后本折改用固定参数重算
            r_e = r_f; me = mf
        rets_evo.append(np.diff(r_e["equity"]) / r_e["equity"][:-1])
        rets_fixed.append(np.diff(r_f["equity"]) / r_f["equity"][:-1])
        rets_ma.append(np.diff(r_m["equity"]) / r_m["equity"][:-1])
        rows.append({"fold": k + 1, "train": fd["train_label"], "test": fd["test_label"],
                     "train_fitness": round(sc, 3),
                     "oos_cagr_evo": me["cagr"], "oos_cagr_fixed": mf["cagr"], "oos_cagr_ma": mm["cagr"],
                     "oos_mdd_evo": me["mdd"], "oos_mdd_fixed": mf["mdd"], "oos_mdd_ma": mm["mdd"],
                     "oos_sharpe_evo": me["sharpe"], "oos_sharpe_fixed": mf["sharpe"],
                     "oos_sharpe_ma": mm["sharpe"],
                     "degraded": int(degraded), "elapsed_sec": round(time.time() - t0, 1)})
        prm = {"fold": k + 1, "fitness": round(sc, 3), "degraded": int(degraded)}
        prm.update({kk: (round(float(vv), 4) if isinstance(vv, (int, float)) and not isinstance(vv, bool) else vv)
                    for kk, vv in p_effective.to_dict().items()})
        params.append(prm)
        registry.append({"version": f"v{k+1:02d}", "train": fd["train_label"],
                         "test": fd["test_label"], "seed": 1000 + k, "fitness": round(sc, 3),
                         "gene_smoothed": [round(float(z), 4) for z in x_used],
                         "params": p_effective.to_dict(), "degraded": bool(degraded)})
        print(f"  [{tag}] fold{k+1:2d} {fd['train_label']}->{fd['test_label']} F={sc:.2f} "
              f"OOS evo {me['cagr']*100:.2f}%/{me['sharpe']:.3f} vs fixed {mf['cagr']*100:.2f}%/{mf['sharpe']:.3f} "
              f"vs MA {mm['cagr']*100:.2f}%" + ("  [已降级固定版]" if degraded else ""), flush=True)
    eq_e, eq_f, eq_m = _concat(rets_evo), _concat(rets_fixed), _concat(rets_ma)
    return {"rows": rows, "params": params, "registry": registry, "conv": conv,
            "equity_evo": eq_e, "equity_fixed": eq_f, "equity_ma": eq_m,
            "oos_start": folds[0]["test"][0], "degraded": degraded,
            "metrics_evo": E.metrics(eq_e), "metrics_fixed": E.metrics(eq_f),
            "metrics_ma": E.metrics(eq_m)}


def sensitivity(start, end, base: E.SatParams) -> pd.DataFrame:
    """参数扰动稳健性：单参数 ±25% 扰动，观察结论是否反转。"""
    rows = []
    d = base.to_dict()
    for key, lo, hi in [("w_gold", 0.0, 0.6), ("bear_bond", 0.3, 1.2),
                        ("b_thr", 0.0, 0.08), ("w_cnbond", 0.0, 0.3)]:
        for mult, lab in [(-0.25, "−25%"), (0.0, "基准"), (0.25, "+25%")]:
            v = d[key]
            nv = float(np.clip(v * (1 + mult) if v != 0 else (0.01 if mult > 0 else 0.0), lo, hi))
            kw = {**d, key: nv}
            kw.pop("budget_split", None)
            p = E.SatParams(**kw)
            m = E.metrics(E.run(start, end, p, collect=False)["equity"])
            rows.append({"param": key, "perturb": lab, "value": round(nv, 4),
                         "cagr": m["cagr"], "mdd": m["mdd"], "sharpe": m["sharpe"], "calmar": m["calmar"]})
    return pd.DataFrame(rows)


def main():
    t0 = time.time()
    s0 = E.month_index("2007-11")     # warmup 后首个可交易月
    e = E.month_index("2026-11")
    s13 = E.month_index("2013-01")

    print("[1/4] 全样本 GA 校准（仅用于对照，含全样本信息，不可作为实盘预期）", flush=True)
    wins_all = subwindows(s0, e - 1)
    gx, gf, gh = ga(wins_all, pop_size=30, generations=20, seed=RNG_SEED)
    p_ga = decode(gx)
    pd.DataFrame(gh).to_csv(os.path.join(OUT, "cal_ga_convergence.csv"), index=False)
    for lab, a in [("全区间 2007-2026", s0), ("2013-2026", s13)]:
        for name, p in [("固定版", FIXED), ("GA全样本", p_ga)]:
            m = E.metrics(E.run(a, e, p)["equity"], E.run(a, e, p)["exposure"])
            print(f"  {lab} {name}: CAGR={m['cagr']*100:.2f}% MDD={m['mdd']*100:.2f}% Sh={m['sharpe']:.3f}", flush=True)
    print(f"  GA 最优参数: { {k:round(v,4) if isinstance(v,float) else v for k,v in p_ga.to_dict().items() if k in ('w_gold','bear_bond','b_thr','w_cnbond')} }", flush=True)

    print("[2/4] Walk-Forward 主协议：训练96月/测试24月", flush=True)
    foldsA = build_folds(s0, e - 1, 96, 24)
    print(f"  {len(foldsA)} 折: {[(f['test_label']) for f in foldsA]}", flush=True)
    wfA = walk_forward(foldsA, "WF-A")

    print("[3/4] Walk-Forward 稳健协议：训练60月/测试12月（更频繁重训）", flush=True)
    foldsB = build_folds(s0, e - 1, 60, 12)
    wfB = walk_forward(foldsB, "WF-B")

    print("[4/4] 参数扰动稳健性 + 结果落盘", flush=True)
    sens = sensitivity(s13, e, FIXED)
    sens.to_csv(os.path.join(OUT, "cal_sensitivity.csv"), index=False)

    def save_wf(wf, tag):
        pd.DataFrame(wf["rows"]).to_csv(os.path.join(OUT, f"cal_{tag}_folds.csv"), index=False)
        pd.DataFrame(wf["params"]).to_csv(os.path.join(OUT, f"cal_{tag}_params.csv"), index=False)
        n = len(wf["equity_evo"])
        months = [E.DATES.iloc[wf["oos_start"] - 1 + i].strftime("%Y-%m") for i in range(n)
                  if wf["oos_start"] - 1 + i < E.N]
        pd.DataFrame({"month": months[:n],
                      "ML受限校准": np.round(wf["equity_evo"], 2),
                      "固定版兜底": np.round(wf["equity_fixed"], 2),
                      "双均线单因子": np.round(wf["equity_ma"], 2)}).to_csv(
            os.path.join(OUT, f"cal_{tag}_equity.csv"), index=False)
    save_wf(wfA, "wfA"); save_wf(wfB, "wfB")
    pd.DataFrame(wfA["conv"]).to_csv(os.path.join(OUT, "cal_wfA_conv.csv"), index=False)
    with open(os.path.join(OUT, "param_registry.json"), "w", encoding="utf-8") as f:
        json.dump({"fixed_baseline": FIXED.to_dict(), "smooth": SMOOTH,
                   "bounds": [{"name": b[0], "lo": b[1], "hi": b[2]} for b in BOUNDS],
                   "versions": wfA["registry"], "degraded": wfA["degraded"]},
                  f, ensure_ascii=False, indent=2, default=str)

    A, B = wfA["metrics_evo"], wfB["metrics_evo"]
    Af, Bf = wfA["metrics_fixed"], wfB["metrics_fixed"]
    Am, Bm = wfA["metrics_ma"], wfB["metrics_ma"]
    summary = {
        "ga_fullsample": {"fitness": gf, "params": p_ga.to_dict()},
        "walkforward_A": {"folds": len(foldsA), "evo": A, "fixed": Af, "ma": Am,
                          "degraded": wfA["degraded"], "rows": wfA["rows"]},
        "walkforward_B": {"folds": len(foldsB), "evo": B, "fixed": Bf, "ma": Bm,
                          "degraded": wfB["degraded"], "rows": wfB["rows"]},
        "sensitivity": sens.to_dict(orient="records"),
        "elapsed_sec": round(time.time() - t0, 1),
    }
    with open(os.path.join(OUT, "cal_summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, default=str)

    print("\n===== 样本外汇总（严格 WF 拼接）=====", flush=True)
    for tag, wf in [("WF-A", wfA), ("WF-B", wfB)]:
        me, mf, mm = wf["metrics_evo"], wf["metrics_fixed"], wf["metrics_ma"]
        print(f"{tag} ({len(wf['rows'])}折, 降级={wf['degraded']}):", flush=True)
        print(f"   ML受限校准 CAGR={me['cagr']*100:.2f}% MDD={me['mdd']*100:.2f}% Sh={me['sharpe']:.3f} Cal={me['calmar']:.3f}", flush=True)
        print(f"   固定版兜底   CAGR={mf['cagr']*100:.2f}% MDD={mf['mdd']*100:.2f}% Sh={mf['sharpe']:.3f} Cal={mf['calmar']:.3f}", flush=True)
        print(f"   双均线单因子 CAGR={mm['cagr']*100:.2f}% MDD={mm['mdd']*100:.2f}% Sh={mm['sharpe']:.3f} Cal={mm['calmar']:.3f}", flush=True)
    print(f"完成，耗时 {time.time()-t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
