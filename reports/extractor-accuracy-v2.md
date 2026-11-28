# Extractor accuracy: spec v2, 105 cases (corrections, @-questions)

Model `qwen2.5:7b-instruct`, 105 synthetic fixture messages ([tests/fixtures/messages.jsonl](../tests/fixtures/messages.jsonl)), run on 2026-11-28 11:20.
Median time per message 1.8 s, slowest 5.3 s.

| Measure | Result |
|---|---|
| all correct | 105/105 (100%) |
| order vs not-an-order | 105/105 (100%) |
| orders detected (recall) | 73/73 (100%) |
| non-orders ignored | 32/32 (100%) |
| op correct on new orders (add) | 51/51 (100%) |
| op correct on changes (reduce/set/cancel) | 22/22 (100%) |
| add fully correct | 51/51 (100%) |
| qty correct on orders | 73/73 (100%) |
| date correct on orders | 73/73 (100%) |

## Misses

| # | Created | Message | Expected | Got |
|---|---|---|---|---|
| | | none | | |
