import argparse
import os
from datetime import datetime, timedelta

import pandas as pd

# 解析命令行参数：股票代码
parser = argparse.ArgumentParser(description="MA5/MA20 双均线金叉死叉回测")
parser.add_argument("code", nargs="?", default="000100", help="股票代码，如 000100")
args = parser.parse_args()
code = args.code

CSV_PATH = f"output/ma_plot_{code}.csv"


def fetch_latest_data(code):
    """通过 akshare 获取最新日线数据，失败则返回 None"""
    try:
        import akshare as ak

        today = datetime.now()
        end_date = today.strftime("%Y%m%d")
        start_date = (today - timedelta(days=360)).strftime("%Y%m%d")

        df = ak.stock_zh_a_hist(
            symbol=code,
            period="daily",
            start_date=start_date,
            end_date=end_date,
            adjust="qfq",
        )

        df = df.rename(
            columns={
                "日期": "date",
                "开盘": "open",
                "收盘": "close",
            }
        )
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").set_index("date")
        df = df[["open", "close"]]

        # 计算 MA5 和 MA20
        df["MA5"] = df["close"].rolling(window=5).mean()
        df["MA20"] = df["close"].rolling(window=20).mean()
        df = df.dropna()

        # 保存到 CSV 供下次回退使用
        os.makedirs("output", exist_ok=True)
        df.reset_index().to_csv(CSV_PATH, index=False, encoding="utf-8-sig")
        print(f"数据来源: akshare 最新数据 (共 {len(df)} 条)")
        return df

    except Exception as e:
        print(f"akshare 获取失败: {type(e).__name__}: {e}")
        return None


def load_from_csv():
    """从已有的 CSV 文件加载数据"""
    if not os.path.exists(CSV_PATH):
        raise FileNotFoundError(f"找不到 {CSV_PATH}，且 akshare 获取失败，无法回测")

    df = pd.read_csv(CSV_PATH, parse_dates=["date"]).set_index("date")
    print(f"数据来源: 本地 CSV ({CSV_PATH})，共 {len(df)} 条")
    return df


# 优先获取最新数据，失败则回退到本地 CSV
df = fetch_latest_data(code)
if df is None:
    df = load_from_csv()

# 计算标的 20 日年化波动率：基于收盘价日收益率的 20 日滚动标准差，年化（×√252），取全期均值
# 该指标是标的本身的属性，与策略/RSI 过滤无关，三组场景共用同一个值
close_returns = df["close"].pct_change()
volatility_20d = close_returns.rolling(window=20).std() * (252 ** 0.5)
volatility_20d = volatility_20d.mean() * 100  # 转为百分比
print(f"标的 20 日年化波动率: {volatility_20d:.2f}%")

# 执行价优先用 open（T+1 开盘），若数据中没有 open 列则回退到 close
exec_col = "open" if "open" in df.columns else "close"
exec_label = "开盘价" if exec_col == "open" else "收盘价"
if exec_col == "close":
    print("警告: 数据中无 open 列，执行价回退为当日收盘价")

# 计算 RSI(14)
delta = df["close"].diff()
gain = delta.clip(lower=0)
loss = -delta.clip(upper=0)
avg_gain = gain.rolling(window=14).mean()
avg_loss = loss.rolling(window=14).mean()
rs = avg_gain / avg_loss
df["RSI"] = 100 - (100 / (1 + rs))
df = df.dropna()  # RSI 计算需要 14 日，丢弃前 13 行

# 计算金叉/死叉信号（基于 T 日收盘价，T 日收盘后才能确认）
df["signal"] = 0
df.loc[(df["MA5"] > df["MA20"]) & (df["MA5"].shift(1) <= df["MA20"].shift(1)), "signal"] = 1   # 金叉
df.loc[(df["MA5"] < df["MA20"]) & (df["MA5"].shift(1) >= df["MA20"].shift(1)), "signal"] = -1  # 死叉

