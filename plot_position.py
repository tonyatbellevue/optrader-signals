#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
持仓盈亏追踪器  (独立于每日荐股流程 optrader_signals.py)

读 positions.json 里的每个期权持仓, 用 yfinance 真实日线 + Black-Scholes 重算
"过去 N 个交易日的滚动盈亏", 生成每个持仓一张图, 并汇总成独立网页 positions.html。

诚实约束: yfinance 无历史期权价 / 历史 IV, 所以每日 mark 用 BS 重算、IV 固定按
持仓记录里的 iv 代理。结论是 mark-to-model 近似, 不是真实成交盈亏。

用法:
  python plot_position.py            # 真实数据
  python plot_position.py --days 5   # 回看交易日数 (默认 5)
"""

import argparse
import json
import math
import os
import sys
from datetime import date, datetime

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

RISK_FREE = 0.045


def _ncdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def prob_above(S, level, T, iv, r=RISK_FREE):
    """风险中性下到期价 > level 的概率 = N(d2)。"""
    if T <= 0 or iv <= 0 or S <= 0 or level <= 0:
        return None
    d2 = (math.log(S / level) + (r - 0.5 * iv * iv) * T) / (iv * math.sqrt(T))
    return _ncdf(d2)


def win_prob(pos, spot, T):
    """该持仓当前的盈利概率 PoP(%): 到期落在盈利侧的概率。各结构通用。"""
    K, iv = pos["strike"], pos["iv"]
    prem, call, sell = pos["entry_premium"], (pos["type"] == "call"), (pos["side"] == "sell")
    be = (K + prem) if call else (K - prem)        # 看涨上破位 / 看跌下破位
    pa = prob_above(spot, be, T, iv)
    if pa is None:
        return None
    # 卖put / 买call 盈利在上方; 卖call / 买put 盈利在下方
    if (sell and not call) or (not sell and call):   # sell put 或 buy call -> 赢在上方
        p = pa
    else:                                            # sell call 或 buy put -> 赢在下方
        p = 1 - pa
    return round(p * 100, 1)


def bs_price(S, K, T, iv, call, r=RISK_FREE):
    """Black-Scholes 期权价 (无股息)。"""
    if T <= 0 or iv <= 0:
        return max((S - K) if call else (K - S), 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * iv * iv) * T) / (iv * math.sqrt(T))
    d2 = d1 - iv * math.sqrt(T)
    if call:
        return S * _ncdf(d1) - K * math.exp(-r * T) * _ncdf(d2)
    return K * math.exp(-r * T) * _ncdf(-d2) - S * _ncdf(-d1)


def next_earnings(tk, exp):
    """返回 (earnings_date_str|None, before_expiry_bool)。"""
    import pandas as pd
    try:
        cal = tk.calendar
        ed = None
        if isinstance(cal, dict):
            v = cal.get("Earnings Date")
            ed = (v[0] if isinstance(v, (list, tuple)) and v else v)
        if ed is None:
            return None, False
        ed = pd.to_datetime(ed).date()
        return ed.isoformat(), (ed <= exp)
    except Exception:
        return None, False


def position_pnl(pos, days):
    """返回 (labels, closes, pnls, summary)。pnl 为该持仓每日相对建仓的盈亏($)。"""
    import yfinance as yf

    tk = yf.Ticker(pos["ticker"])
    hist = tk.history(period="1mo")["Close"].dropna()
    if hist.empty:
        return None
    entry = datetime.strptime(pos["entry_date"], "%Y-%m-%d").date()
    # 取建仓日(含)之后的交易日, 最多 days+1 个
    rows = [(d, float(hist.loc[d])) for d in hist.index if d.date() >= entry]
    if not rows:
        rows = [(hist.index[-1], float(hist.iloc[-1]))]
    rows = rows[-(days + 1):]

    exp = datetime.strptime(pos["expiry"], "%Y-%m-%d").date()
    K, iv, call = pos["strike"], pos["iv"], (pos["type"] == "call")
    sign = 1 if pos["side"] == "sell" else -1     # 卖方: 收-现值; 买方: 现值-付
    n = pos.get("contracts", 1) * 100
    entry_prem = pos["entry_premium"]

    labels, closes, pnls = [], [], []
    for d, c in rows:
        T = max((exp - d.date()).days, 0) / 365.0
        mark = bs_price(c, K, T, iv, call)
        pnl = sign * (entry_prem - mark) * n
        labels.append(d.strftime("%m/%d"))
        closes.append(round(c, 2))
        pnls.append(round(pnl, 0))
    T_now = max((exp - rows[-1][0].date()).days, 0) / 365.0
    pop = win_prob(pos, closes[-1], T_now)
    be = (K + entry_prem) if call else (K - entry_prem)
    ed, before = next_earnings(tk, exp)
    summary = dict(last_pnl=pnls[-1], last_close=closes[-1],
                   max=max(pnls), min=min(pnls), pop=pop, breakeven=round(be, 2),
                   earnings=ed, earnings_before_exp=before)
    return labels, closes, pnls, summary


def make_chart(pos, labels, closes, pnls, out_dir):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams["axes.unicode_minus"] = False
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(9, 6.2), sharex=True,
                                 gridspec_kw={"height_ratios": [2, 1]})
    colors = ["#2ca02c" if p >= 0 else "#d62728" for p in pnls]
    a1.axhline(0, color="#888", lw=1)
    a1.plot(labels, pnls, color="#444", lw=1.2, zorder=1)
    a1.bar(labels, pnls, color=colors, alpha=.85, width=.5, zorder=2)
    for x, p in zip(labels, pnls):
        a1.annotate(f"{p:+.0f}", (x, p), ha="center",
                    va="bottom" if p >= 0 else "top", fontsize=9, weight="bold")
    side = pos["side"].upper()
    a1.set_title(f"{pos['ticker']} {pos['strike']:.0f}{pos['type'][0].upper()} "
                 f"{side} {pos['expiry']} - last {len(labels)-1} trading days P&L "
                 f"(mark-to-model, IV {pos['iv']*100:.0f}%)",
                 fontsize=10.5, weight="bold")
    a1.set_ylabel("P&L /pos ($)"); a1.grid(alpha=.25, axis="y")
    a2.plot(labels, closes, color="#1f77b4", marker="o", lw=1.6)
    a2.axhline(pos["strike"], color="#9467bd", ls=":", lw=1, label=f"Strike {pos['strike']:.0f}")
    for x, c in zip(labels, closes):
        a2.annotate(f"{c:.0f}", (x, c), ha="center", va="bottom", fontsize=8)
    a2.set_ylabel(f"{pos['ticker']} ($)"); a2.grid(alpha=.25); a2.legend(fontsize=8, loc="best")
    plt.tight_layout()
    fname = f"position_{pos['id']}.png"
    plt.savefig(os.path.join(out_dir, fname), dpi=130)
    plt.close(fig)
    return fname


def make_payoff_chart(pos, spot, out_dir):
    """到期损益示意图(4 种结构通用): 到期 P&L 曲线 + 当前 BS 曲线 + 关键价位。"""
    import numpy as np
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from datetime import datetime

    K, iv, prem = pos["strike"], pos["iv"], pos["entry_premium"]
    call, sell = (pos["type"] == "call"), (pos["side"] == "sell")
    n = pos.get("contracts", 1) * 100
    exp = datetime.strptime(pos["expiry"], "%Y-%m-%d").date()
    T = max((exp - date.today()).days, 0) / 365.0

    def intrinsic(S):
        return max(S - K, 0) if call else max(K - S, 0)

    def pnl_exp(S):
        v = intrinsic(S)
        return (prem - v) * n if sell else (v - prem) * n

    def pnl_now(S):
        v = bs_price(S, K, T, iv, call)
        return (prem - v) * n if sell else (v - prem) * n

    lo, hi = min(K, spot) * 0.82, max(K, spot) * 1.18
    S = np.linspace(lo, hi, 400)
    pe = np.array([pnl_exp(s) for s in S])
    pn = np.array([pnl_now(s) for s in S])
    be = (K + prem) if call else (K - prem)

    plt.rcParams["axes.unicode_minus"] = False
    fig, ax = plt.subplots(figsize=(9, 4.6))
    ax.axhline(0, color="#888", lw=1)
    ax.fill_between(S, pe, 0, where=(pe >= 0), color="#2ca02c", alpha=.12)
    ax.fill_between(S, pe, 0, where=(pe < 0), color="#d62728", alpha=.12)
    ax.plot(S, pe, color="#d62728" if sell else "#1f77b4", lw=2.6,
            label="P&L @ expiry")
    ax.plot(S, pn, color="#d62728" if sell else "#1f77b4", lw=1.4, ls="--", alpha=.6,
            label="P&L now (BS)")
    ax.axvline(spot, color="#2ca02c", lw=1.2, ls=":", label=f"Spot {spot:.2f}")
    ax.axvline(K, color="#9467bd", lw=1.1, ls=":", label=f"Strike {K:.0f}")
    ax.axvline(be, color="#ff7f0e", lw=1.1, ls=":", label=f"Breakeven {be:.2f}")
    side = pos["side"].upper()
    ax.set_title(f"{pos['ticker']} {K:.0f}{pos['type'][0].upper()} {side} {pos['expiry']} - payoff",
                 fontsize=10.5, weight="bold")
    ax.set_xlabel(f"{pos['ticker']} price at expiration ($)")
    ax.set_ylabel("P&L /pos ($)")
    ax.grid(alpha=.25); ax.legend(fontsize=8, loc="best")
    plt.tight_layout()
    fname = f"payoff_{pos['id']}.png"
    plt.savefig(os.path.join(out_dir, fname), dpi=125)
    plt.close(fig)
    return fname


PAGE_HEAD = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>持仓盈亏追踪</title><style>
body{margin:0;background:#0b0d10;color:#e6e3db;font-family:"Segoe UI",system-ui,sans-serif;padding:24px}
.wrap{max-width:1000px;margin:0 auto}h1{font-size:22px;margin:0 0 4px}
.sub{color:#8b8f98;font-size:13px;margin-bottom:20px}
.card{background:#12151a;border:1px solid #23262d;border-radius:10px;padding:18px;margin:18px 0}
.card h2{font-size:16px;margin:0 0 8px}
img{width:100%;height:auto;border-radius:8px;background:#fff}
.kv{font-size:14px;margin:10px 0;line-height:1.8}.g{color:#2ca02c}.r{color:#ff6b6b}
.tag{display:inline-block;background:#d62728;color:#fff;font-size:11px;font-weight:600;padding:2px 8px;border-radius:4px;margin-left:6px}
.warn{color:#e0a030;font-size:12.5px;line-height:1.7;margin-top:8px}
</style></head><body><div class="wrap">
<h1>持仓盈亏追踪</h1>
<div class="sub">独立于每日荐股 · 数据 yfinance(延迟) · 盈亏为 BS 模型重算(IV 代理),非真实成交价 · 非投资建议<br>更新: {updated}</div>
"""
PAGE_TAIL = "</div></body></html>"


