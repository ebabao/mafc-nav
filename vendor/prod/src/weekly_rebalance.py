"""
周频调仓敏感性回测（研究脚本 · 不侵入生产引擎 · 不推 git）
==============================================================
问题：把 MAFC 的调仓从月频改为周频、以及不拦截 QDII 溢价，绩效能否提升？

方法（诚实、可审计）：
  1) 规则内核与 engine.run 逐行一致：MA 择时 + RS 轮动 + TSMOM + 黄金腿 +
     利率债承接 + 现金增强 + 回撤熔断。只把决策/结算粒度 月 -> 周。
  2) 参数按"时间跨度等价"缩放，保证是同一个策略、只变调仓频率：
       月频 MA5/12、RS12M、TSMOM12M  ->  周频 MA22/52、RS52、TSMOM52
     (5 月≈22 周, 12 月≈52 周)。
  3) 利率按周折算：现金年息 2% -> (1.02)^(1/52)-1；融资 3.5% 同理。
     成本按换手计（周频换手会更频繁，成本上升如实计入）。
  4) 周频价格用真实日线 resample(周五收盘)，月末值与月度表核对一致(见数据脚本)。
  5) 溢价过滤：把月末 QDII 溢价 forward-fill 到该月各周，用 t 周可得溢价屏蔽 t+1 周
     股票仓，无前视。开关 premium_on 控制。
  6) 黄金日线仅 2016-12 起：覆盖期(2017-12~2026-11, 含52周预热)四资产全真实；
     更早区间黄金腿无真实周线，脚本只在覆盖期内做月/周公平对比，不外推、不编造。

对比矩阵：月频/周频 × 溢价开/关 × (含/不含熔断)，输出 CAGR/MDD/Sharpe/Calmar/换手。
运行：python3.11 src/weekly_rebalance.py
产出：output/weekly_rebalance_results.json
"""
from __future__ import annotations
import json, math, os, warnings
from datetime import datetime
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
HERE = os.path.dirname(os.path.abspath(__file__))
PROD = os.path.dirname(HERE)
QB = os.path.dirname(PROD)
CLEAN = os.path.join(PROD, "data", "clean")
OUT = os.path.join(PROD, "output")
os.makedirs(OUT, exist_ok=True)

# 基准口径：IXIC=纳斯达克综合指数(旧) / NDX=纳斯达克100(新, 更主流更强)
# 研究表 prices_*_ndx.csv 把 ixic 列替换为 NDX 月末/周五值(引擎列名 ixic 不变, 仅换数据)
BENCH = os.environ.get("MAFC_BENCH", "ndx").lower()
W_FILE = "prices_weekly_ndx.csv" if BENCH == "ndx" else "prices_weekly.csv"
M_FILE = "prices_monthly_ndx.csv" if BENCH == "ndx" else "prices_monthly.csv"
BENCH_CN = "纳斯达克100(NDX)" if BENCH == "ndx" else "纳斯达克综指(IXIC)"

W = pd.read_csv(os.path.join(CLEAN, W_FILE), parse_dates=["week"]).set_index("week").sort_index()
WEEKS = W.index
NW = len(W)
ASSETS = ["spx", "ixic", "gold", "cnbond"]
PX = {a: W[a].to_numpy(float) for a in ASSETS}
RET = {a: W[f"r_{a}"].to_numpy(float) for a in ASSETS}

# 月频主表（对照基线）
M = pd.read_csv(os.path.join(CLEAN, M_FILE), parse_dates=["month"]).set_index("month").sort_index()
MPX = {a: M[a].to_numpy(float) for a in ASSETS}
MRET = {a: M[f"r_{a}"].to_numpy(float) for a in ASSETS}
COMBO_M = M["risk_combo_nav"].to_numpy(float)

# 参数（来自体检固化 params，仅时间跨度做周频等价缩放）
PARAMS = dict(w_eq=1.0, w_gold=0.40, w_cnbond=0.0, g_thr=0.0, b_thr=0.0,
              use_g_ma=True, use_b_ma=False, bear_bond=1.0, bond_as_cash=True,
              gold_only_in_bull=True, max_lev=1.20, dd_thr=0.15, brake=0.60)
MONTH_PARAMS = dict(PARAMS, ma_fast=5, ma_slow=12, mom_lb=12, rs_lb=12)
WEEK_PARAMS = dict(PARAMS, ma_fast=22, ma_slow=52, mom_lb=52, rs_lb=52)
FAST_WEEK_PARAMS = dict(PARAMS, ma_fast=5, ma_slow=12, mom_lb=12, rs_lb=12)  # 快信号对照

COST_BPS = 15
COST = COST_BPS / 10000.0
CASH_W = (1.02) ** (1 / 52) - 1      # 现金/利率债周息近似（利率债用真实周收益）
FIN_W = (1.035) ** (1 / 52) - 1      # 融资周息
PREM_THR = 0.02
PREM_PATH = os.path.join(QB, "data", "premium_monthly.csv")


def _mom(px, lb):
    o = np.full(len(px), np.nan); o[lb:] = px[lb:] / px[:-lb] - 1.0; return o


