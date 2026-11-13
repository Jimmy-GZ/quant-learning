# quant-learning

A股双均线（MA5/MA20）金叉死叉策略的可视化与回测脚本。

## 依赖

```
pip install akshare pandas mplfinance matplotlib
```

## 文件说明

| 文件 | 功能 |
|------|------|
| `ma_plot.py` | 下载个股日线数据，绘制 K 线图 + MA5/MA20 均线 + 成交量，并标记金叉/死叉信号 |
| `backtest.py` | 基于 MA5/MA20 金叉死叉策略进行回测，输出最终净值、交易次数、最大回撤 |

## ma_plot.py — K 线图绘制

```bash
python ma_plot.py [股票代码]
```

- 股票代码默认为 `000100`（TCL 科技）
- 数据来源：akshare（东方财富），前复权，近 1 年日线
- 输出文件：
  - `output/ma_plot_{代码}.png` — K 线图（红涨绿跌，含 MA5/MA20、成交量副图）
  - `output/ma_plot_{代码}.csv` — 日期、收盘价、MA5、MA20

示例：

```bash
python ma_plot.py 600519
```

## backtest.py — 金叉死叉回测

```bash
python backtest.py [股票代码]
```

### 策略规则

- 初始资金：100,000 元
- 手续费：万 2.5（买卖双向收取）
- 买入：MA5 上穿 MA20（金叉），全仓买入，按 100 股取整
- 卖出：MA5 下穿 MA20（死叉），全仓卖出
- 信号基于当日收盘价计算并执行

### 数据获取逻辑

1. 优先通过 akshare 获取最新数据，成功后保存到 `output/ma_plot_{代码}.csv`
2. 若获取失败（如接口限流），自动回退到本地已有的 CSV 文件

### 输出

- 每笔交易的日期、价格、数量、手续费
- 最终账户净值、总收益率、交易次数、最大回撤

## 输出目录

`output/` 下生成的文件（已加入 `.gitignore`）：

- `ma_plot_{代码}.png` — K 线图
- `ma_plot_{代码}.csv` — 行情数据（date, close, MA5, MA20）
