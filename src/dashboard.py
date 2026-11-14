"""
mafc-nav 看板页面生成器
==============================
读取每日同步产出的真实数据（model_state / shadow_state / nav_daily / deploy_report），
所有图表现场绘制，产出单文件自包含中文 HTML。

产出：dashboard/index.html
运行：python3.11 src/dashboard.py
"""
from __future__ import annotations

import base64
import io
import json
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager as fm
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA = os.path.join(ROOT, "data")
OUT = os.path.join(ROOT, "output")
DASH = os.path.join(ROOT, "dashboard")
os.makedirs(DASH, exist_ok=True)

import glob
# 多路径探测：本地沙箱 / GitHub Actions ubuntu runner（fonts-noto-cjk）/ 常见 Linux 发行版
_CJK_CANDIDATES = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
]
for pat in ("/usr/share/fonts/**/*CJK*.*tf", "/usr/share/fonts/**/*CJK*.ttc",
            "/usr/share/fonts/**/*wqy*.*", "/usr/share/fonts/**/*SimHei*.*"):
    _CJK_CANDIDATES.extend(sorted(glob.glob(pat, recursive=True)))
_registered = False
for _p in _CJK_CANDIDATES:
    if os.path.exists(_p) and not _registered:
        try:
            fm.fontManager.addfont(_p)
            _registered = True
        except Exception:
            pass
_names = [f.name for f in fm.fontManager.ttflist
          if any(k in f.name for k in ("CJK", "WenQuanYi", "SimHei", "Hei"))]
