#!/usr/bin/env python3
"""Phase 6 eval harness. One command, prints a pass rate.

    uv run python evals/run_evals.py

Drives the REAL, running orchestrator over HTTP (`POST /lifedata-ai/api/chat/`),
the same endpoint the panel calls — not the agent loop directly and not a
mock. Makes real, billed Anthropic calls. Needs the orchestrator and staging
Superset actually running (this box, or set LIFEDATA_EVAL_BASE_URL).

Each question in `evals/questions.py` passes if every string in its `checks`
list appears (case-insensitive) in the assistant's final answer text.
`boundary`-category questions have no fixed checks — they're printed with
their notes for a human/LLM-judge read, and don't count toward the pass rate.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from questions import BOUNDARY_NOTES, QUESTIONS, EvalQuestion  # noqa: E402

SUPERSET_BASE_URL = os.environ.get(
    "LIFEDATA_EVAL_SUPERSET_URL", "http://localhost:8080"
)
ORCHESTRATOR_BASE_URL = os.environ.get(
    "LIFEDATA_EVAL_BASE_URL", "http://localhost:8000"
)
EVAL_USERNAME = os.environ.get("LIFEDATA_EVAL_USERNAME", "eval_runner")
EVAL_PASSWORD = os.environ.get("LIFEDATA_EVAL_PASSWORD", "Eval-Runner-2026!")
ORIGIN = os.environ.get("LIFEDATA_EVAL_ORIGIN", "https://sandbox.lifehealth.app")


def login(client: httpx.Client) -> None:
    """Log into Superset, leaving the session cookie on `client`."""
    login_page = client.get(f"{SUPERSET_BASE_URL}/login/")
    # Attribute order in the rendered <input> isn't guaranteed, so match on
    # "csrf_token" appearing anywhere before the value, not name= first --
    # the stricter pattern silently matched nothing and posted csrf_token=None,
    # which still got a 302 (redirect loop back to /login/) instead of a
    # clear failure.
    match = re.search(r'csrf_token[^>]*value="([^"]+)"', login_page.text)
    csrf_token = match.group(1) if match else None
    resp = client.post(
        f"{SUPERSET_BASE_URL}/login/",
        data={
            "username": EVAL_USERNAME,
            "password": EVAL_PASSWORD,
            "csrf_token": csrf_token,
        },
    )
    if "session" not in client.cookies:
        raise RuntimeError(
            f"Login did not produce a Superset session cookie (status {resp.status_code}). "
            "Check LIFEDATA_EVAL_USERNAME/LIFEDATA_EVAL_PASSWORD."
        )


def ask(client: httpx.Client, prompt: str) -> str:
    """Send one chat turn, return the concatenated assistant text.

    Cookies are domain-scoped in httpx's jar (RFC 6265) -- the session cookie
    login() receives from SUPERSET_BASE_URL's host never auto-attaches to a
    request against ORCHESTRATOR_BASE_URL's (different) host, even on the
    same client, when the two point at different container names. Forwarded
    explicitly instead of relying on the jar.

    No conversation_id is sent: resolve_conversation() only accepts an id
    that already names an existing row it owns (404s otherwise) and creates
    a fresh one when the field is omitted -- pre-generating a uuid client-side
    always 404'd.
    """
    session_cookie = client.cookies.get("session")
    with client.stream(
        "POST",
        f"{ORCHESTRATOR_BASE_URL}/lifedata-ai/api/chat/",
        json={
            "message": prompt,
            "page_context": {"path": "/", "dashboard_id": None, "chart_id": None},
        },
        headers={
            "Origin": ORIGIN,
            "Content-Type": "application/json",
            "Cookie": f"session={session_cookie}",
        },
        timeout=120,
    ) as resp:
        if resp.status_code != 200:
            resp.read()
            raise RuntimeError(f"chat POST failed: {resp.status_code} {resp.text[:300]}")
        text_parts: list[str] = []
        current_event = None
        for line in resp.iter_lines():
            if line.startswith("event: "):
                current_event = line[len("event: ") :].strip()
            elif line.startswith("data: "):
                data = json.loads(line[len("data: ") :])
                if current_event == "text_delta":
                    text_parts.append(data.get("text", ""))
                elif current_event == "error":
                    text_parts.append(f"[ERROR: {data.get('message', data)}]")
        return "".join(text_parts)


def _normalize(text: str) -> str:
    """Collapse phrasing variants so one canonical check string matches any of them.

    Strips thousands-separator commas ('2,996' -> '2996') and folds percent
    phrasing to a bare '%' ('1.2 percent' / '1.2 %' -> '1.2%'). Without this,
    a question's checks=["2996", "2,996"] (meant as "either format is fine")
    silently required BOTH via all() -- a real bug caught only by looking at
    a genuinely correct answer ("2,996 screening records") still failing.
    """
    normalized = re.sub(r"(?<=\d),(?=\d)", "", text.lower())
    normalized = re.sub(r"\s*percent\b", "%", normalized)
    normalized = re.sub(r"(?<=\d)\s+%", "%", normalized)
    return normalized


def score(question: EvalQuestion, answer: str) -> bool | None:
    if question.category == "boundary":
        return None
    if not question.checks:
        return None
    lowered = _normalize(answer)
    if question.category == "regex":
        return all(re.search(check, lowered) is not None for check in question.checks)
    # Every listed check must appear (e.g. a gender-split question genuinely
    # needs both counts present) -- comma-normalization above is what makes
    # "2996" and "2,996" equivalent, not treating checks as alternatives.
    return all(_normalize(check) in lowered for check in question.checks)


def main() -> int:
    client = httpx.Client(follow_redirects=True)
    print(f"Logging in as {EVAL_USERNAME} against {SUPERSET_BASE_URL} ...")
    login(client)
    print(f"Running {len(QUESTIONS)} questions against {ORCHESTRATOR_BASE_URL} ...\n")

    results: list[tuple[EvalQuestion, str, bool | None]] = []
    for q in QUESTIONS:
        start = time.monotonic()
        try:
            answer = ask(client, q.prompt)
        except Exception as exc:  # noqa: BLE001
            answer = f"[EXCEPTION: {exc}]"
        elapsed = time.monotonic() - start
        passed = score(q, answer)
        results.append((q, answer, passed))
        status = "SKIP" if passed is None else ("PASS" if passed else "FAIL")
        print(f"[{status}] {q.id} ({elapsed:.1f}s)")
        if passed is False:
            print(f"       prompt:  {q.prompt}")
            print(f"       wanted:  {q.checks}")
            print(f"       got:     {answer[:300]!r}")
        if passed is None:
            print(f"       prompt:  {q.prompt}")
            print(f"       note:    {BOUNDARY_NOTES.get(q.id, '(manual judgement)')}")
            print(f"       answer:  {answer[:400]!r}")

    scored = [p for _, _, p in results if p is not None]
    n_pass = sum(1 for p in scored if p)
    n_total = len(scored)
    n_skip = len(results) - n_total
    rate = (n_pass / n_total * 100) if n_total else 0.0

    print(f"\n{'=' * 60}")
    print(f"PASS RATE: {n_pass}/{n_total} ({rate:.0f}%)  [{n_skip} boundary question(s) not auto-scored]")
    print(f"{'=' * 60}")

    return 0 if n_pass == n_total else 1


if __name__ == "__main__":
    raise SystemExit(main())
