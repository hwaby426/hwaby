# A股买卖点检测系统 - 功能概述

## 一、系统架构

```
├── main.py                    # 命令行入口
├── data_fetcher/              # 数据获取模块
│   ├── baostock_fetcher.py    # BaoStock日线数据
│   ├── sina_fetcher.py        # 新浪财经实时数据/分钟K线
│   ├── min_kline_builder.py   # 分钟K线合成器
│   └── min_kline_service.py   # 分钟K线存储服务
├── indicators/                # 技术指标计算（MyTT框架）
├── signals/                   # 策略信号模块（7种策略）
├── backtest/                  # 回测引擎（T+1规则）
├── analysis/                  # 策略对比分析
├── scheduler/                 # 任务调度（定时更新+实时监控）
└── db/                        # 数据库模块（MySQL）
```

## 二、核心功能

### 1. 数据获取

| 功能 | 命令 | 说明 |
|------|------|------|
| 全量初始化 | `python main.py init` | 日线+5min+15min，计算信号 |
| 更新日线 | `python main.py update-daily` | 每日收盘后更新 |
| 手动获取分钟K线 | `python main.py fetch-min-kline` | 5/15/30/60分钟 |

### 2. 交易策略（7种）

| 策略名称 | 适用行情 |
|---------|---------|
| 均线金叉 | 趋势行情 |
| MACD金叉 | 中长线趋势 |
| KDJ超买超卖 | 震荡行情 |
| BOLL突破 | 突破行情 |
| OBV量价 | 量价配合 |
| MACD+KDJ共振 | 稳健趋势 |
| 多因子打分 | 综合判断 |

### 3. 回测功能

| 功能 | 命令 | 说明 |
|------|------|------|
| 单股票回测 | `python main.py backtest` | 支持日线/分钟线 |
| 组合回测 | `python main.py backtest-portfolio` | 多股票等权 |
| 策略对比 | `python main.py compare` | 单股票多策略 |
| 综合对比 | `python main.py compare-all` | 多股票多策略+推荐 |

### 4. 实时监控

| 功能 | 命令 | 说明 |
|------|------|------|
| 实时盯盘 | `python main.py realtime` | 盘口监控+信号检测 |
| 调度器 | `python main.py scheduler` | 自动定时任务 |
| 盘中日线信号 | `python main.py intraday` | 14:30尾盘信号计算 |

### 5. 信号分析

| 功能 | 命令 | 说明 |
|------|------|------|
| 信号扫描 | `python main.py scan` | 扫描当日买卖信号 |
| 多周期共振 | `python main.py resonance` | 日线+分钟线共振 |
| 策略列表 | `python main.py strategies` | 列出所有策略 |

## 三、数据来源

| 数据类型 | 来源 | 更新频率 |
|---------|------|---------|
| 日线数据 | BaoStock | 每日20:30 |
| 实时行情 | 新浪财经 | 每3秒 |
| 5/15分钟K线 | 新浪财经 | 实时合成 |

## 四、关键特性

- **T+1/T+0双模式**：默认T+1（A股规则），支持T+0模式
- **增量更新**：只拉取新数据，不重复获取
- **优雅停止**：支持Ctrl+C安全退出
- **14:30尾盘信号**：盘中合成当日K线，提前判断买卖点

## 五、首次使用流程

```bash
# 1. 配置.env
cp .env.example .env
# 编辑数据库配置和股票池

# 2. 初始化数据库
python main.py init-db

# 3. 全量初始化（日线+分钟K线）
python main.py init --stock-pool sz002624,sz002612

# 4. 启动实时监控
python main.py scheduler
```

## 六、配置文件（.env）

```
DB_HOST=127.0.0.1
DB_PORT=3306
DB_USER=root
DB_PASSWORD=password
DB_NAME=stock_analysis

STOCK_POOL=sh600519,sz000001,sz300454

INITIAL_CAPITAL=100000
COMMISSION_RATE=0.0003
STAMP_DUTY_RATE=0.001

REALTIME_POLL_INTERVAL=3
DAILY_UPDATE_TIME=20:30
```