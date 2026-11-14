"""
MAFC (Multi-Asset Fixed Combo) 多资产固定规则组合引擎 · 生产定型版
=================================================================
按 engineering_feasibility.md 生产架构落地：规则写死、可审计、信号按决策时点版本化。

【资产池】美股双指数(SPX/IXIC) + 黄金(XAU) + 中国利率债(上证国债指数 sh000012 财富指数)。
  中国利率债实测：与标普月收益相关 -0.224（负相关）、月波动仅 0.35%、共同区间回撤 -1.96%，
  标普下跌月 88.5% 上涨（黄金仅 50.6%）—— 是比 WTI 原油质量高得多的低相关资产。

【为什么是这个结构】——结构诊断（真实数据，三个区间一致）结论：
  · 「低相关 != 直接加权就提收益」：利率债年化仅 3.76%，把它当多头卫星腿叠加会被
    1.2 杠杆上限等比例压缩、稀释最赚钱的股票腿，加得越多 CAGR 越低（但回撤/夏普改善）。
  · 利率债的正确用法 = 风险预算替代，而非多一条收益腿：
      ① 股票空头时段用「负相关的利率债」承接（替代 2% 现金）——熊市债涨股跌，天然对冲；
      ② 闲置资金配利率债（现金增强）——把 2% 现金息换成 3.76% 且更低波动；
    两者都不占用股票腿预算，三区间 CAGR / 夏普同步改善，不依赖调参拟合。
  · 稳健做法 = 核心-卫星：
      股票核心腿 = 已验证的「RS 相对动量轮动(SPX/IXIC) + 双均线(5/12) 组合择时」
      黄金卫星腿 = 独立 TSMOM(12M) + 自身双均线择时（低相关第二收益流）
      利率债腿   = 空头承接(bear_bond) + 闲置现金增强(bond_as_cash) + 可选多头小卫星(w_cnbond)
    各腿独立择时、各自有趋势保护。

【两套工程方案】
  A 无杠杆预算分流（股 0.7 / 金 0.3，总仓位 <= 1）：风险调整最优，符合工程文档「首发无杠杆」
  B 温和杠杆卫星（股 1.0 / 金 0.4，硬上限 1.2）：绝对收益更高，需披露杠杆与融资成本
  两方案均叠加利率债空头承接与现金增强（结构开关，见 bear_bond / bond_as_cash）。

【成本口径】与研究报告 / composite.py 完全一致：
  COST=0.0005 单边换手；现金年化 2%；杠杆部分年化 3.5%；月度复利。

【信号快照】run() 逐月输出 signal_snapshot（每腿 mom/ma_on/on、regime、目标权重、
  波动率缩放、回撤状态），即工程文档要求的「按决策时点版本化的因子快照」。

运行：python3.11 prod/src/engine.py  打印自检（含两套方案与基准对比）。
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, asdict

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
PROD = os.path.dirname(HERE)
CLEAN = os.path.join(PROD, "data", "clean")

# ---------------- 数据层（清洗后主表，复用研究报告口径） ----------------
M = pd.read_csv(os.path.join(CLEAN, "prices_monthly.csv"), parse_dates=["month"])
DATES = M["month"]
N = len(M)
ASSETS = ["spx", "ixic", "gold", "cnbond"]
ASSET_CN = {"spx": "标普500", "ixic": "纳斯达克", "gold": "黄金", "cnbond": "中国利率债", "cash": "现金"}

PX = {a: M[a].to_numpy(float) for a in ASSETS}
RET = {a: M[f"r_{a}"].to_numpy(float) for a in ASSETS}   # ret[t] = t-1 -> t
COMBO = M["risk_combo_nav"].to_numpy(float)               # 风险域 50/50 净值（旧口径基准）

COST = 0.0005
# 整改（P0-2）成本口径配置化：research 是研究/历史口径，恒定 0.0005 不变，保证既有
# 回测数字与 regression 的 T2/T8 断言逐位可复现；live=15bp（佣金+滑点+印花/申赎摊销）、
# stress=30bp 仅供实盘与压力情景，由 execution.py 显式切换。禁止直接改本模块 COST 常量
# 拿去做实盘决策 —— 那会静默改写全部历史结果。
COST_PROFILES = {"research": 0.0005, "live": 0.0015, "stress": 0.0030}
CASH_M = (1.02) ** (1 / 12) - 1
FIN_M = (1.035) ** (1 / 12) - 1
HARD_LEV_CAP = 1.35      # 引擎硬上限（工程文档实盘建议 <=1.2，由参数控制）


def month_index(ym: str) -> int:
    mask = DATES.dt.strftime("%Y-%m") <= ym
    return int(np.flatnonzero(mask.to_numpy())[-1]) if mask.any() else 0


# ---------------- 因子层：向量化预计算（只用 t 及之前信息） ----------------
def _mom(px: np.ndarray, lb: int) -> np.ndarray:
    o = np.full(len(px), np.nan)
    o[lb:] = px[lb:] / px[:-lb] - 1.0
    return o


def _ma_on(px: np.ndarray, f: int, s: int) -> np.ndarray:
    a = pd.Series(px).rolling(f).mean().to_numpy()
    b = pd.Series(px).rolling(s).mean().to_numpy()
    v = (a > b).astype(float)
    v[:s] = np.nan
    return v


@dataclass
class SatParams:
    """核心-卫星固定规则参数。默认 = 方案B 人工设定版（ML 校准前的对照基线）。

    只有 w_gold / w_cnbond / b_thr / bear_bond 四个低敏感参数交给 ML 校准，
    ma_fast/ma_slow/mom_lb/rs_lb 用户指定、max_lev 风控红线、固定不进化
    （工程文档：约束自由度，防止样本外过拟合）。

    利率债相关结构（本版替换商品后新增，均由真实数据诊断得出、可解释）：
      · b_thr   利率债 12M 动量阈值，量级重标定到 0~6%（利率债动量中位数仅 3.6%，
                沿用商品时代 0.20 会让这条腿永不触发）；
      · bear_bond 股票空头时段用负相关利率债承接的权重（替代空仓现金）；
      · bond_as_cash 闲置资金配利率债（现金增强），不占股票腿预算。
    """
    ma_fast: int = 5           # 双均线快线（用户指定 5/12）
    ma_slow: int = 12          # 双均线慢线
    mom_lb: int = 12           # TSMOM / 绝对动量回看（用户指定 12M）
    rs_lb: int = 12            # RS 相对动量回看
    w_eq: float = 1.0          # 股票核心腿权重
    w_gold: float = 0.40       # 黄金卫星腿权重（ML 校准）
    w_cnbond: float = 0.0       # 利率债多头小卫星权重（ML 校准，默认 0）
    g_thr: float = 0.0         # 黄金绝对动量阈值
    b_thr: float = 0.0         # 利率债 12M 动量阈值（ML 校准，0~6% 量级）
    use_g_ma: bool = True      # 黄金腿是否叠加自身双均线确认
    use_b_ma: bool = False     # 利率债腿是否叠加自身双均线（利率债 98.7% 时间多头，默认关闭）
    bear_bond: float = 1.0     # 股票空头时段利率债承接权重（0=关闭该机制）
    bond_as_cash: bool = True  # 闲置资金配利率债替代现金息（现金增强）
    gold_only_in_bull: bool = True  # 黄金腿只在股票多头时段配置（数据诊断证伪空头持黄金）
    max_lev: float = 1.20      # 总仓位硬上限（风控红线，固定 1.2）
    vol_target: float | None = None   # 波动率目标（None=不启用）
    dd_thr: float | None = None       # 组合回撤刹车阈值（None=不启用）
    brake: float = 0.60        # 刹车后仓位系数
    budget_split: bool = False # True=方案A 无杠杆预算分流（w_eq 降到 1-w_gold）

    def to_dict(self):
        return asdict(self)


def compute_signals(p: SatParams) -> dict:
    """预计算全部因子（无前视）。返回各资产 mom / ma_on 及组合 regime。"""
    sig = {"_warmup": max(p.mom_lb, p.ma_slow, p.rs_lb)}
    for a in ASSETS:
        sig[a] = {"mom": _mom(PX[a], p.mom_lb if a != "cnbond" else p.mom_lb),
                  "ma_on": _ma_on(PX[a], p.ma_fast, p.ma_slow)}
    sig["spx"]["rs_mom"] = _mom(PX["spx"], p.rs_lb)
    sig["ixic"]["rs_mom"] = _mom(PX["ixic"], p.rs_lb)
    sig["_regime"] = _ma_on(COMBO, p.ma_fast, p.ma_slow)   # 股票核心择时
    return sig


def _leg_ok(sig, a: str, t: int, thr: float, use_ma: bool) -> bool:
    mom = sig[a]["mom"][t]
    ok = np.isfinite(mom) and mom > thr
    if use_ma and ok:
        ok = np.isfinite(sig[a]["ma_on"][t]) and sig[a]["ma_on"][t] > 0.5
    return bool(ok)


def _rs_stock(sig, t: int) -> str:
    ms, mi = sig["spx"]["rs_mom"][t], sig["ixic"]["rs_mom"][t]
    return "ixic" if (np.isfinite(mi) and np.isfinite(ms) and mi > ms) else "spx"


# ---------------- 决策 + 回测引擎 ----------------
def run(start: int, end: int, p: SatParams, signals: dict | None = None,
        initial: float = 1_000_000.0, collect: bool = True) -> dict:
    """t 月末决策 -> t+1 月收益。equity[i] 与 months[i] 严格一一对应（含 T0 起点）。"""
    sig = signals if signals is not None else compute_signals(p)
    w_eq = p.w_eq
    if p.budget_split:                       # 方案A：无杠杆，股票腿让出预算给黄金
        w_eq = max(0.0, 1.0 - p.w_gold - p.w_cnbond)

    eq = [initial]
    exps, turns, months = [0.0], [0.0], [DATES.iloc[start].strftime("%Y-%m")]
    chosen, scale_seq = ["cash"], [1.0]
    prev: dict[str, float] = {}
    hist_r: list[float] = []
    peak = initial
    snap_rows = []

    for t in range(start, end):
        # --- 1) 各腿信号（只用 <=t） ---
        regime = sig["_regime"][t]
        stock_on = bool(np.isfinite(regime) and regime > 0.5)
        g_on = _leg_ok(sig, "gold", t, p.g_thr, p.use_g_ma)
        b_on = _leg_ok(sig, "cnbond", t, p.b_thr, p.use_b_ma)   # 利率债趋势健康（动量>阈值）

        w: dict[str, float] = {}
        if stock_on:
            w[_rs_stock(sig, t)] = w_eq
        # 黄金腿：数据诊断三区间一致显示「股票空头时持黄金」会拖累（黄金自身有独立熊市），
        # 故默认只在股票多头时段配置；空头时段由负相关利率债独家承接。
        if g_on and p.w_gold > 0 and (stock_on or not p.gold_only_in_bull):
            w["gold"] = p.w_gold
        # 利率债多头小卫星（默认关闭；叠加会稀释股票腿，仅作 ML 可选项）
        if stock_on and b_on and p.w_cnbond > 0:
            w["cnbond"] = w.get("cnbond", 0.0) + p.w_cnbond
        # 利率债空头承接：股票空头时段用「负相关利率债」替代空仓现金（核心机制①）
        bear_active = bool((not stock_on) and b_on and p.bear_bond > 0)
        if bear_active:
            w["cnbond"] = w.get("cnbond", 0.0) + p.bear_bond

        # --- 2) 组合层：波动率目标 -> 回撤刹车 -> 杠杆硬上限 ---
        scale = 1.0
        if p.vol_target and len(hist_r) >= 12:
            rv = float(np.std(hist_r[-12:], ddof=1) * math.sqrt(12))
            scale = float(np.clip(p.vol_target / max(rv, 1e-6), 0.5, 1.5))
        dd = eq[-1] / peak - 1.0
        if p.dd_thr and dd <= -p.dd_thr:
            scale *= p.brake
        w = {k: v * scale for k, v in w.items()}

        exp = sum(w.values())
        cap = min(p.max_lev, HARD_LEV_CAP)
        if exp > cap:                        # 风控硬校验：超上限按比例压缩
            w = {k: v * cap / exp for k, v in w.items()}
            exp = cap

        # --- 3) 换手成本（含资产间切换） ---
        all_a = set(w) | set(prev)
        turnover = sum(abs(w.get(a, 0.0) - prev.get(a, 0.0)) for a in all_a)
        cost = turnover * COST
        # --- 4) 结算 t+1 ---
        r_asset = sum(w.get(a, 0.0) * RET[a][t + 1] for a in ASSETS)
        idle = 1.0 - exp
        # 闲置资金：配利率债（现金增强，核心机制②）或拿现金息；现金增强不占仓位预算、
        # 不计换手（等同货币基金/短债替换活期），但逐月在快照留痕以便审计。
        use_bond_cash = bool(p.bond_as_cash and idle > 0 and b_on)
        if idle > 0:
            cash_part = idle * (RET["cnbond"][t + 1] if use_bond_cash else CASH_M)
        else:
            cash_part = idle * FIN_M            # 杠杆部分付融资息
        ret = r_asset + cash_part - cost
        eq.append(eq[-1] * (1.0 + ret))
        peak = max(peak, eq[-1]); hist_r.append(ret)

        if collect:
            exps.append(exp); turns.append(turnover); scale_seq.append(scale)
            months.append(DATES.iloc[t + 1].strftime("%Y-%m"))
            chosen.append("+".join(sorted(w.keys())) if w else "cash")
            snap_rows.append({
                "month": DATES.iloc[t].strftime("%Y-%m"),
                "regime": int(stock_on) if np.isfinite(regime) else None,
                "rs_pick": _rs_stock(sig, t),
                "gold_on": int(g_on and p.w_gold > 0), "cnbond_on": int(b_on),
                "bear_bond_active": int(bear_active), "bond_cash": int(use_bond_cash),
                "mom_gold": (round(float(sig["gold"]["mom"][t]), 4)
                             if np.isfinite(sig["gold"]["mom"][t]) else None),
                "mom_cnbond": (round(float(sig["cnbond"]["mom"][t]), 4)
                              if np.isfinite(sig["cnbond"]["mom"][t]) else None),
                "scale": round(scale, 4), "exposure": round(exp, 4),
                "weights": ";".join(f"{k}:{v:.3f}" for k, v in sorted(w.items())),
                "dd": round(dd, 4),
            })
        prev = w

    out = {"equity": np.array(eq), "months": np.array(months),
           "exposure": np.array(exps), "turnover": np.array(turns),
           "chosen": np.array(chosen), "scale": np.array(scale_seq),
           "params": p.to_dict(), "warmup": sig["_warmup"]}
    if collect:
        out["snapshot"] = pd.DataFrame(snap_rows)
    return out


# ---------------- 对照：旧「全池 winner-take-all 轮动」（诊断证伪，保留作归因） ----------------
def run_pool_rotation(start: int, end: int, initial: float = 1_000_000.0) -> dict:
    """原 engine 原型：4 资产全池选动量最强者满仓。用于报告中的「为何不这么设计」对照。"""
    sig = compute_signals(SatParams())
    eq = [initial]; prev: dict[str, float] = {}; exps = [0.0]; turns = [0.0]
    months = [DATES.iloc[start].strftime("%Y-%m")]
    for t in range(start, end):
        cand = [(a, sig[a]["mom"][t]) for a in ASSETS
                if np.isfinite(sig[a]["mom"][t]) and sig[a]["mom"][t] > 0
                and np.isfinite(sig[a]["ma_on"][t]) and sig[a]["ma_on"][t] > 0.5]
        w = {max(cand, key=lambda z: z[1])[0]: 1.0} if cand else {}
        all_a = set(w) | set(prev)
        turn = sum(abs(w.get(a, 0) - prev.get(a, 0)) for a in all_a)
        cost = turn * COST
        ret = sum(w.get(a, 0) * RET[a][t + 1] for a in ASSETS) + (1 - sum(w.values())) * CASH_M - cost
        eq.append(eq[-1] * (1 + ret)); exps.append(sum(w.values())); turns.append(turn)
        months.append(DATES.iloc[t + 1].strftime("%Y-%m")); prev = w
    return {"equity": np.array(eq), "months": np.array(months),
            "exposure": np.array(exps), "turnover": np.array(turns)}


# ---------------- 基准策略（回归对齐，口径复用 composite.py） ----------------
def run_single_ma(start: int, end: int, initial: float = 1_000_000.0) -> dict:
    """双均线(5/12) 单因子，作用于 50/50 风险组合净值。"""
    sig = _ma_on(COMBO, 5, 12); sig = np.nan_to_num(sig, nan=0.0)
    eq = [initial]; exps = [0.0]; turns = [0.0]
    months = [DATES.iloc[start].strftime("%Y-%m")]; prev = 0.0
    for t in range(start, end):
        e = float(sig[t]); cost = abs(e - prev) * COST
        ret = e * 0.5 * (RET["spx"][t + 1] + RET["ixic"][t + 1]) + (1 - e) * CASH_M - cost
        eq.append(eq[-1] * (1 + ret)); exps.append(e); turns.append(abs(e - prev))
        months.append(DATES.iloc[t + 1].strftime("%Y-%m")); prev = e
    return {"equity": np.array(eq), "months": np.array(months),
            "exposure": np.array(exps), "turnover": np.array(turns)}


def run_rs_ma_legacy(start: int, end: int, initial: float = 1_000_000.0) -> dict:
    """原型「RS轮动+MA 固定组合」：MA(组合) 满/空仓 + RS 在 {SPX,IXIC} 二选一。"""
    ma = np.nan_to_num(_ma_on(COMBO, 5, 12), nan=0.0)
    ms, mi = _mom(PX["spx"], 12), _mom(PX["ixic"], 12)
    eq = [initial]; exps = [0.0]; turns = [0.0]
    months = [DATES.iloc[start].strftime("%Y-%m")]; prev_e, prev_a = 0.0, "cash"
    for t in range(start, end):
        e = float(ma[t])
        a = ("ixic" if (np.isfinite(mi[t]) and np.isfinite(ms[t]) and mi[t] > ms[t]) else "spx") if e > 0 else "cash"
        turn = abs(e - prev_e) + (prev_e + e if a != prev_a and a != "cash" else 0.0)
        ret = (e * RET[a][t + 1] if a != "cash" else 0.0) + (1 - e) * CASH_M - turn * COST
        eq.append(eq[-1] * (1 + ret)); exps.append(e); turns.append(turn)
        months.append(DATES.iloc[t + 1].strftime("%Y-%m")); prev_e, prev_a = e, a
    return {"equity": np.array(eq), "months": np.array(months),
            "exposure": np.array(exps), "turnover": np.array(turns)}


def run_buyhold(start: int, end: int, asset: str = "spx", initial: float = 1_000_000.0) -> dict:
    eq = [initial]; months = [DATES.iloc[start].strftime("%Y-%m")]
    for t in range(start, end):
        eq.append(eq[-1] * (1 + RET[asset][t + 1]))
        months.append(DATES.iloc[t + 1].strftime("%Y-%m"))
    return {"equity": np.array(eq), "months": np.array(months)}


# ---------------- 指标（口径与 composite.py / 研究报告一致） ----------------
def metrics(eq: np.ndarray, exposure: np.ndarray | None = None,
            turnover: np.ndarray | None = None) -> dict:
    e = np.asarray(eq, float)
    if len(e) < 2 or e[0] <= 0:
        return {}
    n = len(e) - 1
    years = n / 12.0
    cagr = (e[-1] / e[0]) ** (1 / years) - 1
    r = np.diff(e) / e[:-1]
    sd = float(np.std(r, ddof=1))
    sh = float(np.mean(r - CASH_M) / sd * math.sqrt(12)) if sd > 0 else 0.0
    mdd = float(np.min(e / np.maximum.accumulate(e) - 1))
    return {"final_equity": float(e[-1]), "cagr": float(cagr), "vol": sd * math.sqrt(12),
            "sharpe": sh, "mdd": mdd, "calmar": float(cagr / abs(mdd)) if mdd < 0 else 0.0,
            "monthly_win": float(np.mean(r > 0)), "n_months": n,
            "avg_exposure": float(np.mean(exposure)) if exposure is not None else 0.0,
            "avg_turnover": float(np.mean(turnover)) if turnover is not None else 0.0}


def allocation_stats(res: dict) -> dict:
    ch = res["chosen"]
    cnt: dict[str, int] = {}
    for s in ch:
        for a in str(s).split("+"):
            cnt[ASSET_CN.get(a, a)] = cnt.get(ASSET_CN.get(a, a), 0) + 1
    return dict(sorted(cnt.items(), key=lambda z: -z[1]))


if __name__ == "__main__":
    s, e = month_index("2007-11"), month_index("2026-11")
    print(f"=== MAFC 生产引擎自检  {DATES.iloc[s].strftime('%Y-%m')}~{DATES.iloc[e].strftime('%Y-%m')}  {e-s+1} 月 ===\n")
    A = SatParams(w_eq=1.0, w_gold=0.30, w_cnbond=0.0, budget_split=True, max_lev=1.0)
    B = SatParams(w_eq=1.0, w_gold=0.40, w_cnbond=0.0, max_lev=1.20)
    cases = [
        ("基准A 双均线(5/12)单因子", lambda: run_single_ma(s, e)),
        ("基准B 原型 RS+MA 固定组合", lambda: run_rs_ma_legacy(s, e)),
        ("（对照）全池轮动 证伪结构", lambda: run_pool_rotation(s, e)),
        ("方案A 无杠杆预算分流 股.7/金.3", lambda: run(s, e, A)),
        ("方案B 温和杠杆卫星 股1/金.4", lambda: run(s, e, B)),
    ]
    print(f"{'策略':40s} {'CAGR':>8s} {'MDD':>9s} {'Sharpe':>7s} {'Calmar':>7s} {'仓位':>6s}")
    for lab, fn in cases:
        r = fn(); m = metrics(r["equity"], r.get("exposure"), r.get("turnover"))
        print(f"{lab:40s} {m['cagr']*100:7.2f}% {m['mdd']*100:8.2f}% "
              f"{m['sharpe']:7.3f} {m['calmar']:7.3f} {m['avg_exposure']:6.2f}")
    r = run(s, e, B)
    print("\n方案B 资产选择频率:", allocation_stats(r))
    print("信号快照样例:\n", r["snapshot"].head(3).to_string(index=False))
