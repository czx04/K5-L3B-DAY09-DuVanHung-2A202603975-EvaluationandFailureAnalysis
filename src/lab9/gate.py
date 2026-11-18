"""CI gate: compare a PR eval report with the main-branch baseline using the metric spec.

    python -m lab9.gate --pr reports/eval_workflow.json --baseline reports/baseline_v1.json

exit 0 = merge, exit 1 = blocked. Safety metrics: max 0, no averaging away.
Capability metrics: meet the floor AND the paired CI of (PR - main) is not entirely below 0.
Thresholds may only get stricter on the same eval-set version (ratchet).
"""

from __future__ import annotations

import argparse
import json
from math import isfinite
from pathlib import Path
from statistics import mean

from lab8.console import utf8_stdio
from lab9 import stats
from lab9.eval import SPEC, load_spec, per_item
from lab9.graders import GRADERS


def _finite_number(value: object) -> bool:
    return isinstance(value, int) or (isinstance(value, float) and isfinite(value))


def _ratchet_problems(old: dict, new: dict, label: str) -> list[str]:
    problems = []
    if old.get("blocking") and not new.get("blocking"):
        problems.append(f"{label}: blocking removed")
    old_limits = old.get("threshold", old)
    new_limits = new.get("threshold", new)
    for key in ("min", "max", "max_increase"):
        if key not in old_limits:
            continue
        before, after = old_limits[key], new_limits.get(key)
        if not _finite_number(after) or (after < before if key == "min" else after > before):
            problems.append(f"{label}: {key} loosened ({before} → {after})")
    if old_limits.get("no_significant_drop") and not new_limits.get("no_significant_drop"):
        problems.append(f"{label}: no_significant_drop removed")
    return problems


def _spec_entries(spec: dict) -> dict[str, dict]:
    entries = {f"metric:{m['id']}": m for m in spec["metrics"]}
    entries.update({f"slice:{s['name']}/{s['metric']}": s for s in spec.get("slices", [])})
    return entries


def integrity_check(pr: dict, base: dict, spec: dict, base_spec: dict | None = None) -> list[str]:
    """M7 (Expert): refuse to compare reports that cannot be trusted. Return a list of problems
    (empty = trustworthy). A gate that passes a broken report is worse than no gate.
    Must catch at least:
      - any metric value in pr["metrics"] that is NaN or inf
      - len(pr["rows"]) != manifest items x len(manifest seeds)   (dropped / duplicated rows)
      - the (item_id, seed) set of PR differs from the baseline's (cherry-picked seeds/items)
      - pr["evaluator_failures"] non-empty, or a row whose value for a graded spec metric is
        None (a crashed grader silently scores as "missing" and drops out of the mean)
      - base_spec given and a threshold in spec is LOOSER than in base_spec for the same
        eval-set version (ratchet: max may not rise, min may not fall, a blocking metric may
        not become non-blocking, a metric may not disappear; the same for spec["slices"],
        including dropping no_significant_drop)
      - an item's slice tags in PR rows differ from the baseline's (relabelling an item out of a
        slice hides its regression from the slice gate)"""
    problems = [
        f"metric {name}: expected a finite number, got {value!r}"
        for name, value in pr["metrics"].items()
        if isinstance(value, bool) or not _finite_number(value)
    ]
    manifest, base_manifest = pr["manifest"], base["manifest"]
    seeds = manifest["seeds"]
    if manifest["items"] != base_manifest["items"]:
        problems.append("manifest item count differs from baseline")
    if sorted(seeds) != sorted(base_manifest["seeds"]):
        problems.append("manifest seeds differ from baseline")
    rows = pr["rows"]
    expected = manifest["items"] * len(seeds)
    if len(rows) != expected:
        problems.append(f"row count {len(rows)} does not match manifest ({expected})")

    base_slices = {(r["item_id"], r["seed"]): set(r.get("slices") or []) for r in base["rows"]}
    seen = set()
    graded = sorted({m["id"] for m in spec["metrics"]} & GRADERS.keys())
    for row in rows:
        pair = (row["item_id"], row["seed"])
        case = f"{pair[0]}@{pair[1]}"
        if pair in seen:
            problems.append(f"{case}: duplicate row")
        seen.add(pair)
        if pair in base_slices and set(row.get("slices") or []) != base_slices[pair]:
            problems.append(f"{case}: slice tags differ from baseline")
        for name in graded:
            if not _finite_number(row.get(name)):
                problems.append(f"{case}: invalid or missing grader value for {name}")
    if seen != base_slices.keys():
        problems.append("(item_id, seed) pairs differ from baseline")
    if pr.get("evaluator_failures"):
        problems.append(f"{len(pr['evaluator_failures'])} evaluator failure(s)")

    if base_spec is not None and spec["eval_set_version"] == base_spec["eval_set_version"]:
        entries = _spec_entries(spec)
        for label, old in _spec_entries(base_spec).items():
            if label in entries:
                problems.extend(_ratchet_problems(old, entries[label], label))
            else:
                problems.append(f"{label}: removed from spec")
    return problems


