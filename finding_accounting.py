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
    candidates = list(re.finditer(r"(?m)^```(?:json)?[ \t]*\n(?=\s*\[)", text))
    try:
        if candidates:
            payload = text[candidates[-1].end() :].lstrip()
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
        if re.search(
            r"(?m)^.*hard-stopped at max_turns:|^FINDER_STATUS: blocked|^PROTOPATCH (?:UNAVAILABLE|PARTIAL)|^Gap: |^protoPatch structural pass partial|^TRUNCATED",
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


def dispositions(output):
    text = str(output or "")
    blocks = list(re.finditer(r"(?m)^```accounting[ \t]*\n", text))
    try:
        if not blocks:
            return None
        payload = text[blocks[-1].end() :].lstrip()
        data, end = json.JSONDecoder().raw_decode(payload)
        if not payload[end:].lstrip().startswith("```"):
            return None
        rows = data.get("dispositions") if isinstance(data, dict) else None
        return rows if isinstance(rows, list) else None
    except (ValueError, TypeError):
        return None


def reconcile(accounting, steps, output, *, history=()):
    """Validate identities, not counts, at every boundary and retain transformation evidence."""
    from .rounds import normalize_relisted_priors, relisted_blocking_priors

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
        # Only code-matched prior relistings belong to the separate prior ledger.
        # Current IDs and arbitrary foreign IDs must still pass native accounting.
        _, relistings = relisted_blocking_priors(rows or [], history)
        priors = dict(relistings)
        prior_round = next((r for r in reversed(history) if r.get("findings")), {})
        for index, row in enumerate(rows or []):
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
            if (not isinstance(row.get("claim"), str) or not row["claim"].strip()) or str(
                row.get("severity") or ""
            ) not in {"blocker", "major", "minor", "nit"}:
                result["gaps"].append(f"{stage}: malformed survivor fields")
            ids = row.get("raw_ids")
            if not isinstance(ids, list) or not ids or any(not isinstance(i, str) for i in ids):
                result["gaps"].append(f"{stage}: finding missing raw identities")
                continue
            target = row.get("finding_id")
            if not isinstance(target, str) or target not in ids or target in targets:
                result["gaps"].append(f"{stage}: invalid or duplicate survivor identity")
            targets.add(target if isinstance(target, str) else "")
            covered.extend(ids)
            for rid in ids:
                before = previous.get(rid)
                if rid in settled:
                    result["gaps"].append(f"{stage}: resurrected disposition {rid}")
                if before is not None:
                    changed = any(before.get(k) != row.get(k) for k in ("severity", "file", "line", "evidence"))
                    if changed and not (
                        isinstance(row.get("accounting_reason"), str)
                        and row["accounting_reason"].strip()
                        and row.get("evidence")
                    ):
                        result["gaps"].append(f"{stage}: unexplained transformation {rid}")
                    result["transitions"].append(
                        {"stage": stage, "raw_id": rid, "target": target, "before": before, "after": row}
                    )
                    previous[rid] = row
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
                if rid in settled and settled[rid] != item:
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
