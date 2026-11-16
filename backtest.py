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

# 执行价优先用 open（T+1 开盘），若数据中没有 open 列则回退到 close
exec_col = "open" if "open" in df.columns else "close"
exec_label = "开盘价" if exec_col == "open" else "收盘价"
if exec_col == "close":
    print("警告: 数据中无 open 列，执行价回退为当日收盘价")

# 计算金叉/死叉信号（基于 T 日收盘价，T 日收盘后才能确认）
df["signal"] = 0
df.loc[(df["MA5"] > df["MA20"]) & (df["MA5"].shift(1) <= df["MA20"].shift(1)), "signal"] = 1   # 金叉
df.loc[(df["MA5"] < df["MA20"]) & (df["MA5"].shift(1) >= df["MA20"].shift(1)), "signal"] = -1  # 死叉

# 信号在 T 日收盘后生成，T+1 日才能执行，将信号顺延一日避免未来函数
df["execute_signal"] = df["signal"].shift(1).fillna(0)

# ========== 回测 ==========
initial_capital = 100000  # 初始资金 10 万
commission_rate = 0.00025  # 手续费万 2.5
slippage_rate = 0.001  # 滑点 0.1%
position_ratio = 0.5  # 每次买入使用 50% 现金
stop_loss_rate = 0.08  # 止损：买入后亏损 8% 卖出

cash = initial_capital
shares = 0
buy_price = 0.0  # 买入成交价（用于止损判断）
buy_cost = 0.0   # 买入总成本（含滑点手续费，用于盈亏计算）
trade_count = 0
equity_curve = []
total_commission = 0.0
total_slippage = 0.0
consecutive_losses = 0
max_consecutive_losses = 0
pending_stop_loss = False  # 前一日收盘触发止损，待当日开盘执行

for date, row in df.iterrows():
    exec_price = row[exec_col]  # 执行价（T+1 开盘）
    close_price = row["close"]  # 用于每日持仓估值和止损判断
    sig = row["execute_signal"]  # 前一日（T 日）收盘后生成的信号

    if pending_stop_loss and shares > 0:
        # 止损卖出（前一日收盘已触发，今日开盘执行）
        fee = shares * exec_price * commission_rate
        slip = shares * exec_price * slippage_rate
        proceeds = shares * exec_price * (1 - slippage_rate - commission_rate)
        cash += proceeds
        pnl = proceeds - buy_cost
        trade_count += 1
        total_commission += fee
        total_slippage += slip
        if pnl < 0:
            consecutive_losses += 1
            max_consecutive_losses = max(max_consecutive_losses, consecutive_losses)
        else:
            consecutive_losses = 0
        print(f"[止损] 执行日 {date.date()} {exec_label} {exec_price:.2f} 数量 {shares} 盈亏 {pnl:.2f} 手续费 {fee:.2f} 滑点 {slip:.2f}")
        shares = 0
        buy_price = 0.0
        buy_cost = 0.0
        pending_stop_loss = False

    elif sig == 1 and shares == 0 and cash > 0:
        # 金叉买入：只用 position_ratio 比例的现金
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
            trade_count += 1
            total_commission += fee
            total_slippage += slip
            print(f"[买入] 执行日 {date.date()} {exec_label} {exec_price:.2f} 数量 {buy_shares} 手续费 {fee:.2f} 滑点 {slip:.2f}")

    elif sig == -1 and shares > 0:
        # 死叉卖出
        fee = shares * exec_price * commission_rate
        slip = shares * exec_price * slippage_rate
        proceeds = shares * exec_price * (1 - slippage_rate - commission_rate)
        cash += proceeds
        pnl = proceeds - buy_cost
        trade_count += 1
        total_commission += fee
        total_slippage += slip
        if pnl < 0:
            consecutive_losses += 1
            max_consecutive_losses = max(max_consecutive_losses, consecutive_losses)
        else:
            consecutive_losses = 0
        print(f"[卖出] 执行日 {date.date()} {exec_label} {exec_price:.2f} 数量 {shares} 盈亏 {pnl:.2f} 手续费 {fee:.2f} 滑点 {slip:.2f}")
        shares = 0
        buy_price = 0.0
        buy_cost = 0.0

    # 检查当日收盘是否触发止损（下一交易日开盘执行）
    if shares > 0:
        stop_price = buy_price * (1 - stop_loss_rate)
        pending_stop_loss = close_price <= stop_price
    else:
        pending_stop_loss = False

    # 每日账户净值 = 现金 + 持仓市值（按收盘价估值）
    equity_curve.append(cash + shares * close_price)

# 回测结束时若仍持仓，按最后收盘价估值
final_equity = cash + shares * df["close"].iloc[-1]

# 计算最大回撤
equity_series = pd.Series(equity_curve, index=df.index)
running_max = equity_series.cummax()
drawdown = (equity_series - running_max) / running_max
max_drawdown = drawdown.min()

net_profit = final_equity - initial_capital
total_return = net_profit / initial_capital * 100

print("\n========== 回测结果 ==========")
print(f"初始资金: {initial_capital:,.2f}")
print(f"最终净值: {final_equity:,.2f}")
print(f"净收益: {net_profit:,.2f}")
print(f"总收益率: {total_return:.2f}%")
print(f"交易次数: {trade_count}")
print(f"总手续费: {total_commission:,.2f}")
print(f"总滑点损耗: {total_slippage:,.2f}")
print(f"最大回撤: {max_drawdown * 100:.2f}%")
print(f"连续亏损次数: {max_consecutive_losses}")