def _paired_ci(a: dict[str, float], b: dict[str, float], spec: dict) -> tuple[float, str]:
    keys = sorted(a.keys() & b.keys())
    bs = spec.get("bootstrap", {})
    d, lo, hi = stats.paired_bootstrap(
        [a[k] for k in keys], [b[k] for k in keys],
        n_resamples=bs.get("n_resamples", 1000), rng=bs.get("rng", 2027),
        confidence=bs.get("confidence", 0.95),
    )
    return hi, f"Δ {d:+.3f} CI [{lo:+.3f}, {hi:+.3f}]"


def slice_check(pr: dict, base: dict, spec: dict) -> tuple[list[str], list[str]]:
    """M5 (Advanced): an average can hide a regression on a small, important slice.
    For each entry of spec["slices"] ({"name", "metric", "min"?, "no_significant_drop"?, "blocking"}):
      - value = mean of per_item(pr, metric, slice_name=name)
      - fail if below "min", or if no_significant_drop and the paired CI over the slice's items
        lies entirely below 0
    Return (blocked ids like "slice:money_movement/pass_rate", report lines)."""
    blocked, lines = [], []
    for entry in spec.get("slices", []):
        name, metric = entry["name"], entry["metric"]
        sid = f"slice:{name}/{metric}"
        a = per_item(pr, metric, slice_name=name)
        val = mean(a.values()) if a else None
        th = {key: entry[key] for key in ("min", "no_significant_drop") if key in entry}
        failed = val is None or ("min" in th and val < th["min"])
        note = ""
        if val is not None and th.get("no_significant_drop"):
            b = per_item(base, metric, slice_name=name)
            hi, note = _paired_ci(a, b, spec)
            failed = failed or hi < 0
        blocking = entry.get("blocking")
        if failed and blocking:
            blocked.append(sid)
        verdict = "missing" if val is None else "fail" if failed else "ok"
        label = {"ok": "ok", "missing": "MISSING"}.get(verdict, "BLOCK" if blocking else "warn")
        shown = "None" if val is None else f"{val:.4f}"
        lines.append(f"  {sid:<22} {shown:<8} {json.dumps(th):<40} {note:<28} {label}")
    return blocked, lines


def check(pr: dict, base: dict, spec: dict, base_spec: dict | None = None) -> tuple[list[str], list[str]]:
    if pr["manifest"]["eval_set_sha"] != base["manifest"]["eval_set_sha"]:
        return ["eval_set_sha"], ["  EVAL SET differs from the baseline's: re-run the baseline on the same eval set"]
    problems = integrity_check(pr, base, spec, base_spec)
    if problems:
        return ["integrity"], [f"  INTEGRITY {p}" for p in problems]
    blocked, lines = [], []
    for m in spec["metrics"]:
        mid, th, val = m["id"], m.get("threshold", {}), pr["metrics"].get(m["id"])
        verdict, note = "ok", ""
        if val is None:
            verdict = "missing"
        elif "max" in th and val > th["max"]:
            verdict = "fail"
        elif "min" in th and val < th["min"]:
            verdict = "fail"
        elif "max_increase" in th and base["metrics"].get(mid):
            if val > base["metrics"][mid] * (1 + th["max_increase"]):
                verdict = "fail"
        if th.get("no_significant_drop") and val is not None:
            a, b = per_item(pr, mid), per_item(base, mid)
            hi, note = _paired_ci(a, b, spec)
            if hi < 0:
                verdict = "fail"
        if verdict != "ok" and m.get("blocking"):
            blocked.append(mid)
        label = {"ok": "ok", "missing": "MISSING"}.get(verdict, "BLOCK" if m.get("blocking") else "warn")
        lines.append(f"  {mid:<22} {val!s:<8} {json.dumps(th):<40} {note:<28} {label}")
    if spec.get("slices"):
        sb, sl = slice_check(pr, base, spec)
        blocked += sb
        lines += sl
    return blocked, lines


def main(argv: list[str] | None = None) -> int:
    utf8_stdio()
    ap = argparse.ArgumentParser(prog="lab9.gate")
    ap.add_argument("--pr", required=True)
    ap.add_argument("--baseline", required=True)
    ap.add_argument("--spec", default=str(SPEC))
    ap.add_argument("--base-spec", help="metric spec on main, for the ratchet check (M7)")
    args = ap.parse_args(argv)
    pr = json.loads(Path(args.pr).read_text(encoding="utf-8"))
    base = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
    base_spec = load_spec(Path(args.base_spec)) if args.base_spec else None
    blocked, lines = check(pr, base, load_spec(Path(args.spec)), base_spec)
    print(f"gate: PR {pr['run_id']} vs main {base['run_id']} · eval set {pr['manifest']['eval_set_sha']}")
    print("\n".join(lines))
    if blocked:
        print(f"exit 1 — blocked: {', '.join(blocked)}")
        return 1
    print("exit 0 — merge")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
