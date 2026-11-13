"""Current-attempt raw finding identities and fail-closed transition accounting.

Independent of the prior-round ledger and severity verdict mapping. Only the
plugin-owned structural recipe has a native inventory producer.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re

LANES = ("find_correctness", "find_removed_behavior", "find_crossfile", "find_conventions", "find_structural")
STAGES = ("synthesize", "verify", "report")


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def array(output, *, allow_malformed=False):
    """Strict final array: malformed/truncated candidates never fall back to an earlier draft."""
    text = str(output or "")
    # Select the final findings fence before inspecting its payload. A broken
    # final draft (including an empty fence or an object) must invalidate coverage.
    # Track other fenced blocks too, so their closing fences are not candidates.
    candidate = None
    in_fence = False
    for fence in re.finditer(r"(?m)^```([^`\r\n]*)\r?(?:\n|$)", text):
        language = fence[1].strip()
        if in_fence:
            if not language:
                in_fence = False
        else:
            in_fence = True
            # The accounting object written as a json fence AFTER the findings (#351) is not a
            # findings draft: skip a fence that holds a well-formed `{"dispositions": [...]}`.
            # Anything else — a truncated or malformed final draft — still becomes the candidate
            # and still invalidates the stage.
            if language in ("", "json") and _disposition_block(text, fence) is None:
                candidate = fence
    try:
        if candidate is not None:
            payload = text[candidate.end() :].lstrip()
            rows, end = json.JSONDecoder().raw_decode(payload)
            if not payload[end:].lstrip().startswith("```"):
                return None
        else:
            rows = json.loads(text) if text.strip().startswith("[") else None
    except (ValueError, TypeError):
        return None
    if not isinstance(rows, list) or (not allow_malformed and any(not isinstance(f, dict) for f in rows)):
        return None
    return rows


def inventory(steps, binding):
    """Preserve exact raw records; identical occurrences remain distinct within a lane."""
    result = {"supported": True, "binding": binding, "raw": {}, "lanes": {}, "gaps": [], "transitions": []}
    for lane in LANES:
        output = str(steps.get(lane) or "")
        rows = array(output, allow_malformed=True)
        result["lanes"][lane] = {
            "state": "missing-or-malformed" if rows is None else "observed",
            "count": len(rows) if rows is not None else None,
        }
        if rows is None:
            result["gaps"].append(f"{lane}: missing or malformed raw array")
            continue
        # Signals that the lane's raw ARRAY may be cut short or never produced. The machinery's
        # own `Gap:` lines are such signals: a faithfully relayed structural outage or partial
        # pass (protopatch `GAP_LINE_PREFIX` / `GAP_PARTIAL_PREFIX`, which a relay writes INSTEAD of
        # the PROTOPATCH header) and the workflow engine's step time-budget cut-off. A finder's
        # own `Gap:` note ("could not check X, left it out") is not: the array it wrote is whole,
        # and the note already reaches the report. Counting every `Gap:` line made 48% of
        # structural rounds "incomplete" on Vera (#351: 67 of 140 runs since 2026-11-11).
        if re.search(
            r"(?m)^.*hard-stopped at max_turns:|^FINDER_STATUS: blocked|^PROTOPATCH (?:UNAVAILABLE|PARTIAL)"
            r"|^protoPatch structural pass partial|^TRUNCATED"
            r"|^Gap: structural pass (?:unavailable|partial)|^Gap: step \S+ exceeded its \S+ time budget",
            output,
            re.I,
        ):
            result["gaps"].append(f"{lane}: incomplete raw input")
        status = re.search(r"FINDER_STATUS: reviewed n=(\d+)", output)
        if status and int(status[1]) != len(rows):
            result["gaps"].append(f"{lane}: truncated raw input (status count differs)")
        seen = {}
        for row in rows:
            if not isinstance(row, dict):
                result["gaps"].append(f"{lane}: malformed raw finding")
                continue
            if (not isinstance(row.get("claim"), str) or not row["claim"].strip()) or str(
                row.get("severity") or ""
            ) not in {"blocker", "major", "minor", "nit"}:
                result["gaps"].append(f"{lane}: malformed raw finding fields")
            digest = hashlib.sha256(_json([binding, lane, row]).encode()).hexdigest()[:24]
            seen[digest] = seen.get(digest, 0) + 1
            rid = f"{lane}:{digest}:{seen[digest]}"
            result["raw"][rid] = {"lane": lane, "finding": copy.deepcopy(row)}
    return result


def _disposition_block(text, fence):
    """`dispositions` of the object in `fence`, or None when it does not hold a well-formed one."""
    try:
        payload = text[fence.end() :].lstrip()
        data, end = json.JSONDecoder().raw_decode(payload)
        if not payload[end:].lstrip().startswith("```"):
            return None
        rows = data.get("dispositions") if isinstance(data, dict) else None
        return rows if isinstance(rows, list) else None
    except (ValueError, TypeError):
        return None


def dispositions(output):
    """The stage's dispositions list, or None when it emitted no well-formed accounting object.

    The recipe asks for "a separate fenced `accounting` JSON object", which models write both as
    an ```accounting fence and as an ordinary ```json fence holding `{"dispositions": [...]}`
    (every structural round on Vera after v0.71.0 did the latter, so each read as "missing"
    and properly disposed findings read as silently dropped — #351). Both are accepted; the
    LAST ```accounting fence wins, else the last json/bare fence whose payload is an OBJECT
    with a `dispositions` list. A findings array is a list, so it can never be taken for one."""
    text = str(output or "")
    tagged = list(re.finditer(r"(?m)^```accounting[ \t]*\n", text))
    if tagged:
        return _disposition_block(text, tagged[-1])
    for fence in reversed(list(re.finditer(r"(?m)^```(?:json)?[ \t]*\n", text))):
        rows = _disposition_block(text, fence)
        if rows is not None:
            return rows
    return None


def reconcile(accounting, steps, output, *, history=()):
    """Validate identities, not counts, at every boundary and retain transformation evidence."""
    from .rounds import normalize_relisted_priors, relisted_blocking_priors
    from .verdicts import verification_ran

    result = copy.deepcopy(accounting)
    expected = set(result["raw"])
    previous = {rid: value["finding"] for rid, value in result["raw"].items()}
    settled = {}
    for stage in STAGES:
        text = output if stage == "report" else steps.get(stage)
        rows, rejected = array(text), dispositions(text)
        if rows is None or rejected is None:
            result["gaps"].append(f"{stage}: missing or malformed findings/accounting array")
        covered = []
        targets = set()
        current_survivors = []
        # Only code-matched prior relistings belong to the separate prior ledger.
        # Current IDs and arbitrary foreign IDs must still pass native accounting.
        # These helpers select verdict-less rows for re-verification/normalization.
        # Accounting only needs their prior matching rules: a retained verifier verdict
        # does not turn a prior into a current-run observation. Use copies so the real
        # verdicts and the dispatcher's convergence decisions remain untouched.
        prior_candidates = [dict(row, verdict="") for row in rows or []]
        _, relistings = relisted_blocking_priors(prior_candidates, history)
        priors = dict(relistings)
        prior_round = next((r for r in reversed(history) if r.get("findings")), {})
        for index, row in enumerate(prior_candidates):
            if row.get("severity") in ("minor", "nit"):
                for prior in prior_round.get("findings", []):
                    _, normalized = normalize_relisted_priors([row], [{"findings": [prior]}])
                    if normalized:
                        priors[index] = prior
                        break
        for index, row in enumerate(rows or []):
            prior = priors.get(index)
            ids = row.get("raw_ids")
            if prior is not None and (
                ids is None
                or (
                    ids == prior.get("raw_ids")
                    and isinstance(ids, list)
                    and all(isinstance(i, str) and i not in expected for i in ids)
                )
            ):
                continue
            # Native identities belong to this attempt even if a model labels them
            # carried. Only the matched prior rows above are exempt from verification.
            current_survivors.append(dict(row, carried=False))
            if (not isinstance(row.get("claim"), str) or not row["claim"].strip()) or str(
                row.get("severity") or ""
            ) not in {"blocker", "major", "minor", "nit"}:
                result["gaps"].append(f"{stage}: malformed survivor fields")
            ids = row.get("raw_ids")
            if not isinstance(ids, list) or not ids or any(not isinstance(i, str) for i in ids):
                result["gaps"].append(f"{stage}: finding missing raw identities")
                continue
            # `finding_id` names the survivor; a merge that leaves it out is named by its first raw
            # id (deterministic, and always one of its own). A finding_id that names an id the row
            # does not carry, or one another survivor already took, is still a gap.
            target = row.get("finding_id", ids[0])
            if not isinstance(target, str) or target not in ids or target in targets:
                result["gaps"].append(f"{stage}: invalid or duplicate survivor identity")
            targets.add(target if isinstance(target, str) else "")
            covered.extend(ids)
            for rid in ids:
                before = previous.get(rid)
                if rid in settled:
                    result["gaps"].append(f"{stage}: resurrected disposition {rid}")
                if before is not None:
                    # Severity, file and line are the decision; they need a stated reason to move.
                    # Evidence text is re-checked verbatim against the code by grounding after the
                    # report, and every rewording is kept in `transitions` below, so an evidence-only
                    # edit stays traceable without one (#351: 1 of 4 live rounds was incomplete for
                    # nothing but a synthesizer re-phrasing a structural finding's evidence).
                    # BLANKING evidence is a move too: grounding fails open on a finding with no
                    # quotes, so a stage that empties it would leave nothing to check (#352 review).
                    def _text(value):
                        return str(value or "").strip()

                    blanked = bool(_text(before.get("evidence"))) and not _text(row.get("evidence"))
                    changed = blanked or any(before.get(k) != row.get(k) for k in ("severity", "file", "line"))
                    # Only `accounting_reason` explains a move, at every stage. The verifier's `note`
                    # does not: it is on every verified row, so accepting it would excuse any regrade
                    # (#352 review). The live case that needed it was an evidence-only edit, which no
                    # longer needs a reason at all.
                    if changed and not (_text(row.get("accounting_reason")) and _text(row.get("evidence"))):
                        result["gaps"].append(f"{stage}: unexplained transformation {rid}")
                    result["transitions"].append(
                        {"stage": stage, "raw_id": rid, "target": target, "before": before, "after": row}
                    )
                    previous[rid] = row
        # Check the verifier's entire surviving array before reporting can dispose of
        # a row. Final-report verification alone cannot detect that lost annotation.
        if stage == "verify" and not verification_ran(str(text or ""), current_survivors):
            result["gaps"].append("verify: incomplete survivor verification")
        for item in rejected or []:
            if not isinstance(item, dict):
                result["gaps"].append(f"{stage}: malformed disposition")
                continue
            ids = item.get("raw_ids")
            if not isinstance(ids, list) or not ids or any(not isinstance(i, str) for i in ids):
                result["gaps"].append(f"{stage}: disposition missing identities")
                continue
            covered.extend(ids)
            if str(item.get("disposition") or "") not in {"refuted", "unsupported", "out-of-scope"} or not (
                isinstance(item.get("reason"), str) and item["reason"].strip() and item.get("evidence")
            ):
                result["gaps"].append(f"{stage}: unreasoned disposition")
            for rid in ids:
                # A later stage must not FLIP a disposition (out-of-scope → refuted, or back into
                # the findings — the resurrection check above). Restating its reason in other
                # words is not a flip: every later stage re-emits the block, and models paraphrase
                # (#351: 21 "changed disposition" gaps on one round, every one the same kind).
                if rid in settled and settled[rid].get("disposition") != item.get("disposition"):
                    result["gaps"].append(f"{stage}: changed disposition {rid}")
                settled[rid] = item
                result["transitions"].append(
                    {"stage": stage, "raw_id": rid, "before": previous.get(rid), "after": item}
                )
        actual = set(covered)
        for kind, ids in (("missing", expected - actual), ("foreign or stale", actual - expected)):
            if ids:
                result["gaps"].append(f"{stage}: {kind} identities: {', '.join(sorted(ids))}")
        if len(covered) != len(actual):
            result["gaps"].append(f"{stage}: duplicate raw identities")
    result["complete"] = not result["gaps"]
    return result


def coverage(result, recipe, *, history=()):
    if recipe != "code-review-structural":
        return {"supported": False, "complete": None, "gaps": [], "status": "unsupported"}
    account = result.get("finding_accounting")
    if not (
        isinstance(account, dict)
        and account.get("supported") is True
        and isinstance(account.get("raw"), dict)
        and isinstance(account.get("gaps"), list)
        and isinstance(account.get("transitions"), list)
        and "binding" in account
        and all(
            isinstance(k, str) and isinstance(v, dict) and isinstance(v.get("finding"), dict)
            for k, v in account["raw"].items()
        )
    ):
        return {"supported": True, "complete": False, "gaps": ["native inventory unavailable or malformed"]}
    reconciled = reconcile(account, result.get("steps") or {}, result.get("output"), history=history)
    reconciled["report_findings"] = array(result.get("output"))
    return reconciled


def render(accounting, *, max_chars=65536):
    # Object fence cannot be mistaken for the prior-round findings array.
    rendered = (
        "\n\n<details><summary>Raw finding accounting: "
        + ("unsupported" if not accounting["supported"] else "complete" if accounting["complete"] else "incomplete")
        + "</summary>\n\n```json\n"
        + json.dumps(accounting, ensure_ascii=True).replace("<", "\\u003c").replace("`", "\\u0060")
        + "\n```\n</details>\n"
    )

    if len(rendered) <= max_chars:
        return rendered
    status = "unsupported" if not accounting["supported"] else "complete" if accounting["complete"] else "incomplete"
    summary = (
        f"\n\nRaw finding accounting: {status}. "
        f"Raw findings: {len(accounting.get('raw', {}))}; "
        f"gaps: {len(accounting.get('gaps', []))}. "
        "Full trace omitted from this review to fit GitHub's body limit."
    )
    return summary if len(summary) <= max_chars else ""


def restate_dispositions(output):
    rows = dispositions(output)
    return "\n```accounting\n" + json.dumps({"dispositions": rows}) + "\n```" if rows is not None else ""


def finalize(accounting, output, reported, *, stage="dispatcher"):
    """Audit dispatcher transformations without importing prior-round identities."""
    if not accounting["supported"] or "raw" not in accounting:
        return
    native = set(accounting["raw"])
    final = {}
    for row in reported:
        ids = row.get("raw_ids")
        if isinstance(ids, list):
            for rid in ids:
                if isinstance(rid, str) and rid in native:
                    if rid in final:
                        accounting["gaps"].append(f"final: duplicate identity {rid}")
                    final[rid] = row
    for before in (output if isinstance(output, list) else array(output)) or []:
        for rid in before.get("raw_ids", []) if isinstance(before.get("raw_ids"), list) else []:
            if not isinstance(rid, str) or rid not in native:
                continue
            after = final.get(rid)
            if after is None and rid not in accounting.get("dispatcher_dispositions", {}):
                accounting["gaps"].append(f"final: unexplained omission {rid}")
            if after is None:
                after = accounting.get("dispatcher_dispositions", {}).get(rid)
            accounting["transitions"].append(
                {"stage": stage, "raw_id": rid, "before": copy.deepcopy(before), "after": copy.deepcopy(after)}
            )
    accounting["complete"] = not accounting["gaps"]


def dispose(accounting, finding, disposition, reason, evidence):
    """Record an existing dispatcher guard's reasoned removal by exact raw identity."""
    if not accounting or not evidence:
        return
    ids = finding.get("raw_ids")
    for rid in ids if isinstance(ids, list) else []:
        if isinstance(rid, str) and rid in accounting.get("raw", {}):
            accounting.setdefault("dispatcher_dispositions", {})[rid] = {
                "disposition": disposition,
                "reason": reason,
                "evidence": evidence,
                "before": copy.deepcopy(finding),
            }
