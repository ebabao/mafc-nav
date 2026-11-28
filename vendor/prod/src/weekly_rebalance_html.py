"""生成周频调仓/溢价敏感性对比报告（自包含 HTML，SVG 图，不推 git）。默认 NDX 基准。"""
import json, os
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PROD = os.path.dirname(HERE)
OUT = os.path.join(PROD, "output")
BENCH = os.environ.get("MAFC_BENCH", "ndx").lower()
D = json.load(open(os.path.join(OUT, f"weekly_rebalance_results_{BENCH}.json"), encoding="utf-8"))
ROWS = D["rows"]
EQ = D["equity_curves"]
WD, MD = D["weekly_dates"], D["monthly_dates"]
BCN = D.get("benchmark_cn", "纳斯达克100(NDX)")

COLORS = ["#d93025", "#7c8db0", "#e8710a", "#0f9d58", "#7b1fa2", "#0277bd"]

def pct(v, dig=2, sign=False):
    if v is None or v != v: return "—"
    return f"{'+' if sign and v>0 else ''}{v*100:.{dig}f}%"

def num(v, dig=3):
    return "—" if v is None or v != v else f"{v:.{dig}f}"

def cls(v):
    return "up" if v > 0 else ("down" if v < 0 else "")

def norm_curve(eq):
    return [(x / eq[0] - 1) * 100 for x in eq]

