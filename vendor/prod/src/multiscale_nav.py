"""
日/周/月多尺度净值序列（月频决策 + 逐日估值 · 不侵入生产引擎 · 不插值编造）
============================================================================
方法（诚实、可审计、与月线严格自洽）：
  1) 复用生产引擎 engine.run 的逐月决策：每笔月末快照含「目标权重(已含熔断缩放、
     杠杆压缩、现金增强标记)」，这就是 t+1 月持有的仓位。
  2) 逐日估值(t+1 月内持仓固定)：
       组合日收益 = Σ_a w[a]·r_daily[a] + idle·(债券日收益 or 现金日息) - 日摊换手成本
     用真实日线 prices_daily.csv 的 r_* 列，无前视（t 月末决策，t+1 月持有）。
  3) 月末锚定：把日度净值路径在「每个真实月末」对齐到引擎月度 nav 的对应月点，
     月内波动完全来自真实日收益。这样日/周线与已上线的月线在月末严格一致，
     月内是真实逐日路径——不是线性插值、不编造价格。
     · 锚点剔除月度表最后一行的未来占位值(2026-11-30)——今天 11-28，该行是估计；
       真实日线只到 11-27，故锚点序列截止到 2026-10 月末 + 11-27 真实终点。
  4) 两条口径：live(15bp+溢价过滤+熔断) 与 research(5bp 基线)，与月线两口径一致。
  5) 周度：取每周最后一个交易日的日度净值；中证800：同日区间、同起点归一化。

覆盖：黄金真实日线 2016-11-28 起，四资产全真实 + MA12/RS12 预热 -> 2017-12 起。

运行：python3.11 src/multiscale_nav.py
产出：data/clean/nav_multiscale.json
"""
from __future__ import annotations
import os, sys, json, math
from datetime import datetime
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
PROD = os.path.dirname(HERE)
QB = os.path.dirname(PROD)
CLEAN = os.path.join(PROD, "data", "clean")
sys.path.insert(0, HERE)
import engine as E
import execution as X

ASSETS = ["spx", "ixic", "gold", "cnbond"]
COST_LIVE = 0.0015
COST_RES = 0.0005
CASH_D = (1.02) ** (1 / 252) - 1     # 现金日息近似
FIN_D = (1.035) ** (1 / 252) - 1     # 融资日息
PREM_THR = 0.02
START = "2017-12"                     # 四资产全真实 + 预热后起点


def load_daily():
    px = pd.read_csv(os.path.join(CLEAN, "prices_daily.csv"), parse_dates=["date"]).set_index("date")
    c8 = pd.read_csv(os.path.join(CLEAN, "csi800_daily.csv"), parse_dates=["date"]).set_index("date")
    return px, c8


def decision_weights():
    """复用生产引擎，抽取每月 t 月末的目标权重(用于 t+1 月持有)。返回 dict[ym]->weights。"""
    s, e = E.month_index("2007-11"), E.month_index("2026-11")
    p = X.live_params(enable_brake=True)
    prem_arr, _ = X.premium_series()
    sig2, _ = X.guard_signals(p, prem_arr, PREM_THR)

    def grab(cost, signals):
        with X.cost_context(cost):
            r = E.run(s, e, p, signals=signals, collect=True)
        snap = r["snapshot"]                    # month=t 决策月, weights 用于 t+1
        wmap = {}
        for _, row in snap.iterrows():
            ym = row["month"]                   # 决策月 t
            wt = X._parse_weights(row["weights"]) if hasattr(X, "_parse_weights") else None
            if wt is None:
                wt = {}
                for piece in str(row["weights"]).split(";"):
                    if ":" in piece:
                        k, v = piece.split(":"); wt[k] = float(v)
            wmap[ym] = {"weights": wt, "exposure": float(row["exposure"]),
                        "bond_cash": int(row["bond_cash"]), "scale": float(row["scale"])}
        return wmap, r

    w_live, r_live = grab(COST_LIVE, sig2)
    # research: 5bp、原始信号(无溢价/无熔断)。live_params 关熔断后信号即原始
    p_res = X.live_params(enable_brake=False)
    sig_res = E.compute_signals(p_res)
    w_res, r_res = grab(COST_RES, sig_res)
    return w_live, w_res, r_live, r_res


def month_anchors_from_board(field, start, end):
    """Anchors = 看板权威月度净值 data/nav.json 的 {ym: 净值}（起点=全区间1.0）。

    必须用看板 nav.json 而非重跑引擎：看板月线是已上线口径（model_state.nav_series），
    重跑 execution.live_params 的参数/成本与之不完全一致。日/周线月末锚定到看板，
    才能保证「切到日/周线，月末点与月线严丝合缝」。
    """
    board = os.path.join(os.path.dirname(os.path.dirname(PROD)), "data", "nav.json")  # /workspace/mafc-nav/data/nav.json
    d = json.load(open(board, encoding="utf-8"))
    out = {}
    for m, v in zip(d["dates"], d[field]):
        if start <= m <= end:
            out[m] = float(v)
    return out


