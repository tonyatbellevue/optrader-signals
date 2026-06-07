#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OpTrader 回测  ——  检验筛选规则到底有没有效  (独立于 wheel-screener)

目的: 把 optrader_signals.py 的打分从"看起来合理"变成"被数据检验过"。
具体验证 tastytrade 机械化规则在历史上的表现, 并看 score 是否与 PnL 正相关。

================  诚实的方法论约束 (务必先读)  ================
yfinance 不提供历史期权链 / 历史 IV, 所以无法用真实历史 IV 回测 IVR / VRP。
本回测采用可行且诚实的代理:
  1. IV 代理 = 当时的已实现波动 HV30 (这也是工具冷启动时实际走的 HVR 代理路径)
  2. 排序/门槛用 HV Rank (1年滚动分位) 代替 IV Rank —— 检验的就是
     "在自身波动处于历史高位时卖 premium" 这条核心主张
  3. 价差权利金用 Black-Scholes 定价 (σ = HV 代理), 不是真实成交价
  4. 前向 PnL 用标的真实日线走势; 管理用每日 BS 重估近似
因此结论是"方法学上的方向性证据", 不是可直接套用的精确收益。
真实精确回测需要付费的历史期权数据 (ORATS / CBOE DataShop 等)。

策略 (与 optrader_signals.py 的卖方分支一致):
  入场: HVR > 50, 选 ~45 DTE, 卖 30Δ 看跌, 买下方 8% 做保护 (Put Credit Spread)
  管理: 盈利 50% 止盈 / 剩 21 DTE 强制平仓 / 否则持有到期
对照基线: 同期"无脑每周都卖" (不看 HVR 门槛), 以及标的买入持有。

用法:
  python backtest_optrader.py                         # 默认 10 标的, 3 年
  python backtest_optrader.py --tickers SPY QQQ NVDA  # 指定标的
  python backtest_optrader.py --period 5y --every 5   # 5 年, 每 5 个交易日开一笔
