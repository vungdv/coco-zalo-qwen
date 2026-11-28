# Extractor accuracy: qwen2.5 14b, 105 cases

Model `qwen2.5:14b-instruct`, 105 synthetic fixture messages ([tests/fixtures/messages.jsonl](../tests/fixtures/messages.jsonl)), run on 2026-11-28 12:21.
Median time per message 2.2 s, slowest 22.6 s.

| Measure | Result |
|---|---|
| all correct | 102/105 (97%) |
| order vs not-an-order | 102/105 (97%) |
| orders detected (recall) | 70/73 (96%) |
| non-orders ignored | 32/32 (100%) |
| op correct on new orders (add) | 50/51 (98%) |
| op correct on changes (reduce/set/cancel) | 20/22 (91%) |
| add fully correct | 50/51 (98%) |
| qty correct on orders | 70/73 (96%) |
| date correct on orders | 70/73 (96%) |

## Misses

| # | Created | Message | Expected | Got |
|---|---|---|---|---|
| 11 | 2026-11-23 | cho mình 2 phần cái kia | add qty=2 date=2026-11-23 | not an order (model: several) |
| 90 | 2026-11-23 | thôi chị ơi e không đặt nữa | cancel qty=None date=2026-11-23 | not an order (model: other) |
| 93 | 2026-11-23 | hủy hết nhé | cancel qty=None date=2026-11-23 | not an order (model: other) |
