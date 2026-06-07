# OpTrader Signals — 完整荐股逻辑

> 本文件是 `optrader_signals.py` 选股逻辑的人类可读权威说明(source of truth)。
> 项目**独立于** wheel bot（`ai-trading-wheel` 仓库），两者互不共享代码/状态。
> 数据来自 yfinance（Yahoo 非官方接口，有延迟）。**这是系统化筛选工具，不是投资建议。**

---

## 0. 一句话总括

> **只在波动率分位足够高（IV/HV Rank > 50）、且 VRP 确认期权确实贵时，才卖 premium；
> 用 30Δ/16Δ 定结构、约 45 DTE 进场、盈利 50% 或剩 21 DTE 了结；远离财报、确保流动性。**

内核 = 波动率均值回归 + 正向波动率风险溢价（VRP）收割；纪律 = tastytrade 机械化规则；
方向已用自带回测（`backtest_optrader.py`）检验过——但属 HV 代理下的方向性证据，非精确收益。

---

## 1. 理论基础（两层，均有出处）

### 1.1 地基：波动率风险溢价 VRP（学术认证）
```
VRP = ATM 隐含波动率(IV) / 30天已实现波动率(HV30)
```
- 同行评审结论（Carr & Wu 2009；Bollerslev-Tauchen-Zhou 2009, RFS）：**IV 系统性高于后续实现波动**，卖方长期有正期望。
- VRP > 1 → 期权真贵 → 利好卖方。本工具中 VRP 只作**确认信号**，不作主排序。

### 1.2 骨架：tastytrade 机械化规则（大样本回测认证）
| 环节 | 规则 | 代码常数 |
|------|------|----------|
| 入场门槛 | **IV Rank > 50** 才卖 premium | `TT_IVR_GATE = 50` |
| 主排序 | IVR 越高分越高 | — |
| 进场到期 | **~45 DTE** | `TT_TARGET_DTE = 45` |
| 短腿 delta | **30Δ**（价差）/ **16Δ**（宽跨） | `TT_SHORT_DELTA = 0.30` / `TT_STRANGLE_DELTA = 0.16` |
| 退出管理 | **盈利 50% 止盈 + 21 DTE 强制管理** | `TT_PROFIT_TARGET = 0.50` / `TT_MANAGE_DTE = 21` |

> IVR 需逐日累积 ATM IV 历史（≥20 天）才有；**冷启动期用 HV Rank 代理**，rationale 会标注。

---

## 2. 每标的信号（`fetch_metrics`）

用 yfinance 1 年日线，只用截至当日数据（无前视）：

| 信号 | 算法 |
|------|------|
| 现价 spot | 最新收盘 |
| HV30 | 近30日对数收益标准差 × √252 |
| HV Rank | 20日滚动年化波动的 1 年分位 |
| ATM IV | 最接近现价的 call/put 隐含波动均值 |
| VRP | ATM IV / HV30 |
| IV Rank | ATM IV 在累积历史中的分位（数据足够时） |
| 趋势 | spot vs SMA50 vs SMA200 → bullish / bearish / neutral |
| 预期波动 | ATM straddle 中价 / 现价 |
| 流动性 | 目标到期 calls+puts 的 OI、volume |
| 财报临近 | `t.calendar` 下次财报日距今天数 |
| 目标到期 | 链中最接近 45 DTE 且 ≥5 天者 |

---

## 3. 决策树（`score_and_structure`）

```
                  IV/HV Rank ≥ 50 ?
                  ┌────────┴────────┐
                 是 (SELL_PREMIUM)   否 (门槛外)
                  │                   │
            VRP 确认溢价          有明确趋势?
                  │              ┌────┴────┐
          看趋势选结构          是          否
          ├ bullish → 30Δ Put Credit Spread   方向性     低分
          ├ bearish → 30Δ Call Credit Spread  Debit价差  (基本不选)
          └ neutral → 16Δ Short Strangle
```

---

## 4. 打分公式

