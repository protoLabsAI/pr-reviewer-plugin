"""The structural-finder — the panel seat protoPatch findings enter through.

A deliberately thin relay: the intelligence is in the protoPatch engine (behind the
`protopatch_review` tool) and the judgement is downstream in the synthesizer/verifier.
This subagent exists because the workflow engine's only step type is a subagent step —
it calls the tool once and relays the findings verbatim, or reports the Gap. It must
NEVER fail the step: under ADR 0078 D3 the board gate treats any failed panel step as
not-a-review, and a starved structural pass must degrade the panel to four finders,
not void the review.

Host import (SubagentConfig) stays lazy — the host provides it at register time; the
test suite imports this module with no host present.
"""

from __future__ import annotations


def get_subagents() -> list:
    from graph.subagents.config import SubagentConfig

    structural_finder = SubagentConfig(
        name="structural-finder",
        description=(
            "The review panel's fifth, non-LLM finder: runs the protoPatch structural "
            "analysis engine over a PR via protopatch_review and relays its findings "
            "(source: protopatch) verbatim for the synthesizer. Degrades to a Gap + "
            "empty findings when the engine is unavailable."
        ),
        system_prompt="""You are protoAgent's structural-finder — the relay seat the
protoPatch engine occupies on the adversarial code-review panel. You do not review
code yourself; the engine does. Your entire job:

1. Call ``protopatch_review`` EXACTLY ONCE with the PR number (and repo, when the
   task names one).
2. If it returns a findings block: relay the fenced ```json array EXACTLY as given —
   same items, same fields (each carries `source: "protopatch"`), nothing added,
   edited, re-graded, or dropped. Before the fence, 1-3 lines of prose relaying the
   run header (head/base, elapsed, scope).
3. If it reports PROTOPATCH UNAVAILABLE: output the single Gap line it prescribes
   (`Gap: structural pass unavailable — <reason>`) and an empty fenced array:
   ```json
   []
   ```
   Do NOT call the tool again. Do NOT invent findings.

Judging the findings is the synthesizer's and verifier's job, not yours — a relay
that edits its payload corrupts the panel. A clean (empty) result is a good result.""",
        tools=["protopatch_review"],
        max_turns=4,
        allow_skill_emission=False,
    )
    return [structural_finder]
