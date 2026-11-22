"""批量回测：从沪深300成分股中随机抽取 30 只，运行 4 组过滤场景并汇总。

复用 backtest_tushare 中的 fetch_latest_data / prepare_df / run_backtest，
口径与单只回测完全一致（初始资金 10 万、半仓、8% 止损、T+1 开盘执行、手续费万2.5、滑点 0.1%）。
"""
import os
import random
import time
from datetime import datetime, timedelta

import pandas as pd
import tushare as ts

from backtest_tushare import SCENARIOS, fetch_latest_data, load_from_csv, prepare_df, run_backtest

# 抽样参数：固定种子保证可复现
SAMPLE_SEED = 42
SAMPLE_SIZE = 30
# 每次拉取后短暂休眠，避免触发 tushare 接口限流
FETCH_SLEEP = 0.3
# 明细落盘路径
DETAIL_CSV = "output/batch_hs300_detail.csv"

# 获取成分股失败时的兜底列表（30 只沪深300代表性成分股）
FALLBACK_CODES = [
    "600519", "000858", "601318", "600036", "000333", "600276", "601166", "000651",
    "600887", "601888", "002594", "600030", "601012", "000725", "600009", "601398",
    "600585", "002415", "600900", "601899", "000001", "600028", "601628", "000568",
    "002475", "603259", "600690", "000002", "601668", "600031",
]


def fetch_hs300_codes():
    """获取沪深300最新成分股代码（6 位），失败返回兜底列表"""
    try:
        pro = ts.pro_api()
        today = datetime.now()
        start = (today - timedelta(days=90)).strftime("%Y%m%d")
        end = today.strftime("%Y%m%d")
        w = pro.index_weight(index_code="000300.SH", start_date=start, end_date=end)
        if w is None or w.empty:
            raise ValueError("index_weight 返回空")
        latest = w["trade_date"].max()
        codes = sorted(w.loc[w["trade_date"] == latest, "con_code"].tolist())
        # con_code 形如 600519.SH，取前 6 位
        codes = [c.split(".")[0] for c in codes]
        if len(codes) < 30:
            raise ValueError(f"成分股数量不足: {len(codes)}")
        print(f"获取沪深300成分股成功，共 {len(codes)} 只（最新交易日 {latest}）")
        return codes
    except Exception as e:
        print(f"获取沪深300成分股失败({type(e).__name__}: {e})，使用兜底列表")
        return FALLBACK_CODES


def run_one_stock(code):
    """对单只股票运行全部场景，返回结果行列表；失败返回空列表"""
    df = fetch_latest_data(code)
    if df is None:
        df = load_from_csv(code)
    # 量能共振需要 volume 列；旧 CSV 缺失则重拉一次
    if "volume" not in df.columns:
        fresh = fetch_latest_data(code)
        if fresh is not None and "volume" in fresh.columns:
            df = fresh
    exec_col = "open" if "open" in df.columns else "close"
    exec_label = "开盘价" if exec_col == "open" else "收盘价"
    df = prepare_df(df)

    rows = []
    for sc in SCENARIOS:
        r = run_backtest(
            df, exec_col, exec_label,
            rsi_threshold=sc["rsi_threshold"],
            vol_filter=sc["vol_filter"],
            verbose=False, silent=True,
        )
        closed = len(r["trades"])
        wins = sum(1 for t in r["trades"] if t["pnl"] > 0)
        rows.append({
            "code": code,
            "scenario": sc["name"],
            "trade_count": r["trade_count"],
            "closed": closed,
            "wins": wins,
            "total_return": r["total_return"],
            "max_drawdown": r["max_drawdown"],
        })
    return rows


def main():
    all_codes = fetch_hs300_codes()
    rng = random.Random(SAMPLE_SEED)
    codes = rng.sample(all_codes, min(SAMPLE_SIZE, len(all_codes)))
    print(f"\n样本：沪深300 随机抽取 {len(codes)} 只 (seed={SAMPLE_SEED})")
    print("股票池：", " ".join(codes))

    rows = []
    skipped = []
    for i, code in enumerate(codes, 1):
        print(f"\n[{i}/{len(codes)}] {code} ...", end=" ")
        try:
            stock_rows = run_one_stock(code)
            rows.extend(stock_rows)
            # 取该股票"无过滤"场景的收益作为单行摘要打印
            nr = next(r["total_return"] for r in stock_rows if r["scenario"] == "无过滤")
            print(f"完成 (无过滤收益 {nr:+.2f}%)")
        except Exception as e:
            print(f"跳过: {type(e).__name__}: {e}")
            skipped.append(code)
        time.sleep(FETCH_SLEEP)

    if not rows:
        print("\n无有效回测结果")
        return

    detail = pd.DataFrame(rows)
    os.makedirs("output", exist_ok=True)
    detail.to_csv(DETAIL_CSV, index=False, encoding="utf-8-sig")

    scenario_order = [s["name"] for s in SCENARIOS]
    n_stocks = detail["code"].nunique()

    # 按场景聚合
    agg = detail.groupby("scenario").agg(
        股票数=("code", "nunique"),
        交易总次数=("trade_count", "sum"),
        平仓总数=("closed", "sum"),
        盈利次数=("wins", "sum"),
        平均收益率=("total_return", "mean"),
        平均最大回撤=("max_drawdown", "mean"),
    ).reindex(scenario_order)
    agg["整体胜率"] = agg["盈利次数"] / agg["平仓总数"] * 100
    # 各场景下取得正收益的股票数
    profitable = (
        detail[detail["total_return"] > 0]
        .groupby("scenario")["code"].nunique()
        .reindex(scenario_order).fillna(0).astype(int)
    )
    agg["盈利股票数"] = profitable

    # 打印汇总大表
    print("\n" + "=" * 96)
    print(f"沪深300 样本 {n_stocks} 只股票回测汇总（每只独立 10 万初始资金）")
    if skipped:
        print(f"跳过 {len(skipped)} 只：{' '.join(skipped)}")
    print("=" * 96)
    header = f"{'场景':<12} {'股票数':>6} {'交易总次数':>10} {'整体胜率':>9} {'平均收益率':>11} {'盈利股票数':>10} {'平均最大回撤':>12}"
    print(header)
    print("-" * 96)
    for name, row in agg.iterrows():
        wr = f"{row['整体胜率']:.1f}%" if row["平仓总数"] > 0 else "-"
        print(f"{name:<12} {int(row['股票数']):>6} {int(row['交易总次数']):>10} {wr:>9} "
              f"{row['平均收益率']:>10.2f}% {int(row['盈利股票数']):>10} {row['平均最大回撤']:>11.2f}%")
    print("-" * 96)
    print(f"明细已保存：{DETAIL_CSV}")

    # 每只股票各场景收益率透视表
    pivot = detail.pivot(index="code", columns="scenario", values="total_return").reindex(columns=scenario_order)
    print("\n各股票分场景收益率(%)：")
    print(pivot.round(2).to_string())


if __name__ == "__main__":
    main()
