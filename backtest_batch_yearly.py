"""按年度分组的批量回测：将近 3 年切分成 3 个年度窗口，分别回测 30 只沪深300。

复用 backtest_tushare 的 fetch_latest_data / prepare_df / run_backtest 与 backtest_batch 的成分股抽样，
口径与单只回测一致（初始资金 10 万、半仓、8% 止损、T+1 开盘执行、手续费万2.5、滑点 0.1%）。

为避免每个年度窗口开头缺少 MA20/RSI/VOL_MA20 的预热数据，统一拉取 4 年数据，
在完整数据上计算全部指标后再按窗口切片；窗口内首日的 execute_signal 基于完整数据前一日，无未来函数。
每个年度窗口独立以 10 万初始资金回测，便于跨年对比。
"""
import os
import random
import time
from datetime import datetime, timedelta

import pandas as pd

from backtest_batch import FETCH_SLEEP, SAMPLE_SEED, SAMPLE_SIZE, fetch_hs300_codes
from backtest_tushare import SCENARIOS, fetch_latest_data, load_from_csv, prepare_df, run_backtest

# 拉取 4 年数据：3 年回测 + 1 年预热（供最早窗口计算 MA20/RSI/VOL_MA20）
FETCH_YEARS = 4
# 明细落盘路径
DETAIL_CSV = "output/batch_hs300_yearly_detail.csv"


def build_windows():
    """以今天为基准，切出 3 个连续年度窗口（半开区间，最后一个闭区间，互不重叠）"""
    today = datetime.now()
    return [
        ("2023-2024", today - timedelta(days=365 * 3), today - timedelta(days=365 * 2)),
        ("2024-2025", today - timedelta(days=365 * 2), today - timedelta(days=365 * 1)),
        ("2025-2026", today - timedelta(days=365 * 1), today),
    ]


def slice_window(df, start, end, is_last):
    """按日期窗口切片，前 N-1 个窗口左闭右开，最后一个左闭右闭"""
    if is_last:
        mask = (df.index >= start) & (df.index <= end)
    else:
        mask = (df.index >= start) & (df.index < end)
    return df.loc[mask].copy()


def run_one_stock_yearly(code, windows):
    """对单只股票的全部年度窗口、全部场景回测，返回结果行列表；失败抛异常"""
    df = fetch_latest_data(code, years=FETCH_YEARS)
    if df is None:
        df = load_from_csv(code)
    if "volume" not in df.columns:
        fresh = fetch_latest_data(code, years=FETCH_YEARS)
        if fresh is not None and "volume" in fresh.columns:
            df = fresh
    exec_col = "open" if "open" in df.columns else "close"
    exec_label = "开盘价" if exec_col == "open" else "收盘价"
    df = prepare_df(df)  # 在完整 4 年数据上计算指标，确保各窗口首日指标已预热

    rows = []
    for idx, (name, start, end) in enumerate(windows):
        df_w = slice_window(df, start, end, is_last=(idx == len(windows) - 1))
        if df_w.empty:
            continue  # 该股票上市晚于窗口起点，跳过该窗口
        for sc in SCENARIOS:
            r = run_backtest(
                df_w, exec_col, exec_label,
                vol_filter=sc["vol_filter"],
                verbose=False, silent=True,
            )
            closed = len(r["trades"])
            wins = sum(1 for t in r["trades"] if t["pnl"] > 0)
            rows.append({
                "period": name,
                "code": code,
                "scenario": sc["name"],
                "trade_count": r["trade_count"],
                "closed": closed,
                "wins": wins,
                "total_return": r["total_return"],
                "max_drawdown": r["max_drawdown"],
            })
    return rows


def print_period_table(detail, period_name, windows):
    """打印单个年度窗口的汇总表"""
    scenario_order = [s["name"] for s in SCENARIOS]
    sub = detail[detail["period"] == period_name]
    n_stocks = sub["code"].nunique()

    agg = sub.groupby("scenario").agg(
        股票数=("code", "nunique"),
        交易总次数=("trade_count", "sum"),
        平仓总数=("closed", "sum"),
        盈利次数=("wins", "sum"),
        平均收益率=("total_return", "mean"),
        平均最大回撤=("max_drawdown", "mean"),
    ).reindex(scenario_order)
    agg["整体胜率"] = agg["盈利次数"] / agg["平仓总数"] * 100
    profitable = (
        sub[sub["total_return"] > 0]
        .groupby("scenario")["code"].nunique()
        .reindex(scenario_order).fillna(0).astype(int)
    )
    agg["盈利股票数"] = profitable

    # 找到该窗口的实际起止日期用于表头
    win = next(w for w in windows if w[0] == period_name)
    print(f"\n{'=' * 96}")
    print(f"年度窗口：{period_name}（{win[1].date()} ~ {win[2].date()}）  样本 {n_stocks} 只（每只独立 10 万初始资金）")
    print("=" * 96)
    print(f"{'场景':<12} {'股票数':>6} {'交易总次数':>10} {'整体胜率':>9} {'平均收益率':>11} {'盈利股票数':>10} {'平均最大回撤':>12}")
    print("-" * 96)
    for name, row in agg.iterrows():
        wr = f"{row['整体胜率']:.1f}%" if row["平仓总数"] > 0 else "-"
        print(f"{name:<12} {int(row['股票数']):>6} {int(row['交易总次数']):>10} {wr:>9} "
              f"{row['平均收益率']:>10.2f}% {int(row['盈利股票数']):>10} {row['平均最大回撤']:>11.2f}%")
    print("-" * 96)
    return agg


def main():
    windows = build_windows()
    print("年度窗口：")
    for name, start, end in windows:
        print(f"  {name}: {start.date()} ~ {end.date()}")

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
            stock_rows = run_one_stock_yearly(code, windows)
            rows.extend(stock_rows)
            # 用该股票全部窗口"无过滤"收益之和作为单行摘要
            nr = sum(r["total_return"] for r in stock_rows if r["scenario"] == "无过滤")
            print(f"完成 (无过滤累计收益 {nr:+.2f}%)")
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
    print(f"\n明细已保存：{DETAIL_CSV}")
    if skipped:
        print(f"跳过 {len(skipped)} 只：{' '.join(skipped)}")

    # 逐个年度窗口打印汇总表
    for name, _, _ in windows:
        print_period_table(detail, name, windows)

    # 各年度窗口分场景平均收益率透视（跨年对比一目了然）
    scenario_order = [s["name"] for s in SCENARIOS]
    period_order = [w[0] for w in windows]
    pivot = detail.pivot_table(index="period", columns="scenario", values="total_return", aggfunc="mean")
    pivot = pivot.reindex(index=period_order, columns=scenario_order)
    print("\n" + "=" * 60)
    print("跨年对比：各年度窗口分场景平均收益率(%)")
    print("=" * 60)
    print(pivot.round(2).to_string())


if __name__ == "__main__":
    main()