def _ma_on(px, f, s):
    a = pd.Series(px).rolling(f).mean().to_numpy()
    b = pd.Series(px).rolling(s).mean().to_numpy()
    v = (a > b).astype(float); v[:s] = np.nan; return v


def compute_signals(px, ret, p):
    """择时基准 combo 用该频率的 spx/cnbond 收益合成等权净值，月/周频口径一致，只变频率。"""
    sig = {"_warmup": max(p["mom_lb"], p["ma_slow"], p["rs_lb"])}
    for a in ASSETS:
        sig[a] = {"mom": _mom(px[a], p["mom_lb"]),
                  "ma_on": _ma_on(px[a], p["ma_fast"], p["ma_slow"])}
    sig["spx"]["rs_mom"] = _mom(px["spx"], p["rs_lb"])
    sig["ixic"]["rs_mom"] = _mom(px["ixic"], p["rs_lb"])
    rcombo = 0.5 * (np.nan_to_num(ret["spx"]) + np.nan_to_num(ret["cnbond"]))
    combo_nav = np.cumprod(1.0 + rcombo)
    sig["_regime"] = _ma_on(combo_nav, p["ma_fast"], p["ma_slow"])
    return sig


def premium_to_periods(freq):
    """月末溢价 -> 按 freq(周/月) forward-fill 到每个决策期，t 期值仅用 <=t 信息。"""
    pm = pd.read_csv(PREM_PATH, parse_dates=["month"])
    pm["ym"] = pm["month"].dt.to_period("M")
    mp = dict(zip(pm["ym"], pm["prem_max"].astype(float)))
    if freq == "week":
        idx = pd.Index(WEEKS)
    else:
        idx = pd.Index(M.index)
    yms = idx.to_period("M")
    arr = np.array([mp.get(y, 0.0) for y in yms], float)
    return arr


def run(px, ret, freq, p, premium_on=False, brake_on=True, start=0, end=None, cash_rate=CASH_W, fin_rate=FIN_W):
    """通用回测：freq='week'/'month'。t 期末决策 -> t+1 期收益。"""
    if end is None:
        end = len(ret["spx"]) - 1
    sig = compute_signals(px, ret, p)
    reg0 = np.array(sig["_regime"], float, copy=True)
    if premium_on:
        parr = premium_to_periods(freq)
        reg0[parr > PREM_THR] = 0.0
    sig["_regime"] = reg0

    w_eq = p["w_eq"]
    eq = [1.0]; peak = 1.0; prev = {}; hist = []
    exps, turns = [0.0], [0.0]

    def leg_ok(a, t):
        mom = sig[a]["mom"][t]
        thr = p["g_thr"] if a == "gold" else p["b_thr"]
        use_ma = p["use_g_ma"] if a == "gold" else p["use_b_ma"]
        ok = np.isfinite(mom) and mom > thr
        if use_ma and ok:
            ok = np.isfinite(sig[a]["ma_on"][t]) and sig[a]["ma_on"][t] > 0.5
        return bool(ok)

    def rs_pick(t):
        ms, mi = sig["spx"]["rs_mom"][t], sig["ixic"]["rs_mom"][t]
        return "ixic" if (np.isfinite(mi) and np.isfinite(ms) and mi > ms) else "spx"

    for t in range(start, end):
        regime = sig["_regime"][t]
        stock_on = bool(np.isfinite(regime) and regime > 0.5)
        g_on = leg_ok("gold", t); b_on = leg_ok("cnbond", t)
        w = {}
        if stock_on:
            w[rs_pick(t)] = w_eq
        if g_on and p["w_gold"] > 0 and (stock_on or not p["gold_only_in_bull"]):
            w["gold"] = p["w_gold"]
        if stock_on and b_on and p["w_cnbond"] > 0:
            w["cnbond"] = w.get("cnbond", 0.0) + p["w_cnbond"]
        bear_active = bool((not stock_on) and b_on and p["bear_bond"] > 0)
        if bear_active:
            w["cnbond"] = w.get("cnbond", 0.0) + p["bear_bond"]
        scale = 1.0
        dd = eq[-1] / peak - 1.0
        if brake_on and p["dd_thr"] and dd <= -p["dd_thr"]:
            scale *= p["brake"]
        w = {k: v * scale for k, v in w.items()}
        exp = sum(w.values())
        cap = min(p["max_lev"], 1.35)
        if exp > cap:
            w = {k: v * cap / exp for k, v in w.items()}; exp = cap
        all_a = set(w) | set(prev)
        turnover = sum(abs(w.get(a, 0.0) - prev.get(a, 0.0)) for a in all_a)
        cost = turnover * COST
        r_asset = sum(w.get(a, 0.0) * ret[a][t + 1] for a in ASSETS)
        idle = 1.0 - exp
        use_bond_cash = bool(p["bond_as_cash"] and idle > 0 and b_on)
        if idle > 0:
            cash_part = idle * (ret["cnbond"][t + 1] if use_bond_cash else cash_rate)
        else:
            cash_part = idle * fin_rate
        eq.append(eq[-1] * (1.0 + r_asset + cash_part - cost))
        peak = max(peak, eq[-1]); hist.append(r_asset + cash_part - cost)
        exps.append(exp); turns.append(turnover); prev = w

    n = len(eq) - 1
    years = (end - start + 1) / (52 if freq == "week" else 12)
    cagr = eq[-1] ** (1 / years) - 1 if years > 0 else float("nan")
    eqs = pd.Series(eq)
    mdd = float((eqs / eqs.cummax() - 1).min())
    r = pd.Series(hist)
    sharpe = float(r.mean() / r.std(ddof=1) * math.sqrt(52 if freq == "week" else 12)) if r.std() > 0 else 0.0
    calmar = cagr / abs(mdd) if mdd < 0 else float("nan")
    return dict(cagr=round(cagr, 6), mdd=round(mdd, 6), sharpe=round(sharpe, 4),
                calmar=round(calmar, 4), total_return=round(eq[-1] - 1, 6),
                avg_turnover=round(float(np.mean(turns)), 4),
                n_periods=n, years=round(years, 2), equity=[round(x, 6) for x in eq])


