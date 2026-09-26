#!/usr/bin/env python3
"""The NORTH STAR depth test — prints transcripts for human/LLM-judge review.

    uv run python evals/run_depth_eval.py

Not auto-scored (see depth_questions.py's module docstring for why: these
are judged on correctness + depth of reasoning + output quality, which a
substring match cannot capture). Makes real, billed Anthropic calls.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from depth_questions import TASKS  # noqa: E402
from run_evals import ask, login  # noqa: E402
import httpx  # noqa: E402


def main() -> int:
    client = httpx.Client(follow_redirects=True)
    print("Logging in...")
    login(client)

    for task in TASKS:
        print(f"\n{'=' * 70}")
        print(f"TASK: {task.id}")
        print(f"PROMPT: {task.prompt}")
        print(f"{'-' * 70}")
        print(f"RUBRIC: {task.rubric}")
        print(f"{'-' * 70}")
        answer = ask(client, task.prompt)
        print(f"RESPONSE:\n{answer}")

    print(f"\n{'=' * 70}")
    print(f"{len(TASKS)} depth tasks run. Judge each against its rubric above —")
    print("this script deliberately does not auto-score them.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