```
# 卖方分支（过门槛）
ivr_edge = (rank − 50) / (100 − 50) × 100          # 50→0, 100→100
vrp_conf = clamp((VRP − 1.0) / 0.5 × 15, 0, 15)    # 至多 +15
base     = min(100, 30 + 0.55 × ivr_edge + vrp_conf)   # 过门槛即有 30 底分

# 买方分支（门槛外）
vrp_cheap   = clamp((1.0 − VRP) / 0.25 × 100, 0, 100)
directional = 20 if 趋势∈{bullish,bearish} else 0
base        = 0.5 × vrp_cheap + directional

# 流动性闸门（共用）
liq   = clamp(log10(max(OI,1)) / 6.0 × 100, 0, 100)
score = base × (0.5 + 0.5 × liq/100)               # OI 不足最多砍一半
```
跨标的按 score 降序，取前 N（默认 3）→ `signals.json` + `index.html`。

> **诚实声明**：评分曲线是对被验证信号（IVR）的单调变换，**不是拟合出来的 alpha**。
> tastytrade 认证的是「门槛/Δ/DTE/管理」这套规则，排序曲线本身是合理但未单独回测的选择。

---

## 5. 风控叠加

- **财报避雷**：财报落在到期内 → rationale 打 ⚠（IV crush / 跳空风险）。
- **流动性闸门**：OI 不足按上式砍分，防止挂不出去。
- **退出纪律**：每条候选都附「盈利 50% 止盈 / 21 DTE 强制管理」。

---

## 6. 回测验证（`backtest_optrader.py`）

**数据约束（重要）**：yfinance 无历史期权链/IV → 回测用 **HV 代理 IV + Black-Scholes 定价 + 真实标的日线走势**。结论是**方向性证据，非精确收益**；精确回测需付费历史期权数据（ORATS / CBOE DataShop）。

**结论（10 标的 / 3 年 / 910 笔 Put Credit Spread）**：
| 分组 | 笔数 | 胜率 | 总 PnL | 均 RoR |
|------|------|------|--------|--------|
| 策略：HVR>50 才卖 | 506 | 79.4% | +$41,022 | +5.8% |
| 被门槛挡掉的低 HVR | 404 | 76.0% | −$16,952 | −1.3% |

- ✅ **门槛有效**：高波动分位才卖 → 赚钱；低分位卖 → 亏钱。
- ✅ **打分有区分力**：score↔RoR Pearson **+0.185**。
- ⚠️ **edge 主要来自个股**：指数 ETF（SPY/QQQ）高波动常伴随下跌，卖看跌反而吃亏。

---

## 7. 宇宙与运行

- **默认 80 标的**（`DEFAULT_UNIVERSE`）：指数/板块 ETF(15) + 科技/半导体/AI(24) + 金融(12) + 消费(10) + 医药(6) + 能源工业(7) + 加密相关(3) + 成长(3)。故意与 wheel 的 32 标的区分。
- **运行**：
  ```bash
  python optrader_signals.py            # 真实数据
  python optrader_signals.py --demo     # 合成数据预览
  python optrader_signals.py --open     # 跑完自动开网页
  python backtest_optrader.py           # 回测验证
  ```
- **最佳时机**：美股盘中/收盘后（约 21:30–04:00 SGT），此时期权 bid/ask 为当日数据；白天 SGT 跑拿到上一交易日快照。
- **自动化**：`.github/workflows/daily.yml` 每交易日跑 → commit 回仓库 → GitHub Pages。

---

## 8. 已知局限 / 下一步

1. **IVR 冷启动**：上线初期 IV 历史不足，全程用 HVR 代理，需每天跑攒够 ~20 天。
2. **数据源脆弱**：yfinance 依赖 Yahoo 非官方接口，可能限流/断流；依赖已 pin 到确切版本防自动升级破坏。
3. **回测是 HV 代理**：非精确收益；要严肃验证需付费历史期权数据。
4. **指数与个股行为不同**：可考虑给 ETF 单独逻辑（高 vol 时不卖看跌）。
5. **排序曲线未单独回测**：权重虽锚定 IVR，仍建议后续做参数敏感性分析。