def main():
    # 公平对比区间：黄金真实周线覆盖(2016-12起) + 52周预热 -> 2017-12 起
    # 周频起点：2017-12 的第一个周五（周线已有52周黄金历史可供RS52/MA52预热）
    w_idx = WEEKS.get_indexer([pd.Timestamp("2017-12-01")], method="bfill")
    w_start_idx = int(w_idx[0]) if w_idx[0] >= 0 else 52
    w_end = NW - 1
    # 月频起点对齐到同一日历月（12月），保证两份对比同期
    m_idx = M.index.get_indexer([pd.Timestamp("2017-12-01")], method="bfill")
    m_start_idx = int(m_idx[0]) if m_idx[0] >= 0 else 12
    m_end = len(M) - 1
    print(f"周频起点 {WEEKS[w_start_idx].date()} (idx={w_start_idx}), 月频起点 {M.index[m_start_idx].date()} (idx={m_start_idx})")

    rows = []
    def add(name, **kw):
        r = dict(name=name, **kw); rows.append(r)
        print(f"  {name:34s} CAGR {r['cagr']*100:6.2f}%  MDD {r['mdd']*100:7.2f}%  "
              f"Sharpe {r['sharpe']:.3f}  Calmar {r['calmar']:.3f}  换手/期 {r['avg_turnover']:.3f}", flush=True)

    print(f"=== 公平对比区间 2017-12 ~ 2026-11（四资产全真实周线，含52周预热）===")
    segs = [
        ("月频-等价参数(溢价开+熔断)", "month", MONTH_PARAMS, True, True, m_start_idx, m_end),
        ("月频-等价参数(溢价关+熔断)", "month", MONTH_PARAMS, False, True, m_start_idx, m_end),
        ("周频-等价参数(溢价开+熔断)", "week", WEEK_PARAMS, True, True, w_start_idx, w_end),
        ("周频-等价参数(溢价关+熔断)", "week", WEEK_PARAMS, False, True, w_start_idx, w_end),
        ("周频-等价参数(溢价关+无熔断)", "week", WEEK_PARAMS, False, False, w_start_idx, w_end),
        ("周频-快信号(溢价关+熔断)", "week", FAST_WEEK_PARAMS, False, True, w_start_idx, w_end),
    ]
    res = {}
    for name, freq, p, prem, brake, s, e in segs:
        r = run(PX if freq == "week" else MPX, RET if freq == "week" else MRET,
                freq, p, premium_on=prem, brake_on=brake, start=s, end=e)
        add(name, freq=freq, premium=prem, brake=brake, **r)
        res[name] = r

    # 时间轴（周/月）用于画图
    weekly_dates = [d.strftime("%Y-%m-%d") for d in WEEKS[w_start_idx:w_end + 2]]
    monthly_dates = [d.strftime("%Y-%m") for d in M.index[m_start_idx:m_end + 2]]

    out = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "benchmark": BENCH, "benchmark_cn": BENCH_CN,
        "question": "QDII溢价不拦截 / 调仓改周频，绩效能否提升（纳指基准=" + BENCH_CN + "）",
        "cost_bps": COST_BPS, "premium_threshold": PREM_THR,
        "compare_window": "2017-12~2026-11（黄金真实周线覆盖期 + 预热）",
        "param_scaling": {"月频": "MA5/12, RS/TSMOM12月", "周频等价": "MA22/52, RS/TSMOM52周（时间跨度等价，策略语义不变）",
                          "周频快信号": "MA5/12, RS/TSMOM12周（信号更快，属不同策略，仅参考）"},
        "data_note": "标普/纳指/国债日线2003-2004起、黄金COMEX期货2016-12起；月末值与月度表核对相关≥0.999。黄金代理口径(期货vs现货)如实标注。",
        "rows": rows,
        "equity_curves": {k: v["equity"] for k, v in res.items()},
        "weekly_dates": weekly_dates, "monthly_dates": monthly_dates,
    }
    fp = os.path.join(OUT, f"weekly_rebalance_results_{BENCH}.json")
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    print(f"\n落盘 {fp}")


if __name__ == "__main__":
    main()
