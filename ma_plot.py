import argparse
import os
import akshare as ak
import pandas as pd
import mplfinance as mpf
from datetime import datetime, timedelta

# 解析命令行参数：股票代码
parser = argparse.ArgumentParser(description="绘制A股日K线及MA5/MA20均线图")
parser.add_argument("code", nargs="?", default="000100", help="股票代码，如 000100")
args = parser.parse_args()
code = args.code

# 下载近1年日线数据
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

# 重命名列并设置 DatetimeIndex，mplfinance 需要标准 OHLCV 列名
df = df.rename(
    columns={
        "日期": "date",
        "开盘": "open",
        "最高": "high",
        "最低": "low",
        "收盘": "close",
        "成交量": "volume",
    }
)
df["date"] = pd.to_datetime(df["date"])
df = df.sort_values("date").set_index("date")
df = df[["open", "high", "low", "close", "volume"]]

# 计算 MA5 和 MA20
df["MA5"] = df["close"].rolling(window=5).mean()
df["MA20"] = df["close"].rolling(window=20).mean()

df = df.dropna()

# 导出数据到 CSV（含 OHLCV 及 MA5/MA20）
os.makedirs("output", exist_ok=True)
df.reset_index()[["date", "close", "MA5", "MA20"]].to_csv(f"output/ma_plot_{code}.csv", index=False, encoding="utf-8-sig")

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

mpf.plot(
    df,
    type="candle",
    title=f"{code} K线图(MA5+MA20)",
    ylabel="价格",
    addplot=[
        mpf.make_addplot(df["MA5"], color="orange", width=1.5, label="MA5"),
        mpf.make_addplot(df["MA20"], color="blue", width=1.5, label="MA20"),
    ],
    style=style,
    volume=True,
    figratio=(12, 6),
    show_nontrading=False,
    savefig=f"output/ma_plot_{code}.png",
)