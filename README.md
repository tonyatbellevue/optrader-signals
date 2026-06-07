# OpTrader · Signals

每日生成 **3 个期权候选**,用顶尖期权交易员常用的量化方法打分,并用数据论证,通过一个终端风格的网页展示、自动更新。

> ⚠️ 这是一个**独立项目**,与 `wheel-screener` 无关、互不干扰。wheel-screener 专注现金担保看跌(sell-put)的年化筛选;本项目跨标的、跨多种结构(credit/debit spread、strangle)做每日机会发现。

---

## 方法论(每个候选的论证依据)

| 信号 | 含义 | 谁有利 |
|---|---|---|
| **VRP = ATM IV ÷ HV30** | 隐含波动率相对已实现波动率的溢价 | >1 期权偏贵 → 卖方 |
| **IV Rank / Percentile** | 当前 IV 在自身历史中的分位(程序每天存档逐渐累积) | 高→卖,低→买 |
| **HV Rank** | 已实现波动率 1 年分位(价格即可算,IVR 不足时的即时代理) | 同上 |
| **预期波动幅度** | 由 ATM straddle 中价反推 | 评估到期前可能振幅 |
| **趋势** | 现价 vs SMA50 / SMA200 | 决定方向性结构 |
| **流动性** | ATM 到期的 OI / 量 / 价差 | 闸门,过滤难成交合约 |
| **财报临近** | 到期窗口内是否有财报 | 卖方避雷(IV crush / 跳空) |

**选结构逻辑(规则化、可解释):**

- IV 贵(VRP 高 + IV Rank 高):**卖方** —— 看多用 put credit spread、看空用 call credit spread、中性用 short strangle,行权价按 ~0.18–0.25 delta 选。
- IV 便宜(VRP 低 + IV Rank 低)+ 有趋势:**买方** —— 顺势 call/put debit spread,风险=权利金。
- 跨标的按 edge score 排序,取前 3。

---

## 安装 & 运行

```bash
pip install -r requirements.txt

python optrader_signals.py            # 真实数据(需联网)
python optrader_signals.py --demo     # 合成数据,不联网,先预览仪表盘
python optrader_signals.py --open     # 跑完自动用默认浏览器打开 index.html
```

跑完打开生成的 `index.html`(双击即可,数据已内嵌,无需服务器);或加 `--open` 让脚本自动弹出。

**常用参数**

```bash
python optrader_signals.py --tickers SPY QQQ NVDA TSLA --top 3
python optrader_signals.py --out-dir ./docs        # 输出到别的目录
```

最佳运行时机:美股盘中或收盘后(约 **21:30–04:00 SGT**),此时期权 bid/ask 是当日数据;白天(SGT)跑拿到的是上一交易日快照。

---

## 每天自动更新

**方式 A — GitHub Actions + Pages(推荐,无需本机常开)**

1. 把本目录推到一个 GitHub 仓库。
2. 仓库 Settings → Pages → Source 选 `main` 分支根目录。
3. `.github/workflows/daily.yml` 已配置好:每个交易日 21:30 UTC 自动跑、把 `index.html / signals.json / data/iv_history.json` commit 回仓库。
4. 访问 `https://<用户名>.github.io/<仓库名>/` 看每日更新。
   - IV 历史会随每日 commit 累积,**IV Rank 会越来越准**(初期数据不足时自动用 HV Rank 代理)。

**方式 B — Windows 任务计划(本机)**

任务计划程序新建每日任务,操作设为:

```
程序: python
参数: C:\path\to\optrader-signals\optrader_signals.py
起始于: C:\path\to\optrader-signals
```

网页每 15 分钟自带刷新(`<meta refresh>`),本机用浏览器开着即可。

---

## 免责声明

数据来自 Yahoo Finance,有延迟;IV 为 Yahoo 自算,与券商会有出入。本工具是**系统化筛选 / 教育用途,不构成投资建议**。实际行权价、仓位与成交价请以券商(如 Power E*Trade)实时盘口为准,并结合自身整体持仓与风险承受能力自行判断。卖方结构(尤其裸 strangle)有重大双向风险。
