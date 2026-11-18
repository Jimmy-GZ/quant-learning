import argparse
import os
import pandas as pd
import mplfinance as mpf
from datetime import datetime, timedelta

import tushare as ts

# 解析命令行参数：股票代码
parser = argparse.ArgumentParser(description="绘制A股日K线及MA5/MA20均线图（Tushare 数据源）")
parser.add_argument("code", nargs="?", default="000100", help="股票代码，如 000100")
args = parser.parse_args()
code = args.code

# 根据股票代码判断交易所后缀：6 开头为沪市，0/3 开头为深市
if code.startswith("6"):
    ts_code = f"{code}.SH"
elif code.startswith(("0", "3")):
    ts_code = f"{code}.SZ"
else:
    ts_code = f"{code}.BJ"

# Tushare Token：优先读取环境变量，否则使用本地缓存的 token
token = os.environ.get("TUSHARE_TOKEN") or ts.get_token()
if not token:
    raise SystemExit("未设置 Tushare Token，请先执行 ts.set_token('你的token') 或设置环境变量 TUSHARE_TOKEN")

ts.set_token(token)

# 下载近1年日线数据（前复权）
today = datetime.now()
end_date = today.strftime("%Y%m%d")
start_date = (today - timedelta(days=360)).strftime("%Y%m%d")

df = ts.pro_bar(
    ts_code=ts_code,
    adj="qfq",
    start_date=start_date,
    end_date=end_date,
)

# Tushare 返回的列为中文语义但列名是英文：trade_date, open, high, low, close, vol
df = df.rename(columns={"vol": "volume"})
df["date"] = pd.to_datetime(df["trade_date"])
df = df.sort_values("date").set_index("date")
df = df[["open", "high", "low", "close", "volume"]]

# 计算 MA5 和 MA20
df["MA5"] = df["close"].rolling(window=5).mean()
df["MA20"] = df["close"].rolling(window=20).mean()

df = df.dropna()

# 导出数据到 CSV（含 OHLCV 及 MA5/MA20）
os.makedirs("output", exist_ok=True)
df.reset_index()[["date", "open", "close", "MA5", "MA20"]].to_csv(f"output/ma_plot_tushare_{code}.csv", index=False, encoding="utf-8-sig")

# 自定义样式：红涨绿跌（A股习惯），并配置中文字体
style = mpf.make_mpf_style(
    base_mpf_style="yahoo",
    marketcolors=mpf.make_marketcolors(up="r", down="g", inherit=True),
    rc={
        "font.family": "Microsoft YaHei",
        "font.weight": "normal",
        "axes.titleweight": "normal",
        "axes.labelweight": "normal",
        "figure.titleweight": "normal",
        "axes.unicode_minus": False,
    },
)

df["signal"] = 0
df.loc[(df["MA5"] > df["MA20"]) & (df["MA5"].shift(1) <= df["MA20"].shift(1)), "signal"] = 1   # 金叉
df.loc[(df["MA5"] < df["MA20"]) & (df["MA5"].shift(1) >= df["MA20"].shift(1)), "signal"] = -1  # 死叉

# 打印所有金叉/死叉信号
signals = df[df["signal"] != 0]
print(signals[["close", "MA5", "MA20", "signal"]])
print(f"\n金叉(买入)次数: {(df['signal'] == 1).sum()}")
print(f"死叉(卖出)次数: {(df['signal'] == -1).sum()}")

mpf.plot(
    df,
    type="candle",
    title=f"{code} K线图(MA5+MA20) [Tushare]",
    ylabel="价格",
    addplot=[
        mpf.make_addplot(df["MA5"], color="orange", width=1.5, label="MA5"),
        mpf.make_addplot(df["MA20"], color="blue", width=1.5, label="MA20"),
    ],
    style=style,
    volume=True,
    figratio=(12, 6),
    show_nontrading=False,
    savefig=f"output/ma_plot_tushare_{code}.png",
)
