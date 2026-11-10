# hwaby

精简后只保留 4 项功能，其余研究/回测/因子/ML/复盘代码已移除。

| # | 功能 | 入口 |
|---|---|---|
| 1 | 每日收盘后更新全市场 K 线 / 估值 / 快照 / 特征 | `main.py daily-update` 或定时 `scheduler` |
| 2 | 交易时段盯配置股票池，1min MACD 信号发邮件，分时数据落库 | `main.py monitor` 或定时 `scheduler` |
| 3 | 手动跑预测（分数 / 行业 / 消息面 / 技术位 / 公告） | `main.py premarket` / `main.py announce` |
| 4 | 每工作日定时盘前分析配置股票池当天走势 | `main.py scheduler` |

## 快速开始

```bash
cp .env.example .env          # 填数据库、STOCK_POOL、邮件
python main.py init-db        # 首次部署建表

# 4 项功能的常用命令
python main.py daily-update --check          # 日更体检（只读）
python main.py daily-update                  # 收盘后一键日更
python main.py monitor --once                # 盯盘单轮（调试/补分时数据）
python main.py monitor                       # 盯盘常驻
python main.py premarket --codes sz.300454   # 手动预测
python main.py premarket --pool --trade-date 2026-11-11
python main.py announce -c 300454 --days 10  # 公告归因
python main.py scheduler                     # 固定进程（盘前 + 日更 + 盯盘启停）
```

所有命令用 `.venv/Scripts/python.exe`，Windows 下加 `-X utf8`；项目根目录执行。

## 1. 每日 K 线更新

正式脚本 `tools/update_daily_tencent.py`：`日线 → 估值 → 快照 → 特征`。
四项前置检查（交易日 / 已收盘 / 近 15 日缺口 / 估值跟上日线），收尾核对三表行数 ≥4900。

- 数据源是腾讯 `qt.gtimg.cn` **当前快照**，没有 `date=` 参数：**漏一天就永久缺失**，必须当天收盘后跑。
- 唯一例外是漏掉「最后一个交易日」且尚未开盘新交易日，可用 `--date` 补回；一旦新交易日开盘，窗口永久关闭。
- 日更内部会 subprocess 调 `main.py build-features --append-parquet`，**该命令名和这 4 个选项不能改**。
- `build-features` 必须带 `--append-parquet`：`save_parquet` 是覆盖写，日更会把全量 parquet 缩成 1 天。

## 2. 1min MACD 盯盘

`signals/macd_intraday.py`（计算与归档）+ `scheduler/macd_monitor.py`（进程与邮件）。

- **卖出**：`DIF>0 且 DEA>0 且 DIF 处滚动 60 根分位 ≥0.7` 时死叉
- **买入**：`DIF<0 且 DEA<0 且 DEA 处滚动 60 根分位 ≤0.3` 时金叉
- EMA12/26/9 跨日连续、种子=序列首值；前 35 根预热不出信号
- 🔴 高低位**只能用「截至当根」的滚动分位**。用全天/全序列分位会把尾盘信息泄进早盘，是未来函数，盘中会冒出不存在的信号。
- 每轮重拉全量 1min 线重算（因果计算，历史值不变），只处理新增 bar；按 bar 时间戳去重，20 秒轮询也不会重复发邮件。
- 首次启动只建基线并归档，**不补发历史信号**；同标的同向默认冷却 5 分钟。
- 分时数据幂等写入 `min_kline`（`period='1min'`），**code 统一带点** `sz.300454`。旧实时任务写的是不带点的 `sz002624`，同表两份数据会让回测查不到——现在只有一种格式。

新浪 1min 主源（约 1023 根 / 5 个交易日），失败退腾讯；两源都要过新鲜度校验。

⚠ 该信号是**状态提示，不是买卖建议**。精简前的历史检验：1/5/15/30/60min 上 `signed` 口径 alpha 基本全负；`level` 口径做时段匹配安慰剂后缩水 2/3，主体是「上午卖下午买」的日内均值回归结构，不是 MACD 选时能力。邮件正文已带风险提示。

## 3. 手动预测

`premarket/` 包：外围市场 + 消息面 + 板块/行业 + 技术位 → 综合分与开盘应对预案，
输出分数、行业、消息面、技术位，默认落 `premarket_scores`（供事后回填涨跌算准确率）。
`--trade-date` 指定的是**预测指向的交易日**；预测明天要显式传。
个股公告不在报告里，用 `main.py announce` 单查（深交所 / 巨潮官方源，只做归因不做预测）。

## 4. 定时盘前

`main.py scheduler` 是常驻进程，工作日注册：

| 时间 | 任务 |
|---|---|
| `PREMARKET_TIME`（默认 08:30） | 盘前分析 `STOCK_POOL`，落库 + HTML + 邮件 |
| 09:25 / 15:05 | 启动 / 停止 1min MACD 盯盘线程 |
| `DAILY_UPDATE_TIME`（默认 16:30） | 收盘后腾讯全市场日更 |

启动时若正处于交易时段会立即拉起盯盘。
⚠ 日更任务与 WorkBuddy 外部 16:30 自动化**二选一**，两边都开会各跑一次（幂等，但白跑 5~6 分钟）。

## 目录

```
main.py                    7 个命令入口
config/  db/  data/        配置、ORM、数据口径
data_fetcher/              日线/快照/分钟取数（硬约束：不改）
features/  indicators/  lib/   日更特征构建依赖
premarket/                 盘前预测（10 个模块）
signals/macd_intraday.py   1min MACD 判据与归档
scheduler/                 固定进程、日更任务、盯盘进程
tools/                     腾讯日更 4 件套 + 分片同步 + 公告查询
utils/                     邮件、HTML 样式、文本格式
tests/                     盘前 104 项 + 盯盘 10 项
```

## 已知边界

- `ps_ttm` / `pcf` 与行业分类永久停在 2026-10-30（腾讯无此字段，不推算）。
- BaoStock 已永久封号，`data_fetcher/baostock_fetcher.py` 仍保留，但只用到它的本地 DB 读写与 `normalize_code`，**不触发网络调用**。
- 东财公告接口 IP 限流，全量历史不可用；单票查询走官方源不受影响。
- 1 分钟免费源只有约 5 个交易日；盯盘进程会逐日把 `min_kline` 攒起来，攒够 ≥20 天再做样本外复核。
- `db/models.py` 保留了几张历史表定义（`backtest` / `trade_signals` 等），`create_all` 会多建空表，无副作用。

## 精简与回滚

本轮从 122 个 py / 34439 行精简到 52 个 / 10674 行，删掉 analysis、backtest、ml、
review、signals 旧策略、7 个废弃 tools、3 个旧 scheduler、16 个测试及 COMMANDS/FEATURES 文档。
精简前的完整代码在 git 快照 **`backup/pre-trim-20261110`**：

```bash
git show backup/pre-trim-20261110 --stat
git checkout backup/pre-trim-20261110 -- .    # 整仓恢复到精简前
```
