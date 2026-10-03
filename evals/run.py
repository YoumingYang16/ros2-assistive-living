"""Run curated, software-only semantic regression cases; never calls a model."""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from robot_voice_patrol.config import load_config
from robot_voice_patrol.contracts import CommandError
from robot_voice_patrol.natural_language import DialoguePlanner


def condition_outcomes(condition):
    if not condition:
        return []
    if "outcome" in condition:
        return [condition["outcome"]]
    if "not" in condition:
        return condition_outcomes(condition["not"])
    return [outcome for child in condition.get("all", condition.get("any", [])) for outcome in condition_outcomes(child)]


def evaluate(path: Path, config: dict) -> dict:
    cases = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(cases, list) or not cases:
        raise ValueError("evaluation corpus must be a nonempty list")
    identifiers, identities = set(), set()
    for case in cases:
        identity = json.dumps({key: case.get(key) for key in ("text", "prepare", "context", "context_after_prepare")}, sort_keys=True)
        if case["id"] in identifiers or identity in identities:
            raise ValueError("duplicate evaluation case; repeated samples must not inflate the score")
        identifiers.add(case["id"])
        identities.add(identity)
    planner = DialoguePlanner(config, provider=False)
    groups = defaultdict(lambda: {"passed": 0, "total": 0})
    records, timings = [], []
    for case in cases:
        context = dict(case.get("context", {}))
        start = time.perf_counter()
        actual, problems = {}, []
        try:
            for turn in case.get("prepare", []):
                context = planner.interpret(turn, context).context
            context.update(case.get("context_after_prepare", {}))
            result = planner.interpret(case["text"], context)
            steps = result.plan.steps if result.plan else []
            actual = {"kind": result.kind, "targets": [s.target for s in steps if s.kind == "navigate"],
                      "objects": [s.object_name for s in steps if s.kind == "inspect"],
                      "seconds": [s.seconds for s in steps if s.kind == "wait"],
                      "kinds": [s.kind for s in steps], "step_count": len(steps), "options": result.options,
                      "condition_outcomes": [outcome for s in steps for outcome in condition_outcomes(s.condition)],
                      "conditions": [s.condition for s in steps if s.condition],
                      "on_failure": [s.on_failure for s in steps],
                      "goal": result.plan.metadata.get("goal") if result.plan else None,
                      "requires_confirmation": bool(result.plan and result.plan.metadata.get("requires_confirmation"))}
        except CommandError:
            actual = {"kind": "reject"}
        except Exception as exc:
            actual = {"kind": "error", "error_type": type(exc).__name__}
        elapsed = (time.perf_counter() - start) * 1000
        timings.append(elapsed)
        for key, expected in case["expected"].items():
            if actual.get(key) != expected:
                problems.append({"field": key, "expected": expected, "actual": actual.get(key)})
        category = groups[case["category"]]
        category["total"] += 1
        category["passed"] += not problems
        records.append({"id": case["id"], "category": case["category"], "passed": not problems,
                        "expected_kind": case["expected"]["kind"], "actual_kind": actual["kind"],
                        "latency_ms": round(elapsed, 3), "mismatches": problems})
    passed = sum(item["passed"] for item in records)
    sorted_times = sorted(timings)
    return {"suite": "curated_chinese_semantics_v3", "model_calls": 0,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "corpus_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "scope": "deterministic regression only; not real-world ASR or general language accuracy",
            "total": len(cases), "passed": passed, "failed": len(cases) - passed,
            "accuracy": round(passed / len(cases), 4) if cases else 0,
            "unintended_task_acceptances": sum(item["actual_kind"] == "task" and item["expected_kind"] in {"reject", "clarify", "answer"} for item in records),
            "latency_ms": {"p50": round(statistics.median(timings), 3) if timings else 0,
                           "p95": round(sorted_times[min(len(sorted_times)-1, int(len(sorted_times)*.95))], 3) if timings else 0},
            "categories": dict(groups), "cases": records}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=Path(__file__).with_name("cases.json"))
    parser.add_argument("--config")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    report = evaluate(args.cases, load_config(args.config))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary = {key: value for key, value in report.items() if key != "cases"}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if report["failed"]:
        for item in report["cases"]:
            if not item["passed"]:
                print(json.dumps(item, ensure_ascii=False))
    return 0 if report["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
