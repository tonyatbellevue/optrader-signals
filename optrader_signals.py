#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OpTrader Signals  ——  每日 3 个期权候选生成器  (独立于 wheel-screener)

方法论 —— 以 tastytrade 机械化口径(大样本回测验证)为骨架, 取代旧版手调权重:
  入场门槛: IV Rank > 50 才卖 premium (高于自身历史中位才有卖方边际)
  进场到期: 约 45 DTE (收益/theta 衰减平衡最佳)
  短腿选择: 价差 30Δ / 宽跨 16Δ (风险调整后甜区)
  退出管理: 盈利 50% 止盈 + 21 DTE 强制管理 (对收益贡献不亚于选标的)

辅助信号(用于确认/排序/避雷):
  1. VRP 波动率风险溢价 = ATM IV / HV30  (>1 确认 IV 真贵于已实现; 学术验证的 variance risk premium)
  2. IV Rank / Percentile (程序每天把 ATM IV 存档, 逐日累积出历史来算) —— 首要排序信号
  3. HV Rank (1年已实现波动率分位, 价格即可算, 作为 IVR 不足时的即时代理)
  4. 预期波动幅度 = ATM straddle 中价 / 现价
  5. 趋势 = 现价 vs SMA50 / SMA200 (IVR 不够门槛时才退回方向性 debit)
  6. 流动性 = ATM 到期的 OI / 量 (闸门, OI 不足砍分)
  7. 财报临近避雷 (裸卖 premium 不宜跨财报, 除非有意做 vol crush)

评分 = IVR 在门槛之上的高度(主) + VRP 确认(次), 再乘流动性闸门; 跨标的取前 3。
评分曲线是对被验证信号(IVR)的单调变换, 不是拟合的 alpha —— 仍建议后续加回测检验。

用法:
  python optrader_signals.py                 # 真实数据 (需联网 + yfinance)
  python optrader_signals.py --demo          # 合成数据, 不联网, 用于预览
  python optrader_signals.py --csp           # bullish 标的改出单腿裸卖 put (CSP)
  python optrader_signals.py --tickers SPY QQQ NVDA --top 3
  python optrader_signals.py --out-dir .     # 输出目录

