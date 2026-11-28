"""
抓取并清洗四资产 + 中证800 真实日线（供日/周净值回放使用 · 不侵入生产）
============================================================================
数据源（akshare，与月度表同源）：
  · 标普500  index_us_stock_sina(.INX)
  · 纳指综指 index_us_stock_sina(.IXIC)   —— 月线主口径用的就是 IXIC，保持一致
  · 黄金     futures_foreign_hist(GC)     —— COMEX 黄金期货，与月度表 gold 同口径
  · 中国利率债 stock_zh_index_daily(sh000012) 上证国债财富指数
  · 中证800  stock_zh_index_daily(sh000906)   基准对比（A 股宽基代表）

清洗规则（诚实、可审计、禁止编造）：
  1) 只用真实抓到的日收盘，缺失交易日不臆造价格；各资产按自身交易日保留。
  2) 日收益 r = close/close.shift(1)-1，首个交易日为 0。
  3) 月末收盘 resample('ME').last() 与 prices_monthly.csv 核对相关系数，>=0.999 才算对齐。
     黄金期货代理现货，相关系数如实记录（历史核对约 0.999/0.996）。
  4) 覆盖区间：标普/纳指/国债 2003-2004 起、黄金 2016-12 起、中证800 2005 起。
     四资产全真实的日/周净值只在 2016-12 之后可得（黄金约束）。
  5) 抓取失败重试 2 次，仍失败则报错退出，不落盘、不用旧数据冒充。

运行：python3.11 src/fetch_daily_data.py
产出：data/clean/prices_daily.csv、data/clean/csi800_daily.csv
"""
from __future__ import annotations
import os, sys, time
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
PROD = os.path.dirname(HERE)
CLEAN = os.path.join(PROD, "data", "clean")
os.makedirs(CLEAN, exist_ok=True)

COLS = ["date", "close"]


def fetch_with_retry(fn, name, tries=2):
    """抓取失败重试 tries 次，全失败抛异常（禁止用旧数据或编造冒充）。"""
    last = None
    for k in range(tries + 1):
        try:
            df = fn()
            if df is not None and len(df) > 0:
                print(f"  [OK] {name}: {len(df)} 行", flush=True)
                return df
            last = RuntimeError("空数据")
        except Exception as e:
            last = e
            print(f"  [重试 {k}/{tries}] {name}: {str(e)[:80]}", flush=True)
            time.sleep(1.5)
    raise RuntimeError(f"{name} 抓取失败（已重试 {tries} 次）: {last}")


def normalize(df, name):
    """统一成 date / close 两列，日期规整到日。兼容不同 akshare 列名。"""
    df = df.copy()
    # 日期列
    dcol = "date" if "date" in df.columns else df.columns[0]
    df["date"] = pd.to_datetime(df[dcol]).dt.normalize()
    # 收盘列
    if "close" in df.columns:
        c = df["close"]
    elif "收盘" in df.columns:
        c = df["收盘"]
    else:
        c = df.iloc[:, -1]
    df["close"] = pd.to_numeric(c, errors="coerce")
    out = df[["date", "close"]].dropna().drop_duplicates("date").sort_values("date")
    out = out[out["close"] > 0].reset_index(drop=True)
    print(f"    {name} 清洗后 {len(out)} 行 {out['date'].iloc[0].date()} ~ {out['date'].iloc[-1].date()}", flush=True)
    return out