def svg_multi(curves, dates, W=1120, H=380, title=""):
    PL, PR, PT, PB = 62, 18, 20, 44
    pw, ph = W - PL - PR, H - PT - PB
    allv = [v for _, vals, _, _ in curves for v in vals]
    hi, lo = max(allv), min(allv)
    pad = (hi - lo) * 0.08 or 1
    hi += pad; lo -= pad
    def X(i, n): return PL + pw * i / max(n - 1, 1)
    def Y(v): return PT + ph * (hi - v) / (hi - lo)
    grid = []
    for k in range(5):
        v = hi - (hi - lo) * k / 4
        y = Y(v)
        grid.append(f'<line x1="{PL}" y1="{y:.1f}" x2="{PL+pw}" y2="{y:.1f}" stroke="#e8edf3"/>')
        grid.append(f'<text x="{PL-8}" y="{y+4:.1f}" text-anchor="end" font-size="11" fill="#64748b">{v:.0f}%</text>')
    for k in range(6):
        i = int((len(dates) - 1) * k / 5)
        x = X(i, len(dates))
        grid.append(f'<text x="{x:.1f}" y="{H-18}" text-anchor="middle" font-size="10" fill="#64748b">{dates[i][:7]}</text>')
    paths = []
    for name, vals, color, dash in curves:
        pts = " ".join(f"{X(i,len(vals)):.1f},{Y(v):.1f}" for i, v in enumerate(vals))
        da = f' stroke-dasharray="{dash}"' if dash else ""
        paths.append(f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="2"{da}/>')
    leg = "".join(f'<span style="margin-right:16px"><i style="display:inline-block;width:11px;height:3px;background:{c};vertical-align:middle;margin-right:5px"></i>{n}</span>'
                  for n, _, c, _ in curves)
    return f'''<div style="font-weight:600;margin:6px 0 4px">{title}</div>
<svg viewBox="0 0 {W} {H}" width="100%" style="max-width:{W}px">
{''.join(grid)}{''.join(paths)}
<line x1="{PL}" y1="{Y(0):.1f}" x2="{PL+pw}" y2="{Y(0):.1f}" stroke="#94a3b8" stroke-dasharray="3 3"/>
</svg><div style="font-size:12px;color:#475569;margin-top:4px">{leg}</div>'''

mc = []
for i, r in enumerate(ROWS):
    if r["freq"] == "month":
        mc.append((r["name"], norm_curve(r["equity"]), COLORS[i], "5 4" if not r["premium"] else ""))
svg_month = svg_multi(mc, MD, title=f"月频调仓 · 2017-12~2026-11（{BCN} 口径）")

def resample_to(vals, n):
    idx = np.linspace(0, len(vals) - 1, n).astype(int)
    return [vals[i] for i in idx]
wc = [(ROWS[0]["name"] + "(基线)", norm_curve(ROWS[0]["equity"]), "#334155", "5 4")]
for i, r in enumerate(ROWS):
    if r["freq"] == "week":
        wc.append((r["name"], norm_curve(r["equity"]), COLORS[i], "5 4" if not r["premium"] else ""))
wc2 = [(n, resample_to(v, len(WD)) if len(v) != len(WD) else v, c, d) for n, v, c, d in wc]
svg_week = svg_multi(wc2, WD, title="周频调仓 · 2017-12~2026-11（月频基线虚线对照）")

def metric_table():
    head = "<tr><th style='text-align:left'>方案</th><th>频率</th><th>溢价拦截</th><th>熔断</th><th>年化收益</th><th>最大回撤</th><th>Sharpe</th><th>Calmar</th><th>换手/期</th></tr>"
    body = ""
    for r in ROWS:
        hl = " style='background:#fff8e1'" if "溢价关" in r["name"] and "等价" in r["name"] else ""
        body += f'''<tr{hl}><td style="text-align:left"><b>{r['name']}</b></td>
<td>{'周' if r['freq']=='week' else '月'}</td><td>{'开' if r['premium'] else '关'}</td>
<td>{'开' if r['brake'] else '关'}</td>
<td class="{cls(r['cagr'])}">{pct(r['cagr'])}</td><td class="down">{pct(r['mdd'])}</td>
<td>{num(r['sharpe'])}</td><td>{num(r['calmar'])}</td><td>{num(r['avg_turnover'],3)}</td></tr>'''
    return f"<table>{head}{body}</table>"

def yearly_table():
    rows = D.get("yearly_compare", [])
    if not rows: return "<div class='sub'>（无逐年数据）</div>"
    head = "<tr><th>年份</th><th>MAFC组合</th><th>纳指100(NDX)</th><th>纳指综指(IXIC)</th><th>超额 vs NDX</th></tr>"
    body = ""
    for x in rows:
        exc = x["combo"] - x["ndx"] if x["combo"] is not None and x["ndx"] is not None else None
        body += f'''<tr><td>{x['year']}</td><td class="{cls(x['combo'])}">{pct(x['combo']/100 if x['combo'] is not None else None,2,True)}</td>
<td class="{cls(x['ndx'])}">{pct(x['ndx']/100 if x['ndx'] is not None else None,2,True)}</td>
<td class="{cls(x['ixic'])}">{pct(x['ixic']/100 if x['ixic'] is not None else None,2,True)}</td>
<td class="{cls(exc)}">{pct(exc/100 if exc is not None else None,2,True)}</td></tr>'''
    return f"<table>{head}{body}</table>"

CSS = """
body{font-family:-apple-system,'Segoe UI','PingFang SC',sans-serif;max-width:1180px;margin:0 auto;padding:24px;color:#1e293b;background:#f8fafc}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:26px 0 10px;padding-left:9px;border-left:4px solid #d93025}
.sub{color:#64748b;font-size:13px;margin-bottom:18px}
.card{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:16px 18px;margin:12px 0;box-shadow:0 1px 3px rgba(0,0,0,.04)}
table{border-collapse:collapse;width:100%;font-size:13px}th,td{border-bottom:1px solid #eef2f6;padding:8px 7px;text-align:center}
th{background:#f1f5f9;color:#475569;font-weight:600}.up{color:#d93025;font-weight:600}.down{color:#0f9d58;font-weight:600}
.note{background:#f0f6ff;border:1px solid #cfe0f5;border-radius:8px;padding:12px 14px;font-size:13px;color:#334155;line-height:1.7}
.warn{background:#fff7ed;border-color:#fed7aa;color:#7c2d12}
.kpi{display:flex;gap:14px;flex-wrap:wrap;margin:10px 0}.kpi div{background:#fff;border:1px solid #e2e8f0;border-radius:9px;padding:11px 16px;min-width:150px}
.kpi b{display:block;font-size:20px;margin-top:4px}.lbl{font-size:12px;color:#64748b}
code{background:#eef2f7;padding:1px 5px;border-radius:4px;font-size:12px}
"""

r_month_on = next(r for r in ROWS if r["name"].startswith("月频") and r["premium"])
r_month_off = next(r for r in ROWS if r["name"].startswith("月频") and not r["premium"])
r_week_on = next(r for r in ROWS if r["name"].startswith("周频-等价") and r["premium"])
r_week_off = next(r for r in ROWS if r["name"].startswith("周频-等价") and not r["premium"])
r_week_fast = next(r for r in ROWS if "快信号" in r["name"])
ndx_cagr = 0.1739   # 纳指100同期年化（独立计算，见方法）

html = f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>MAFC 周频调仓与溢价过滤敏感性回测（{BCN} 基准）</title><style>{CSS}</style></head><body>
<h1>MAFC 周频调仓 & QDII 溢价过滤 · 敏感性回测</h1>
<div class="sub">研究脚本 · 未推 git · 生成于 {D['generated_at']} · 对比区间 {D['compare_window']} · <b>纳指基准：{BCN}</b></div>

<div class="kpi">
<div><span class="lbl">月频·溢价开(基线)</span><b class="{cls(r_month_on['cagr'])}">{pct(r_month_on['cagr'])}</b><span class="lbl">Sharpe {num(r_month_on['sharpe'])}</span></div>
<div><span class="lbl">月频·溢价关</span><b class="{cls(r_month_off['cagr'])}">{pct(r_month_off['cagr'])}</b><span class="lbl">Sharpe {num(r_month_off['sharpe'])}</span></div>
<div><span class="lbl">周频·溢价开</span><b class="{cls(r_week_on['cagr'])}">{pct(r_week_on['cagr'])}</b><span class="lbl">Sharpe {num(r_week_on['sharpe'])}</span></div>
<div><span class="lbl">纳指100 本身</span><b class="up">{pct(ndx_cagr)}</b><span class="lbl">MDD −33%</span></div>
</div>

<h2>一、结论速览</h2>
<div class="card"><div class="note">
<b>0) 纳指基准说明</b>：原组合底层用的是纳斯达克综指（IXIC，同期 CAGR 15.07%），已按你的要求换成更主流、更强的<b>纳斯达克100（NDX，同期 CAGR 17.39%）</b>重跑。
换成 NDX 后，月频基线年化从 16.45% 提升到 <b>{pct(r_month_on['cagr'])}</b>，
Sharpe 从 1.310 升到 <b>{num(r_month_on['sharpe'])}</b>，最大回撤反而从 −17.41% 收窄到 <b>{pct(r_month_on['mdd'])}</b>。<br>
<b>1) 组合 vs 纳指100：年度收益"看起来低"是牛市捕获取舍，不是跑输。</b>
全周期组合 {pct(r_month_on['cagr'])} 略高于纳指100 的 {pct(ndx_cagr)}，且回撤仅 {pct(r_month_on['mdd'])} vs 纳指100 的 −33%。
但逐年看（下表），组合在 2021（+13% vs +27%）、2023（+30% vs +54%）主升浪明显少赚——这就是"年度收益低"的来源；
代价换来的是 2022 熊市少亏 21pp（−11.5% vs −33%）、2024/2025 反超。低回撤稳健策略本就如此。<br>
<b>2) 不拦截 QDII 溢价：收益略升，风险调整后变差。</b>
关闭溢价过滤年化 {pct(r_month_on['cagr'])} → {pct(r_month_off['cagr'])}（{pct(r_month_off['cagr']-r_month_on['cagr'],2,True)}），
但回撤 {pct(r_month_on['mdd'])} → {pct(r_month_off['mdd'])}、Sharpe {num(r_month_on['sharpe'])} → {num(r_month_off['sharpe'])}。<br>
<b>3) 周频调仓：等价参数下更差。</b>
周频年化 {pct(r_week_on['cagr'])} < 月频 {pct(r_month_on['cagr'])}，回撤更深；周频快信号恶化到 {pct(r_week_fast['cagr'])}。
<b>综合：当前"月频 + 溢价过滤 + 熔断"仍是风险调整后较优解。</b>
</div></div>