plt.rcParams["font.sans-serif"] = (_names[:6] or ["Noto Sans CJK SC"]) + ["DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False
plt.rcParams["figure.dpi"] = 110
C = {"ink": "#2c3e50", "blue": "#2980b9", "red": "#c0392b", "amber": "#d4ac0d",
     "green": "#27ae60", "grey": "#7f8c8d", "grid": "#e8ecef", "teal": "#16a085"}


def jload(p, default=None):
    if not os.path.exists(p):
        return default
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def fig64(fig):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode()


def img(b64, cap):
    return f'<figure><img src="data:image/png;base64,{b64}" alt="{cap}"/><figcaption>{cap}</figcaption></figure>'


def pc(x, d=2):
    return "—" if x is None else f"{x * 100:.{d}f}%"


def n3(x):
    return "—" if x is None else f"{x:.3f}"


def badge(s):
    cls = {"OK": "go", "PASS": "go", "GO": "go", "EVOLVED": "go",
           "WARN": "warn", "NO_NEW_SAMPLE": "warn", "PENDING": "warn",
           "DEGRADED": "warn", "FAIL": "nogo", "NO-GO": "nogo"}.get(str(s), "warn")
    return f'<span class="pill {cls}">{s}</span>'


def tbl(headers, rows):
    th = "".join(f"<th>{h}</th>" for h in headers)
    trs = []
    for r in rows:
        tds = "".join(c if str(c).startswith("<td") else f"<td>{c}</td>" for c in r)
        trs.append(f"<tr>{tds}</tr>")
    return f'<table class="tbl"><thead><tr>{th}</tr></thead><tbody>{"".join(trs)}</tbody></table>'


def lcell(s):
    return f'<td class="l">{s}</td>'


def f_nav(M):
    nav = M["nav_series"]
    x = np.arange(len(nav))
    y0 = [z["research"] for z in nav]
    y1 = [z["live"] for z in nav]
    fig, ax = plt.subplots(figsize=(11, 4.2))
    ax.plot(x, y0, color=C["grey"], lw=1.9, label="研究口径（5bp，仅对照）")
    ax.plot(x, y1, color=C["red"], lw=2.1, label="上线口径（15bp+溢价过滤+熔断）")
    ax.set_yscale("log")
    ax.set_ylabel("净值（起点=1，对数轴）")
    ax.set_xlabel("月份序号")
    step = max(1, len(x) // 10)
    ax.set_xticks(x[::step])
    ax.set_xticklabels([nav[i]["month"] for i in x[::step]], rotation=40, ha="right", fontsize=8.5)
    ax.set_title("净值曲线：研究口径 vs 唯一实盘口径", fontsize=13, fontweight="bold")
    ax.legend(frameon=False, fontsize=10, loc="upper left")
    ax.grid(color=C["grid"], lw=.8)
    ax.set_axisbelow(True)
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    fig.tight_layout()
    return fig64(fig)


def f_shadow(S):
    ms = S.get("equity_months", [])
    series = [("candidate_equity_dry_run", "候选参数（旁路观察）", C["amber"]),
              ("fixed_equity_dry_run", "上线固定参数", C["blue"]),
              ("shadow_equity_dry_run", "影子账户有效净值", C["red"])]
    fig, ax = plt.subplots(figsize=(11, 4.0))
    for key, lab, col in series:
        v = S.get(key) or []
        if not v:
            continue
        ax.plot(range(len(v)), v, color=col, lw=2.0 if key != "candidate_equity_dry_run" else 1.7,
                ls="--" if key == "candidate_equity_dry_run" else "-", label=lab)
    ax.set_xticks(range(len(ms)))
    ax.set_xticklabels(ms, rotation=40, ha="right", fontsize=8.5)
    ax.set_ylabel("净值（验证窗，起点=1）")
    ax.set_title("影子账户进化学习 · 历史回灌演练（非实盘记录）", fontsize=13, fontweight="bold")
    ax.legend(frameon=False, fontsize=9.5, loc="upper left")
    ax.grid(color=C["grid"], lw=.8)
    ax.set_axisbelow(True)
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    fig.tight_layout()
    return fig64(fig)


def f_weights(M):
    ws = M["latest_signal"]["weights"]
    pairs = []
    for seg in str(ws).split(";"):
        if ":" in seg:
            k, v = seg.split(":", 1)
            pairs.append((k, float(v)))
    cn = {"spx": "标普500", "ixic": "纳斯达克", "gold": "黄金", "cnbond": "中国利率债"}
    fig, ax = plt.subplots(figsize=(11, 2.7))
    if pairs:
        names = [cn.get(k, k) for k, _ in pairs]
        vals = [v for _, v in pairs]
        bars = ax.barh(range(len(pairs)), vals, color=[C["red"], C["blue"], C["amber"], C["green"]][:len(pairs)], alpha=.88)
        for b, v in zip(bars, vals):
            ax.text(v + .01, b.get_y() + b.get_height() / 2, f"{v:.3f}", va="center", fontsize=10)
        ax.set_yticks(range(len(pairs)))
        ax.set_yticklabels(names, fontsize=10)
        ax.set_xlim(0, max(vals) * 1.25 + .1)
    else:
        ax.text(.5, .5, "当前空仓（由利率债 / 现金承接）", ha="center", va="center", fontsize=12)
        ax.set_xticks([]); ax.set_yticks([])
    ax.set_xlabel("目标权重")
    ax.set_title(f"最新决策月 {M['latest_signal']['decision_month']} 的持仓权重", fontsize=13, fontweight="bold")
    ax.grid(axis="x", color=C["grid"], lw=.8)
    ax.set_axisbelow(True)
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    fig.tight_layout()
    return fig64(fig)


def build() -> dict:
    M = jload(os.path.join(DATA, "model_state.json"), {})
    S = jload(os.path.join(DATA, "shadow_state.json"), {})
    DR = jload(os.path.join(OUT, "deploy_report.json"))
    L = pd.read_csv(os.path.join(DATA, "nav_daily.csv"), dtype={"date": str}) if os.path.exists(
        os.path.join(DATA, "nav_daily.csv")) else pd.DataFrame()
    last = L.iloc[-1].to_dict() if len(L) else {}
    SYN = jload(os.path.join(OUT, "sync_run.json"), {})

    F1, F2, F3 = f_nav(M), f_shadow(S), f_weights(M)
    live = M.get("metrics", {}).get("live", {})
    sig = M.get("latest_signal", {})
    disc = M.get("disclosure", {})
    cap = M.get("capacity", {})

    CSS = """
:root{--ink:#2c3e50;--sub:#5d6d7e;--line:#e8ecef;--bg:#f7f9fa;--card:#fff;--red:#c0392b;--blue:#2980b9;--amber:#d4ac0d;--green:#27ae60}
*{box-sizing:border-box}body{margin:0;font-family:"Noto Sans CJK SC","Microsoft YaHei",-apple-system,sans-serif;color:var(--ink);background:var(--bg);line-height:1.75}
.wrap{max-width:1120px;margin:0 auto;padding:36px 26px 80px}
header{background:linear-gradient(135deg,#1a2a3a,#2c3e50);color:#fff;border-radius:14px;padding:32px 38px;margin-bottom:24px}
header h1{margin:0 0 8px;font-size:27px}header .sub{color:#bdc9d4;font-size:14px}
header .tags{margin-top:14px}header .tag{display:inline-block;background:rgba(255,255,255,.13);border-radius:20px;padding:4px 13px;font-size:12px;margin:3px 6px 3px 0}
h2{font-size:20px;border-left:4px solid var(--red);padding-left:12px;margin:34px 0 14px}
h3{font-size:16px;margin:22px 0 10px;color:var(--blue)}p{margin:9px 0;font-size:14px}.muted{color:var(--sub);font-size:13px}
.card{background:var(--card);border:1px solid var(--line);border-radius:11px;padding:18px 22px;margin:14px 0;box-shadow:0 1px 3px rgba(0,0,0,.03)}
.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:16px 0}
.cards .card{margin:0;text-align:center;padding:16px 8px}.cards .k{font-size:12px;color:var(--sub)}
.cards .v{font-size:22px;font-weight:700;margin:5px 0}.cards .n{font-size:11px;color:var(--sub)}
figure{margin:18px 0;text-align:center}figure img{max-width:100%;border:1px solid var(--line);border-radius:8px}
figcaption{font-size:12px;color:var(--sub);margin-top:8px;text-align:left;padding:0 6px}
table.tbl{border-collapse:collapse;width:100%;font-size:12.5px;margin:8px 0}table.tbl th,table.tbl td{border:1px solid var(--line);padding:7px 9px;text-align:right}
table.tbl th{background:#eef2f5;font-weight:600;text-align:center;color:var(--ink)}
table.tbl td:first-child,table.tbl td:nth-child(2){text-align:left}table.tbl td.l{text-align:left}
.callout{background:#fff8e6;border-left:4px solid var(--amber);padding:13px 18px;border-radius:6px;margin:14px 0;font-size:13.5px}
.good{background:#eafaf1;border-left:4px solid var(--green);padding:13px 18px;border-radius:6px;margin:14px 0;font-size:13.5px}
.bad{background:#fdecea;border-left:4px solid var(--red);padding:13px 18px;border-radius:6px;margin:14px 0;font-size:13.5px}
.info{background:#eaf3fa;border-left:4px solid var(--blue);padding:13px 18px;border-radius:6px;margin:14px 0;font-size:13.5px}
code{background:#eef2f5;padding:1px 6px;border-radius:4px;font-size:12.5px}
.pill{display:inline-block;padding:2px 9px;border-radius:12px;font-size:11px;font-weight:600}
.pill.go{background:#eafaf1;color:#1e8449}.pill.warn{background:#fff4d6;color:#a67c00}.pill.nogo{background:#fdecea;color:#c0392b}
ul{margin:8px 0 8px 20px;padding:0;font-size:13.5px}li{margin:5px 0}
footer{margin-top:42px;padding-top:18px;border-top:1px solid var(--line);color:var(--sub);font-size:12px}
@media(max-width:760px){.cards{grid-template-columns:repeat(2,1fr)}}
"""

    run_rows = [
        ["run_id", lcell(str(last.get("run_id", SYN.get("run_id", "—"))))],
        ["同步日期", lcell(str(last.get("date", "—")))],
        ["scraped（探针成功标的）", f"{last.get('scraped', SYN.get('scraped', '—'))}/4"],
        ["new（新增月度净值点）", f"{last.get('new_monthly_points', SYN.get('new', '—'))}"],
        ["md（维护数据/看板文件）", str(SYN.get("md", "—"))],
        ["健康检查", badge("OK" if all(SYN.get("health", {}).values()) else "DEGRADED") if SYN.get("health") else "—"],
        ["净值来源", lcell(str(last.get("nav_source", "—")))],
    ]
    pa = SYN.get("per_account", {})
    pa_rows = [[k, lcell(v.get("last_trade_date") or "unavailable"),
                badge("OK" if v.get("available") else "WARN"), lcell(v.get("source") or "—")]
               for k, v in pa.items()]

    cand = S.get("oos", {}).get("candidate", {})
    fixed = S.get("oos", {}).get("fixed", {})
    sh_rows = [
        ["版本 / 状态", lcell(f"{S.get('version','—')} / {S.get('status','—')}")],
        ["训练参数来源", lcell("vendor/prod/output/param_registry.json（上线前研究训练参数）")],
        ["维度 / 平滑 / 种子", lcell(f"{S.get('inherited_config',{}).get('dim','—')} 维 / "
                                f"smooth={S.get('inherited_config',{}).get('smooth','—')} / "
                                f"seed={S.get('seed','—')}")],
        ["训练窗 → 验证窗", lcell(f"{S.get('train_window','—')} → {S.get('eval_window','—')}")],
        ["候选 样本外 CAGR / Sharpe", f"{pc(cand.get('cagr'))} / {n3(cand.get('sharpe'))}"],
        ["固定版 样本外 CAGR / Sharpe", f"{pc(fixed.get('cagr'))} / {n3(fixed.get('sharpe'))}"],
        ["本折是否跑赢", badge("OK" if S.get("single_fold_win") else "WARN")],
        ["连续跑赢折数 / 降级状态", f"{S.get('win_streak','—')} / {badge('DEGRADED' if S.get('degraded') else 'OK')}"],
        ["影子账户有效参数", lcell(str(S.get("effective_params", "—")))],
        ["实盘观察月数", f"{S.get('live_observation_months', 0)} 月（演练数据，非实盘）"],
    ]
    disc_rows = [
        ["DSR（强证据门槛 0.95）", n3(disc.get("dsr"))],
        ["试验次数下界 N", f"{disc.get('n_lower'):,}" if disc.get("n_lower") else "—"],
        ["收益中 beta 占比", pc(disc.get("beta_share"), 1)],
        ["对外结论", lcell(str(disc.get("external_verdict", "—")))],
        ["禁用表述条数", str(disc.get("forbidden_claims", "—"))],
    ]
    cap_rows = [
        ["1% ADV 最大 AUM", f"{cap.get('aum_cap_cny_yi')} 亿元" if cap.get("aum_cap_cny_yi") is not None else "—"],
        ["容量瓶颈", lcell(str(cap.get("binding_name", "—")))],
        ["真实 ADV 标的数", f"{cap.get('n_available','—')}/4"],
    ]
    recent = L.tail(7).to_dict("records") if len(L) else []
    led_rows = [[str(r.get("date")), f"{float(r.get('nav_live',0)):.4f}",
                 lcell(str(r.get("nav_source", ""))), lcell(str(r.get("shadow_version", ""))),
                 badge("OK" if str(r.get("shadow_status")) in ("EVOLVED", "NO_NEW_SAMPLE") else "WARN")]
                for r in recent]

    if DR:
        dep_rows = [[k, lcell(str(v))] for k, v in DR.get("schedule_status", {}).items()]
        dep_note = DR.get("verdict", "—")
    else:
        dep_rows = [["状态", lcell("部署报告尚未生成（首次同步中）")]]
        dep_note = "PENDING"

    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>mafc-nav · MAFC 固定规则组合净值看板</title><style>{CSS}</style></head><body><div class="wrap">
<header>
  <h1>mafc-nav · MAFC 固定规则组合净值看板</h1>
  <div class="sub">策略模型接口与看板数据每日自动同步 ｜ 数据源：MAFC 生产工程（RS轮动 + MA5/12 + 黄金 + 中国利率债）</div>
  <div class="tags">
    <span class="tag">同步 {last.get('date', SYN.get('as_of','—'))}</span>
    <span class="tag">run_id {last.get('run_id', SYN.get('run_id','—'))}</span>
    <span class="tag">区间 {M.get('period',{}).get('start','—')}~{M.get('period',{}).get('end','—')}</span>
    <span class="tag">影子 {S.get('version','—')} {S.get('status','—')}</span>
    <span class="tag">部署 {dep_note}</span>
  </div>
</header>

<div class="info"><b>工程边界（重要）：</b>本看板为 <b>mafc-nav · MAFC 固定规则组合</b>（RS 相对动量轮动 + 双均线 MA5/12 择时 + 黄金 + 中国利率债）的独立工程，
与 <code>ebabao/all-weather-nav</code>（全天候策略 v1.4 · 七 ETF 风险平价）是<b>两套完全不同的策略</b>：
底层资产、模型引擎、参数体系、仓库、Pages 站点、定时任务均相互独立，互不引用、互不覆盖、不共享发布产物。
本页所有数字均来自本工程 <code>vendor/prod</code> 引擎现场回测与真实行情探针，与全天候策略无关。</div>

<div class="cards">
  <div class="card"><div class="k">上线口径年化</div><div class="v">{pc(live.get('cagr'))}</div><div class="n">研究口径 {pc(M.get('metrics',{}).get('research',{}).get('cagr'))}</div></div>
  <div class="card"><div class="k">上线口径 Sharpe</div><div class="v">{n3(live.get('sharpe'))}</div><div class="n">MDD {pc(live.get('mdd'))}</div></div>
  <div class="card"><div class="k">最新净值</div><div class="v">{float(last.get('nav_live',1)):.3f}</div><div class="n">{last.get('nav_month','—')} 月度口径 · 净值倍数(起点=1)</div></div>
  <div class="card"><div class="k">影子账户</div><div class="v">{str(S.get('version','—'))}</div><div class="n">降级={S.get('degraded','—')} · 实盘观察 {S.get('live_observation_months',0)} 月</div></div>
</div>

<div class="callout"><b>口径说明：</b>看板「上线口径」= 15bp 单边成本 + QDII 溢价&gt;2% 不建仓 + 回撤熔断，
是<b>唯一实盘决策口径</b>；「研究口径」5bp 仅作对照，不得用于对外业绩展示。
日间无新月度行情时净值沿用上月（<code>carry_forward</code>），<b>不编造日波动</b>；
月度净值仅在月末数据确认后更新。</div>

<div class="bad"><b>双口径结论未变：</b>按文章原表 CONDITIONAL-GO（有条件上线），
按 DSR≥0.95 严格口径仍为 <b>NO-GO</b>（DSR={n3(disc.get('dsr'))}）。
影子账户进化学习是旁路监控，<b>不能提高统计证据强度</b>，上线版固定参数始终不变。</div>

<h2>一、净值总览</h2>
{img(F1, '图 1 · 研究口径 vs 上线口径真实净值（对数轴，引擎现场回测）')}
{tbl(['运行指标', '取值'], run_rows)}

<h2>二、影子账户进化学习（旁路）</h2>
<div class="info"><b>衔接上线前研究：</b>训练参数体系（4 维边界 / smooth=0.65 / 种子延续 / 连续 2 折降级规则）
完全继承 <code>prod/output/param_registry.json</code>，并继承其 <code>degraded=true</code> 初始状态 ——
影子账户从固定参数起步，每次真实训练一折候选参数并做严格样本外检验，
只有连续 2 折同时跑赢 CAGR 与 Sharpe 才可能解除降级。</div>
{img(F2, '图 2 · 影子账户历史回灌演练：候选参数（虚线）vs 固定参数 vs 有效净值')}
{tbl(['项目', '取值 / 证据'], sh_rows)}
<div class="callout">本折候选参数在样本外 <b>{'跑赢' if S.get('single_fold_win') else '跑输'}</b>固定版
（CAGR {pc(cand.get('cagr'))} vs {pc(fixed.get('cagr'))}，Sharpe {n3(cand.get('sharpe'))} vs {n3(fixed.get('sharpe'))}），
但<b>单折跑赢不足以解除降级</b>：{str(S.get('degrade_rule_note',''))}
因此影子账户仍按固定参数记账，与上线版净值一致。当前实盘观察月数 <b>0</b>，三条曲线均为历史演练、非实盘记录。</div>

<h2>三、最新信号与持仓</h2>
{img(F3, '图 3 · 最新决策月目标权重（含 RS 轮动与利率债空头承接）')}
<p class="muted">口径说明：<b>决策月 t 的权重在 t+1 月生效</b>，因此最新决策月
{sig.get('decision_month','—')} 比最新净值月 {last.get('nav_month','—')} 早 1 个月；
下图与下表展示的是为该净值月设定的目标仓位，不是当月收盘持仓。</p>
{tbl(['字段', '取值'], [
    ['决策月', lcell(str(sig.get('decision_month','—')))],
    ['股票择时 regime', str(sig.get('regime','—'))],
    ['RS 选中', lcell(str(sig.get('rs_pick','—')))],
    ['目标权重', lcell(str(sig.get('weights','—')))],
    ['总仓位', str(sig.get('exposure','—'))],
    ['QDII 溢价 / 是否拦截', f"{sig.get('premium_pct','—')}% / {'是' if sig.get('premium_blocked') else '否'}"],
    ['利率债空头承接 / 现金增强', f"{sig.get('bear_bond_active','—')} / {sig.get('bond_cash','—')}"],
])}

<h2>四、数据新鲜度（每日探针）</h2>
{tbl(['标的', '最新交易日', '状态', '通道'], pa_rows)}
<p class="muted">探针仅用于判断数据新鲜度，不写入模型价格数据；补数最多尝试 2 次（新浪→东财），
失败如实标 unavailable，不填假设值。</p>

<h2>五、强制披露与容量</h2>
{tbl(['披露项', '取值'], disc_rows)}
{tbl(['容量项', '取值'], cap_rows)}

<h2>六、自动化部署状态</h2>
{tbl(['项目', '状态 / 说明'], dep_rows)}

<h2>七、日台账（最近 7 天）</h2>
{tbl(['日期', '上线净值', '净值来源', '影子版本', '状态'], led_rows)}

<footer>
mafc-nav ｜ 每日同步 <code>src/sync_daily.py</code> ｜ 看板 <code>src/dashboard.py</code> ｜
部署 <code>src/deploy.py</code>。所有数字来自 prod 引擎现场回测与真实行情探针，未编造。
策略源 {M.get('source_prod','—')} ｜ 生成 {M.get('generated_at','—')}
</footer>
</div></body></html>"""

    path = os.path.join(DASH, "index.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return {"file": os.path.relpath(path, ROOT),
            "size_kb": round(os.path.getsize(path) / 1024, 1),
            "figures": 3, "tables": 8}


if __name__ == "__main__":
    print(build())
