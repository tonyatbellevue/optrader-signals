# CLAUDE.md — OpTrader Signals

给 Claude Code 的项目说明。打开本目录运行 `claude` 时会自动加载。

## 这是什么
一个**独立**的期权信号 + 本地模拟交易工具:每天用量化方法(VRP、IV/HV Rank、预期波动、趋势、流动性、财报避雷)从 80 个美股里选 3 个期权候选,自动按 1 张合约"模拟建仓",持有到期结算,生成终端风格的 web 仪表盘(`index.html`)。

## ⚠️ 隔离要求
本项目与用户的另一个项目 **wheel-screener 完全独立**,不要把两者的代码、文件名、默认标的池或逻辑混在一起。wheel-screener 做现金担保看跌的年化筛选;本项目跨标的、跨多种结构(credit/debit spread、strangle)做每日机会发现 + 模拟交易。

## 运行
```bash
pip install -r requirements.txt
python optrader_signals.py                 # 真实数据(需联网, 美股盘中/盘后最准)
python optrader_signals.py --demo           # 合成数据离线预览(干净, 不建仓历史)
python optrader_signals.py --demo --seed-demo  # 回填假历史, 预览面板长相
```
跑完打开 `index.html`(数据已内嵌, 双击即可)。

## 关键文件
- `optrader_signals.py` — 全部逻辑:数据抓取(yfinance)、打分、结构选择(Black-Scholes 定价)、模拟交易结算、HTML 生成。HTML 模板是文件末尾的 `HTML_SHELL` 常量(数据通过 `/*__DATA__*/null` 占位符注入)。
- `signals.json` / `index.html` — 每次运行生成的输出。
- `data/iv_history.json` — 每日 ATM IV 存档, 用于算 IV Rank(逐日累积)。
- `data/paper_ledger.json` — 模拟交易持仓/结算账本(状态文件)。
- `.github/workflows/daily.yml` — 每交易日自动跑并 commit 回仓库 → GitHub Pages。

## 重要语义
- **模拟交易起始日** `SIM_START`(脚本顶部, 默认 2026-06-08);此日期前运行只选股不建仓。命令行 `--sim-start` 可覆盖。
- **每笔 1 张合约**(`--contracts`)。入场权利金用 BS 模型估算, 到期按标的收盘价的内在价值结算。
- `data/*.json` 是**状态文件, 必须随每日运行 commit**(IV Rank 和模拟盈亏靠它们累积)——不要加进 .gitignore。
- `--reset` 清空模拟账本重来;`--date YYYY-MM-DD` 覆盖运行日期(补跑/测试)。
- 仪表盘含:3 候选卡 + 数据论证、模拟交易面板(实现盈亏/胜率/资金曲线/SPY α/卖方买方分组)、全市场扫描表。

## 约定与注意
- 改默认观察池 → 编辑脚本顶部 `DEFAULT_UNIVERSE`(已分组注释)。
- 主题小盘(无人机 AVAV/KTOS/RCAT、电池 QS/ENVX/AMPX、SiC NVTS)期权流动性弱、价差宽, 信号噪音大, 处理时保留这层提醒。
- Yahoo 数据有延迟、会限流;真实抓取已内置 `--sleep` 间隔 + 失败重试。
- demo 数字是合成的, 不要当成真实回测结果呈现。
- **不构成投资建议**:这是系统化筛选 + 教育工具, 任何关于行权价/仓位/盈亏的表述都应带此前提。

## 常见后续任务(供参考)
- 加单结构类型(credit spread / strangle / debit)分项统计
- SPY 基准叠加最大回撤对比
- 按主题分组各出 1 个 top 候选
- IV 历史走势小图
