# BTC/ETH Options Portfolio Risk and PnL Engine

在 `/app` 工作目录实现一个离线、确定性的 BTC/ETH European option portfolio risk/PnL engine。公开契约为 `/app/input_files/README.md`、`/app/input_files/input_schema.json` 和 `/app/input_files/output_schema.json`。

公开输入数据位于 `/app/input_files/input/`：`config.json`、`initial_positions.csv`、`trades.csv`、`instrument_metadata.csv`、`rates.csv`、`market_t0.parquet`、`market_t1.parquet`、`scenarios.json`。

Candidate 可在 `/app/solution/` 中实现模块，入口必须支持：

```text
python -m solution.main --input-dir /app/input_files/input --output-dir /app/output
```

成功运行须在 `/app/output/` 生成且仅使用以下正式交付文件名：

- `positions_eod.csv`
- `valuation.csv`
- `greeks.csv`
- `pnl_attribution.csv`
- `stress_report.json`
- `validation_report.json`

程序不得访问网络、wall clock 或机器时区。不可恢复错误须按公开契约失败，不得留下部分金融结果。
