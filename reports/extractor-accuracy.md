# Extractor accuracy: T4a: model + code date resolver

Model `qwen2.5:7b-instruct`, 69 synthetic fixture messages ([tests/fixtures/messages.jsonl](../tests/fixtures/messages.jsonl)), run on 2026-11-23 10:09.
Median time per message 1.6 s, slowest 3.6 s.

| Measure | Result |
|---|---|
| all correct | 67/69 (97%) |
| order vs not-an-order | 67/69 (97%) |
| orders detected (recall) | 43/44 (98%) |
| non-orders ignored | 24/25 (96%) |
| qty correct on orders | 43/44 (98%) |
| date correct on orders | 43/44 (98%) |

## Misses

| # | Created | Message | Expected | Got |
|---|---|---|---|---|
| 11 | 2026-11-23 | cho mình 2 phần cái kia | order qty=2 date=2026-11-23 | not an order (model: other) |
| 51 | 2026-11-23 | mai có món gì vậy chị? | no order (question) | order qty=1 date=2026-11-24 |