def make_page(cards, out_dir, updated):
    html = PAGE_HEAD.replace("{updated}", updated) + "\n".join(cards) + PAGE_TAIL
    with open(os.path.join(out_dir, "positions.html"), "w", encoding="utf-8") as f:
        f.write(html)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=5, help="回看交易日数")
    p.add_argument("--out-dir", default=".")
    p.add_argument("--open", action="store_true")
    args = p.parse_args()

    cfg = json.load(open(os.path.join(args.out_dir, "positions.json"), encoding="utf-8"))
    cards = []
    for pos in cfg.get("positions", []):
        print(f"追踪 {pos['id']} ...")
        res = position_pnl(pos, args.days)
        if not res:
            print(f"  {pos['id']}: 无数据,跳过"); continue
        labels, closes, pnls, s = res
        img = make_chart(pos, labels, closes, pnls, args.out_dir)
        payoff = make_payoff_chart(pos, s["last_close"], args.out_dir)
        pnl_cls = "g" if s["last_pnl"] >= 0 else "r"
        rows = "".join(
            f"<tr><td>{l}</td><td>${c}</td><td class='{'g' if v>=0 else 'r'}'>{v:+,.0f}</td></tr>"
            for l, c, v in zip(labels, closes, pnls))
        cards.append(f"""<div class="card">
  <h2>{pos['ticker']} {pos.get('name','')} · {pos['strike']:.0f} {pos['type'].upper()} <span class="tag">{pos['side'].upper()}</span></h2>
  <div class="kv">到期 {pos['expiry']} · 建仓 {pos['entry_date']} @ 权利金 ${pos['entry_premium']} · {pos.get('contracts',1)} 张<br>
  最新({labels[-1]}): 标的 ${s['last_close']} · 浮动盈亏 <b class="{pnl_cls}">{s['last_pnl']:+,.0f}</b>
  (区间 {s['min']:+,.0f} ~ {s['max']:+,.0f})<br>
  盈亏平衡 ${s['breakeven']} · <b>赢的机率 PoP ≈ {s['pop']}%</b>(到期落在盈利侧, BS 估算)<br>
  {('下次财报 ' + s['earnings'] + (' ⚠ 在到期前(IV crush/跳空风险)' if s['earnings_before_exp'] else ' ✅ 在到期后')) if s.get('earnings') else '下次财报: 查不到, 请手动核对'}</div>
  <div style="font-size:12px;color:#8b8f98;margin:8px 0 2px">到期损益示意图</div>
  <img src="{payoff}" alt="{pos['id']} payoff">
  <div style="font-size:12px;color:#8b8f98;margin:12px 0 2px">过去 {len(labels)-1} 个交易日滚动盈亏</div>
  <img src="{img}" alt="{pos['id']} pnl">
  <table style="border-collapse:collapse;margin-top:12px;font-size:13px">
    <tr><th style="border:1px solid #23262d;padding:5px 12px;color:#e0a030">日期</th>
        <th style="border:1px solid #23262d;padding:5px 12px;color:#e0a030">标的</th>
        <th style="border:1px solid #23262d;padding:5px 12px;color:#e0a030">P&L</th></tr>
    {rows.replace('<td>', '<td style="border:1px solid #23262d;padding:5px 12px">')}
  </table>
  <div class="warn">⚠ mark-to-model:用真实日线 + BS 重算(IV 固定 {pos['iv']*100:.0f}% 代理),非真实期权成交价。</div>
</div>""")
        print(f"  最新浮盈亏 {s['last_pnl']:+,.0f}")

    updated = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    make_page(cards, args.out_dir, updated)
    print(f"完成: {len(cards)} 个持仓 -> positions.html")
    if args.open:
        import webbrowser
        url = "file://" + os.path.abspath(os.path.join(args.out_dir, "positions.html")).replace(os.sep, "/")
        print("打开:", url); webbrowser.open(url)


if __name__ == "__main__":
    main()
