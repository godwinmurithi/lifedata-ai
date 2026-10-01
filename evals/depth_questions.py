"""The NORTH STAR depth test — 5-10 multi-step tasks, scored on correctness,
depth of reasoning, and output quality, not just completion (per the plan's
own text). These have no fixed-string answer to check against; they need a
human or LLM-judge read of the full transcript (tool calls, reasoning, final
output), which `run_depth_eval.py` prints for that purpose rather than trying
to auto-score.

Grounded in what actually exists on this deployment (real staging data,
real registered datasets/charts) — not generic prompts that could apply to
any Superset instance.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class DepthTask:
    id: str
    prompt: str
    rubric: str


TASKS: list[DepthTask] = [
    DepthTask(
        "district_comparison_dashboard",
        "Why might Iganga district have so many more sickle cell screening "
        "records than other districts? Build a dashboard that shows the "
        "district breakdown, styled for a ministry-of-health audience.",
        rubric=(
            "Correctness: district counts match ground truth (Iganga 1148, "
            "Wakiso 330, Kampala 260, ...). Depth: does it distinguish "
            "'more screening activity happened there' from 'more disease "
            "there' (screening volume is not prevalence — the dataset's own "
            "traps note says this explicitly) rather than assuming the "
            "obvious wrong reading? Output quality: does the built dashboard "
            "actually exist as a saved Superset object, with a clean layout "
            "and a title/labels a non-technical ministry reader could follow "
            "(not raw column names)?"
        ),
    ),
    DepthTask(
        "referral_cascade_analysis",
        "Walk me through the sickle cell referral cascade — where are we "
        "losing people between being screened and actually getting referred "
        "for care? Is that drop-off concerning?",
        rubric=(
            "Correctness: cites the real funnel (2996 screened -> 620 "
            "carriers 20.7% -> 38 disease 1.3% -> 37 referred 1.2%). Depth: "
            "recognizes referred (37) sits just under disease-confirmed (38) "
            "-- correctly reasons about whether that's a healthy near-100% "
            "referral rate among disease cases or flags the 1-person gap, "
            "rather than treating 'referred' as a fraction of all screened "
            "(which would wrongly read as a crisis-level drop-off). Output "
            "quality: clear, correctly contextualized, not alarmist over a "
            "misread denominator."
        ),
    ),
    DepthTask(
        "anomaly_hunt",
        "Look at the sickle cell screening data and tell me if anything "
        "looks unusual or worth a second look.",
        rubric=(
            "Depth: a genuinely good answer notices something like the "
            "'Not Tested' bucket being tiny (2 of 2996) — is that a data-"
            "quality artifact or genuinely complete testing? Or the "
            "source split (outreach vs newborn) having very different "
            "volumes. A shallow answer just repeats the result_category "
            "counts with no scrutiny. Correctness: whatever it flags must "
            "be real (check against the data), not invented."
        ),
    ),
    DepthTask(
        "cross_dataset_reasoning",
        "Is there any facility-level data we could combine with the sickle "
        "cell screening results to see which health facilities need more "
        "screening support?",
        rubric=(
            "Depth: should recognize scd_screening_v2's testing_location "
            "isn't a foreign key into buikwe_level1_facilities (different "
            "districts covered, different grain) and either propose a real "
            "join path or honestly say the join doesn't exist cleanly, "
            "rather than confidently fabricating a combined view that "
            "doesn't actually work. This is the harder, more valuable "
            "answer than just 'yes I can join these.'"
        ),
    ),
    DepthTask(
        "new_visual_type",
        "Is there a way to visualize the referral funnel as an actual "
        "funnel chart rather than a table? If not, what would it take?",
        rubric=(
            "Depth: this deployment's viz_allowlist doctrine "
            "(validation/schemas/actions.py) requires building and looking "
            "at a chart type before adding it "
            "-- a good answer either finds a viz type already in the "
            "allowlist that renders this reasonably (e.g. a bar chart "
            "ordered by stage) or is honest that a true funnel type isn't "
            "available and explains why (allowlist doctrine, not just 'no'), "
            "rather than claiming to build something that doesn't exist "
            "in the allowed set."
        ),
    ),
]
