# BTC/ETH 欧式期权组合风险与 PnL 契约

输入与输出的文件结构、字段、类型、枚举、单位、granularity、顺序和 validation categories 以 `schema/input_schema.json`、`schema/output_schema.json` 为准；本文件补充金融语义。

## 1. 估值与单位

- 报告货币为 USD；`S`、`K`、期权价值 `V` 均按每单位 BTC/ETH 的 USD 口径。
- 使用 European Black-Scholes valuation、连续复利年化 `r`、`q_div = 0` 和 decimal implied volatility。
- 时间使用 UTC exact timestamps 和 ACT/365 exact-second fraction，`T` 不小于零。
- 当 `T <= 0`，Call/Put 分别按 `max(S-K, 0)`、`max(K-S, 0)` 估值，全部 unit/position Greeks 为 `0`。

`q` 为 contracts，`m` 为 metadata 给出的 underlying-per-contract `contract_multiplier`。Position monetary/risk values 使用相应 unit value 的 `q * m` scaling：

`market_value = q * m * V`

`fee_usd` 为整笔 trade 的 USD fee。

## 2. Greeks

输出标准 Black-Scholes unit Delta、Gamma、Vega、Theta、Vanna 和 Volga。

单位约定：

- volatility inputs/changes 使用 decimal；`Vega_decimal` 对应 decimal volatility，`Vega_1vol = 0.01 * Vega_decimal`。
- `theta_year` 是市场状态不变时日历时间向前的年化导数；`theta_day = theta_year / 365`。
- `Vanna = ∂²V / (∂S ∂σ)`。
- `Volga = ∂²V / ∂σ²`。

## 3. Position 与 Actual PnL

Trade `signed_qty` 对 BUY 为正、SELL 为负：

`q1 = q0 + sum(signed_qty)`

对 trade `j`，`dq_j` 为 signed quantity，`Pexec_j` 为每单位标的的 USD execution price：

`trade_cashflow_j = -dq_j * m * Pexec_j`

定义：

- `MV0 = q0 * m * V0`；`MV1 = q1 * m * V1`。
- `total_pnl = MV1 - MV0 + sum(trade_cashflow_j) - sum(fee_usd_j)`。
- `carry_actual_pnl = q0 * m * (V1 - V0)`。
- `trade_to_t1_pnl = sum(dq_j * m * (V1 - Pexec_j))`。

Portfolio 数值字段为 instrument-level 对应字段之和。

## 4. Carry attribution

Attribution 使用 beginning position `q0` 的 t0 position Greeks。令：

- `dS = S1 - S0`；
- `dSigma = sigma1 - sigma0`；
- `elapsed_years` 为 t0 到 t1 的 UTC/ACT/365 exact-second fraction。

规范项为：

| Component | Definition |
|---|---|
| `delta_pnl` | `Delta0_pos * dS` |
| `gamma_pnl` | `0.5 * Gamma0_pos * dS^2` |
| `vega_pnl` | `VegaDecimal0_pos * dSigma` |
| `theta_pnl` | `ThetaYear0_pos * elapsed_years` |
| `vanna_pnl` | `Vanna0_pos * dS * dSigma` |
| `volga_pnl` | `0.5 * Volga0_pos * dSigma^2` |

`explained_pnl` 为上述六项之和。

`residual_pnl = carry_actual_pnl - explained_pnl`

Residual 不要求为零。日内 trades 不纳入 carry attribution。

## 5. Stress

Stress 使用 EOD position `q1` 和 t1 base market：

`S_stress = S1 * (1 + spot_shock_pct)`

`sigma_stress = sigma1 + vol_shock_abs`

每个 scenario 对每个 metadata instrument 执行 Black-Scholes full repricing；stress PnL 为 stressed value 减 base value。非有限或无效 stressed spot/IV 为 Non-recoverable `INVALID_SCENARIO`。

## 6. Validation 与运行结果

### Recoverable

Schemas 标记的 normalization/optional-observation warning rules 可恢复且不改变核心金融结果。

### Non-recoverable

Schema、referential integrity、authoritative core-state consistency、核心数值/时间、scenario 或 finite-output 约束失败均不可恢复。程序以非零状态退出，只生成 `validation_report.json`，不生成其余金融输出。

成功运行退出码为零，生成六个输出。状态、record shape、排序和序列化规则以 `output_schema.json` 为准。
