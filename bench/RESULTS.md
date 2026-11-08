# sessionsteward benchmark (20 seeded corpora)

Synthetic memory files with known defects; token counts are characters / 4.
Re-run: `python bench/run.py`. Detector numbers describe this corpus only.

## Detectors (`diet --lint`, `scan`)
| detector | recall | precision | note |
|---|---|---|---|
| same line twice in a file | 100% | 100% | |
| near-duplicate (filler, case, plural) | 99% | 98% | |
| partial synonym swap | 48% | - | word overlap only; a full rewrite is not found |
| do vs. do-not on one topic | 100% | 88% | polarity flips only |
| path that does not exist | 100% | 100% | 0 false alarms on existing paths |
| secret-shaped value | 100% | 100% | 0 false alarms on hashes, uuids, prose |

## `diet --fix` (safe part only)
- session-start tokens: 70,356 -> 66,046 (6.1% fewer); `--lint` sees 12.5% in reach if near-duplicates are merged by hand
- distinct instructions lost by `--fix`: 0

## `lost` vs. a line diff (simulated compaction: drop 10%, reword 10%, move 5%, shuffle, add)
| | line diff | `lost` |
|---|---|---|
| lines you must read | 2863 removed lines | 638 (dropped + reworded) |
| truly dropped lines found | all 320, buried | recall 98%, precision 100% |
| reworded lines paired with their new wording | no | 99% |
| lines moved to another file recognised | no | 100% |

## Speed (3,433 lines, 12 files)
diet 0.10 s, diet --lint 4.68 s, lost 0.24 s, scan 0.24 s

Hostile line (200,000 characters of `a.a.a.`): scan 0.11 s;
a token 200,000 characters into one line is found.

