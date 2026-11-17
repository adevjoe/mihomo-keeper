#!/usr/bin/env python3
"""Deep-merge a local override.yaml onto a downloaded mihomo subscription config.

Usage: mihomo-merge-config.py <subscription.yaml> <override.yaml> <output.yaml>

Merge rules:
  - dict + dict        -> recursive merge (override wins on scalar conflicts)
  - proxy-groups list  -> merged by `name`; the override `name` may be a glob
                          (fnmatch, e.g. "*节点") and will patch every matching group.
                          A non-glob name that matches nothing is appended as a new group.
  - rules              -> `rules` replaces the whole list; instead prefer
                            rules-prepend: [..]   inserted at the top
                            rules-append:  [..]   inserted before the final MATCH rule
                            rules-insert:  [{before: "<exact rule>", rules: [..]},
                                            {after:  "<exact rule>", rules: [..]}]
  - any other list     -> replaced entirely by the override value
  - key set to null    -> key removed from the result
  - top-level keys starting with "x-" are override-local (YAML anchors etc.) and dropped
"""
import fnmatch
import sys
from pathlib import Path

import yaml

GLOB_CHARS = set("*?[")


RULE_OPS = ("rules-prepend", "rules-append", "rules-insert")


def apply_rule_ops(rules, over):
    rules = list(rules)
    for r in over.get("rules-prepend") or []:
        if r not in rules:
            rules.insert(0, r)
    for item in over.get("rules-insert") or []:
        anchor_key = "before" if "before" in item else "after"
        anchor = item[anchor_key]
        if anchor not in rules:
            raise SystemExit(f"rules-insert anchor not found in subscription rules: {anchor!r}")
        idx = rules.index(anchor) + (1 if anchor_key == "after" else 0)
        for r in reversed(item.get("rules") or []):
            if r not in rules:
                rules.insert(idx, r)
    for r in over.get("rules-append") or []:
        if r in rules:
            continue
        if rules and str(rules[-1]).startswith("MATCH,"):
            rules.insert(len(rules) - 1, r)
        else:
            rules.append(r)
    return rules


def merge_dict(base, over):
    out = dict(base)
    for k, v in over.items():
        if k in RULE_OPS:
            continue
        if v is None:
            out.pop(k, None)
        elif k == "proxy-groups" and isinstance(v, list) and isinstance(out.get(k), list):
            out[k] = merge_groups(out[k], v)
        elif isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge_dict(out[k], v)
        else:
            out[k] = v
    if any(k in over for k in RULE_OPS):
        out["rules"] = apply_rule_ops(out.get("rules") or [], over)
    return out


def merge_groups(base, over):
    groups = [dict(g) for g in base]
    for patch in over:
        if not isinstance(patch, dict) or "name" not in patch:
            raise SystemExit(f"override proxy-groups entry must have a name: {patch!r}")
        pattern = str(patch["name"])
        body = {k: v for k, v in patch.items() if k != "name"}
        is_glob = bool(GLOB_CHARS & set(pattern))
        hit = False
        for i, g in enumerate(groups):
            if fnmatch.fnmatchcase(str(g.get("name", "")), pattern):
                groups[i] = merge_dict(g, body)
                hit = True
        if not hit:
            if is_glob:
                print(f"warn: override group pattern {pattern!r} matched nothing", file=sys.stderr)
            else:
                groups.append(dict(patch))
    return groups


def main():
    sub_path, over_path, out_path = map(Path, sys.argv[1:4])
    sub = yaml.safe_load(sub_path.read_text(encoding="utf-8")) or {}
    over = yaml.safe_load(over_path.read_text(encoding="utf-8")) or {}
    if not isinstance(sub, dict) or not isinstance(over, dict):
        raise SystemExit("both subscription and override must be YAML mappings")
    over = {k: v for k, v in over.items() if not str(k).startswith("x-")}
    merged = merge_dict(sub, over)
    out_path.write_text(
        yaml.safe_dump(merged, allow_unicode=True, sort_keys=False, width=4096),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
