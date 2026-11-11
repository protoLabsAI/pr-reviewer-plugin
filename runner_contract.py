"""Host-free consumer of protoAgent #5091 construction evidence (schema version 1).

Construction is not semantic coverage or proof of external policy. Session IDs and
model-authored claims are deliberately not evidence. No prompt text is published.
"""

from __future__ import annotations


def _map(value):
    return value if isinstance(value, dict) else {}


def _id(value):
    return isinstance(value, str) and bool(value.strip())


def _text(value):
    value = _map(value)
    text = value.get("text")
    count, omitted = value.get("characters"), value.get("omitted_characters")
    return (
        isinstance(text, str)
        and type(count) is int
        and type(omitted) is int
        and omitted >= 0
        and count == len(text) + omitted
    )


def _strings(value):
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def validate_step(envelope, receipt, *, run_id, step_id, execution_id):
    """Return a Gap reason, or None for matching native construction observations."""
    envelope, receipt = _map(envelope), _map(receipt)
    if not all(_id(v) for v in (run_id, step_id, execution_id)):
        return "missing invocation identity"
    expected = dict(run_id=run_id, step_id=step_id, execution_id=execution_id)
    if any(envelope.get(k) != v or receipt.get(k) != v for k, v in expected.items()):
        return "foreign or missing run/step/execution identity"
    if type(envelope.get("schema_version")) is not int or envelope["schema_version"] != 1:
        return "unsupported context schema"
    if envelope.get("status") != "observed":
        return "construction unknown or not executed"
    if receipt.get("capture_status") not in ("captured", "partial", "unsupported", "not_executed"):
        return "missing or malformed receipt dispatch status"
    if receipt.get("capture_status") == "not_executed":
        return "construction contradicts receipt dispatch"
    if envelope.get("workflow_state") not in ("fresh", "resumed"):
        return "observed construction has invalid workflow state"
    if envelope.get("conversation") != "fresh" or envelope.get("parent_history") != "not_supplied":
        return "parent-conversation exclusion unverified"
    inputs = _map(envelope.get("inputs"))
    if inputs.get("history") != "not_supplied" or not _text(inputs.get("prompt")):
        return "prompt/history observation missing or malformed"
    instructions = _map(envelope.get("instruction_sources"))
    if (
        instructions.get("status") != "observed"
        or not _id(instructions.get("subagent"))
        or not _text(instructions.get("system_prompt"))
        or instructions.get("external_sources") != "unknown"
        or instructions.get("runtime_additions") != "unknown"
    ):
        return "instruction-source observation missing or malformed (external sources remain unknown)"
    restrictions = _map(envelope.get("tool_restrictions"))
    enforcement = _map(restrictions.get("enforcement"))
    if (
        restrictions.get("status") != "observed"
        or not _strings(restrictions.get("bound_tools"))
        or not _strings(restrictions.get("inherited_fence"))
        or type(enforcement.get("enabled")) is not bool
        or not _strings(enforcement.get("disallowed_tools"))
        or not isinstance(enforcement.get("rate_limits"), dict)
        or restrictions.get("external_policy") != "unknown"
    ):
        return "effective tool restrictions missing or malformed (external policy remains unknown)"
    return None


def validate_result(result, *, seed_outputs=None, seed_source=None):
    """Validate outputs against the returned invocation and independent receipt identities.

    A seed reference alone is never a new observation. Only a retained matching
    original envelope and receipt can establish its original construction.
    """
    result = _map(result)
    context, receipts = _map(result.get("review_context")), _map(result.get("tool_receipts"))
    steps = _map(result.get("steps"))
    if any(not isinstance(sid, str) for sid in [*steps, *_map(seed_outputs)]):
        return {"execution": "Gap: isolation — malformed step map"}
    required = set(steps) | set(_map(seed_outputs)) | {"verify", "report"}
    gaps = {}
    for sid in sorted(required):
        envelope = _map(_map(context.get("steps")).get(sid))
        receipt = _map(_map(receipts.get("steps")).get(sid))
        execution = receipts.get("execution_id")
        why = None
        if (
            type(context.get("schema_version")) is not int
            or context.get("schema_version") != 1
            or type(receipts.get("schema_version")) is not int
            or receipts.get("schema_version") != 1
            or context.get("execution_id") != execution
        ):
            why = "missing or malformed review_context segment"
        elif envelope.get("status") == "not_executed" and envelope.get("workflow_state") == "seeded":
            expected = dict(run_id=result.get("run_id"), step_id=sid, execution_id=execution)
            origin = _map(envelope.get("origin"))
            if (
                not all(_id(v) for v in expected.values())
                or any(envelope.get(k) != v or receipt.get(k) != v for k, v in expected.items())
                or receipt.get("capture_status") != "not_executed"
                or origin.get("step_id") != sid
                or not all(_id(origin.get(k)) for k in expected)
                or origin == expected
                or envelope.get("schema_version") != 1
                or envelope.get("conversation") != "unknown"
                or envelope.get("parent_history") != "unknown"
                or any(envelope.get(k) != "unknown" for k in ("inputs", "instruction_sources", "tool_restrictions"))
            ):
                why = "seed origin unknown or contradictory"
            else:
                candidates = []
                if (
                    isinstance(seed_source, dict)
                    and sid in _map(seed_source.get("steps"))
                    and sid not in _map(seed_source.get("context_adoptions"))
                    and _map(seed_source.get("steps")).get(sid) == _map(seed_outputs).get(sid, steps.get(sid))
                ):
                    candidates.append(
                        (seed_source.get("review_context"), seed_source.get("tool_receipts"), seed_source.get("run_id"))
                    )
                histories = context.get("previous_executions", [])
                receipt_histories = receipts.get("previous_executions", [])
                if isinstance(histories, list) and isinstance(receipt_histories, list):
                    candidates.extend((c, r, result.get("run_id")) for c in histories for r in receipt_histories)
                why = "seed origin has no matching retained construction evidence"
                for c, r, rid in candidates:
                    c, r = _map(c), _map(r)
                    if rid != origin.get("run_id") or r.get("execution_id") != origin.get("execution_id"):
                        continue
                    if (
                        type(c.get("schema_version")) is not int
                        or c.get("schema_version") != 1
                        or type(r.get("schema_version")) is not int
                        or r.get("schema_version") != 1
                        or c.get("execution_id") != r.get("execution_id")
                    ):
                        continue
                    original = _map(c.get("steps")).get(sid)
                    original_receipt = _map(r.get("steps")).get(sid)
                    why = validate_step(original, original_receipt, **{k: origin[k] for k in expected})
                    if why is None:
                        break
        else:
            why = validate_step(envelope, receipt, run_id=result.get("run_id"), step_id=sid, execution_id=execution)
            if seed_outputs and sid in seed_outputs:
                why = "supplied seed cannot claim new construction"
        if why:
            gaps[sid] = "Gap: isolation — " + why
    return gaps


def context_note(gaps):
    """Code-authored scope of the guarantee; never echo redacted input text."""
    status = "unverified" if gaps else "observed"
    return (
        f"Runner context: {status}. Native parent-conversation exclusion and separate history exclusion "
        "are checked per execution. Intended prior findings may remain in the rendered prompt. "
        "Instruction-source observations describe the assembled system prompt; mandatory repository guidance "
        "remains applicable. External instruction sources and runtime additions remain unknown. "
        "Tool observations describe bound tools, inherited fence and enforcement; external policy remains unknown. "
        "Construction evidence does not establish semantic review coverage."
    )
