# mafc-nav · MAFC 固定规则组合净值看板

**RS 相对动量轮动 + 双均线 MA(5/12) 择时 + 黄金 + 中国利率债**，独立仓库、独立 Pages 站点、独立定时任务。

> ## 当前上线状态：`BLOCKED_PERMISSION`（未谎称上线）
>
> 工程已完成本地重命名、vendor 依赖打包、两套独立工作流编写与 **7 阶段 / 10 项验收全 PASS**。
> 但通过 GitHub 连接器自动创建 `ebabao/mafc-nav` 仓库时，`POST /user/repos` 返回
> **`403 Resource not accessible by integration`**（App 集成令牌无权在个人命名空间建仓），重试 2 次后停止。
> 因此**仓库尚未创建、Pages 未启用、云端定时任务未注册**。详见 `config/launch_state.json`。
>
> **解除方式（任选其一）**：
> 1. 在 GitHub 网页手动新建**空公开仓库** `ebabao/mafc-nav`（不要初始化任何文件），
>    随后由连接器一次性推送全部文件，工作流即随 push 自动触发首次同步与 Pages 部署；
> 2. 在目标机器 `gh auth login` 后运行 `deploy/deploy_to_github.sh` 或 `deploy_to_github.ps1`；
> 3. 为 GitHub 连接器授予仓库创建权限后重新执行自动建仓。

> ## ⚠️ 与「全天候策略」的关系：完全独立
>
> 本工程**不是** `ebabao/all-weather-nav`（全天候策略 v1.4 · 七 ETF 风险平价）。两者是**两套完全不同的策略**：
>
> | 维度 | 本工程 mafc-nav | all-weather-nav |
> |---|---|---|
> | 策略内核 | RS 动量轮动 + 双均线择时 + 空头承接 | 四象限风险平价 + 国债专属杠杆 |
> | 底层资产 | 标普500/纳指/黄金/中国利率债（月度） | 7 只境内 ETF（日频） |
> | 参数体系 | 固定规则组合 + 4 维旁路影子进化 | 15 维 GA 训练参数 |
> | 仓库 / Pages | `ebabao/mafc-nav` → `/mafc-nav/` | `ebabao/all-weather-nav` → `/all-weather-nav/` |
> | 定时任务 | UTC 01:30 + 独立 watchdog | UTC 11:20 / 13:00 + 独立 watchdog |
> | 并发组 | `mafc-nav-sync` | `aw-nav-sync` |
>
> 站点产物只含 `index.html + data/` 接口，各自独立发布，**互不引用、互不覆盖、不共享发布源**。
> 本仓库未改动 `all-weather-nav` 的任何文件。

## 每日执行链

```
新浪行情探针(4 ETF) ──► sync_daily.py ──► data/*.json（策略模型接口）
                            │                    │
                            │ 复用 vendor/prod   │ 同源组装 site/
                            ▼                    ▼
                   现场回测 + 影子进化     dashboard/index.html（看板页面数据）
                                                │
                                                ▼
                                  GitHub Pages 独立站点（每日自动刷新）
```

| 阶段 | 动作 | 失败处理 |
|---|---|---|
| 0 前置校验 | 校验随仓库打包的 `vendor/prod` 引擎 / 清洗主表 / 研究参数版本库 | 缺失则中止，**不编造数据** |
| 1 行情探针 | 4 只实盘 ETF 最新交易日（新浪→东财，尝试 ≤2 次） | 如实标 `unavailable`，不填假设值，不中止 |
| 2 模型状态 | `model_api.py` 现场回测，产出 `data/model_state.json` | 沿用引擎真实结果 |
| 3 影子进化 | `shadow_evolution.py` 旁路训练一折候选参数 + 严格样本外检验 | 继承上线前研究的 `degraded=true`，连续 2 折跑赢才可能解除 |
| 4 日台账 | `nav_daily.csv` 按 date 幂等写入 | 同日覆盖，不重复追加 |
| 5 看板 | `dashboard.py` 重生成自包含中文页面（3 图 8 表） | 中文字体多路径探测，云端自动装 `fonts-noto-cjk` |
| 6 健康检查 | 数字一致 / 台账唯一 / 页面完整 / 无占位残留 | 失败标 DEGRADED |

