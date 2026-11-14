"""
影子账户进化学习（旁路监控，绝不覆盖上线版固定参数）
=====================================================
用户要求：「影子账户运行要进行进化学习，按上线前的研究训练参数」。

衔接刚验收的整改结论（重要）：
  · 上线版 = 固定参数，ML 受限校准在 WF 样本外跑输并自动降级；
  · param_registry.json 顶层 degraded=true，6 折中 5 折已降级；
  · 体检关3 DSR=0.346（试得太多），关4 固定参数优于优化版。
因此进化学习的定位是「旁路监控」：
  1) 训练参数体系完全继承上线前研究：4 维 BOUNDS / SMOOTH=0.65 / 种子 1000+k /
     degrade_patience=2 / 温启动，直接复用 prod/src/calibrate.py，不重写；
  2) 初始状态继承研究的 degraded=true —— 影子账户从固定参数起步；
  3) 每次同步仍真实训练一折候选参数，并在严格样本外检验；
  4) 只有候选参数连续 2 折在样本外同时跑赢固定版的 CAGR 与 Sharpe，才可能解除降级；
  5) 影子账户净值独立记账，上线版固定参数始终不动。

诚实约束：
  · 训练沿用研究口径 5bp（与 param_registry 可比）；验证与影子净值用上线口径
    15bp + 溢价过滤 + 熔断（更严格的样本外检验）。
  · 当前影子账户实盘观察月数 = 0，所有净值均为历史回灌演练（dry_run），
    不编造实盘记录；无新样本时不产生新版本，如实标注 NO_NEW_SAMPLE。

产出：data/shadow_state.json、data/shadow_versions.csv
运行：python3.11 src/shadow_evolution.py
"""
from __future__ import annotations

import csv
import json
import os
import sys
from datetime import datetime

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WORKSPACE = os.path.dirname(ROOT)
POLICY = os.path.join(ROOT, "config", "sync_policy.json")


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
sys.path.insert(0, os.path.join(PROD, "src"))
import engine as E            # noqa: E402
import execution as X         # noqa: E402
import calibrate as C         # noqa: E402

DATA = os.path.join(ROOT, "data")
os.makedirs(DATA, exist_ok=True)
REG = os.path.join(PROD, "output", "param_registry.json")
STATE = os.path.join(DATA, "shadow_state.json")
VERSIONS = os.path.join(DATA, "shadow_versions.csv")
VCOLS = ["version", "as_of", "train", "eval", "seed", "fitness", "gene",
         "oos_cagr_cand", "oos_sharpe_cand", "oos_cagr_fixed", "oos_sharpe_fixed",
         "win_streak", "degraded", "effective", "sample_type", "note"]


