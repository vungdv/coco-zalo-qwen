# Extractor accuracy: spec v2 (order adjustments)

Model `qwen2.5:7b-instruct`, 101 synthetic fixture messages ([tests/fixtures/messages.jsonl](../tests/fixtures/messages.jsonl)), run on 2026-11-26 15:39.
Median time per message 1.8 s, slowest 6.0 s.

| Measure | Result |
|---|---|
| all correct | 100/101 (99%) |
| order vs not-an-order | 100/101 (99%) |
| orders detected (recall) | 70/71 (99%) |
| non-orders ignored | 30/30 (100%) |
| op correct on new orders (add) | 50/51 (98%) |
| op correct on changes (reduce/set/cancel) | 20/20 (100%) |
| add fully correct | 50/51 (98%) |
| qty correct on orders | 70/71 (99%) |
| date correct on orders | 70/71 (99%) |

## Misses

| # | Created | Message | Expected | Got |
|---|---|---|---|---|
| 11 | 2026-11-23 | cho mình 2 phần cái kia | add qty=2 date=2026-11-23 | not an order (model: other) |
