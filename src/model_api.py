"""
策略模型接口层（mafc-nav → prod）
========================================
把「前一个工程」/workspace/quant_backtest/prod 的 MAFC 引擎封装成看板可消费的
标准化 JSON，不重写引擎、不复制数据、不改写历史。

唯一实盘口径：live 15bp + QDII 溢价>2% 不建仓 + 回撤熔断（execution.py）。
research 5bp 仅作对照。所有数字现场回测生成。

产出：data/model_state.json
运行：python3.11 src/model_api.py
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                       # /workspace/mafc-nav
WORKSPACE = os.path.dirname(ROOT)
POLICY = os.path.join(ROOT, "config", "sync_policy.json")
DEFAULT_PROD = os.path.join(WORKSPACE, "quant_backtest", "prod")


def _prod_dir() -> str:
    """prod 路径可由 config/sync_policy.json 覆盖，缺省按 workspace 推导（可移植）。"""
    if os.path.exists(POLICY):
        try:
            cfg = json.load(open(POLICY, encoding="utf-8"))
            p = cfg.get("prod_dir")
            if p:
                return p if os.path.isabs(p) else os.path.normpath(os.path.join(ROOT, p))
        except Exception:
            pass
    return DEFAULT_PROD


PROD = _prod_dir()
if not os.path.exists(os.path.join(PROD, "src", "engine.py")):
    raise FileNotFoundError(f"策略模型源工程不存在：{PROD}（禁止编造数据，接口层拒绝启动）")
sys.path.insert(0, os.path.join(PROD, "src"))
import engine as E          # noqa: E402
import execution as X       # noqa: E402

DATA = os.path.join(ROOT, "data")
OUT = os.path.join(PROD, "output")
CFG = os.path.join(PROD, "config")
os.makedirs(DATA, exist_ok=True)


def _j(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def state() -> dict:
    s, e = E.month_index("2007-11"), E.month_index("2026-11")

    # 研究口径（仅对照）
    p0 = X.live_params(enable_brake=False)
    with X.cost_context(5):
        r0 = E.run(s, e, p0, collect=True)

    # 上线口径（唯一实盘决策口径）
    p3 = X.live_params(enable_brake=True)
    prem, cov = X.premium_series()
    sig3, blocked = X.guard_signals(p3, prem)
    with X.cost_context(15):
        r3 = E.run(s, e, p3, signals=sig3, collect=True)

    m0 = E.metrics(r0["equity"], r0["exposure"], r0["turnover"])
    m3 = E.metrics(r3["equity"], r3["exposure"], r3["turnover"])
    snap = r3["snapshot"].iloc[-1]

    nav = [{"month": str(r3["months"][i]),
            "research": round(float(r0["equity"][i] / r0["equity"][0]), 6),
            "live": round(float(r3["equity"][i] / r3["equity"][0]), 6)}
           for i in range(len(r3["equity"]))]

    disc = _j(os.path.join(CFG, "disclosure.json"), {})
    cap = _j(os.path.join(OUT, "capacity_report.json"), {})
    exam = _j(os.path.join(OUT, "exam_results.json"), {})
    dsr = None
    n_lower = None
    beta = None
    for d in disc.get("mandatory", []):
        if d["id"] == "D1":
            dsr = d["values"]["DSR"]
            n_lower = d["values"]["N_lower"]
        if d["id"] == "D2":
            beta = d["values"]["beta_share"]

    out = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "as_of_date": datetime.now().strftime("%Y-%m-%d"),
        "model": "MAFC 固定规则组合（RS轮动+MA5/12+黄金+中国利率债）",
        "source_prod": os.path.relpath(PROD, WORKSPACE),
        "engine_cost_restored": E.COST == 0.0005,
        "period": {"start": str(r3["months"][0]), "end": str(r3["months"][-1]),
                   "n_months": int(m3["n_months"])},
        "live_policy": {"cost_bps_one_way": 15, "premium_threshold": X.PREMIUM_THRESHOLD,
                        "dd_thr": p3.dd_thr, "brake": p3.brake, "max_lev": p3.max_lev},
        "metrics": {
            "research": {k: round(float(v), 6) for k, v in m0.items()},
            "live": {k: round(float(v), 6) for k, v in m3.items()},
        },
        "latest_signal": {
            "decision_month": str(snap["month"]),
            "regime": int(snap["regime"]) if snap["regime"] is not None else None,
            "rs_pick": str(snap["rs_pick"]),
            "weights": str(snap["weights"]),
            "exposure": float(snap["exposure"]),
            "premium_pct": round(float(prem[e]) * 100, 4),
            "premium_blocked": bool(prem[e] > X.PREMIUM_THRESHOLD),
            "bear_bond_active": int(snap["bear_bond_active"]),
            "bond_cash": int(snap["bond_cash"]),
        },
        "premium_coverage": cov,
        "nav_series": nav,
        "disclosure": {"dsr": dsr, "n_lower": n_lower, "beta_share": beta,
                       "external_verdict": disc.get("external_verdict"),
                       "forbidden_claims": len(disc.get("forbidden_claims", []))},
        "capacity": {"aum_cap_cny_yi": cap.get("aum_cap_cny_yi"),
                     "binding_name": cap.get("binding_name"),
                     "n_available": cap.get("n_available"),
                     "status": cap.get("status")},
        "exam_summary": {"run_id": exam.get("run_id"),
                         "verdict": exam.get("summary", {}).get("verdict"),
                         "counts": exam.get("summary", {}).get("counts")},
    }
    with open(os.path.join(DATA, "model_state.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)
    return out


if __name__ == "__main__":
    d = state()
    print(f"模型接口 OK：{d['period']['start']}~{d['period']['end']} {d['period']['n_months']}月，"
          f"上线口径 CAGR {d['metrics']['live']['cagr']*100:.2f}% / Sharpe {d['metrics']['live']['sharpe']:.3f}，"
          f"净值点 {len(d['nav_series'])}，COST恢复={d['engine_cost_restored']}")