运行指标写入 `output/sync_run.json`：`run_id / scraped / new / md / per_account / health / verdict`。

**月度净值只在月末数据确认后更新**；日间沿用上月并标 `carry_forward`，不编造日波动。

## 定时与兜底（两套并行，均在本仓库内）

- `.github/workflows/mafc-daily-sync.yml`：`cron 30 1 * * *`（UTC 01:30 = 北京 09:30）、`workflow_dispatch`、以及 `src/ config/ vendor/ 工作流自身` 变更触发。同步 → 接口契约校验 → 幂等提交 → `configure-pages` + `upload-pages-artifact(site/)` + `deploy-pages`。
- `.github/workflows/mafc-watchdog.yml`：`cron 50 */3 * * *` 每 3 小时巡检，**超过 26h 无同步**或**数据过期 3 天**（冷却 6h）即 `workflow_dispatch` 重启同步链。

线上看板（仓库创建并推送后生效）：<https://ebabao.github.io/mafc-nav/>

## vendor 依赖（云端能真跑的关键）

独立 runner 上没有外部 prod 工程，因此把 prod 的**最小运行依赖**随仓库打包：

```
vendor/prod/src/        engine.py / execution.py / calibrate.py（复用，不重写）
vendor/prod/data/clean/ prices_monthly.csv（清洗后月度主表，全量）
vendor/prod/output/     param_registry.json / capacity_report.json /
                        exam_results.json / remediation_results.json（运行最小子集，已标注）
vendor/prod/config/     disclosure.json / cost_policy.json
vendor/data/            premium_monthly.csv（真实 QDII 溢价序列，全量）
```

`config/sync_policy.json` 的 `prod_dir = "vendor/prod"` 为**相对仓库根目录**的路径，本地工作区与云端 runner 通用。
`exam_results.json` / `remediation_results.json` 为运行最小子集（仅保留运行时读取的字段），完整体检与整改报告仍存于源工程，未编造。

## 影子账户进化学习（按上线前研究训练参数）

直接复用 `vendor/prod/src/calibrate.py`：4 维边界、`smooth=0.65`、种子体系延续、`degrade_patience=2`、温启动。
**继承 `param_registry.json` 的 `degraded=true`** —— 研究阶段 6 折中 5 折已证明进化版跑输固定版并自动降级，
因此影子账户从固定参数起步，进化学习只作**旁路监控**，上线版固定参数始终不动。
当前**实盘观察月数 = 0**，所有净值均为历史回灌演练（`historical_dry_run`），不是实盘记录。

## 如实披露的边界

- 双口径结论未变：按文章原表 **CONDITIONAL-GO**（有条件上线），按 `DSR≥0.95` 严格口径仍为 **NO-GO**（DSR=0.346）。
- 影子进化不能提高统计证据强度；收益中约 65.4% 为 beta，择时净增量约 0.295 Sharpe。
- 回测为**价格指数口径**，未计股息；QDII 溢价序列为**估算代理**非官方 IOPV，2014-03 前无数据按 0 处理。
- 实盘唯一口径：单边 15bp 成本 + QDII 溢价 >2% 不建仓 + 回撤熔断。上线口径 12.02% / -17.78% / Sharpe 0.867。
- 容量按 1% ADV 反算上限约 **335 万元**（瓶颈为标普500ETF 513500）。
- MC 95 分位压力回撤 -31.74% 仅作止损 / 资金预算，不作常态降仓依据。
- `source=cache_fallback` 时表示当日抓数失败、沿用上一版快照，页面会如实标注。

本仓库仅用于策略研究与工程演示，**不构成投资建议**。

## 本地复现

```bash
pip install -r requirements.txt
python3.11 src/deploy.py        # 本地部署：7 阶段（环境→配置→定时→冒烟→幂等→状态上板→健康检查）
python3.11 src/sync_daily.py    # 单次每日同步
```

`output/deploy_report.json`、`output/health.json`、`output/sync_run.json` 为验收与运行记录。