注意: 数据来自 Yahoo, 有延迟; 这是系统化筛选工具, 不是投资建议。
"""

import argparse
import json
import math
import os
import random
import sys
import webbrowser
from datetime import datetime, timedelta

# Windows 控制台默认 GBK,中文 print 会乱码;强制 stdout/stderr 用 UTF-8
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

# 默认观察池 (流动性好、期权活跃; 故意与 wheel 篮子区分开) —— 80 个
DEFAULT_UNIVERSE = [
    # 指数 / 板块 ETF (15)
    "SPY", "QQQ", "IWM", "DIA", "GLD", "SLV", "TLT", "HYG",
    "XLF", "XLE", "XLK", "SMH", "ARKK", "EEM", "FXI",
    # 科技龙头 / 半导体 / AI (24)
    "NVDA", "AAPL", "MSFT", "AMZN", "META", "GOOGL", "TSLA", "AMD",
    "AVGO", "NFLX", "CRM", "ORCL", "ADBE", "INTC", "QCOM", "MU",
    "AMAT", "TXN", "CSCO", "PLTR", "SMCI", "MRVL", "ARM", "SNOW",
    # 金融 / 支付 (12)
    "JPM", "BAC", "GS", "MS", "WFC", "C", "V", "MA",
    "AXP", "COIN", "PYPL", "SOFI",
    # 消费 / 零售 (10)
    "DIS", "NKE", "SBUX", "MCD", "COST", "WMT", "TGT", "HD", "LOW", "BA",
    # 医药 / 生物 (6)
    "UNH", "LLY", "JNJ", "PFE", "MRNA", "ABBV",
    # 能源 / 工业 (7)
    "XOM", "CVX", "OXY", "CAT", "GE", "F", "GM",
    # 加密相关 / 高波动 (3)
    "MSTR", "RIOT", "MARA",
    # 其它成长 (3)
    "UBER", "ABNB", "SHOP",
]
RISK_FREE = 0.045

# ----------------------------- tastytrade 机械化口径 -----------------------------
# 以下常数来自 tastytrade/tastylive 的大样本(数万笔)回测结论, 而非手调:
#   - IV Rank > 50 才有卖方边际 (高于自身历史中位才卖 premium)
#   - 短腿 30Δ (价差) / 16Δ (宽跨) 是风险调整后的甜区
#   - 约 45 DTE 进场, 收益/theta 衰减平衡最佳
#   - 盈利 50% 止盈 + 21 DTE 强制管理, 对最终收益贡献不亚于选标的
TT_IVR_GATE = 50           # IV Rank 卖方门槛
TT_SHORT_DELTA = 0.30      # 价差短腿目标 delta
TT_STRANGLE_DELTA = 0.16   # 宽跨式两腿 delta
TT_TARGET_DTE = 45         # 进场目标 DTE
TT_PROFIT_TARGET = 0.50    # 盈利 50% 平仓
TT_MANAGE_DTE = 21         # 21 DTE 前强制管理


# ----------------------------- 数学工具 -----------------------------
def norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_delta(spot, strike, dte, iv, call=True, r=RISK_FREE):
    """Black-Scholes delta (无股息近似)"""
    if not iv or iv <= 0 or dte <= 0 or spot <= 0 or strike <= 0:
        return None
    T = dte / 365.0
    d1 = (math.log(spot / strike) + (r + 0.5 * iv ** 2) * T) / (iv * math.sqrt(T))
    return norm_cdf(d1) if call else norm_cdf(d1) - 1.0


def pct_rank(series, value):
    """value 在 series 中的百分位 (0-100)"""
    if not series:
        return None
    below = sum(1 for x in series if x <= value)
    return round(100.0 * below / len(series), 1)


def prob_above(spot, level, dte, iv, r=RISK_FREE):
    """风险中性下到期价 S_T > level 的概率 = N(d2)。iv 为小数(如 0.30)。"""
    if not iv or iv <= 0 or dte <= 0 or spot <= 0 or level <= 0:
        return None
    T = dte / 365.0
    d2 = (math.log(spot / level) + (r - 0.5 * iv ** 2) * T) / (iv * math.sqrt(T))
    return norm_cdf(d2)


def pop_for_structure(m, kind, breakevens):
    """估算结构的盈利概率 PoP (0-100)。
    breakevens: 看跌侧下盈亏平衡价 / 看涨侧上盈亏平衡价 (任一可为 None)。
    模型化估计(风险中性 BS), 持有到期口径; 不含 tastytrade 50% 止盈带来的提升。
    """
    spot, dte, iv = m["spot"], m["dte"], (m["atm_iv"] or 0) / 100.0
    lo, hi = breakevens                                  # (下破位, 上破位)
    if kind == "put_credit":          # 盈利: S_T > 下破位
        p = prob_above(spot, lo, dte, iv)
    elif kind == "call_credit":       # 盈利: S_T < 上破位
        pa = prob_above(spot, hi, dte, iv)
        p = (1 - pa) if pa is not None else None
    elif kind == "strangle":          # 盈利: 下破位 < S_T < 上破位
        pl = prob_above(spot, lo, dte, iv)
        ph = prob_above(spot, hi, dte, iv)
        p = (pl - ph) if (pl is not None and ph is not None) else None
    elif kind == "call_debit":        # 盈利: S_T > 上破位
        p = prob_above(spot, hi, dte, iv)
    elif kind == "put_debit":         # 盈利: S_T < 下破位
        pa = prob_above(spot, lo, dte, iv)
        p = (1 - pa) if pa is not None else None
    else:
        p = None
    return round(p * 100, 1) if p is not None else None


# ----------------------------- IV 历史存档 -----------------------------
def load_iv_history(path):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def update_iv_history(hist, ticker, iv, today, keep_days=400):
    rec = hist.setdefault(ticker, [])
    # 同日覆盖
    rec = [r for r in rec if r["date"] != today]
    rec.append({"date": today, "iv": round(iv, 4)})
    rec = sorted(rec, key=lambda r: r["date"])[-keep_days:]
    hist[ticker] = rec
    return [r["iv"] for r in rec]


# ----------------------------- 真实数据抓取 -----------------------------
def fetch_metrics(ticker, iv_hist, today):
    """用 yfinance 抓一个标的的全部信号; 失败返回 None"""
    import numpy as np
    import pandas as pd
    import yfinance as yf

    tk = yf.Ticker(ticker)
    hist = tk.history(period="1y")["Close"].dropna()
    if len(hist) < 60:
        return None
    spot = float(hist.iloc[-1])

    # 已实现波动率
    logret = np.log(hist / hist.shift(1)).dropna()
    hv30 = float(logret.tail(30).std() * math.sqrt(252))
    # HV rank: 滚动20日年化波动率的1年分位
    roll = logret.rolling(20).std() * math.sqrt(252)
    roll = roll.dropna().tolist()
    hv20 = roll[-1] if roll else hv30
    hv_rank = pct_rank(roll, hv20)

    # 趋势
    sma50 = float(hist.tail(50).mean())
    sma200 = float(hist.tail(200).mean()) if len(hist) >= 200 else float(hist.mean())
    if spot > sma50 > sma200:
        trend = "bullish"
    elif spot < sma50 < sma200:
        trend = "bearish"
    else:
        trend = "neutral"

    # 选目标到期 (tastytrade: 最接近 45 DTE)
    exps = tk.options
    if not exps:
        return None
    target = today_plus = datetime.now().date()
    best_exp, best_dte = None, None
    for e in exps:
        try:
            d = (datetime.strptime(e, "%Y-%m-%d").date() - target).days
        except ValueError:
            continue
        if d < 5:
            continue
        if best_dte is None or abs(d - TT_TARGET_DTE) < abs(best_dte - TT_TARGET_DTE):
            best_exp, best_dte = e, d
    if best_exp is None:
        return None

    chain = tk.option_chain(best_exp)
    calls, puts = chain.calls, chain.puts
    if calls.empty or puts.empty:
        return None

    # ATM IV (最接近现价的 call/put IV 平均)
    catm = calls.iloc[(calls["strike"] - spot).abs().argmin()]
    patm = puts.iloc[(puts["strike"] - spot).abs().argmin()]
    ivs = [v for v in [catm.get("impliedVolatility"), patm.get("impliedVolatility")]
           if v and v > 0]
    atm_iv = float(sum(ivs) / len(ivs)) if ivs else hv30

    # 预期波动幅度 (ATM straddle 中价)
    def mid(row):
        b, a = row.get("bid", 0) or 0, row.get("ask", 0) or 0
        return (b + a) / 2 if (b > 0 and a > 0) else (row.get("lastPrice", 0) or 0)
    straddle = mid(catm) + mid(patm)
    exp_move_pct = round(100.0 * straddle / spot, 2) if spot else None

    # 流动性
    oi = int((calls["openInterest"].fillna(0).sum() + puts["openInterest"].fillna(0).sum()))
    vol = int((calls["volume"].fillna(0).sum() + puts["volume"].fillna(0).sum()))

    # 财报临近
    earnings_in = None
    try:
        cal = tk.calendar
        ed = None
        if isinstance(cal, dict):
            v = cal.get("Earnings Date")
            ed = (v[0] if isinstance(v, (list, tuple)) and v else v)
        if ed is not None:
            ed = pd.to_datetime(ed).date()
            earnings_in = (ed - target).days
    except Exception:
        pass

    # IV rank (累积历史)
    iv_series = update_iv_history(iv_hist, ticker, atm_iv, today)
    iv_rank = pct_rank(iv_series, atm_iv) if len(iv_series) >= 20 else None

    return dict(
        ticker=ticker, spot=round(spot, 2), exp=best_exp, dte=best_dte,
        atm_iv=round(atm_iv * 100, 1), hv30=round(hv30 * 100, 1),
        vrp=round(atm_iv / hv30, 2) if hv30 else None,
        iv_rank=iv_rank, hv_rank=hv_rank, exp_move=exp_move_pct,
        trend=trend, oi=oi, vol=vol, earnings_in=earnings_in,
        _calls=calls, _puts=puts,
    )


# ----------------------------- 合成数据 (demo) -----------------------------
def demo_metrics(ticker, iv_hist, today):
    random.seed(hash(ticker) % 9999)
    spot = round(random.uniform(40, 700), 2)
    hv30 = round(random.uniform(18, 65), 1)
    vrp = round(random.uniform(0.75, 1.6), 2)
    atm_iv = round(hv30 * vrp, 1)
    trend = random.choice(["bullish", "bearish", "neutral", "neutral"])
    dte = random.choice([40, 43, 45, 49])
    return dict(
        ticker=ticker, spot=spot,
        exp=(datetime.now().date() + timedelta(days=dte)).isoformat(), dte=dte,
        atm_iv=atm_iv, hv30=hv30, vrp=vrp,
        iv_rank=round(random.uniform(5, 95), 1),
        hv_rank=round(random.uniform(5, 95), 1),
        exp_move=round(atm_iv / 100 * math.sqrt(dte / 365) * 100, 2),
        trend=trend, oi=random.randint(2000, 900000), vol=random.randint(500, 400000),
        earnings_in=random.choice([None, None, None, 8, 21, 40]),
        _demo=True,
    )


# ----------------------------- 打分 + 选结构 -----------------------------
def score_and_structure(m):
    """返回 (opportunity_score 0-100, regime, structure, rationale[])。

    采用 tastytrade 机械化口径(大样本回测验证),取代旧版手调权重:
      - IV Rank 是首要门槛(>50 才卖 premium)与排序依据
      - VRP>1 仅作"IV 确实贵于已实现"的确认, 不再当主排序
      - 过不了门槛 → 退回方向性 debit (仅在有明确趋势时给分)
    评分曲线是对 IVR(被验证的信号)的单调变换, 不是拟合出来的 alpha。
    """
    vrp = m["vrp"] or 1.0
    ivr = m["iv_rank"]
    hvr = m["hv_rank"]
    # IVR 是 tastytrade 的核心信号; 历史不足时用 HV Rank 代理
    rank = ivr if ivr is not None else (hvr if hvr is not None else 50)
    rank_src = "IV Rank" if ivr is not None else "HV Rank(代理)"

    liq = max(0, min(100, math.log10(max(m["oi"], 1)) / 6.0 * 100))
    rationale = []

    if rank >= TT_IVR_GATE:
        # 过卖方门槛: 分数以 IVR 在门槛之上的高度为主, VRP 作确认加成
        regime = "SELL_PREMIUM"
        ivr_edge = (rank - TT_IVR_GATE) / (100 - TT_IVR_GATE) * 100   # 50->0, 100->100
        vrp_conf = max(0, min(15, (vrp - 1.0) / 0.5 * 15))            # 至多 +15
        base = min(100, 30 + 0.55 * ivr_edge + vrp_conf)             # 过门槛即有 30 底分
        structure = pick_sell_structure(m)
        rationale.append(f"{rank_src} {round(rank)} ≥ {TT_IVR_GATE} → 过 tastytrade 卖方门槛")
        if vrp >= 1.0:
            rationale.append(f"VRP={vrp}: IV 比 HV30 高 {round((vrp-1)*100)}% → 确认溢价真实")
        else:
            rationale.append(f"VRP={vrp}: IV 略低于 HV30, 溢价偏弱, 谨慎建仓")
        if ivr is None:
            rationale.append("IV 历史不足, 暂用 HV Rank 代理(程序每天跑会逐渐补全 IVR)")
    else:
        # IVR 不够: tastytrade 不建议卖 premium, 仅在有趋势时退回方向性买方
        regime = "BUY_PREMIUM"
        vrp_cheap = max(0, min(100, (1.0 - vrp) / 0.25 * 100))
        directional = 20 if m["trend"] in ("bullish", "bearish") else 0
        base = 0.5 * vrp_cheap + directional
        structure = pick_buy_structure(m)
        rationale.append(f"{rank_src} {round(rank)} < {TT_IVR_GATE} → 低于卖方门槛, 不卖 premium")
        rationale.append(f"改走方向性 debit(趋势 {m['trend']}); 期权不贵时买方更划算")

    # 流动性闸门 (OI 不足砍分, 防止挂不出去)
    score = round(base * (0.5 + 0.5 * liq / 100), 1)

    # tastytrade 管理纪律: 盈利 50% 止盈 + 21 DTE 强制管理
    structure["manage"] = (f"盈利 {int(TT_PROFIT_TARGET*100)}% 平仓 / 到 {TT_MANAGE_DTE} DTE 强制管理")
    pop = structure.get("pop")
    if pop is not None:
        rationale.append(
            f"盈利概率 PoP ≈ {pop}% (BS 模型, 持有到期口径; tastytrade 50% 止盈通常会再抬高实际胜率)")
    rationale.append(
        f"管理: 盈利 {int(TT_PROFIT_TARGET*100)}% 止盈, {TT_MANAGE_DTE} DTE 前了结(tastytrade 机械化规则)")
    rationale.append(f"预期波动 ±{m['exp_move']}% / 流动性 OI {m['oi']:,} 量 {m['vol']:,}")
    if m["earnings_in"] is not None and m["earnings_in"] <= m["dte"]:
        rationale.append(f"⚠ 约 {m['earnings_in']} 天后财报落在到期内 —— 卖方注意 IV crush/跳空,可改做财报后或缩仓")
    return score, regime, structure, rationale


def _pick_strike_by_delta(m, target_delta, call):
    """从链里挑最接近目标 delta 的行权价 (demo 模式用近似)。返回 (strike, delta, prem)。"""
    if m.get("_demo"):
        # 近似: 用 BS 反推一个合理 OTM 行权价
        spot, iv, dte = m["spot"], m["atm_iv"] / 100, m["dte"]
        z = 0.6 if target_delta >= 0.3 else 0.9
        k = spot * (1 + z * iv * math.sqrt(dte / 365)) if call else spot * (1 - z * iv * math.sqrt(dte / 365))
        k = round(k, 0)
        d = bs_delta(spot, k, dte, iv, call)
        prem = round(spot * iv * math.sqrt(dte / 365) * 0.4, 2)
        return k, round(d, 2) if d else None, prem
    df = m["_calls"] if call else m["_puts"]
    spot, dte = m["spot"], m["dte"]
    best, bestdiff = None, 1e9
    for _, r in df.iterrows():
        iv = r.get("impliedVolatility")
        d = bs_delta(spot, float(r["strike"]), dte, float(iv) if iv else None, call)
        if d is None:
            continue
        diff = abs(abs(d) - target_delta)
        if diff < bestdiff:
            b, a = r.get("bid", 0) or 0, r.get("ask", 0) or 0
            prem = (b + a) / 2 if (b > 0 and a > 0) else (r.get("lastPrice", 0) or 0)
            best, bestdiff = (float(r["strike"]), round(d, 2), round(float(prem), 2)), diff
    return best if best else (None, None, None)


def pick_sell_structure(m):
    trend = m["trend"]
    if trend == "bullish":
        ks, ds, ps = _pick_strike_by_delta(m, TT_SHORT_DELTA, call=False)
        pop = pop_for_structure(m, "put_credit", ((ks - (ps or 0)) if ks else None, None))
        if m.get("_csp"):
            # 单腿裸卖 put (Cash-Secured Put): 无下方保护腿, 需备足现金接货
            collateral = round((ks or 0) * 100, 0)
            be = round((ks - (ps or 0)), 2) if ks else None
            return dict(type="Cash-Secured Put (bull)", legs=f"卖 {ks}P",
                        exp=m["exp"], short_delta=ds, credit=ps, pop=pop,
                        breakeven=be, collateral=collateral,
                        note=f"单腿裸卖; 30Δ, 收满权利金, 需备现金 ≈ ${collateral:,.0f}/张, 跌破或被指派则接货")
        kl = round(ks * 0.92, 0) if ks else None
        return dict(type="Put Credit Spread (bull)", legs=f"卖 {ks}P / 买 {kl}P",
                    exp=m["exp"], short_delta=ds, credit=ps, pop=pop,
                    note=f"看不跌就行; 30Δ 短腿, 净收权利金, 风险有限")
    if trend == "bearish":
        ks, ds, ps = _pick_strike_by_delta(m, TT_SHORT_DELTA, call=True)
        kl = round(ks * 1.08, 0) if ks else None
        pop = pop_for_structure(m, "call_credit", (None, (ks + (ps or 0)) if ks else None))
        return dict(type="Call Credit Spread (bear)", legs=f"卖 {ks}C / 买 {kl}C",
                    exp=m["exp"], short_delta=ds, credit=ps, pop=pop,
                    note=f"看不涨就行; 30Δ 短腿, 净收权利金, 风险有限")
    kp, dp, pp = _pick_strike_by_delta(m, TT_STRANGLE_DELTA, call=False)
    kc, dc, pc = _pick_strike_by_delta(m, TT_STRANGLE_DELTA, call=True)
    total = round((pp or 0) + (pc or 0), 2)
    pop = pop_for_structure(m, "strangle",
                            ((kp - total) if kp else None, (kc + total) if kc else None))
    return dict(type="Short Strangle (neutral)", legs=f"卖 {kp}P + 卖 {kc}C",
                exp=m["exp"], short_delta=f"±{TT_STRANGLE_DELTA}", credit=total, pop=pop,
                note="中性收两边权利金; 16Δ 宽跨, 高 IV 经典卖方结构, 注意双向风险")


def pick_buy_structure(m):
    trend = m["trend"]
    if trend == "bearish":
        ks, ds, ps = _pick_strike_by_delta(m, 0.45, call=False)
        kl = round(ks * 0.90, 0) if ks else None
        pop = pop_for_structure(m, "put_debit", ((ks - (ps or 0)) if ks else None, None))
        return dict(type="Put Debit Spread (bear)", legs=f"买 {ks}P / 卖 {kl}P",
                    exp=m["exp"], long_delta=ds, debit=ps, pop=pop,
                    note="顺势看跌; 付权利金, 风险=权利金")
    ks, ds, ps = _pick_strike_by_delta(m, 0.45, call=True)
    kl = round(ks * 1.10, 0) if ks else None
    pop = pop_for_structure(m, "call_debit", (None, (ks + (ps or 0)) if ks else None))
    return dict(type="Call Debit Spread (bull)", legs=f"买 {ks}C / 卖 {kl}C",
                exp=m["exp"], long_delta=ds, debit=ps, pop=pop,
                note="顺势看涨; 付权利金, 风险=权利金")


# ----------------------------- 主流程 -----------------------------
def run(args):
    today = datetime.now().date().isoformat()
    iv_path = os.path.join(args.out_dir, "data", "iv_history.json")
    os.makedirs(os.path.dirname(iv_path), exist_ok=True)
    iv_hist = load_iv_history(iv_path)

    scanned = []
    for tk in args.tickers:
        print(f"扫描 {tk} ...")
        try:
            m = demo_metrics(tk, iv_hist, today) if args.demo else fetch_metrics(tk, iv_hist, today)
        except Exception as e:
            print(f"  {tk}: 失败 ({e}),跳过")
            continue
        if not m:
            print(f"  {tk}: 数据不足,跳过")
            continue
        m["_csp"] = bool(getattr(args, "csp", False))   # 单腿裸卖 put 开关
        score, regime, structure, rationale = score_and_structure(m)
        clean = {k: v for k, v in m.items() if not k.startswith("_")}
        clean.update(score=score, regime=regime, structure=structure, rationale=rationale)
        scanned.append(clean)

    if not args.demo:
        with open(iv_path, "w", encoding="utf-8") as f:
            json.dump(iv_hist, f, indent=2)

    scanned.sort(key=lambda x: x["score"], reverse=True)
    picks = scanned[:args.top]

    payload = dict(
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
        demo=bool(args.demo), picks=picks, universe=scanned,
    )
    with open(os.path.join(args.out_dir, "signals.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    html = HTML_SHELL.replace("/*__DATA__*/null", json.dumps(payload, ensure_ascii=False))
    html_path = os.path.join(args.out_dir, "index.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"\n完成: {len(picks)} 个候选 -> index.html / signals.json"
          + ("  [DEMO 合成数据]" if args.demo else ""))

    if getattr(args, "open", False):
        url = "file://" + os.path.abspath(html_path).replace(os.sep, "/")
        print(f"打开网页: {url}")
        webbrowser.open(url)


# ----------------------------- Web 仪表盘模板 -----------------------------
HTML_SHELL = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="900">
<title>OpTrader · Signals</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,500;12..96,700;12..96,800&family=IBM+Plex+Mono:wght@400;500;600&family=Newsreader:ital,opsz@0,6..72;1,6..72&display=swap" rel="stylesheet">
<style>
  :root{
    --bg:#0a0b0d; --panel:#121419; --panel2:#171a21; --line:#262a33;
    --ink:#e8e6e0; --dim:#8a8f9a; --amber:#ffb000; --amber2:#5a4410;
    --green:#3fb950; --red:#f0564a; --blue:#5aa9e6;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);
    font-family:"IBM Plex Mono",monospace;
    background-image:radial-gradient(circle at 1px 1px,#ffffff08 1px,transparent 0);
    background-size:22px 22px;}
  .wrap{max-width:1080px;margin:0 auto;padding:32px 20px 80px}
  header{display:flex;justify-content:space-between;align-items:flex-end;
    border-bottom:2px solid var(--amber);padding-bottom:14px;margin-bottom:6px}
  h1{font-family:"Bricolage Grotesque",sans-serif;font-weight:800;
    font-size:clamp(30px,6vw,52px);margin:0;letter-spacing:-.02em;line-height:.95}
  h1 .dot{color:var(--amber)}
  .meta{text-align:right;font-size:12px;color:var(--dim);line-height:1.7}
  .meta b{color:var(--ink)}
  .demo{display:inline-block;background:var(--amber);color:#000;font-weight:600;
    padding:2px 8px;border-radius:3px;font-size:11px;margin-top:4px}
  .tag{font-size:11px;color:var(--dim);letter-spacing:.18em;text-transform:uppercase;
    margin:30px 0 12px}
  .cards{display:grid;gap:16px}
  .card{background:linear-gradient(180deg,var(--panel),var(--panel2));
    border:1px solid var(--line);border-radius:10px;padding:20px 22px;position:relative;
    overflow:hidden;opacity:0;transform:translateY(14px);
    animation:rise .6s cubic-bezier(.2,.8,.2,1) forwards}
  .card:nth-child(1){animation-delay:.05s}
  .card:nth-child(2){animation-delay:.15s}
  .card:nth-child(3){animation-delay:.25s}
  @keyframes rise{to{opacity:1;transform:none}}
  .card::before{content:"";position:absolute;left:0;top:0;bottom:0;width:4px}
  .card.sell::before{background:var(--green)}
  .card.buy::before{background:var(--blue)}
  .ctop{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}
  .tk{font-family:"Bricolage Grotesque",sans-serif;font-weight:800;font-size:32px;letter-spacing:-.02em}
  .tk small{font-family:"IBM Plex Mono";font-weight:400;font-size:13px;color:var(--dim);margin-left:8px}
  .badge{font-size:11px;font-weight:600;padding:5px 10px;border-radius:4px;letter-spacing:.08em}
  .badge.sell{background:#143020;color:var(--green);border:1px solid #1f5132}
  .badge.buy{background:#102234;color:var(--blue);border:1px solid #1d3a55}
  .struct{margin:14px 0;padding:12px 14px;background:#0d0f13;border:1px dashed var(--line);border-radius:7px}
  .struct .ty{font-weight:600;color:var(--amber);font-size:14px}
  .struct .lg{font-size:18px;margin:6px 0;letter-spacing:.01em}
  .struct .nt{font-size:12px;color:var(--dim)}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(92px,1fr));gap:1px;
    background:var(--line);border:1px solid var(--line);border-radius:7px;overflow:hidden;margin:6px 0 14px}
  .cell{background:var(--panel);padding:9px 10px}
  .cell .k{font-size:10px;color:var(--dim);letter-spacing:.08em;text-transform:uppercase}
  .cell .v{font-size:17px;font-weight:600;margin-top:2px}
  .v.up{color:var(--green)} .v.down{color:var(--red)} .v.hot{color:var(--amber)}
  ul.why{list-style:none;padding:0;margin:0}
  ul.why li{font-family:"Newsreader",serif;font-size:15.5px;line-height:1.5;color:#d6d3cb;
    padding-left:18px;position:relative;margin:5px 0}
  ul.why li::before{content:"▸";position:absolute;left:0;color:var(--amber)}
  .score{position:absolute;top:18px;right:22px;text-align:center}
  .score .n{font-family:"Bricolage Grotesque";font-weight:800;font-size:30px;color:var(--amber);line-height:1}
  .score .l{font-size:9px;color:var(--dim);letter-spacing:.15em}
  table{width:100%;border-collapse:collapse;font-size:12.5px;margin-top:8px}
  th,td{text-align:right;padding:7px 9px;border-bottom:1px solid var(--line)}
  th{color:var(--dim);font-weight:500;text-transform:uppercase;font-size:10px;letter-spacing:.08em}
  td:first-child,th:first-child{text-align:left;font-weight:600}
  tr:hover td{background:#15171d}
  footer{margin-top:40px;padding-top:16px;border-top:1px solid var(--line);
    font-size:11.5px;color:var(--dim);line-height:1.7}
  footer b{color:var(--amber)}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>OpTrader<span class="dot">.</span>Signals</h1>
    <div class="meta" id="meta"></div>
  </header>
  <div class="tag">// 今日 3 个候选 · 量化方法论 + 数据论证</div>
  <div class="cards" id="picks"></div>
  <div class="tag">// 全市场扫描</div>
  <div id="scan"></div>
  <footer id="foot"></footer>
</div>
<script id="payload" type="application/json">/*__DATA__*/null</script>
<script>
const DATA = JSON.parse(document.getElementById('payload').textContent);
const f = n => (n==null?'—':n);
const trendCls = t => t==='bullish'?'up':(t==='bearish'?'down':'');

document.getElementById('meta').innerHTML =
  `更新 <b>${DATA.generated_at}</b><br>扫描 ${DATA.universe.length} 标的 · 取前 ${DATA.picks.length}`
  + (DATA.demo?'<br><span class="demo">DEMO 合成数据</span>':'')
  + `<br>每 15 分钟自动刷新`;

document.getElementById('picks').innerHTML = DATA.picks.map(p=>{
  const sell = p.regime==='SELL_PREMIUM';
  const s = p.structure;
  const legLabel = sell ? (s.credit!=null?`净收权利金 ≈ $${s.credit}`:'') : (s.debit!=null?`成本 ≈ $${s.debit}`:'');
  return `<div class="card ${sell?'sell':'buy'}">
    <div class="score"><div class="n">${f(p.score)}</div><div class="l">EDGE</div>${s.pop!=null?`<div class="n" style="font-size:20px;margin-top:6px">${f(s.pop)}%</div><div class="l">PoP 赢面</div>`:''}</div>
    <div class="ctop">
      <div class="tk">${p.ticker}<small>$${f(p.spot)} · ${f(p.dte)}DTE · 到期 ${f(p.exp)}</small></div>
      <span class="badge ${sell?'sell':'buy'}">${sell?'卖方 · SELL PREMIUM':'买方 · BUY PREMIUM'}</span>
    </div>
    <div class="struct">
      <div class="ty">${s.type}</div>
      <div class="lg">${s.legs}　<span class="nt">(${legLabel})</span></div>
      <div class="nt">${s.note}　|　short Δ ${f(s.short_delta||s.long_delta)}</div>
      ${s.manage?`<div class="nt">管理　${s.manage}</div>`:''}
    </div>
    <div class="grid">
      <div class="cell"><div class="k">ATM IV</div><div class="v hot">${f(p.atm_iv)}%</div></div>
      <div class="cell"><div class="k">HV30</div><div class="v">${f(p.hv30)}%</div></div>
      <div class="cell"><div class="k">VRP</div><div class="v ${p.vrp>=1.15?'hot':''}">${f(p.vrp)}×</div></div>
      <div class="cell"><div class="k">IV Rank</div><div class="v">${f(p.iv_rank)}</div></div>
      <div class="cell"><div class="k">HV Rank</div><div class="v">${f(p.hv_rank)}</div></div>
      <div class="cell"><div class="k">预期波动</div><div class="v">±${f(p.exp_move)}%</div></div>
      <div class="cell"><div class="k">趋势</div><div class="v ${trendCls(p.trend)}">${f(p.trend)}</div></div>
    </div>
    <ul class="why">${p.rationale.map(r=>`<li>${r}</li>`).join('')}</ul>
  </div>`;
}).join('');

document.getElementById('scan').innerHTML = `<table>
  <tr><th>标的</th><th>现价</th><th>IV</th><th>HV30</th><th>VRP</th><th>IVR</th><th>HVR</th>
      <th>±预期</th><th>趋势</th><th>OI</th><th>Edge</th><th>判定</th></tr>
  ${DATA.universe.map(u=>`<tr>
    <td>${u.ticker}</td><td>$${f(u.spot)}</td><td>${f(u.atm_iv)}%</td><td>${f(u.hv30)}%</td>
    <td>${f(u.vrp)}×</td><td>${f(u.iv_rank)}</td><td>${f(u.hv_rank)}</td>
    <td>±${f(u.exp_move)}%</td><td class="${trendCls(u.trend)}">${f(u.trend)}</td>
    <td>${(u.oi||0).toLocaleString()}</td><td><b>${f(u.score)}</b></td>
    <td>${u.regime==='SELL_PREMIUM'?'卖方':'买方'}</td></tr>`).join('')}
</table>`;

document.getElementById('foot').innerHTML =
  `<b>方法论</b>: VRP=ATM IV÷HV30(>1 期权偏贵利好卖方) · IV/HV Rank=波动率自身历史分位 · `
  + `预期波动由 ATM straddle 反推 · 趋势=价 vs SMA50/200 · 财报临近避雷。<br>`
  + `<b>免责</b>: 数据来自 Yahoo Finance,有延迟;盘后为上一交易日快照。这是系统化筛选/教育工具,`
  + `<b>不构成投资建议</b>。实际行权价、仓位与成交价请以券商实时盘口为准,并结合你的整体持仓自行判断。`;
</script>
</body>
</html>"""


def main():
    p = argparse.ArgumentParser(description="OpTrader Signals — 每日 3 个期权候选 (独立于 wheel)")
    p.add_argument("--tickers", nargs="+", default=DEFAULT_UNIVERSE)
    p.add_argument("--top", type=int, default=3)
    p.add_argument("--out-dir", default=".")
    p.add_argument("--demo", action="store_true", help="用合成数据, 不联网, 预览用")
    p.add_argument("--open", action="store_true", help="跑完用默认浏览器打开 index.html")
    p.add_argument("--csp", action="store_true",
                   help="bullish 标的改出单腿裸卖 put (Cash-Secured Put) 而非看跌价差")
    run(p.parse_args())


if __name__ == "__main__":
    main()
