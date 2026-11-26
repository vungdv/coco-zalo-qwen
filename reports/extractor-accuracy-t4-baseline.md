# Extractor accuracy: T4: model only (before the T4a date resolver)

Model `qwen2.5:7b-instruct`, 69 synthetic fixture messages ([tests/fixtures/messages.jsonl](../tests/fixtures/messages.jsonl)), run on 2026-11-23 10:05.
Median time per message 1.6 s, slowest 5.7 s.

| Measure | Result |
|---|---|
| all correct | 60/69 (87%) |
| order vs not-an-order | 65/69 (94%) |
| orders detected (recall) | 43/44 (98%) |
| non-orders ignored | 22/25 (88%) |
| qty correct on orders | 43/44 (98%) |
| date correct on orders | 38/44 (86%) |

## Misses

| # | Created | Message | Expected | Got |
|---|---|---|---|---|
| 6 | 2026-11-23 | thứ sáu 1 phần | order qty=1 date=2026-11-27 | order qty=1 date=2026-11-29 |
| 7 | 2026-11-23 | thứ hai 1 phần | order qty=1 date=2026-11-30 | order qty=1 date=2026-11-27 |
| 11 | 2026-11-23 | cho mình 2 phần cái kia | order qty=2 date=2026-11-23 | not an order (model: other) |
| 15 | 2026-11-23 | ngày 5 2 phần | order qty=2 date=2026-12-05 | order qty=2 date=2026-11-28 |
| 19 | 2026-11-23 | đặt 5 hộp cho thứ 7 | order qty=5 date=2026-11-28 | order qty=5 date=2026-11-29 |
| 40 | 2026-11-23 | thứ 2 tuần sau mình đặt 2 phần | order qty=2 date=2026-11-30 | order qty=2 date=2026-12-03 |
| 50 | 2026-11-23 | 31/11 2 phần | no order (invalid date) | order qty=2 date=2026-11-30 |
| 51 | 2026-11-23 | mai có món gì vậy chị? | no order (question) | order qty=1 date=2026-11-24 |
| 66 | 2026-11-23 | 30/2 cho mình 1 phần | no order (invalid date) | order qty=3 date=2026-12-02 |