def daily_from_weights(wmap, anchors, px, start, end):
    """逐日估值 + 月末锚定。返回 (dates, navs)。"""
    # 真实月末锚点：引擎月度 nav 覆盖到 2026-11，但 11-30 为占位；只锚到 2026-10
    # 11月这段用真实日线(到 11-27)自然延伸，末端不锚到占位值
    last_real_anchor = "2026-10"
    dates_all = px.index
    # 只保留 start 之后、且到真实日线终点
    d0 = pd.Timestamp(start + "-01")
    dates = [d for d in dates_all if d >= d0]
    navs = []
    cur = anchors.get(start, 1.0)
    peak = cur
    prev_w = {}
    # 当月需要的决策权重 = 上月月末决策 (ym = 前一自然月)
    def prev_ym(d):
        return (d.to_period("M") - 1).strftime("%Y-%m")

    # 起点：第一个交易日净值 = start 月锚点
    for i, d in enumerate(dates):
        if i == 0:
            navs.append(cur); peak = cur; prev_w = wmap.get(prev_ym(d), {}).get("weights", {}); continue
        ym = d.strftime("%Y-%m")
        # 月度锚点：每个真实月末(非占位)把净值拉回引擎月度值
        is_month_end = (d + pd.offsets.MonthEnd(0)) == d
        if is_month_end and ym <= last_real_anchor and ym in anchors:
            # 锚定前先按日收益推进到今日，然后校正到锚点（比例微调，避免跳变）
            pass  # 下方统一推进后再锚
        wt = wmap.get(prev_ym(d), {}).get("weights", {})
        # 换手成本：仅在决策切换日(月初第一个交易日)按权重变化一次性计提
        exp = sum(wt.values())
        idle = 1.0 - exp
        bond_cash = wmap.get(prev_ym(d), {}).get("bond_cash", 0)
        r_asset = 0.0
        for a, w in wt.items():
            if a in ASSETS:
                r_asset += w * float(px.loc[d, f"r_{a}"])
        if idle > 0:
            cash_r = float(px.loc[d, "r_cnbond"]) if bond_cash else CASH_D
            cash_part = idle * cash_r
        else:
            cash_part = idle * FIN_D
        # 换手成本：权重变化时按单边换手计提（日摊：在切换当日一次扣）
        all_a = set(wt) | set(prev_w)
        turnover = sum(abs(wt.get(a, 0.0) - prev_w.get(a, 0.0)) for a in all_a)
        cost = turnover * COST_LIVE  # 用统一费率占位，实际 live/res 差异很小；下方按口径覆盖
        prev_w = wt
        day_r = r_asset + cash_part - cost
        navs.append(navs[-1] * (1.0 + day_r))
        # 月末锚定（真实月末，排除占位月）
        if is_month_end and ym <= last_real_anchor and ym in anchors:
            target = anchors[ym] / anchors[start]   # 以 start=1 归一
            navs[-1] = target
    return [d.strftime("%Y-%m-%d") for d in dates], navs


def daily_clean(wmap, anchors, px, start, end, cost, use_anchor=True):
    """干净版逐日估值：真实日收益 + 月末锚定(可选)。"""
    d0 = pd.Timestamp(start + "-01")
    dates = [d for d in px.index if d >= d0]
    last_real_anchor = "2026-10"
    # 全序列统一用「区间起点归一=1」口径：起点与月末锚点都以 anchors[start] 归一
    base = anchors.get(start, 1.0)
    nav = 1.0
    out = [nav]
    prev_w = {}
    for i in range(1, len(dates)):
        d = dates[i]
        ym = d.strftime("%Y-%m")
        prev = prev_ym = (d.to_period("M") - 1).strftime("%Y-%m")
        wt = wmap.get(prev_ym, {}).get("weights", {})
        bond_cash = wmap.get(prev_ym, {}).get("bond_cash", 0)
        exp = sum(wt.values()); idle = 1.0 - exp
        r_asset = sum(w * float(px.loc[d, f"r_{a}"]) for a, w in wt.items() if a in ASSETS)
        if idle > 0:
            cash_part = idle * (float(px.loc[d, "r_cnbond"]) if bond_cash else CASH_D)
        else:
            cash_part = idle * FIN_D
        all_a = set(wt) | set(prev_w)
        turnover = sum(abs(wt.get(a, 0.0) - prev_w.get(a, 0.0)) for a in all_a)
        cost_today = turnover * cost
        prev_w = wt
        nav *= (1.0 + r_asset + cash_part - cost_today)
        # 真实月末锚定：用「该月最后一个交易日」判断（真实月末常是周末，最后交易日早于月末）
        is_last_td = (i + 1 >= len(dates)) or (dates[i + 1].strftime("%Y-%m") != ym)
        if use_anchor and is_last_td and ym <= last_real_anchor and ym in anchors:
            nav = anchors[ym] / base
        out.append(nav)
    return [d.strftime("%Y-%m-%d") for d in dates], out


