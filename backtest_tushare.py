import argparse
import os
from datetime import datetime, timedelta

import pandas as pd
import tushare as ts

# 量能共振参数：均量周期、放量倍数（金叉当日成交量 > VOL_MA_PERIOD 日均量 * VOL_RATIO 才买入）
VOL_MA_PERIOD = 20
VOL_RATIO = 1.5


def _setup_token():
    """设置 Tushare Token：优先本地缓存（只读不写），其次环境变量"""
    token = ts.get_token()  # 从本地 tk.csv 读取并设置全局 token（只读，避免每次写文件触发权限错误）
    if not token:
        token = os.environ.get("TUSHARE_TOKEN")
        if token:
            ts.set_token(token)
    if not token:
        raise SystemExit("未设置 Tushare Token，请先执行 ts.set_token('你的token') 或设置环境变量 TUSHARE_TOKEN")
    return token


# 模块导入即设置 token，供 fetch 使用（无其它副作用，便于批量脚本导入复用）
_setup_token()


def code_to_ts_code(code):
    """根据股票代码判断交易所后缀：6 开头为沪市，0/3 开头为深市"""
    if code.startswith("6"):
        return f"{code}.SH"
    elif code.startswith(("0", "3")):
        return f"{code}.SZ"
    else:
        return f"{code}.BJ"


def fetch_latest_data(code, years=3):
    """通过 Tushare 获取最新日线数据，失败则返回 None；years 控制拉取年数（用于更长预热期）"""
    try:
        today = datetime.now()
        end_date = today.strftime("%Y%m%d")
        start_date = (today - timedelta(days=365 * years)).strftime("%Y%m%d")

        df = ts.pro_bar(
            ts_code=code_to_ts_code(code),
            adj="qfq",
            start_date=start_date,
            end_date=end_date,
        )

        # Tushare 列名为英文：trade_date, open, high, low, close, vol
        df = df.rename(columns={"vol": "volume"})
        df["date"] = pd.to_datetime(df["trade_date"])
        df = df.sort_values("date").set_index("date")
        df = df[["open", "close", "volume"]]

        # 计算 MA5 和 MA20
        df["MA5"] = df["close"].rolling(window=5).mean()
        df["MA20"] = df["close"].rolling(window=20).mean()
        df = df.dropna()

        # 保存到 CSV 供下次回退使用
        os.makedirs("output", exist_ok=True)
        df.reset_index().to_csv(f"output/ma_plot_tushare_{code}.csv", index=False, encoding="utf-8-sig")
        print(f"数据来源: Tushare 最新数据 (共 {len(df)} 条)")
        return df

    except Exception as e:
        print(f"Tushare 获取失败: {type(e).__name__}: {e}")
        return None


def load_from_csv(code):
    """从已有的 CSV 文件加载数据"""
    csv_path = f"output/ma_plot_tushare_{code}.csv"
    if not os.path.exists(csv_path):
        raise FileNotFoundError(f"找不到 {csv_path}，且 Tushare 获取失败，无法回测")

    df = pd.read_csv(csv_path, parse_dates=["date"]).set_index("date")
    print(f"数据来源: 本地 CSV ({csv_path})，共 {len(df)} 条")
    return df


def prepare_df(df):
    """计算 RSI、金叉死叉信号、量能共振指标，返回带全部指标列的 df"""
    df = df.copy()  # 避免对可能是视图的 df 赋值触发 SettingWithCopyWarning
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

    # 计算量能共振：T 日成交量 > VOL_MA_PERIOD 日均量 * VOL_RATIO（T 日收盘后已知）
    # 与 execute_signal 同样顺延一日，T+1 执行买入时取 T 日的量能确认
    has_volume = "volume" in df.columns
    if has_volume:
        df["VOL_MA20"] = df["volume"].rolling(window=VOL_MA_PERIOD).mean()
        df["vol_ratio"] = df["volume"] / df["VOL_MA20"]
        df["vol_confirm"] = df["volume"] > df["VOL_MA20"] * VOL_RATIO
        df["vol_confirm_lag"] = df["vol_confirm"].shift(1) == True
        df["vol_ratio_lag"] = df["vol_ratio"].shift(1)
        df = df.dropna(subset=["VOL_MA20"])  # 丢弃均量计算不足的行
    else:
        df["vol_confirm_lag"] = False
        df["vol_ratio_lag"] = float("nan")
    return df


