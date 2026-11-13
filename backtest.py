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
                "收盘": "close",
            }
        )
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").set_index("date")
        df = df[["close"]]

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

# 计算金叉/死叉信号
df["signal"] = 0
df.loc[(df["MA5"] > df["MA20"]) & (df["MA5"].shift(1) <= df["MA20"].shift(1)), "signal"] = 1   # 金叉
df.loc[(df["MA5"] < df["MA20"]) & (df["MA5"].shift(1) >= df["MA20"].shift(1)), "signal"] = -1  # 死叉

# ========== 回测 ==========
initial_capital = 100000  # 初始资金 10 万
commission_rate = 0.00025  # 手续费万 2.5

cash = initial_capital
shares = 0
trade_count = 0
equity_curve = []

for date, row in df.iterrows():
    price = row["close"]
    sig = row["signal"]

    if sig == 1 and cash > 0:
        # 金叉：全仓买入（按 100 股取整，扣除手续费）
        buy_shares = int(cash / (price * (1 + commission_rate)) // 100 * 100)
        if buy_shares > 0:
            fee = buy_shares * price * commission_rate
            cash -= buy_shares * price * (1 + commission_rate)
            shares = buy_shares
            trade_count += 1
            print(f"[买入] {date.date()} 价格 {price:.2f} 数量 {buy_shares} 手续费 {fee:.2f}")

    elif sig == -1 and shares > 0:
        # 死叉：全仓卖出（扣除手续费）
        fee = shares * price * commission_rate
        proceeds = shares * price * (1 - commission_rate)
        cash += proceeds
        trade_count += 1
        print(f"[卖出] {date.date()} 价格 {price:.2f} 数量 {shares} 手续费 {fee:.2f}")
        shares = 0

    # 每日账户净值 = 现金 + 持仓市值
    equity_curve.append(cash + shares * price)

# 回测结束时若仍持仓，按最后收盘价估值
final_equity = cash + shares * df["close"].iloc[-1]

# 计算最大回撤
equity_series = pd.Series(equity_curve, index=df.index)
running_max = equity_series.cummax()
drawdown = (equity_series - running_max) / running_max
max_drawdown = drawdown.min()

total_return = (final_equity - initial_capital) / initial_capital * 100

print("\n========== 回测结果 ==========")
print(f"初始资金: {initial_capital:,.2f}")
print(f"最终净值: {final_equity:,.2f}")
print(f"总收益率: {total_return:.2f}%")
print(f"交易次数: {trade_count}")
print(f"最大回撤: {max_drawdown * 100:.2f}%")
