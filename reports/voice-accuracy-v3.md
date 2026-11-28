# Voice command accuracy (qwen2.5:7b-instruct)

- Cases: 81 (52 should change an order)
- Fully correct: 79 (98%)
- **Wrong actions: 0**
- Valid commands done correctly: 50/52 (96%)
- Different error code only: 0
- Latency: median 1.3s, p95 1.4s

| Transcript | Expected | Got | Kind |
|---|---|---|---|
| Lê Hương 0 suất | {"outcome": "done", "op": "cancel", "qty": null, "customer_id": "zalo:111"} | {"outcome": "failed", "error": "not_understood"} | missed |
| le huong bot 1 | {"outcome": "done", "op": "reduce", "qty": 1, "customer_id": "zalo:111"} | {"outcome": "failed", "error": "not_understood"} | missed |
