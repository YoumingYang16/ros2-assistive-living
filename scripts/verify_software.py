#!/usr/bin/env python3
"""Run the hardware-independent regression suites and preserve their evidence.

Never labels a missing Node/ROS runtime as a passed test. No model calls,
microphone access, dependency installation or server startup are performed.
"""
from __future__ import annotations
import argparse
import ast
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def run_phase(name, command, environment):
    started = time.monotonic()
    try:
        result = subprocess.run(command, cwd=ROOT, env=environment, capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=180)
        output = (result.stdout + result.stderr).strip()
        phase = {"name": name, "status": "passed" if result.returncode == 0 else "failed",
                 "exit_code": result.returncode, "seconds": round(time.monotonic() - started, 3), "output": output}
        match = re.search(r"Ran (\d+) tests", output)
        if match:
            phase["test_count"] = int(match[1])
        return phase
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"name": name, "status": "failed", "error": str(exc)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="work/software-validation.json")
    parser.add_argument("--node", help="Optional explicit path to Node.js")
    args = parser.parse_args()
    destination = Path(args.output).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8",
                       VOICE_PATROL_MODEL_PROVIDER="none")
    phases = []
    sources = [p for directory in ("robot_voice_patrol", "tests", "tests_ros", "examples", "launch", "scripts", "evals")
               for p in (ROOT / directory).rglob("*.py")]
    try:
        for path in sources:
            ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path.relative_to(ROOT)))
        phases.append({"name": "python_syntax", "status": "passed", "file_count": len(sources)})
    except SyntaxError as exc:
        phases.append({"name": "python_syntax", "status": "failed", "error": str(exc)})
    phases.append(run_phase("python_regressions", [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q"], environment))
    evaluation_path = destination.with_name(destination.stem + "-language.json")
    phases.append(run_phase("language_regressions", [sys.executable, "evals/run.py", "--output", str(evaluation_path)], environment))
    node = args.node or shutil.which("node")
    if node:
        js_tests = [str(path.relative_to(ROOT)) for path in sorted((ROOT / "tests").glob("test_*.cjs"))]
        phases.append(run_phase("browser_voice_state", [node, "--test", *js_tests], environment))
        for name in (path.name for path in sorted((ROOT / "robot_voice_patrol" / "web").glob("*.js"))):
            phases.append(run_phase("javascript_syntax_" + name, [node, "--check", "robot_voice_patrol/web/" + name], environment))
    else:
        phases.append({"name": "browser_voice_state", "status": "not_run", "reason": "Node.js unavailable"})
    result = {"version": "7.0.0", "recorded_at": datetime.now(timezone.utc).isoformat(),
              "python_version": sys.version.split()[0], "phases": phases,
              "all_available_checks_passed": all(p["status"] != "failed" for p in phases),
              "external_checks_not_covered": ["real ROS DDS/colcon/container", "actual model inference",
                                               "real microphone", "browser end-to-end", "physical hardware"]}
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for phase in phases:
        print(f"{phase['name']}: {phase['status']}")
    print(f"Report: {destination}")
    return 0 if result["all_available_checks_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
