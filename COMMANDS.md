# 股票买卖点检测系统 — 命令手册

> 入口：`python main.py <命令> [参数...]`

---

## 目录

1. [数据初始化与更新](#1-数据初始化与更新)
2. [实时盯盘与盘中计算](#2-实时盯盘与盘中计算)
3. [信号扫描与查询](#3-信号扫描与查询)
4. [回测与策略对比](#4-回测与策略对比)
5. [MACD 专项分析](#5-macd-专项分析)
6. [辅助工具](#6-辅助工具)

---

## 通用说明

- 日期格式：`YYYY-MM-DD`，如 `2026-06-30`
- 股票代码格式：`sh.600519` / `sz.000001`（沪市小写 sh，深市 sz）
- 股票列表格式：`code1,code2,code3`，如 `sh.600519,sz.000001`
- `is_flag=True` 的参数只需加参数名即可开启，如 `--no-save`
- 所有命令支持 `Ctrl+C` 中断
- 回测命令自动提前 70 天加载数据用于指标预热（MACD EMA 依赖）

---

## 1. 数据初始化与更新

### 1.1 init — 首次全量初始化

```bash
python main.py init [--stock-pool sh.600519,sz.000001] [--skip-min-kline]
```

| 参数 | 类型 | 必填 | 默认 | 说明 |
|------|------|------|------|------|
| `--stock-pool` | string | 否 | None | 指定股票池（逗号分隔），不传则使用配置池 |
| `--skip-min-kline` | flag | 否 | False | 跳过分钟K线（5min/15min）的获取 |

**功能：**
1. 初始化数据库表结构
2. 全量拉取日线历史数据
3. 获取 5min + 15min 分钟K线数据（可跳过）

**组合示例：**

| 场景 | 命令 |
|------|------|
| 首次完整初始化（配置池） | `python main.py init` |
| 只初始化指定股票 | `python main.py init --stock-pool sh.600519,sz.000001` |
| 只初始化日线（跳过分钟K线） | `python main.py init --skip-min-kline` |

---

### 1.2 update-daily — 每日更新（日线+信号）

```bash
python main.py update-daily [--stock-pool sh.600519,sz.000001]
```

| 参数 | 类型 | 必填 | 默认 | 说明 |
|------|------|------|------|------|
| `--stock-pool` | string | 否 | None | 指定股票池，不传则使用配置池 |

**功能：** 更新日线数据并重新计算信号

**组合示例：**

| 场景 | 命令 |
|------|------|
| 每日更新配置池股票 | `python main.py update-daily` |
| 每日更新指定股票 | `python main.py update-daily --stock-pool sh.600519` |

---

### 1.3 init-market — 全市场日线初始化

```bash
python main.py init-market [--max-stocks 100]
```

| 参数 | 类型 | 必填 | 默认 | 说明 |
|------|------|------|------|------|
| `--max-stocks` | int | 否 | None | 最多处理多少只（测试用，限制数量加速） |

**功能：** 从 `stock_info` 表读取全市场股票列表，批量初始化日线数据

**组合示例：**

| 场景 | 命令 |
|------|------|
| 全市场完整初始化 | `python main.py init-market` |
| 测试模式（只处理前100只） | `python main.py init-market --max-stocks 100` |

---

### 1.4 update-market — 全市场每日更新

```bash
python main.py update-market
```

| 参数 | 类型 | 必填 | 默认 | 说明 |
|------|------|------|------|------|
| 无 | - | - | - | - |

**功能：** 每日更新全市场日线数据 + 信号计算

---

### 1.5 update-stock-list — 更新全A股票列表

```bash
python main.py update-stock-list [--date 2026-06-30]
```

| 参数 | 类型 | 必填 | 默认 | 说明 |
|------|------|------|------|------|
| `--date` | string | 否 | 今天 | 目标日期，用于过滤（自动找最近交易日） |

**功能：** 更新 `stock_info` 表，过滤掉 ETF、指数、ST 股票

**组合示例：**

| 场景 | 命令 |
|------|------|
| 按今天更新 | `python main.py update-stock-list` |
| 按指定日期更新 | `python main.py update-stock-list --date 2026-06-27` |

---

### 1.6 stock-pool — 查看股票池

```bash
python main.py stock-pool [--market sh] [--limit 50]
```

| 参数 | 类型 | 必填 | 默认 | 说明 |
|------|------|------|------|------|
| `--market` | string | 否 | None | 市场过滤：`sh` 沪市 / `sz` 深市，不传则全部 |
| `--limit` | int | 否 | None | 限制显示数量 |

**组合示例：**

| 场景 | 命令 |
|------|------|
| 查看全部 | `python main.py stock-pool` |
| 只看沪市前100只 | `python main.py stock-pool --market sh --limit 100` |

---

### 1.7 init-db — 仅初始化表结构

```bash
python main.py init-db
```

**功能：** 只建表不拉数据（数据库已存在时不会重复创建）

---

## 2. 实时盯盘与盘中计算

### 2.1 realtime — 启动实时盯盘

```bash
python main.py realtime
```

**功能：** 启动常驻进程，盘中实时监控股票池信号变化

**注意：** 阻塞式运行，`Ctrl+C` 停止

---

### 2.2 intraday — 盘中日线信号计算

```bash
python main.py intraday [--stock-pool ...] [--strategies ...] [--no-save]
```

| 参数 | 类型 | 必填 | 默认 | 说明 |
|------|------|------|------|------|
| `--stock-pool` | string | 否 | 配置池 | 股票池，逗号分隔 |
| `--strategies` | string | 否 | 全部策略 | 策略列表，逗号分隔 |
| `--no-save` | flag | 否 | False | 不保存到数据库，只打印到控制台 |

**功能：** 用实时价格合成当日K线，计算盘中信号

**组合示例：**

| 场景 | 命令 |
|------|------|
| 配置池全部策略（保存结果） | `python main.py intraday` |
| 指定股票池 + 指定策略 | `python main.py intraday --stock-pool sh.600519 --strategies MACD金叉` |
| 测试模式（只看不存） | `python main.py intraday --no-save` |

---

### 2.3 scheduler — 启动完整调度器

```bash
python main.py scheduler
```

**功能：** 启动完整调度器（每日日线更新 + 盘中实时监控）

**注意：** 阻塞式运行，`Ctrl+C` 停止

---

## 3. 信号扫描与查询

### 3.1 scan-market — 全市场日线信号扫描 ★

```bash
python main.py scan-market [--strategies ...] [--signal-type buy] [--min-price 2] [--max-price 200] [--no-save] [--date 2026-06-27] [--codes ...] [--no-volume]
```

| 参数 | 类型 | 必填 | 默认 | 说明 |
|------|------|------|------|------|
| `--strategies` | string | 否 | 全部策略 | 策略列表，逗号分隔 |
| `--signal-type` | string | 否 | `buy` | 信号类型：`buy` / `sell` / `all` |
| `--min-price` | float | 否 | 2.0 | 最低价格过滤（剔除低价股） |
| `--max-price` | float | 否 | 200.0 | 最高价格过滤（剔除高价股） |
| `--no-save` | flag | 否 | False | 不保存到数据库 |
| `--date` | string | 否 | None | 指定扫描日期 `YYYY-MM-DD`。不传=盘中实时扫描，传入=历史扫描 |
| `--codes` | string | 否 | 全市场 | 指定股票代码列表，逗号分隔 |
| `--no-volume` | flag | 否 | False | 关闭 MACD金叉策略的成交量过滤（默认开启） |

**信号检测时序：**

| 模式 | 触发条件 | signal_time 存库 | 价格存库 |
|------|----------|-----------------|---------|
| 不传 --date（盘中） | 检查「今日合成K线」是否出现信号 | 今日（扫描当日） | 实时价/收盘价 |
| 传 --date（历史） | 检查「指定日期」是否出现信号 | 指定日期 + 1工作日（跳过周末） | 指定日期收盘价 |

**组合示例：**

| 场景 | 命令 | 说明 |
|------|------|------|
| 全市场盘中扫描 MACD金叉（默认参数） | `python main.py scan-market --strategies MACD金叉` | 检查今日K线信号 |
| 指定股票快速测试 | `python main.py scan-market --strategies MACD金叉 --codes sh.600519,sz.000001 --no-volume` | 剔除成交量过滤 |
| 周末回看26号的信号 | `python main.py scan-market --strategies MACD金叉 --date 2026-06-26` | 检查26号K线是否出现金叉 |
| 测试不保存 | `python main.py scan-market --strategies MACD金叉 --codes sh.600519 --no-save` | 只打印不落库 |
| 多策略扫描 | `python main.py scan-market --strategies MACD金叉,KDJ金叉` | 同时扫描多个策略 |
| 限制价格区间 | `python main.py scan-market --min-price 5 --max-price 50` | 只扫描5~50元的股票 |

---

### 3.2 scan — 日线买卖信号查询

```bash
python main.py scan [--date 2026-06-30] [--type all] [--min-strength 0] [--limit 50]
```

| 参数 | 类型 | 必填 | 默认 | 说明 |
|------|------|------|------|------|
| `--date` | string | 否 | 今天 | 查询日期 |
| `--type` | string | 否 | `all` | 信号类型：`buy` / `sell` / `all` |
| `--min-strength` | float | 否 | 0 | 最小信号强度过滤 |
| `--limit` | int | 否 | 50 | 最大返回条数 |

**功能：** 从数据库查询已计算好的日线信号

**组合示例：**

| 场景 | 命令 |
|------|------|
| 今天的全部信号 | `python main.py scan` |
| 29号的买入信号（前50条） | `python main.py scan --date 2026-06-29 --type buy --limit 50` |
| 强信号过滤 | `python main.py scan --min-strength 80` |

---

### 3.3 resonance — 多周期共振扫描

```bash
python main.py resonance [--date 2026-06-30] [--type buy] [--min-strength 50]
```

| 参数 | 类型 | 必填 | 默认 | 说明 |
|------|------|------|------|------|
| `--date` | string | 否 | 今天 | 目标日期 |
| `--type` | string | 否 | `buy` | 信号类型：`buy` / `sell` |
| `--min-strength` | float | 否 | 50 | 最小信号强度 |

**功能：** 扫描同时在日线和分钟线出现信号的股票

**组合示例：**

| 场景 | 命令 |
|------|------|
| 今日买入共振 | `python main.py resonance --type buy` |
| 29号强共振 | `python main.py resonance --date 2026-06-29 --min-strength 80` |

---

## 4. 回测与策略对比

### 4.1 backtest — 单只股票回测 ★

```bash
python main.py backtest --code sh.600519 --strategy MACD金叉 --start 2026-01-01 --end 2026-06-30 [--capital 100000] [--period daily] [--t0]
```

| 参数 | 类型 | 必填 | 默认 | 说明 |
|------|------|------|------|------|
| `--code` | string | 是 | - | 股票代码 |
| `--strategy` | string | 是 | - | 策略名称 |
| `--start` | string | 是 | - | 起始日期 `YYYY-MM-DD` |
| `--end` | string | 是 | - | 结束日期 `YYYY-MM-DD` |
| `--capital` | float | 否 | 100000 | 初始资金 |
| `--period` | string | 否 | `daily` | K线周期：`daily` / `5min` / `15min` |
| `--t0` | flag | 否 | False | T+0模式：当天买入当天可卖出。默认T+1（信号次日执行） |

**信号执行时序：**
- T+1模式（默认）：第N天出现买入信号 → 第N+1天开盘买入；第N天出现卖出信号 → 第N+1天开盘卖出
- T+0模式（`--t0`）：信号日当日即可买卖

**输出指标：** 初始资金、最终资金、总收益%、年化收益%、最大回撤%、夏普比率、胜率%、交易次数、盈亏比

**组合示例：**

| 场景 | 命令 |
|------|------|
| 常规日线回测（T+1） | `python main.py backtest --code sh.600519 --strategy MACD金叉 --start 2026-01-01 --end 2026-06-30` |
| T+0模式 | `python main.py backtest --code sh.600519 --strategy MACD金叉 --start 2026-01-01 --end 2026-06-30 --t0` |
| 大资金回测 | `python main.py backtest --code sz.000001 --strategy MACD金叉 --start 2026-01-01 --end 2026-06-30 --capital 500000` |
| 分钟线回测 | `python main.py backtest --code sh.600519 --strategy MACD金叉 --start 2026-06-01 --end 2026-06-30 --period 5min` |

---

### 4.2 backtest-portfolio — 多标的组合回测

```bash
python main.py backtest-portfolio --codes sh.600519,sz.000001 --strategy MACD金叉 --start 2026-01-01 --end 2026-06-30 [--capital 100000] [--t0]
```

| 参数 | 类型 | 必填 | 默认 | 说明 |
|------|------|------|------|------|
| `--codes` | string | 是 | - | 股票代码列表，逗号分隔 |
| `--strategy` | string | 是 | - | 策略名称 |
| `--start` | string | 是 | - | 起始日期 |
| `--end` | string | 是 | - | 结束日期 |
| `--capital` | float | 否 | 100000 | 初始资金 |
| `--t0` | flag | 否 | False | T+0模式 |

**功能：** 多只股票同一策略组合回测，输出组合级别收益指标

---

### 4.3 compare — 单/多只股票 多策略对比 ★

```bash
python main.py compare [--code sh.600519] [--strategies ...] --start 2026-01-01 [--end 2026-06-30] [--capital 100000] [--period daily] [--sort-by total_return] [--t0]
```

| 参数 | 类型 | 必填 | 默认 | 说明 |
|------|------|------|------|------|
| `--code` | string | 否 | 配置池 | 股票代码，可传多只（逗号分隔），不传用配置池 |
| `--strategies` | string | 否 | 全部策略 | 策略列表 |
| `--start` | string | 是 | - | 起始日期 |
| `--end` | string | 否 | 今天 | 结束日期 |
| `--capital` | float | 否 | 100000 | 初始资金 |
| `--period` | string | 否 | `daily` | 周期：`daily` / `5min` / `15min` |
| `--sort-by` | string | 否 | `total_return` | 排序指标：`total_return` / `annual_return` / `sharpe_ratio` / `win_rate` / `profit_factor` |
| `--t0` | flag | 否 | False | T+0模式 |

**功能：** 对一只（或多只）股票运行所有指定策略，按指标排序对比

**组合示例：**

| 场景 | 命令 |
|------|------|
| 单只股票多策略对比 | `python main.py compare --code sh.600519 --strategies MACD金叉,KDJ金叉 --start 2026-01-01 --end 2026-06-30` |
| 按夏普比率排序 | `python main.py compare --code sh.600519 --start 2026-01-01 --end 2026-06-30 --sort-by sharpe_ratio` |
| 配置池批量对比 | `python main.py compare --start 2026-01-01 --end 2026-06-30` |

---

### 4.4 compare-all — 多股票多策略综合对比 + 策略推荐 ★★

```bash
python main.py compare-all --start 2026-01-01 --end 2026-06-30 [--codes ...] [--all-code] [--strategies ...] [--capital 100000] [--period daily] [--top-n 3] [--max-stocks 50] [--t0] [--no-volume]
```

| 参数 | 类型 | 必填 | 默认 | 说明 |
|------|------|------|------|------|
| `--codes` | string | 否 | 配置池 | 股票代码列表，逗号分隔 |
| `--all-code` | flag | 否 | False | 使用全市场所有股票（覆盖 `--codes` 和配置池） |
| `--strategies` | string | 否 | 全部策略 | 策略列表 |
| `--start` | string | 是 | - | 起始日期 |
| `--end` | string | 是 | - | 结束日期 |
| `--capital` | float | 否 | 100000 | 初始资金 |
| `--period` | string | 否 | `daily` | 周期 |
| `--top-n` | int | 否 | 3 | 推荐策略数量 |
| `--max-stocks` | int | 否 | None | 最多处理多少只（测试用） |
| `--t0` | flag | 否 | False | T+0模式 |
| `--no-volume` | flag | 否 | False | 关闭 MACD 成交量过滤 |

**输出内容：**
1. 多股票策略综合对比表（按策略聚合收益）
2. 每只股票收益明细
3. Top N 推荐策略（综合分 = 平均收益 + 夏普 + 盈利占比 + 胜率）

**组合示例：**

| 场景 | 命令 |
|------|------|
| 指定股票对比 | `python main.py compare-all --codes sh.600519,sz.000001 --start 2026-01-01 --end 2026-06-30 --strategies MACD金叉 --no-volume` |
| 全市场对比（谨慎使用，耗时长） | `python main.py compare-all --all-code --start 2026-01-01 --end 2026-06-30` |
| 测试模式（小范围） | `python main.py compare-all --codes sh.600519,sz.000001 --max-stocks 2 --start 2026-01-01 --end 2026-06-30 --strategies MACD金叉 --no-volume` |

---

## 5. MACD 专项分析

### 5.1 analyze-macd — 最近N天MACD金叉信号收益分析

```bash
python main.py analyze-macd [--days 10] [--codes ...] [--all-code] [--max-print 500] [--end 2026-06-30] [--no-volume]
```

| 参数 | 类型 | 必填 | 默认 | 说明 |
|------|------|------|------|------|
| `--days` | int | 否 | 10 | 分析最近多少个交易日的信号 |
| `--codes` | string | 否 | 全市场 | 股票代码列表 |
| `--all-code` | flag | 否 | False | 使用全市场所有股票 |
| `--max-print` | int | 否 | 500 | 表格最大打印条数 |
| `--end` | string | 否 | 今天 | 分析截止日期 |
| `--no-volume` | flag | 否 | False | 关闭成交量过滤 |

**功能：** 分析最近N天内所有MACD金叉买入信号的次日表现

---

### 5.2 macd-intraday-pnl-from-db — 数据库信号盈亏统计 ★★

```bash
python main.py macd-intraday-pnl-from-db [--start 2026-06-29] [--end 2026-06-30] [--codes ...]
```

| 参数 | 类型 | 必填 | 默认 | 说明 |
|------|------|------|------|------|
| `--start` | string | 否 | None | 信号过滤起始日期（**只取 start 当天出现的信号**） |
| `--end` | string | 否 | None | 持有到该日收盘卖出；如当日无K线则自动拉实时行情 |
| `--codes` | string | 否 | 全部 | 指定股票代码过滤 |

**核心逻辑：**
1. 从 `trade_signals` 表读取 `signal_time = start_date` 的 MACD金叉信号（只筛选 start 当天的信号，end 不影响信号筛选）
2. 买入价 = 信号日 `open`；如信号日无日线数据 → 用信号日实时 `open`
3. 卖出价 = end_date `close`；如 end_date 无日线数据 → 自动调用新浪实时接口取最新价
4. 盈亏计算：`pnl = 卖出价 - 买入价`，`pnl_pct = (sell/buy - 1) × 100%`

**信号检测 → 买入价映射：**
| 信号来源（scan-market） | signal_time 含义 | buy_date | 买入价来源 |
|------------------------|-----------------|----------|-----------|
| 不传 --date（盘中扫描） | 扫描当日（今日） | = signal_time | 信号日日线 open，无 → 实时 open |
| 传 --date 2026-06-26（历史扫描） | 下一交易日（06-27/29） | = signal_time | 信号日日线 open |

**卖出价智能回退：**
| end_date 是否为交易日 | 卖出价来源 |
|----------------------|-----------|
| 是（有日线数据） | 日线 close |
| 否（今日/周末/数据未更新） | 新浪实时行情最新价 |

**输出格式：**
```
======================================================================
共 N 笔 MACD 买入信号（信号日 open 买入 → 2026-06-30 close 卖出）
  盈利 X / 亏损 Y / 持平 Z · 平均 +A% · 最高 +B% · 最低 -C%
----------------------------------------------------------------------
   1. 2026-06-29→2026-06-30( 2日) sh.600519 buy=1725.00 sell=1750.50 ++25.50  (+1.48%)
   2. 2026-06-29→2026-06-30( 2日) sz.000001 buy=13.820 sell=13.500 --0.320  (-2.32%)
...
======================================================================
```

**组合示例：**

| 场景 | 命令 | 说明 |
|------|------|------|
| 29号信号 → 30号卖出（含实时行情回退） | `python main.py macd-intraday-pnl-from-db --start 2026-06-29 --end 2026-06-30` | 今日无日线 → 自动拉实时价 |
| 29号信号 → 29号卖出（T+0验证） | `python main.py macd-intraday-pnl-from-db --start 2026-06-29 --end 2026-06-29` | 日内盈亏，有日线 |
| 指定股票 | `python main.py macd-intraday-pnl-from-db --start 2026-06-29 --end 2026-06-30 --codes sh.600519,sz.000001` | 只看指定标的 |
| 全部历史信号回看 | `python main.py macd-intraday-pnl-from-db --start 2026-06-01 --end 2026-06-30` | 只取 start 当天信号 |

---

## 6. 辅助工具

### 6.1 fetch-min-kline — 手动获取分钟K线

```bash
python main.py fetch-min-kline --code sh.600519 [--period 5min] [--datalen 1023]
```

| 参数 | 类型 | 必填 | 默认 | 说明 |
|------|------|------|------|------|
| `--code` | string | 是 | - | 股票代码 |
| `--period` | string | 否 | `5min` | 周期：`5min` / `15min` / `30min` / `60min` |
| `--datalen` | int | 否 | 1023 | 数据条数（最大1023，新浪接口限制） |

**功能：** 增量获取分钟K线并保存到数据库（自动跳过已有数据）

**组合示例：**

| 场景 | 命令 |
|------|------|
| 默认5分钟K线 | `python main.py fetch-min-kline --code sh.600519` |
| 15分钟K线 | `python main.py fetch-min-kline --code sh.600519 --period 15min` |

---

### 6.2 strategies — 列出所有可用策略

```bash
python main.py strategies
```

**输出：** 所有已注册的策略名称列表

---

## 常用工作流

### 工作流1：每日盘后分析

```bash
# 1. 更新日线数据（含信号计算）
python main.py update-daily

# 2. 查看今日信号
python main.py scan --date 2026-06-30 --type buy

# 3. 回测近半年表现
python main.py backtest --code sh.600519 --strategy MACD金叉 --start 2026-01-01 --end 2026-06-30
```

### 工作流2：盘中决策

```bash
# 1. 实时扫描 MACD金叉信号
python main.py scan-market --strategies MACD金叉 --no-volume

# 2. 查看最近信号的持仓盈亏
python main.py macd-intraday-pnl-from-db --start 2026-06-29 --end 2026-06-30
```

### 工作流3：策略选型

```bash
# 多股票多策略对比 + 推荐
python main.py compare-all --codes sh.600519,sz.000001,sh.600036 \
    --start 2026-01-01 --end 2026-06-30 \
    --top-n 3
```

### 工作流4：历史信号回看

```bash
# 周末回看26号那根K线是否出现金叉
python main.py scan-market --strategies MACD金叉 --date 2026-06-26 --no-save

# 查看那些信号如果持有到现在的盈亏
python main.py macd-intraday-pnl-from-db --start 2026-06-27 --end 2026-06-30
```

---

## 参数优先级说明

- `compare-all` 股票选择优先级：`--all-code` > `--codes` > 配置池 > 全市场
- 价格过滤只在 `scan-market` 中生效，回测命令不受影响
- `--no-volume` 只对 MACD金叉策略生效，其他策略不受影响
- `--date` 参数对 `scan-market` 有特殊语义（见 3.1 表格），其他命令的 `--date` 只是简单的日期过滤