def _read_versions():
    if not os.path.exists(VERSIONS):
        return []
    with open(VERSIONS, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _month(ym: str) -> int:
    return E.month_index(ym)


def evolve(as_of: str | None = None, new_samples: int = 0) -> dict:
    """执行一次影子账户进化学习。new_samples>0 才允许产生新版本。"""
    as_of = as_of or datetime.now().strftime("%Y-%m-%d")
    if not os.path.exists(REG):
        raise FileNotFoundError(f"缺上线前研究训练参数：{REG}（禁止编造）")
    reg = json.load(open(REG, encoding="utf-8"))
    versions = reg["versions"]
    bounds = reg["bounds"]
    smooth = float(reg["smooth"])
    inherited_degraded = bool(reg.get("degraded", True))
    seed = 1000 + len(versions)                  # 研究种子体系延续：v01..v06 -> 1006
    prev_gene = np.array(versions[-1]["gene_smoothed"], float)
    last = versions[-1]
    tr_s, tr_e = last["train"].split("~")
    ev_s, _ = last["test"].split("~")

    hist = _read_versions()
    if hist and new_samples <= 0:
        prev = json.load(open(STATE, encoding="utf-8")) if os.path.exists(STATE) else {}
        if prev.get("as_of") == as_of:
            # 同一天重复同步：完全幂等，连影子状态都不改写（部署幂等复验依赖此语义）
            return prev
        out = dict(prev)
        out.update({"as_of": as_of, "status": "NO_NEW_SAMPLE", "reused": True,
                    "note": "影子账户无新增实盘观察样本，本日不产生新参数版本；"
                            "沿用上一版本并复核，不编造进化结果"})
        with open(STATE, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2, default=str)
        return out

    # --- 训练窗继承研究最后一折；验证窗扩展到当前数据末端（增量） ---
    a, b = _month(tr_s), _month(tr_e)
    ev_a, ev_b = _month(ev_s), E.month_index("2026-11")
    wins = C.subwindows(a, b)
    x_opt, sc, _ = C.ga(wins, seed=seed, x0=prev_gene)
    x_used = smooth * prev_gene + (1 - smooth) * x_opt
    p_cand = C.decode(x_used)
    p_cand.dd_thr, p_cand.brake = 0.15, 0.60        # 执行层约束与上线版对齐
    p_fixed = X.live_params(enable_brake=True)       # 上线版固定参数（含熔断）

    # --- 严格样本外检验：上线口径 15bp + 溢价硬过滤 + 熔断 ---
    prem, _ = X.premium_series()
    sig_c, _ = X.guard_signals(p_cand, prem)
    sig_f, _ = X.guard_signals(p_fixed, prem)
    with X.cost_context(15):
        rc = E.run(ev_a - 1, ev_b, p_cand, signals=sig_c)
        rf = E.run(ev_a - 1, ev_b, p_fixed, signals=sig_f)
    mc = E.metrics(rc["equity"], rc["exposure"], rc["turnover"])
    mf = E.metrics(rf["equity"], rf["exposure"], rf["turnover"])

    win = bool(mc["sharpe"] > mf["sharpe"] and mc["cagr"] > mf["cagr"])
    prev_streak = int(hist[-1]["win_streak"]) if hist else 0
    win_streak = prev_streak + 1 if win else 0
    degraded = bool(inherited_degraded and win_streak < 2)
    effective = "fixed" if degraded else "candidate"
    p_eff = p_fixed if degraded else p_cand

    # 影子账户净值（历史回灌演练，非实盘记录）
    sig_e, _ = X.guard_signals(p_eff, prem)
    with X.cost_context(15):
        re = E.run(ev_a - 1, ev_b, p_eff, signals=sig_e)
    eq_shadow = (re["equity"] / re["equity"][0]).tolist()
    eq_fixed = (rf["equity"] / rf["equity"][0]).tolist()
    eq_cand = (rc["equity"] / rc["equity"][0]).tolist()

    ver = f"s{len(hist)+1:03d}"
    row = {
        "version": ver, "as_of": as_of,
        "train": f"{tr_s}~{tr_e}", "eval": f"{ev_s}~2026-11",
        "seed": seed, "fitness": round(float(sc), 3),
        "gene": ";".join(f"{z:.4f}" for z in x_used),
        "oos_cagr_cand": round(float(mc["cagr"]), 6),
        "oos_sharpe_cand": round(float(mc["sharpe"]), 6),
        "oos_cagr_fixed": round(float(mf["cagr"]), 6),
        "oos_sharpe_fixed": round(float(mf["sharpe"]), 6),
        "win_streak": win_streak, "degraded": int(degraded),
        "effective": effective, "sample_type": "historical_dry_run",
        "note": "训练参数继承上线前研究；当前实盘观察月数=0，净值为历史回灌演练"
                + ("；候选本折跑赢，但需连续 2 折才解除降级，本次仍按固定参数记账"
                   if (degraded and win) else
                   ("；候选跑输，维持降级" if degraded else "；连续 2 折跑赢，解除降级")),
    }
    write_header = not os.path.exists(VERSIONS)
    with open(VERSIONS, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=VCOLS)
        if write_header:
            w.writeheader()
        w.writerow(row)

    out = {
        "as_of": as_of, "status": "EVOLVED", "reused": False,
        "version": ver, "seed": seed,
        "training_params_source": os.path.relpath(REG, WORKSPACE),
        "inherited_config": {"dim": C.DIM, "bounds": bounds, "smooth": smooth,
                             "degrade_patience": 2, "warm_start": True,
                             "train_cost_bps": 5, "eval_cost_bps": 15,
                             "inherited_degraded": inherited_degraded},
        "train_window": row["train"], "eval_window": row["eval"],
        "candidate_params": p_cand.to_dict(), "gene_smoothed": [round(float(z), 4) for z in x_used],
        "oos": {"candidate": {k: round(float(v), 6) for k, v in mc.items()},
                "fixed": {k: round(float(v), 6) for k, v in mf.items()}},
        "win": win, "win_streak": win_streak, "degraded": degraded,
        "effective_params": effective,
        "effective_params_detail": p_eff.to_dict(),
        "shadow_equity_dry_run": [round(float(z), 6) for z in eq_shadow],
        "fixed_equity_dry_run": [round(float(z), 6) for z in eq_fixed],
        "candidate_equity_dry_run": [round(float(z), 6) for z in eq_cand],
        "equity_months": [str(x) for x in rf["months"]],
        "single_fold_win": win,
        "degrade_rule_note": "解除降级需连续 2 折样本外同时跑赢 CAGR 与 Sharpe（研究阶段 "
                             "degrade_patience=2 的延续）；单折跑赢不足以解除，"
                             "因此即使本折候选跑赢，影子账户仍按固定参数记账。",
        "live_observation_months": 0,
        "sample_type": "historical_dry_run",
        "boundary": "进化学习为旁路监控：上线版固定参数始终不变；候选参数不得直接用于实盘；"
                    "解除降级需连续 2 折样本外同时跑赢 CAGR 与 Sharpe。"
                    "严格 DSR 口径仍为 NO-GO，进化不提高统计证据强度。",
        "note": row["note"],
    }
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)
    return out


if __name__ == "__main__":
    d = evolve()
    print(f"影子进化 {d['status']} 版本 {d.get('version')} 降级={d.get('degraded')} "
          f"有效参数={d.get('effective_params')} 实盘观察月数={d.get('live_observation_months')}")