"""

import argparse
import math
import sys

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

# 复用主模块的常数与数学工具, 保持口径一致
from optrader_signals import (
    norm_cdf, bs_delta, pct_rank,
    TT_IVR_GATE, TT_SHORT_DELTA, TT_TARGET_DTE,
    TT_PROFIT_TARGET, TT_MANAGE_DTE, RISK_FREE,
)

DEFAULT_BT_UNIVERSE = ["SPY", "QQQ", "IWM", "NVDA", "AAPL",
                       "MSFT", "AMD", "META", "TSLA", "GLD"]
SPREAD_WIDTH_PCT = 0.08   # 长腿在短腿下方 8% (与主模块 0.92 系数一致)
TRADING_DAYS = 252


# ----------------------------- 期权定价 -----------------------------
def bs_put(spot, strike, dte, iv, r=RISK_FREE):
    """Black-Scholes 看跌期权价 (无股息)。"""
    if iv is None or iv <= 0 or dte <= 0 or spot <= 0 or strike <= 0:
        return max(strike - spot, 0.0)            # 退化为内在价值
    T = dte / 365.0
    d1 = (math.log(spot / strike) + (r + 0.5 * iv ** 2) * T) / (iv * math.sqrt(T))
    d2 = d1 - iv * math.sqrt(T)
    return strike * math.exp(-r * T) * norm_cdf(-d2) - spot * norm_cdf(-d1)


def put_spread_value(spot, k_short, k_long, dte, iv):
    """看跌价差现值 = 卖短腿 - 买长腿 (净负债, 越小越好)。"""
    return bs_put(spot, k_short, dte, iv) - bs_put(spot, k_long, dte, iv)


def strike_for_put_delta(spot, dte, iv, target_delta):
    """二分搜索一个使看跌 |delta| ≈ target 的行权价 (OTM, 低于现价)。"""
    lo, hi = spot * 0.5, spot                       # OTM 看跌在现价下方
    for _ in range(40):
        mid = (lo + hi) / 2
        d = bs_delta(spot, mid, dte, iv, call=False)   # 看跌 delta 为负
        if d is None:
            return round(spot * (1 - 0.5 * iv * math.sqrt(dte / 365)), 0)
        if abs(d) > target_delta:    # 太深 ITM 方向 -> 行权价太高, 下调
            hi = mid
        else:
            lo = mid
    return round((lo + hi) / 2, 0)


# ----------------------------- 单标的回测 -----------------------------
def backtest_ticker(ticker, period, every):
    import numpy as np
    import yfinance as yf

    hist = yf.Ticker(ticker).history(period=period)["Close"].dropna()
    if len(hist) < TRADING_DAYS + TT_TARGET_DTE:
        return []
    closes = hist.values
    logret = np.log(closes[1:] / closes[:-1])

    trades = []
    # 留出 1 年算 HVR, 末尾留出持有期
    start = TRADING_DAYS
    end = len(closes) - TT_TARGET_DTE - 2
    for i in range(start, end, every):
        spot = float(closes[i])
        # 截至当日的 HV30 与 HV Rank (只用过去数据, 无前视)
        window = logret[max(0, i - TRADING_DAYS):i]
        if len(window) < 60:
            continue
        hv30 = float(np.std(window[-30:]) * math.sqrt(TRADING_DAYS))
        roll = [float(np.std(window[j - 20:j]) * math.sqrt(TRADING_DAYS))
                for j in range(20, len(window))]
        if not roll:
            continue
        hvr = pct_rank(roll, hv30)
        if hvr is None:
            continue

        iv = hv30                                    # IV 代理 = HV30
        dte0 = TT_TARGET_DTE
        k_short = strike_for_put_delta(spot, dte0, iv, TT_SHORT_DELTA)
        k_long = round(k_short * (1 - SPREAD_WIDTH_PCT), 0)
        if k_long <= 0 or k_long >= k_short:
            continue
        width = k_short - k_long
        credit = put_spread_value(spot, k_short, k_long, dte0, iv)
        if credit <= 0:
            continue

        # 回测口径下的 score (镜像主模块卖方分支, 用 HVR 当 rank, VRP≈1)
        ivr_edge = max(0.0, (hvr - TT_IVR_GATE) / (100 - TT_IVR_GATE) * 100)
        score = round(min(100, 30 + 0.55 * ivr_edge), 1) if hvr >= TT_IVR_GATE else 0.0
        gated = hvr >= TT_IVR_GATE

        # 前向走日线, 套用管理规则
        exit_val, exit_reason, held = None, None, 0
        for d in range(1, dte0 + 1):
            idx = i + d
            if idx >= len(closes):
                break
            s = float(closes[idx])
            rem = dte0 - d
            held = d
            cur_iv = iv                              # 简化: 持有期 IV 代理不变
            val = (put_spread_value(s, k_short, k_long, rem, cur_iv)
                   if rem > 0 else
                   max(k_short - s, 0) - max(k_long - s, 0))
            pnl_open = credit - val
            if pnl_open >= TT_PROFIT_TARGET * credit:
                exit_val, exit_reason = val, "止盈50%"
                break
            if rem <= TT_MANAGE_DTE:
                exit_val, exit_reason = val, "21DTE管理"
                break
        if exit_val is None:                         # 持有到期结算
            s_exp = float(closes[min(i + dte0, len(closes) - 1)])
            exit_val = max(k_short - s_exp, 0) - max(k_long - s_exp, 0)
            exit_reason = "到期"

        pnl = (credit - exit_val) * 100              # 每 1 张合约 ($)
        max_risk = (width - credit) * 100
        ror = pnl / max_risk if max_risk > 0 else 0.0
        trades.append(dict(
            ticker=ticker, score=score, gated=gated, hvr=round(hvr, 1),
            credit=round(credit, 2), width=round(width, 1),
            pnl=round(pnl, 2), ror=round(ror, 3),
            reason=exit_reason, held=held,
        ))
    return trades


# ----------------------------- 统计与报告 -----------------------------
def summarize(trades, label):
    if not trades:
        print(f"  {label}: 无交易")
        return
    n = len(trades)
    wins = sum(1 for t in trades if t["pnl"] > 0)
    total = sum(t["pnl"] for t in trades)
    avg = total / n
    avg_ror = sum(t["ror"] for t in trades) / n
    print(f"  {label}: {n} 笔 | 胜率 {wins/n*100:.1f}% | "
          f"总 PnL ${total:,.0f} | 均 ${avg:,.1f}/张 | 均 RoR {avg_ror*100:.1f}%")


def pearson(xs, ys):
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx <= 0 or vy <= 0:
        return None
    return cov / math.sqrt(vx * vy)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--tickers", nargs="+", default=DEFAULT_BT_UNIVERSE)
    p.add_argument("--period", default="3y", help="yfinance 历史区间, 如 2y/3y/5y")
    p.add_argument("--every", type=int, default=5, help="每隔几个交易日开一笔")
    args = p.parse_args()

    all_trades = []
    for tk in args.tickers:
        print(f"回测 {tk} ...")
        try:
            all_trades.extend(backtest_ticker(tk, args.period, args.every))
        except Exception as e:
            print(f"  {tk}: 失败 ({e}),跳过")

    if not all_trades:
        print("没有产生任何交易, 检查标的/区间。")
        return

    gated = [t for t in all_trades if t["gated"]]          # HVR>50 才卖 (策略)
    ungated = [t for t in all_trades if not t["gated"]]    # HVR<=50 的那些 (被门槛挡掉)

    print("\n" + "=" * 64)
    print("Put Credit Spread 回测  (HV 代理 IV; 方向性证据, 非精确收益)")
    print("=" * 64)
    summarize(all_trades, "全部样本(不看门槛)  ")
    summarize(gated,      "策略: HVR>50 才卖    ")
    summarize(ungated,    "被门槛挡掉的低 HVR   ")

    # 核心检验: score 与 PnL / RoR 是否正相关
    sc = [t["score"] for t in all_trades]
    r_pnl = pearson(sc, [t["pnl"] for t in all_trades])
    r_ror = pearson(sc, [t["ror"] for t in all_trades])
    print("\n  score↔PnL 相关性 (Pearson): "
          + (f"{r_pnl:+.3f}" if r_pnl is not None else "n/a")
          + " | score↔RoR: "
          + (f"{r_ror:+.3f}" if r_ror is not None else "n/a"))
    print("  正相关 → 高分确实对应更好结果, 打分有效; 接近 0 → 打分没区分力。")

    # 退出原因分布
    from collections import Counter
    reasons = Counter(t["reason"] for t in gated or all_trades)
    print("  退出原因:", dict(reasons))
    print("\n注: 见文件头方法论约束 —— 这是 HV 代理下的方向性结论, 不是可套用的精确收益。")


if __name__ == "__main__":
    main()