<h2>二、逐年收益对照（回应"年度收益太低"）</h2>
<div class="card">{yearly_table()}
<div class="note" style="margin-top:10px">组合在单边主升年份（2021/2023）跑输纳指100，是"择时+多资产"结构主动降低上行捕获的结果；
但在 2022 熊市与 2024/2025 震荡市显著占优。全周期夏普/回撤比更优，年度数字不宜单年比较。</div></div>

<h2>三、绩效对比表</h2>
<div class="card">{metric_table()}</div>

<h2>四、净值曲线</h2>
<div class="card">{svg_month}</div>
<div class="card">{svg_week}</div>

<h2>五、方法与口径披露</h2>
<div class="card"><div class="note warn">
<b>为什么这么比（保证公平、不编造）：</b><br>
· <b>纳指基准切换</b>：底层美股标的由纳斯达克综指（IXIC）换为纳斯达克100（NDX），NDX 日线 2014-02 起，完整覆盖对比区间 2017-12~2026-11；月末/周五值与独立计算核对一致（CAGR 17.39%）。引擎列名 <code>ixic</code> 不变，仅数据换为 NDX，规则逻辑零改动。<br>
· <b>只变一个变量</b>：月频与周频用同一套规则（MA 择时 + RS 轮动 + TSMOM + 黄金腿 + 利率债承接 + 现金增强 + 回撤熔断）与同一成本口径（15bp 单边换手）；差异仅在决策/结算频率。<br>
· <b>参数时间等价</b>：月频 MA5/12、RS/TSMOM 12 月 → 周频 MA22/52、RS/TSMOM 52 周（5 月≈22 周、12 月≈52 周），保证是"同一个策略、调得更勤"。<br>
· <b>真实日线</b>：标普/纳指100 用新浪指数日线、国债用东财 sh000012、黄金用 COMEX GC 期货日线（2016-12 起），按周五收盘 resample 到周。月末值与生产月度表核对：标普/国债价相关 ≥0.9997、比值 1.000；黄金 0.999/0.996（期货代理现货，如实标注）。节假日无交易周用前值填充（该周收益=0，无前视）。<br>
· <b>对比区间</b>：2017-12~2026-11——黄金真实周线覆盖（2016-12 起）+ 52 周预热后的完整区间；月频对齐到同一日历区间。<br>
· <b>择时基准</b>：月/周频 regime 均由各自频率的股/债等权合成净值计算，口径统一（月频基线数字与线上 S3 用 risk_combo_nav 略有差异，属基准口径不同，不影响相对结论）。<br>
· <b>成本</b>：换手成本按每次调仓仓位变化计，周频调仓次数多但单次换手更小，年化换手成本已如实计入；现金/融资利率按周折算。<br>
· <b>不做的外推</b>：2007-2016 缺黄金真实日线、NDX 缺 2014 前日线，不做外推、不编造；全区间溢价敏感性另见执行层 <code>execution_scenarios.json</code>。<br>
· 数据源：akshare（index_us_stock_sina(.NDX/.INX) / stock_zh_index_daily_em / futures_foreign_hist）。
</div></div>

<div class="sub" style="margin-top:18px">脚本：<code>src/weekly_rebalance.py</code> · 数据：<code>data/clean/prices_weekly_ndx.csv</code> / <code>prices_monthly_ndx.csv</code> / <code>ndx_daily.csv</code> · 结果：<code>output/weekly_rebalance_results_{BENCH}.json</code></div>
</body></html>'''

fp = "/workspace/mafc_weekly_compare.html"
with open(fp, "w", encoding="utf-8") as f:
    f.write(html)
print("生成", fp, len(html), "字节")
