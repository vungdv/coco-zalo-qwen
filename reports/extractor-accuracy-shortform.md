# Extractor accuracy: t6

Model `qwen2.5:7b-instruct`, 74 synthetic fixture messages ([tests/fixtures/messages.jsonl](../tests/fixtures/messages.jsonl)), run on 2026-11-25 13:53.
Median time per message 1.6 s, slowest 1.7 s.

| Measure | Result |
|---|---|
| all correct | 70/74 (95%) |
| order vs not-an-order | 70/74 (95%) |
| orders detected (recall) | 47/49 (96%) |
| non-orders ignored | 23/25 (92%) |
| qty correct on orders | 47/49 (96%) |
| date correct on orders | 47/49 (96%) |

## Misses

| # | Created | Message | Expected | Got |
|---|---|---|---|---|
| 11 | 2026-11-23 | cho mình 2 phần cái kia | order qty=2 date=2026-11-23 | not an order (model: other) |
| 33 | 2026-11-23 | Mình đặt 10 phần cho công ty ngày 27 | order qty=10 date=2026-11-27 | not an order (model: several) |
| 51 | 2026-11-23 | mai có món gì vậy chị? | no order (question) | order qty=1 date=2026-11-24 |
| 63 | 2026-11-23 | Mai đặt 2 phần cho mình, Lan đặt 1 phần | no order (several orders) | order qty=2 date=2026-11-24 |
