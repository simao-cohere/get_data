Download all remaining World Cup matches (set TARGET_SUCCESSES=100 to cover the full list).

Landing page

https://www.footballorgin.com/international-games/fifa-world-cup-2026/

Rules

- Skip unavailable videos.
- Skip matches already completed (progress.json is the source of truth — do not re-download matches already listed there).
- Continue after failures.
- Update progress.json.
- Produce report.md.
- Finish only after all matches in world_cup_matches.txt have been attempted or TARGET_SUCCESSES=100 successes are reached.