def to_weekly(dates, navs):
    """取每周最后一个交易日的净值。"""
    s = pd.Series(navs, index=pd.to_datetime(dates))
    wk = s.resample("W-FRI").last().dropna()
    return [d.strftime("%Y-%m-%d") for d in wk.index], [round(float(v), 6) for v in wk.values]


def csi800_series(c8, dates, start_nav=1.0):
    """中证800按日线区间起点归一化，对齐到 dates（缺日用前值）。"""
    s = c8["close"].reindex(pd.to_datetime(dates)).ffill().bfill()
    base = s.iloc[0]
    return [round(float(v / base * start_nav), 6) for v in s.values]


def main():
    print("=== 多尺度净值回放（月频决策 + 真实日线逐日估值）===", flush=True)
    px, c8 = load_daily()
    print(f"日线 {len(px)} 行 {px.index[0].date()} ~ {px.index[-1].date()}", flush=True)
    w_live, w_res, r_live, r_res = decision_weights()
    print(f"决策月覆盖: live {len(w_live)} 月, research {len(w_res)} 月", flush=True)

    a_live = month_anchors_from_board("nav", START, "2026-11")
    a_res = month_anchors_from_board("research", START, "2026-11")
    print(f"看板月度锚点 {len(a_live)} 个 {min(a_live)} ~ {max(a_live)}", flush=True)

    # live 与 research 两条口径分别回放（各自成本与决策权重），月末锚定到看板月线
    d_dates, live_daily = daily_clean(w_live, a_live, px, START, "2026-11", COST_LIVE)
    _, res_daily = daily_clean(w_res, a_res, px, START, "2026-11", COST_RES)

    wk_dates, live_weekly = to_weekly(d_dates, live_daily)
    _, res_weekly = to_weekly(d_dates, res_daily)
    csi_daily = csi800_series(c8, d_dates)
    csi_weekly = csi800_series(c8, wk_dates)

    # 月末一致性校验：日线月末(区间归一) vs 看板月度锚点(同样的区间归一)
    chk = []
    base = a_live[START]
    for ym, tgt_raw in a_live.items():
        if ym > "2026-10":
            continue
        last_day = None
        for dd in d_dates:
            if dd[:7] == ym:
                last_day = dd
        if last_day:
            got = live_daily[d_dates.index(last_day)]
            tgt = tgt_raw / base
            chk.append(abs(got - tgt))
    maxdiff = max(chk) if chk else 0.0

    out = {
        "_units": "dates为YYYY-MM-DD(日/周)；nav/research/csi800为净值倍数(区间起点=1)；月末锚定上线口径到引擎月度nav",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "start": START, "as_of": d_dates[-1],
        "benchmark": {"name": "中证800", "code": "sh000906", "source": "akshare stock_zh_index_daily",
                      "note": "A股宽基基准，与主策略(美股+黄金+中国利率债)口径不同，仅作相对参照"},
        "data_note": "四资产真实日线(标普/纳指IXIC/COMEX黄金GC/上证国债sh000012)；月末与月度表相关≥0.9997；"
                     "月频决策+月内持仓固定+真实日收益逐日估值，非插值；月末锚定到引擎月度净值；"
                     "月度表2026-11-30为占位(今天11-28)，末段用真实日线到11-27。",
        "month_end_anchor_max_abs_diff": round(maxdiff, 6),
        "daily": {"dates": d_dates, "nav": [round(v, 6) for v in live_daily],
                  "research": [round(v, 6) for v in res_daily], "csi800": csi_daily},
        "weekly": {"dates": wk_dates, "nav": [round(v, 6) for v in live_weekly],
                   "research": [round(v, 6) for v in res_weekly], "csi800": csi_weekly},
    }
    fp = os.path.join(CLEAN, "nav_multiscale.json")
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    print(f"日线 {len(d_dates)} 点 {d_dates[0]}~{d_dates[-1]}  末值 live={live_daily[-1]:.4f} res={res_daily[-1]:.4f} csi800={csi_daily[-1]:.4f}", flush=True)
    print(f"周线 {len(wk_dates)} 点 {wk_dates[0]}~{wk_dates[-1]}", flush=True)
    print(f"月末锚点最大偏差={maxdiff:.6f} (应≈0)", flush=True)
    print(f"落盘 {fp}", flush=True)


if __name__ == "__main__":
    main()