# 信号在 T 日收盘后生成，T+1 日才能执行，将信号顺延一日避免未来函数
df["execute_signal"] = df["signal"].shift(1).fillna(0)


def run_backtest(df, exec_col, exec_label, rsi_threshold=None, verbose=False):
    """运行单组回测，rsi_threshold 为 None 表示不启用 RSI 过滤"""
    initial_capital = 100000
    commission_rate = 0.00025
    slippage_rate = 0.001
    position_ratio = 0.5
    stop_loss_rate = 0.08

    cash = initial_capital
    shares = 0
    buy_price = 0.0
    buy_cost = 0.0
    buy_date = None
    buy_rsi = 0.0
    trade_count = 0
    equity_curve = []
    total_commission = 0.0
    total_slippage = 0.0
    consecutive_losses = 0
    max_consecutive_losses = 0
    win_count = 0
    sell_count = 0
    pending_stop_loss = False
    trades = []  # 记录每笔完整交易

    for date, row in df.iterrows():
        exec_price = row[exec_col]
        close_price = row["close"]
        sig = row["execute_signal"]

        if pending_stop_loss and shares > 0:
            fee = shares * exec_price * commission_rate
            slip = shares * exec_price * slippage_rate
            proceeds = shares * exec_price * (1 - slippage_rate - commission_rate)
            cash += proceeds
            pnl = proceeds - buy_cost
            trade_count += 1
            sell_count += 1
            total_commission += fee
            total_slippage += slip
            if pnl < 0:
                consecutive_losses += 1
                max_consecutive_losses = max(max_consecutive_losses, consecutive_losses)
            else:
                consecutive_losses = 0
                win_count += 1
            trades.append({
                "buy_date": buy_date,
                "buy_price": buy_price,
                "buy_rsi": buy_rsi,
                "sell_date": date.date(),
                "sell_price": exec_price,
                "sell_reason": "止损",
                "pnl": pnl,
            })
            if verbose:
                print(f"[止损] 执行日 {date.date()} {exec_label} {exec_price:.2f} 数量 {shares} 盈亏 {pnl:.2f}")
            shares = 0
            buy_price = 0.0
            buy_cost = 0.0
            buy_date = None
            pending_stop_loss = False

        elif sig == 1 and shares == 0 and cash > 0:
            # 金叉买入：若启用 RSI 过滤，要求 RSI < 阈值
            if rsi_threshold is not None and row["RSI"] >= rsi_threshold:
                pass  # RSI 不满足，跳过买入
            else:
                invest_cash = cash * position_ratio
                buy_shares = int(invest_cash / (exec_price * (1 + slippage_rate + commission_rate)) // 100 * 100)
                if buy_shares > 0:
                    fee = buy_shares * exec_price * commission_rate
                    slip = buy_shares * exec_price * slippage_rate
                    cost = buy_shares * exec_price * (1 + slippage_rate + commission_rate)
                    cash -= cost
                    shares = buy_shares
                    buy_price = exec_price
                    buy_cost = cost
                    buy_date = date.date()
                    buy_rsi = row["RSI"]
                    trade_count += 1
                    total_commission += fee
                    total_slippage += slip
                    if verbose:
                        print(f"[买入] 执行日 {date.date()} {exec_label} {exec_price:.2f} 数量 {buy_shares} RSI {row['RSI']:.1f}")

        elif sig == -1 and shares > 0:
            fee = shares * exec_price * commission_rate
            slip = shares * exec_price * slippage_rate
            proceeds = shares * exec_price * (1 - slippage_rate - commission_rate)
            cash += proceeds
            pnl = proceeds - buy_cost
            trade_count += 1
            sell_count += 1
            total_commission += fee
            total_slippage += slip
            if pnl < 0:
                consecutive_losses += 1
                max_consecutive_losses = max(max_consecutive_losses, consecutive_losses)
            else:
                consecutive_losses = 0
                win_count += 1
            trades.append({
                "buy_date": buy_date,
                "buy_price": buy_price,
                "buy_rsi": buy_rsi,
                "sell_date": date.date(),
                "sell_price": exec_price,
                "sell_reason": "死叉",
                "pnl": pnl,
            })
            if verbose:
                print(f"[卖出] 执行日 {date.date()} {exec_label} {exec_price:.2f} 数量 {shares} 盈亏 {pnl:.2f}")
            shares = 0
            buy_price = 0.0
            buy_cost = 0.0
            buy_date = None

        # 检查当日收盘是否触发止损
        if shares > 0:
            stop_price = buy_price * (1 - stop_loss_rate)
            pending_stop_loss = close_price <= stop_price
        else:
            pending_stop_loss = False

        equity_curve.append(cash + shares * close_price)

    final_equity = cash + shares * df["close"].iloc[-1]
    equity_series = pd.Series(equity_curve, index=df.index)
    running_max = equity_series.cummax()
    drawdown = (equity_series - running_max) / running_max
    max_drawdown = drawdown.min()

    net_profit = final_equity - initial_capital
    total_return = net_profit / initial_capital * 100
    win_rate = win_count / sell_count * 100 if sell_count > 0 else 0.0

    # 打印交易明细表
    rsi_note = f"RSI < {rsi_threshold}" if rsi_threshold is not None else "无 RSI 过滤"
    print(f"\n--- 交易明细 ({rsi_note}) ---")
    if trades:
        print(f"{'买入日期':<12} {'买入价':>8} {'RSI':>6} {'卖出日期':<12} {'卖出价':>8} {'卖出原因':>8} {'盈亏':>10}")
        print("-" * 70)
        for t in trades:
            print(f"{str(t['buy_date']):<12} {t['buy_price']:>8.2f} {t['buy_rsi']:>6.1f} {str(t['sell_date']):<12} {t['sell_price']:>8.2f} {t['sell_reason']:>8} {t['pnl']:>10.2f}")
    else:
        print("无交易")

    return {
        "final_equity": final_equity,
        "net_profit": net_profit,
        "total_return": total_return,
        "trade_count": trade_count,
        "win_rate": win_rate,
        "max_drawdown": max_drawdown * 100,
        "max_consecutive_losses": max_consecutive_losses,
        "total_commission": total_commission,
        "total_slippage": total_slippage,
        "trades": trades,
    }


# ========== 运行三组对比 ==========
scenarios = [
    ("无 RSI 过滤", None),
    ("RSI < 50", 50),
    ("RSI < 45", 45),
]

results = []
for name, threshold in scenarios:
    print(f"\n{'=' * 60}")
    print(f"场景: {name}")
    print(f"{'=' * 60}")
    r = run_backtest(df, exec_col, exec_label, rsi_threshold=threshold, verbose=False)
    r["name"] = name
    results.append(r)
    print(f"\n--- {name} 结果 ---")
    print(f"最终净值: {r['final_equity']:,.2f}")
    print(f"净收益: {r['net_profit']:,.2f}")
    print(f"总收益率: {r['total_return']:.2f}%")
    print(f"交易次数: {r['trade_count']}")
    print(f"胜率: {r['win_rate']:.1f}%")
    print(f"最大回撤: {r['max_drawdown']:.2f}%")
    print(f"连续亏损次数: {r['max_consecutive_losses']}")

# ========== 对比汇总表 ==========
print(f"\n{'=' * 70}")
print("过滤前后对比")
print(f"{'=' * 70}")
print(f"{'场景':<14} {'交易次数':>8} {'胜率':>8} {'最大回撤':>10} {'总收益率':>10} {'最终净值':>12}")
print("-" * 70)
for r in results:
    print(f"{r['name']:<14} {r['trade_count']:>8} {r['win_rate']:>7.1f}% {r['max_drawdown']:>9.2f}% {r['total_return']:>9.2f}% {r['final_equity']:>12,.2f}")