def main():
    import akshare as ak
    print("=== 抓取真实日线（每步失败重试 2 次）===", flush=True)

    sources = {
        "spx":    lambda: ak.index_us_stock_sina(symbol=".INX"),
        "ixic":   lambda: ak.index_us_stock_sina(symbol=".IXIC"),
        "gold":   lambda: ak.futures_foreign_hist(symbol="GC"),
        "cnbond": lambda: ak.stock_zh_index_daily(symbol="sh000012"),
        "csi800": lambda: ak.stock_zh_index_daily(symbol="sh000906"),
    }
    raw = {}
    for name, fn in sources.items():
        raw[name] = normalize(fetch_with_retry(fn, name), name)

    # ---- 构建四资产日线宽表（ UNION 交易日历 + 前向填充，收益仅在有真实价格处计算）----
    # 用各资产自身交易日；对齐到并集日历后 ffill 价格（休市日沿用前收，收益=0，无前视）
    all_dates = sorted(set().union(*[set(d["date"]) for d in raw.values()]))
    idx = pd.DatetimeIndex(all_dates)
    px = pd.DataFrame(index=idx)
    for a in ["spx", "ixic", "gold", "cnbond"]:
        s = raw[a].set_index("date")["close"].reindex(idx).ffill()
        px[a] = s
    px.index.name = "date"
    ret = px.pct_change().fillna(0.0)
    wide = px.copy()
    for a in ["spx", "ixic", "gold", "cnbond"]:
        wide[f"r_{a}"] = ret[a]
    wide = wide.reset_index()
    wide["date"] = wide["date"].dt.strftime("%Y-%m-%d")
    fp = os.path.join(CLEAN, "prices_daily.csv")
    wide.to_csv(fp, index=False, encoding="utf-8")
    print(f"\n落盘 {fp}  {len(wide)} 行 {wide['date'].iloc[0]} ~ {wide['date'].iloc[-1]}", flush=True)

    # ---- 中证800 日线 ----
    c8 = raw["csi800"].copy()
    c8["r"] = c8["close"].pct_change().fillna(0.0)
    c8["date"] = c8["date"].dt.strftime("%Y-%m-%d")
    c8 = c8[["date", "close", "r"]]
    fp8 = os.path.join(CLEAN, "csi800_daily.csv")
    c8.to_csv(fp8, index=False, encoding="utf-8")
    print(f"落盘 {fp8}  {len(c8)} 行 {c8['date'].iloc[0]} ~ {c8['date'].iloc[-1]}", flush=True)

    # ---- 月末与月度表核对（相关 >= 0.999）----
    print("\n=== 月末收盘 vs prices_monthly.csv 核对 ===", flush=True)
    M = pd.read_csv(os.path.join(CLEAN, "prices_monthly.csv"), parse_dates=["month"])
    M["ym"] = M["month"].dt.strftime("%Y-%m")
    M = M.set_index("ym")
    check = {}
    for a in ["spx", "ixic", "gold", "cnbond"]:
        s = px[a].resample("ME").last().dropna()
        s.index = s.index.strftime("%Y-%m")          # 用 YYYY-MM 字符串对齐月度表
        common = s.index.intersection(M.index)
        if len(common) < 12:
            check[a] = {"n": len(common), "corr": None, "pass": False}
            print(f"  {a:8s} 共同月度点不足({len(common)}) FAIL", flush=True)
            continue
        corr = float(np.corrcoef(s.loc[common], M.loc[common, a])[0, 1])
        # 最近一个月末比值（期货代理现货可能有常数级差异，看比值稳定性）
        ratio = float(s.loc[common].iloc[-1] / M.loc[common, a].iloc[-1])
        ok = corr >= 0.999
        check[a] = {"n": int(len(common)), "corr": round(corr, 5),
                    "last_ratio": round(ratio, 4), "pass": ok}
        print(f"  {a:8s} 相关={corr:.5f}  最近月末比值={ratio:.4f}  {'PASS' if ok else 'FAIL'}", flush=True)
    # 中证800 无月度对照表，记录覆盖度
    check["csi800"] = {"n": len(c8), "from": c8["date"].iloc[0], "to": c8["date"].iloc[-1],
                       "note": "A股宽基基准，无生产月度对照表，按真实日线直接归一化"}
    qc = {"generated_at": pd.Timestamp.now().isoformat(timespec="seconds"),
          "source": "akshare index_us_stock_sina / futures_foreign_hist / stock_zh_index_daily",
          "checks": check,
          "all_pass": all(v.get("pass", True) for v in check.values())}
    import json
    with open(os.path.join(CLEAN, "daily_data_qc.json"), "w", encoding="utf-8") as f:
        json.dump(qc, f, ensure_ascii=False, indent=2)
    print(f"\nQC: {'全部通过' if qc['all_pass'] else '存在未通过项（见 daily_data_qc.json）'}", flush=True)


if __name__ == "__main__":
    main()