def run_backtest(df, exec_col, exec_label, vol_filter=False, verbose=False, silent=False):
    """运行单组回测
    vol_filter 为 True 表示启用成交量共振过滤（金叉当日量 > VOL_MA_PERIOD 日均量 * VOL_RATIO 才买入）
    silent 为 True 时不打印交易明细表（供批量回测调用）
    """
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
    buy_vol_ratio = 0.0
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
                "buy_vol_ratio": buy_vol_ratio,
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
            buy_vol_ratio = 0.0
            pending_stop_loss = False

        elif sig == 1 and shares == 0 and cash > 0:
            # 金叉买入：量能不共振则跳过（仅量能共振场景启用）
            vol_ok = (not vol_filter) or bool(row["vol_confirm_lag"])
            if vol_ok:
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
                    buy_vol_ratio = row["vol_ratio_lag"]
                    trade_count += 1
                    total_commission += fee
                    total_slippage += slip
                    if verbose:
                        print(f"[买入] 执行日 {date.date()} {exec_label} {exec_price:.2f} 数量 {buy_shares} RSI {row['RSI']:.1f} 量比 {row['vol_ratio_lag']:.2f}")

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
                "buy_vol_ratio": buy_vol_ratio,
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
            buy_vol_ratio = 0.0

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

    # 打印交易明细表（批量回测时 silent 跳过）
    if not silent:
        notes = []
        if vol_filter:
            notes.append(f"量能共振(量>{VOL_MA_PERIOD}日均量*{VOL_RATIO})")
        label = "、".join(notes) if notes else "无过滤"
        print(f"\n--- 交易明细 ({label}) ---")
        if trades:
            print(f"{'买入日期':<12} {'买入价':>8} {'RSI':>6} {'量比':>6} {'卖出日期':<12} {'卖出价':>8} {'卖出原因':>8} {'盈亏':>10}")
            print("-" * 78)
            for t in trades:
                vr = t['buy_vol_ratio']
                vr_str = f"{vr:.2f}" if pd.notna(vr) else "-"
                print(f"{str(t['buy_date']):<12} {t['buy_price']:>8.2f} {t['buy_rsi']:>6.1f} {vr_str:>6} {str(t['sell_date']):<12} {t['sell_price']:>8.2f} {t['sell_reason']:>8} {t['pnl']:>10.2f}")
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


# 对比场景：每组指定是否启用成交量共振过滤(vol_filter)
SCENARIOS = [
    {"name": "无过滤",   "vol_filter": False},
    {"name": "量能共振", "vol_filter": True},
]


def run_scenarios(df, exec_col, exec_label, verbose=False):
    """对一只股票运行全部场景，返回结果列表（每项含 name 字段）"""
    results = []
    for sc in SCENARIOS:
        r = run_backtest(df, exec_col, exec_label,
                         vol_filter=sc["vol_filter"],
                         verbose=verbose)
        r["name"] = sc["name"]
        results.append(r)
    return results


def calc_volatility(df):
    """标的 20 日年化波动率：基于收盘价日收益率的 20 日滚动标准差，年化（×√252），取全期均值"""
    close_returns = df["close"].pct_change()
    vol = close_returns.rolling(window=20).std() * (252 ** 0.5)
    return vol.mean() * 100


def main():
    parser = argparse.ArgumentParser(description="MA5/MA20 双均线金叉死叉回测（Tushare 数据源）")
    parser.add_argument("code", nargs="?", default="000100", help="股票代码，如 000100")
    args = parser.parse_args()
    code = args.code

    # 优先获取最新数据，失败则回退到本地 CSV
    df = fetch_latest_data(code)
    if df is None:
        df = load_from_csv(code)

    # 量能共振需要 volume 列；若本地旧 CSV 缺少该列，尝试重新拉取一次
    if "volume" not in df.columns:
        print("本地数据缺少 volume 列，尝试重新获取最新数据...")
        fresh = fetch_latest_data(code)
        if fresh is not None and "volume" in fresh.columns:
            df = fresh
        else:
            print("警告: 仍未获取到 volume 列，量能共振过滤将无法生效")

    print(f"标的 20 日年化波动率: {calc_volatility(df):.2f}%")

    # 执行价优先用 open（T+1 开盘），若数据中没有 open 列则回退到 close
    exec_col = "open" if "open" in df.columns else "close"
    exec_label = "开盘价" if exec_col == "open" else "收盘价"
    if exec_col == "close":
        print("警告: 数据中无 open 列，执行价回退为当日收盘价")

    df = prepare_df(df)

    results = run_scenarios(df, exec_col, exec_label, verbose=False)
    for r in results:
        print(f"\n{'=' * 60}")
        print(f"场景: {r['name']}")
        print(f"{'=' * 60}")
        print(f"最终净值: {r['final_equity']:,.2f}")
        print(f"净收益: {r['net_profit']:,.2f}")
        print(f"总收益率: {r['total_return']:.2f}%")
        print(f"交易次数: {r['trade_count']}")
        print(f"胜率: {r['win_rate']:.1f}%")
        print(f"最大回撤: {r['max_drawdown']:.2f}%")
        print(f"连续亏损次数: {r['max_consecutive_losses']}")

    # 对比汇总表
    print(f"\n{'=' * 70}")
    print("过滤前后对比")
    print(f"{'=' * 70}")
    print(f"{'场景':<14} {'交易次数':>8} {'胜率':>8} {'最大回撤':>10} {'总收益率':>10} {'最终净值':>12}")
    print("-" * 70)
    for r in results:
        print(f"{r['name']:<14} {r['trade_count']:>8} {r['win_rate']:>7.1f}% {r['max_drawdown']:>9.2f}% {r['total_return']:>9.2f}% {r['final_equity']:>12,.2f}")


if __name__ == "__main__":
    main()
