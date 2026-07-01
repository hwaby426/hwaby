## 全市场每日更新(更新所有股票的数据)
python main.py update-market



## 全市场盘中扫描
--signal-type all (买入和卖出信号,默认买入)
--no-volume(关闭量比, 适合早盘查询信号)

// 不带codes 扫描全市场
python main.py scan-market  --strategies MACD金叉  --date 2026-06-29  --codes sz.300454 


## 单股票多策略对比
python main.py compare \
  --code sz.300454 \
  --strategies MACD金叉 \
  --start 2026-01-01



## 分析所有股票最近 10 天 MACD 买入信号的收益
python main.py analyze-macd


## 回测所有股票(--no-volume 不分析量比)
1. 回测全市场
```
python main.py compare-all --strategies MACD金叉 --start 2026-01-01 --end 2026-06-26  --all-code
```

2. 指定股票回测
```
python main.py compare-all --strategies MACD金叉 --start 2026-01-01 --end 2026-06-26  --codes sz.300136
```

3. 所有股票回测
```
python main.py compare-all --strategies MACD金叉 --start 2026-01-01 --end 2026-06-26 --all-code 
```


## 计算最近N个交易日出现信号的股票, 直到信号买入,计算盈亏
python main.py analyze_macd_signals


## 计算指定某天出现信号的数据, 在end结束日期的close价格的盈利情况(示例数据为26号出现信号的数据, 计算27号买入到30号收盘的盈利)
python main.py macd-intraday-pnl-from-db --start  2026-06-26  --end 2026-06-30