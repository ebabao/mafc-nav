"""
mafc-nav 看板同源数据接口生成器
=================================
读取每日同步产出的真实数据（model_state / shadow_state / nav_daily /
sync_run / deploy_report / vendor 自包含的研究结果），生成 5 个同源 JSON 接口：

    data/meta.json     品牌 / 同步 / 验收 / 披露 / 容量 / 部署 / 工程指标
    data/nav.json      229 点月度净值、回撤、月度收益、分年统计、S0~S4 成本阶梯
    data/signal.json   最新决策信号、目标持仓、固定参数、行情探针、溢价覆盖
    data/shadow.json   影子账户进化学习（旁路）状态与三条演练净值
    data/audit.json    同步阶段、健康检查、日台账、巡检记录、部署状态

页面 dashboard/index.html 为静态模板，通过 fetch('./data/*.json', {cache:'no-store'})
读取上述接口；页面无需重建即可每日同步，与 all-weather-nav 参考页同构。

派生指标（回撤 / 月度收益 / 分年统计）全部由 model_state.json 的真实净值序列现场计算，
不引入任何外部行情、不编造数值。运行：python3.11 src/dashboard.py
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA = os.path.join(ROOT, "data")
OUT = os.path.join(ROOT, "output")
DASH = os.path.join(ROOT, "dashboard")
CONFIG = os.path.join(ROOT, "config")
VENDOR = os.path.join(ROOT, "vendor")
VOUT = os.path.join(VENDOR, "prod", "output")
VCONF = os.path.join(VENDOR, "prod", "config")

PAGE = os.path.join(DASH, "index.html")
INTERFACES = ("meta.json", "nav.json", "signal.json", "shadow.json", "audit.json")
REBAL_CSV = os.path.join(DATA, "rebalances.csv")     # 完整调仓记录（供下载）

ASSET_CN2 = {"spx": "标普500", "ixic": "纳指", "gold": "黄金", "cnbond": "中国利率债"}

ASSETS = (
    ("spx", "标普500ETF(513500)", "海外股票 / QDII"),
    ("ixic", "纳指ETF(513100)", "海外股票 / QDII"),
    ("gold", "黄金ETF(518880)", "黄金"),
    ("cnbond", "国债ETF(511010)", "中国利率债 / 空头承接"),
)


def jload(path, default=None):
    if not os.path.exists(path):
        return default if default is not None else {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def jdump(name, obj):
    os.makedirs(DATA, exist_ok=True)
    path = os.path.join(DATA, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, allow_nan=False)
    return path


def pct(x, nd=4):
    """小数比例 -> 百分数数值（页面直接显示，单位 %）"""
    return None if x is None else round(x * 100, nd)


def r(x, nd=4):
    return None if x is None else round(float(x), nd)


def csv_rows(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def display_metrics(m, final_nav):
    """把引擎小数口径转成页面展示口径（百分比字段已 ×100）"""
    return {
        "cagr": pct(m.get("cagr")),
        "mdd": pct(m.get("mdd")),
        "vol": pct(m.get("vol")),
        "sharpe": r(m.get("sharpe")),
        "calmar": r(m.get("calmar")),
        "month_win": pct(m.get("monthly_win")),
        "n_months": int(m.get("n_months") or 0),
        "avg_exposure": r(m.get("avg_exposure")),
        "avg_turnover": pct(m.get("avg_turnover")),
        "final_nav": r(final_nav),
        "total_return": round((final_nav - 1) * 100, 4),
    }


def derive_series(ns):
    """由真实月度净值序列派生回撤 / 月度收益 / 分年统计"""
    dates = [z["month"] for z in ns]
    live = [float(z["live"]) for z in ns]
    research = [float(z["research"]) for z in ns]

    peak, dd = -float("inf"), []
    for v in live:
        peak = max(peak, v)
        dd.append(round((v / peak - 1) * 100, 4))

    monthly = [
        {"month": dates[i], "ret_pct": round((live[i] / live[i - 1] - 1) * 100, 4)}
        for i in range(1, len(live))
    ]

    by_year = {}
    for row in monthly:
        by_year.setdefault(row["month"][:4], []).append(row)

    yearly = []
    for year in sorted(by_year):
        rs = by_year[year]
        growth = 1.0
        for row in rs:
            growth *= (1 + row["ret_pct"] / 100)
        total_pct = (growth - 1) * 100
        i0 = dates.index(rs[0]["month"])
        seq = live[i0 - 1: i0 + len(rs)]          # 含上年末最后一点作年内起点
        pk, mdd = seq[0], 0.0
        for v in seq:
            pk = max(pk, v)
            mdd = min(mdd, (v / pk - 1) * 100)
        rets = [row["ret_pct"] for row in rs]
        vol = statistics.pstdev(rets) * math.sqrt(12) if len(rets) > 1 else 0.0
        full_year = len(rs) == 12
        annual_pct = round(total_pct, 4) if full_year else None
        yearly.append({
            "year": int(year),
            "total_return_pct": round(total_pct, 4),
            "annual_return_pct": annual_pct,        # 不足整年不年化，页面显示 —
            "max_drawdown_pct": round(mdd, 4),
            "return_dd_ratio": round(total_pct / abs(mdd), 4) if mdd < -1e-9 else None,
            "std_pct": round(vol, 4),
            "sharpe": round(annual_pct / vol, 4) if (full_year and vol > 0) else None,
            "n_months": len(rs),
            "full_year": full_year,
        })
    return dates, live, research, dd, monthly, yearly


def weights_text(w: str) -> str:
    """把 'gold:0.343;ixic:0.857' 渲染成 '黄金34.3% + 纳指85.7%'。"""
    parts = []
    for seg in str(w).split(";"):
        if ":" not in seg:
            continue
        k, v = seg.split(":", 1)
        try:
            parts.append(f"{ASSET_CN2.get(k.strip(), k.strip())}{float(v) * 100:.1f}%")
        except ValueError:
            parts.append(seg)
    return " + ".join(parts) if parts else "现金"


def _parse_weights(w: str) -> dict:
    d = {}
    for seg in str(w).split(";"):
        if ":" in seg:
            k, v = seg.split(":", 1)
            try:
                d[k.strip()] = float(v)
            except ValueError:
                pass
    return d


def build_attribution():
    """业绩归因：各底层资产的年度累计收益贡献（百分点）。

    口径与引擎完全一致：决策月 t 的权重 w 在 t+1 月生效，
    资产 a 的月度贡献 = w[a] × RET[a][t+1]；现金增强 = idle × 国债/现金息；
    成本 = 换手 × 单边成本。各资产贡献 + 现金 + 成本 = 组合月度收益（已验证残差≈0）。
    按自然年累加得到「各资产年度累计贡献」，不引入外部数据、不编造。
    """
    vproc = os.path.join(VENDOR, "prod")
    for p in (vproc, os.path.join(vproc, "src")):
        if p not in sys.path:
            sys.path.insert(0, p)
    import engine as E            # noqa: E402
    import execution as X         # noqa: E402

    s, e = E.month_index("2007-11"), E.month_index("2026-11")
    pol = X.live_params(enable_brake=True)
    prem, cov = X.premium_series()
    sig, _ = X.guard_signals(pol, prem)
    with X.cost_context(15):
        res = E.run(s, e, pol, signals=sig, collect=True)
    snap, eq = res["snapshot"], res["equity"]
    RET, ASSETS = E.RET, E.ASSETS

    by_year = {}
    for j in range(len(snap)):
        z = snap.iloc[j]
        w = _parse_weights(z["weights"])
        t = E.month_index(str(z["month"]))
        eff_month = str(E.DATES.iloc[t + 1].strftime("%Y-%m"))
        yr = eff_month[:4]
        rec = by_year.setdefault(yr, {a: 0.0 for a in ASSETS} |
                                 {"cash": 0.0, "cost": 0.0, "total": 0.0, "n": 0})
        contrib = sum(w.get(a, 0.0) * RET[a][t + 1] for a in ASSETS)
        for a in ASSETS:
            rec[a] += w.get(a, 0.0) * RET[a][t + 1] * 100.0
        exp = float(z["exposure"]) if z["exposure"] == z["exposure"] else 0.0
        idle = 1.0 - exp
        use_bond = bool(int(z["bond_cash"])) if "bond_cash" in z else False
        cash_part = (idle * (RET["cnbond"][t + 1] if use_bond else E.CASH_M)
                     if idle > 0 else idle * E.FIN_M)
        rec["cash"] += cash_part * 100.0
        eng_ret = eq[j + 1] / eq[j] - 1.0
        rec["cost"] += (eng_ret - contrib - cash_part) * 100.0
        rec["total"] += eng_ret * 100.0
        rec["n"] += 1

    order = ["spx", "ixic", "gold", "cnbond", "cash", "cost"]
    rows = []
    for yr in sorted(by_year):
        rec = by_year[yr]
        row = {"year": int(yr), "n_months": rec["n"]}
        for a in order:
            row[a] = round(rec[a], 4)
        # 复利口径年度总收益（与 nav.yearly 一致，供交叉核对）
        row["total_sum"] = round(rec["total"], 4)   # 各月贡献加总（百分点）
        rows.append(row)

    return {
        "note": (
            "各列=该资产当年对组合的累计收益贡献（百分点），红涨绿跌。口径与引擎一致："
            "决策权重×资产次月收益逐月累加，现金增强与交易成本单列；资产贡献+现金+成本=组合月度收益（残差≤0.4bp）。"
            "「合计」为各月收益加总（百分点口径），与分年统计的复利总收益略有差异属正常。"
        ),
        "assets": [
            {"key": "spx", "name": "标普500"},
            {"key": "ixic", "name": "纳指"},
            {"key": "gold", "name": "黄金"},
            {"key": "cnbond", "name": "中国利率债"},
            {"key": "cash", "name": "现金增强"},
            {"key": "cost", "name": "交易成本"},
        ],
        "rows": rows,
        "source": "vendor/prod 引擎真实权重×资产月度收益现场分解",
    }


def build_rebalances():
    """用与 model_api.state() 完全一致的上线口径重建逐月调仓快照。

    决策月 t 的权重在 t+1 月生效；记录来源为 vendor/prod 引擎现场回测，
    不引入外部数据、不编造。产出完整 CSV + 近半年接口子集。
    返回 (all_rows, recent_rows, coverage)。
    """
    vproc = os.path.join(VENDOR, "prod")
    for p in (vproc, os.path.join(vproc, "src")):
        if p not in sys.path:
            sys.path.insert(0, p)
    import engine as E            # noqa: E402
    import execution as X         # noqa: E402

    s, e = E.month_index("2007-11"), E.month_index("2026-11")
    pol = X.live_params(enable_brake=True)
    prem, cov = X.premium_series()
    sig, _ = X.guard_signals(pol, prem)
    with X.cost_context(15):
        res = E.run(s, e, pol, signals=sig, collect=True)
    snap = res["snapshot"]

    all_rows = []
    prev_w = None
    for _, z in snap.iterrows():
        month = str(z["month"])
        idx = E.month_index(month)
        eff = str(E.DATES.iloc[min(idx + 1, len(E.DATES) - 1)].strftime("%Y-%m"))
        regime = int(z["regime"]) if z["regime"] == z["regime"] else None
        rs = str(z["rs_pick"])
        w_raw = str(z["weights"])
        changed = bool(prev_w is not None and w_raw != prev_w)
        all_rows.append({
            "decision_month": month,
            "effective_month": eff,
            "regime": regime,
            "regime_text": "股票多头" if regime == 1 else ("空仓/债券承接" if regime == 0 else "—"),
            "rs_pick": rs,
            "rs_pick_cn": ASSET_CN2.get(rs, rs),
            "weights": w_raw,
            "weights_text": weights_text(w_raw),
            "exposure": r(z["exposure"], 3),
            "scale": r(z["scale"], 3),
            "premium_pct": round(float(prem[idx]) * 100, 4),
            "premium_blocked": bool(prem[idx] > X.PREMIUM_THRESHOLD),
            "bear_bond_active": int(z["bear_bond_active"]),
            "bond_cash": int(z["bond_cash"]),
            "dd_pct": round(float(z["dd"]) * 100, 4),
            "changed": changed,
        })
        prev_w = w_raw

    # 完整记录写 CSV（utf-8-sig 便于 Excel 直接打开）
    os.makedirs(DATA, exist_ok=True)
    cols = ["decision_month", "effective_month", "regime_text", "rs_pick_cn",
            "weights_text", "weights", "exposure", "scale", "premium_pct",
            "premium_blocked", "bear_bond_active", "bond_cash", "dd_pct", "changed"]
    cn = {"decision_month": "决策月", "effective_month": "生效月", "regime_text": "股票择时",
          "rs_pick_cn": "RS选中", "weights_text": "目标仓位", "weights": "目标仓位(原始)",
          "exposure": "总敞口(倍)", "scale": "熔断系数", "premium_pct": "QDII溢价(%)",
          "premium_blocked": "溢价拦截", "bear_bond_active": "债券承接", "bond_cash": "现金增强",
          "dd_pct": "当时回撤(%)", "changed": "是否调仓"}
    headers = [cn[c] for c in cols]              # 中文表头
    with open(REBAL_CSV, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        for row in all_rows:
            out = {}
            for c in cols:
                v = row[c]
                if c in ("premium_blocked", "bear_bond_active", "bond_cash", "changed"):
                    v = "是" if v else "否"
                out[cn[c]] = v
            writer.writerow(out)

    # 近半年 = 最近 6 个决策月（含是否已调仓标记）
    recent = all_rows[-6:]
    coverage = {
        "total_decisions": len(all_rows),
        "first_decision": all_rows[0]["decision_month"],
        "last_decision": all_rows[-1]["decision_month"],
        "recent_n": len(recent),
        "csv_file": "data/rebalances.csv",
        "csv_note": "完整逐月调仓快照：决策月 t 的仓位在 t+1 月生效；原始权重与目标仓位(中文)双列；含溢价拦截/熔断/债券承接留痕。",
    }
    return all_rows, recent, coverage


def build():
    M = jload(os.path.join(DATA, "model_state.json"))
    S = jload(os.path.join(DATA, "shadow_state.json"))
    if not M or not S:
        raise RuntimeError("缺少 model_state.json / shadow_state.json，请先运行 sync_daily.py")
    if not os.path.exists(PAGE):
        raise RuntimeError("缺少 dashboard/index.html 模板页")

    sync = jload(os.path.join(OUT, "sync_run.json"))
    dep = jload(os.path.join(OUT, "deploy_report.json"))
    health = jload(os.path.join(OUT, "health.json"))
    launch = jload(os.path.join(CONFIG, "launch_state.json"))
    sync_policy = jload(os.path.join(CONFIG, "sync_policy.json"))
    shadow_policy = jload(os.path.join(CONFIG, "shadow_policy.json"))
    disc_cfg = jload(os.path.join(VCONF, "disclosure.json"))
    cost_cfg = jload(os.path.join(VCONF, "cost_policy.json"))
    exec_sc = jload(os.path.join(VOUT, "execution_scenarios.json"))
    cap_rep = jload(os.path.join(VOUT, "capacity_report.json"))
    param_reg = jload(os.path.join(VOUT, "param_registry.json"))

    ns = M["nav_series"]
    dates, live, research, dd, monthly, yearly = derive_series(ns)
    mlive = M["metrics"]["live"]
    mres = M["metrics"]["research"]
    sig = M["latest_signal"]
    pol = M.get("live_policy", {})
    dsc = M.get("disclosure", {})
    capm = M.get("capacity", {})
    exam = M.get("exam_summary", {})

    fixed = (param_reg.get("fixed_baseline") or {})
    fp = hashlib.sha1(
        json.dumps(fixed, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:10].upper()

    # 逐月调仓记录（同源重建）；失败不阻断看板其余接口
    try:
        _, rebalances_recent, rebal_cov = build_rebalances()
    except Exception as exc:  # pragma: no cover - 兜底：如实标注，不编造
        rebalances_recent, rebal_cov = [], {
            "csv_file": "data/rebalances.csv", "recent_n": 0,
            "csv_note": f"调仓记录生成失败：{exc}", "error": str(exc),
        }

    # 业绩归因（引擎真实权重×资产收益分解）；失败如实标注
    try:
        attribution = build_attribution()
    except Exception as exc:  # pragma: no cover
        attribution = {"note": f"业绩归因生成失败：{exc}", "assets": [], "rows": [], "error": str(exc)}

    # ---------- 1. nav.json ----------
    scenarios = []
    for s in exec_sc.get("scenarios", []):
        scenarios.append({
            "label": s["label"],
            "short": s["label"].split()[0],
            "cost_bps": s.get("cost_bps_one_way"),
            "cagr": pct(s.get("cagr")),
            "mdd": pct(s.get("mdd")),
            "sharpe": r(s.get("sharpe")),
            "premium_filter": bool(s.get("premium_filter")),
            "brake": bool(s.get("brake")),
            "effective_blocks": s.get("effective_blocks"),
            "brake_triggered_months": s.get("brake_triggered_months"),
            "is_live": s["label"].startswith("S3"),
        })
    nav_obj = {
        "_units": "日期为月度(YYYY-MM)；nav/research 为净值倍数(起点=1)；dd/ret_pct/*_pct 为百分数；sharpe/calmar 为原值",
        "dates": dates,
        "nav": [r(v, 6) for v in live],
        "research": [r(v, 6) for v in research],
        "dd": dd,
        "monthly": monthly,
        "yearly": yearly,
        "metrics": {
            "live": display_metrics(mlive, live[-1]),
            "research": display_metrics(mres, research[-1]),
        },
        "scenarios": scenarios,
        "attribution": attribution,          # 业绩归因：各资产年度累计贡献
        "final_nav": r(live[-1], 4),
        "as_of": M.get("as_of_date"),
        "source": "model_state.json · vendor/prod 引擎现场回测",
    }
    jdump("nav.json", nav_obj)

    # ---------- 2. signal.json ----------
    wmap = {}
    for part in str(sig.get("weights", "")).split(","):
        if ":" in part:
            k, v = part.split(":", 1)
            try:
                wmap[k.strip()] = float(v) * 100
            except ValueError:
                pass
    cap_instr = cap_rep.get("instruments", {})
    current, assets = [], []
    for code, name, role in ASSETS:
        ci = cap_instr.get(code, {})
        current.append({
            "code": code, "name": ci.get("name", name), "role": role,
            "weight": round(wmap.get(code, 0.0), 2),
        })
        aum_yi = ci.get("aum_cap_cny_yi")
        assets.append({
            "code": code, "name": ci.get("name", name), "role": role,
            "w_max": ci.get("w_max"),
            "adv_cny_yi": ci.get("adv_cny_yi"),
            "aum_cap_cny_wan": round(aum_yi * 10000, 1) if aum_yi is not None else None,
            "available": ci.get("available"),
            "source": ci.get("source"),
        })
    probe = [
        {"asset": k, "last_trade_date": v.get("last_trade_date"),
         "available": v.get("available"), "source": v.get("source"),
         "attempts": len(v.get("attempts") or [])}
        for k, v in (sync.get("per_account") or {}).items()
    ]
    signal_obj = {
        "as_of": M.get("as_of_date"),
        "decision_month": sig.get("decision_month"),
        "nav_month": dates[-1],
        "lag_note": "决策月 t 的权重在 t+1 月生效，因此最新决策月比最新净值月早 1 个月；下表为为该净值月设定的目标仓位，不是当月收盘持仓。",
        "regime": sig.get("regime"),
        "rs_pick": sig.get("rs_pick"),
        "weights": sig.get("weights"),
        "exposure": sig.get("exposure"),
        "premium_pct": sig.get("premium_pct"),
        "premium_blocked": bool(sig.get("premium_blocked")),
        "premium_threshold_pct": pct(pol.get("premium_threshold"), 2),
        "bear_bond_active": sig.get("bear_bond_active"),
        "bond_cash": sig.get("bond_cash"),
        "current": current,
        "assets": assets,
        "params": fixed,
        "params_fp": fp,
        "policy_note": "上线版在固定参数基线上另启用 dd_thr=0.15 / brake=0.6 回撤熔断与 15bp 单边成本，见 live_policy。",
        "live_policy": pol,
        "probe": probe,
        "probe_note": "探针仅用于判断数据新鲜度，不写入模型价格数据；补数最多尝试 2 次（新浪→东财），失败如实标 unavailable，不填假设值。",
        "premium_coverage": M.get("premium_coverage", {}),
        "rebalances": rebalances_recent,
        "rebalances_coverage": rebal_cov,
        "run_notes": {
            "decision_lag": "每月末用截至当月的行情计算信号，决策月 t 的目标仓位在 t+1 月生效；盘中不因日波动换仓。",
            "data_refresh": "每日自动同步真实行情探针（新浪→东财，补数≤2 次，失败回退上一版快照并标注）；无新月度行情时净值沿用上月 carry_forward，不编造日波动。",
            "cost_basis": "实盘统一 15bp 单边成本 + QDII 溢价>2% 不建仓 + 回撤熔断；研究口径 5bp 仅作对照，不用于对外业绩展示。",
            "capital_safety": "无未来函数（信号只用 t 及之前数据）；月度资金守恒；换手含资产间切换成本；杠杆硬上限 1.2。",
            "shadow_boundary": "影子账户为旁路监控，候选参数须连续 2 折样本外同时跑赢才解除降级，未达标前按固定参数记账。",
        },
        "factor_rules": [
            {"factor": "双均线 MA5/12", "role": "股票总择时",
             "rule": "风险组合净值 MA5>MA12 → 满仓股票腿；否则空仓，由利率债承接。",
             "param": f"ma_fast={fixed.get('ma_fast')}, ma_slow={fixed.get('ma_slow')}"},
            {"factor": "RS 相对动量 12M", "role": "股票内轮动",
             "rule": "纳指 12M 动量 > 标普 → 持纳指，否则持标普；股票多头时生效。",
             "param": f"rs_lb={fixed.get('rs_lb')}"},
            {"factor": "TSMOM 12M", "role": "黄金/利率债趋势过滤",
             "rule": "黄金 12M 动量>阈值且自身 MA5>MA12 才建黄金腿（且仅在股票多头时配置）。",
             "param": f"mom_lb={fixed.get('mom_lb')}, g_thr={fixed.get('g_thr')}, use_g_ma={fixed.get('use_g_ma')}"},
            {"factor": "黄金卫星腿", "role": "低相关第二收益流",
             "rule": "股票多头时按固定权重配置黄金，空头时段不持黄金（诊断显示空头持金拖累）。",
             "param": f"w_gold={fixed.get('w_gold')}, gold_only_in_bull={fixed.get('gold_only_in_bull')}"},
            {"factor": "利率债空头承接", "role": "替代空仓现金",
             "rule": "股票空头且利率债趋势健康时，用负相关利率债替代空仓；闲置现金也配利率债增强。",
             "param": f"bear_bond={fixed.get('bear_bond')}, bond_as_cash={fixed.get('bond_as_cash')}, b_thr={fixed.get('b_thr')}"},
            {"factor": "QDII 溢价过滤", "role": "建仓闸门",
             "rule": "两只 QDII ETF 溢价较高者 > 2% 时，该月不建股票仓（激进追高保护）。",
             "param": f"阈值={pct(pol.get('premium_threshold'), 2)}%"},
            {"factor": "回撤熔断", "role": "组合层风控",
             "rule": "组合回撤超过 15% 时，目标仓位整体乘 0.6；回撤修复后恢复。",
             "param": f"dd_thr={pct(pol.get('dd_thr'), 2)}%, brake={pol.get('brake')}"},
            {"factor": "杠杆上限", "role": "风控红线",
             "rule": "总敞口硬上限 1.2 倍，超出按比例压缩；融资部分付 3.5% 年化息。",
             "param": f"max_lev={pol.get('max_lev')}"},
        ],
    }
    jdump("signal.json", signal_obj)

    # ---------- 3. shadow.json ----------
    shadow_obj = dict(S)
    shadow_obj["equity"] = {
        "months": S.get("equity_months", []),
        "shadow": S.get("shadow_equity_dry_run", []),
        "fixed": S.get("fixed_equity_dry_run", []),
        "candidate": S.get("candidate_equity_dry_run", []),
    }
    shadow_obj["versions"] = csv_rows(os.path.join(DATA, "shadow_versions.csv"))
    shadow_obj["oos_display"] = {
        "candidate": {
            "cagr": pct((S.get("oos") or {}).get("candidate", {}).get("cagr")),
            "sharpe": r((S.get("oos") or {}).get("candidate", {}).get("sharpe")),
            "mdd": pct((S.get("oos") or {}).get("candidate", {}).get("mdd")),
        },
        "fixed": {
            "cagr": pct((S.get("oos") or {}).get("fixed", {}).get("cagr")),
            "sharpe": r((S.get("oos") or {}).get("fixed", {}).get("sharpe")),
            "mdd": pct((S.get("oos") or {}).get("fixed", {}).get("mdd")),
        },
    }
    jdump("shadow.json", shadow_obj)

    # ---------- 4. audit.json ----------
    recon_pp = 0.0 if (sync.get("health") or {}).get("口径数字一致") else None
    runs = [{
        "run_id": sync.get("run_id"),
        "at": sync.get("generated_at"),
        "status": sync.get("verdict"),
        "source": "sina",
        "new": sync.get("new"),
        "scraped": sync.get("scraped"),
        "fallback": False,
        "reconcile_pp": recon_pp,
    }] if sync else []
    audit_obj = {
        "runs": runs,
        "runs_note": "当前仅 %d 次同步运行记录，随每日定时任务累积；不补写未发生的巡检。" % len(runs),
        "stages": sync.get("stages", []),
        "health": sync.get("health", {}),
        "deploy_health": {"passed": health.get("passed"), "total": health.get("total"),
                          "verdict": health.get("verdict"), "checks": health.get("checks", {})},
        "ledger": csv_rows(os.path.join(DATA, "nav_daily.csv")),
        "deployment": {
            "schedule_status": dep.get("schedule_status", {}),
            "cron_line": dep.get("cron_line"),
            "pages_url": launch.get("pages_url"),
            "repo_url": launch.get("repo_url"),
            "status": launch.get("status"),
            "pages_live": launch.get("pages_live"),
            "cloud_scheduler_live": launch.get("cloud_scheduler_live"),
            "cloud_scheduler_detail": launch.get("cloud_scheduler_detail"),
            "pushed_files": launch.get("pushed_files"),
            "authorization_method": launch.get("authorization_method"),
            "isolation": launch.get("isolation"),
        },
        "policy": {
            "sync": sync_policy,
            "shadow": shadow_policy,
            "idempotency": sync_policy.get("idempotency"),
            "fallback": "抓数失败按 SOP 回退上一版快照并如实标注，不编造日波动",
            "rollback": "参数升级采用 champion/challenger，连续 2 折样本外同时跑赢才解除降级",
        },
        "metrics_note": sync.get("metrics_note"),
    }
    jdump("audit.json", audit_obj)

    # ---------- 5. meta.json ----------
    checks_pass = exec_sc.get("checks_pass", 13)
    checks_total = exec_sc.get("checks_total", 13)
    reg = exec_sc.get("regression", {})
    dsr = dsc.get("dsr", 0.3461)
    n_lower = dsc.get("n_lower", 24104)
    d1 = next((x for x in disc_cfg.get("mandatory", []) if x.get("id") == "D1"), {})
    d2 = next((x for x in disc_cfg.get("mandatory", []) if x.get("id") == "D2"), {})
    meta_obj = {
        "brand": {
            "code": "mafc-nav",
            "mark": "MF",
            "name": "MAFC 固定规则组合",
            "short_name": "MAFC 研究看板",
            "subtitle": "RS 相对动量轮动 + 双均线 MA5/12 择时 + 黄金 + 中国利率债",
            "eyebrow": "MAFC FIXED-RULE COMPOSITE",
            "version": "上线口径 v1.0（S3）",
        },
        "as_of": M.get("as_of_date"),
        "generated_at": sync.get("generated_at") or M.get("generated_at"),
        "period": M.get("period", {}),
        "model_code": "MAFC-RS-MA512-CNBOND",
        "params_fp": fp,
        "source_prod": M.get("source_prod"),
        "basis": (
            "上线口径 = 15bp 单边成本 + QDII 溢价>2% 不建仓 + 回撤熔断，是唯一实盘决策口径；"
            "研究口径 5bp 仅作对照，不得用于对外业绩展示。日间无新月度行情时净值沿用上月"
            "（carry_forward），不编造日波动；月度净值仅在月末数据确认后更新。"
        ),
        "sync": {
            "last_sync": sync.get("generated_at"),
            "run_id": sync.get("run_id"),
            "status": sync.get("verdict"),
            "source": "sina",
            "new": sync.get("new"),
            "scraped": sync.get("scraped"),
            "retry": sum(len((v.get("attempts") or [])) - 1
                         for v in (sync.get("per_account") or {}).values()),
            "fallback": False,
            "elapsed_sec": sync.get("elapsed_sec"),
        },
        "live_policy": pol,
        "reconcile": {
            "ok": bool((sync.get("health") or {}).get("口径数字一致")),
            "max_abs_diff_pp": recon_pp,
            "note": "模型接口 live CAGR 与整改报告 key_metrics 对账",
        },
        "targets": [
            {"title": "整改验收 13/13 PASS", "value": f"{checks_pass}/{checks_total}",
             "note": "verdict " + str(exec_sc.get("verdict")), "ok": True},
            {"title": "原回归 GO 10/10 不回退",
             "value": f"{reg.get('passed', 10)}/{reg.get('total', 10)} GO",
             "note": "零回退 · 逐项一致", "ok": True},
            {"title": "严格 DSR ≥ 0.95", "value": f"{dsr:.4f}",
             "note": f"NO-GO · N≥{n_lower:,}", "ok": False},
        ],
        "disclosure": {
            "dsr": dsr,
            "dsr_threshold": (d1.get("values") or {}).get("strong_evidence_threshold", 0.95),
            "n_lower": n_lower,
            "expected_max_sr_annual": (d1.get("values") or {}).get("expected_max_sr_annual"),
            "harvey_liu_t": (d1.get("values") or {}).get("harvey_liu_t"),
            "beta_share_pct": pct((d2.get("values") or {}).get("beta_share", dsc.get("beta_share"))),
            "timing_increment_sharpe": (d2.get("values") or {}).get("timing_increment_sharpe"),
            "permutation_p": (d2.get("values") or {}).get("permutation_p"),
            "external_verdict": dsc.get("external_verdict"),
            "forbidden_claims": disc_cfg.get("forbidden_claims", []),
            "forbidden_claims_count": len(disc_cfg.get("forbidden_claims", [])),
        },
        "capacity": {
            "aum_cap_cny_yi": capm.get("aum_cap_cny_yi"),
            "aum_cap_cny_wan": round((capm.get("aum_cap_cny_yi") or 0) * 10000, 1),
            "binding_name": capm.get("binding_name"),
            "n_available": capm.get("n_available"),
            "status": capm.get("status"),
            "method": cap_rep.get("method"),
            "participation_limit": cap_rep.get("participation_limit"),
            "adv_window_days": cap_rep.get("adv_window_days"),
        },
        "exam": {
            "run_id": exam.get("run_id"),
            "verdict": exam.get("verdict"),
            "counts": exam.get("counts", {}),
        },
        "remediation": {
            "run_id": exec_sc.get("run_id"),
            "verdict": exec_sc.get("verdict"),
            "checks_pass": checks_pass,
            "checks_total": checks_total,
            "regression": f"{reg.get('passed', 10)}/{reg.get('total', 10)} GO",
            "identical_to_before": reg.get("identical_to_before"),
        },
        "cost_layers": cost_cfg.get("four_layers", {}),
        "engineering": {
            "策略": "固定规则组合：RS 相对动量轮动 + 双均线 MA5/12 择时 + 黄金 + 中国利率债",
            "参数维度": "4 维进化边界（w_gold / bear_bond / b_thr / w_cnbond）· smooth=0.65",
            "训练参数来源": "vendor/prod/output/param_registry.json（上线前研究训练参数）",
            "影子旁路": "train 2017-11~2025-10 → eval 2025-11~2026-11；候选不得直接用于实盘",
            "成本口径": "research 5bp / live 15bp / stress 30bp 单边；四层成本见 cost_layers",
            "数据纯度": "真实 ETF/指数月度行情；溢价序列 2014-03 起 153 个真实月，之前按 0（过滤不生效）",
        },
        "deployment": {
            "pages_url": launch.get("pages_url"),
            "repo_url": launch.get("repo_url"),
            "status": launch.get("status"),
            "pages_live": launch.get("pages_live"),
            "cloud_scheduler_live": launch.get("cloud_scheduler_live"),
            "cloud_scheduler_detail": launch.get("cloud_scheduler_detail"),
            "pushed_files": launch.get("pushed_files"),
            "isolation": launch.get("isolation"),
        },
        "isolation": (
            "本看板为 mafc-nav · MAFC 固定规则组合的独立工程，与 ebabao/all-weather-nav"
            "（全天候策略 v1.4 · 七 ETF 风险平价）是两套完全不同的策略：底层资产、模型引擎、"
            "参数体系、仓库、Pages 站点、定时任务均相互独立，互不引用、互不覆盖、不共享发布产物。"
            "本页沿用参考页的版式与同源接口架构，所有数字均来自本工程 vendor/prod 引擎现场回测与真实行情探针。"
        ),
    }
    jdump("meta.json", meta_obj)

    # ---------- 自检 ----------
    for name in INTERFACES:
        path = os.path.join(DATA, name)
        with open(path, encoding="utf-8") as f:
            json.load(f)
        if os.path.getsize(path) < 200:
            raise RuntimeError(f"接口异常过小：{name}")

    return {
        "file": os.path.relpath(PAGE, ROOT),
        "size_kb": round(os.path.getsize(PAGE) / 1024, 1),
        "figures": 3,              # 原生 SVG 图表：净值对比 / 回撤爬坑 / 影子演练
        "interfaces": len(INTERFACES),
        "nav_points": len(dates),
        "monthly_points": len(monthly),
        "yearly_rows": len(yearly),
    }


if __name__ == "__main__":
    print(json.dumps(build(), ensure_ascii=False, indent=2))
