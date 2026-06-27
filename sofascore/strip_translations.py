#!/usr/bin/env python3
"""Strip ``fieldTranslations`` blocks from already-crawled SofaScore JSON files.

Removes the per-language name dictionaries (Arabic/Hindi/Russian/... ) we don't
need, in place, keeping the English ``name`` fields and any unicode in genuine
foreign names. Skips ``*.meta.json`` sidecars (they contain no translations) and
non-JSON files. Re-serialises with ``ensure_ascii=False`` so remaining foreign
names are stored as readable unicode rather than ``\\uXXXX`` escapes.

Usage:
    python strip_translations.py [event_dir]   # default: ./event
"""

from __future__ import annotations

import json
import os
import sys

from crawl import strip_field_translations


def process(root: str) -> None:
    changed = before_total = after_total = files = 0
    for dirpath, _dirs, names in os.walk(root):
        for name in names:
            if not name.endswith(".json") or name.endswith(".meta.json"):
                continue
            path = os.path.join(dirpath, name)
            with open(path, "rb") as f:
                raw = f.read()
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            files += 1
            had = b'"fieldTranslations"' in raw
            strip_field_translations(obj)
            out = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
            with open(path, "w", encoding="utf-8") as f:
                f.write(out)
            before_total += len(raw)
            after_total += len(out.encode("utf-8"))
            if had:
                changed += 1
    print(f"processed {files} json files, {changed} contained fieldTranslations")
    print(f"size: {before_total/1e6:.2f} MB -> {after_total/1e6:.2f} MB "
          f"({100*(before_total-after_total)/max(before_total,1):.1f}% smaller)")


if __name__ == "__main__":
    root = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "event")
    process(os.path.abspath(root))
