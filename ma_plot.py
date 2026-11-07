import akshare as ak
import pandas as pd
import matplotlib.pyplot as plt
import os

# 下载平安银行近3年日线数据
df = ak.stock_zh_a_hist(symbol="000100", period="daily", adjust="qfq")

# 保留需要的列并转换日期
df = df[["日期", "收盘"]].rename(columns={"日期": "date", "收盘": "close"})
df["date"] = pd.to_datetime(df["date"])
df = df.sort_values("date").reset_index(drop=True)

# 计算 MA5 和 MA20
df["ma5"] = df["close"].rolling(5).mean()
df["ma20"] = df["close"].rolling(20).mean()

# 画图
plt.figure(figsize=(12, 6))
plt.plot(df["date"], df["close"], label="Close", alpha=0.7)
plt.plot(df["date"], df["ma5"], label="MA5", linewidth=1.5)
plt.plot(df["date"], df["ma20"], label="MA20", linewidth=1.5)
plt.legend()
plt.title("000100 TCL - MA5 / MA20")
plt.tight_layout()
os.makedirs("output", exist_ok=True)
plt.savefig("output/ma_plot.png", dpi=150)
plt.savefig("output/ma_plot.png", dpi=150)
plt.show()