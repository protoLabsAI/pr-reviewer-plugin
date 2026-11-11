"""#5091-shaped construction fixtures, not native host probe evidence."""

from copy import deepcopy
from functools import wraps
from uuid import uuid4


def text_record(text):
    return {"text": text, "characters": len(text), "omitted_characters": 0}


def observed_result(result, *, workflow_state="fresh"):
    result = deepcopy(result)
    rid, eid = str(uuid4()), str(uuid4())
    contexts, receipts = {}, {}
    for sid in set(result.get("steps", {})) | {"verify", "report"}:
        identity = dict(run_id=rid, step_id=sid, execution_id=eid)
        contexts[sid] = {
            **identity,
            "schema_version": 1,
            "status": "observed",
            "workflow_state": workflow_state,
            "origin": "unknown",
            "conversation": "fresh",
            "parent_history": "not_supplied",
            "inputs": {"prompt": text_record("Intended prior findings"), "history": "not_supplied"},
            "instruction_sources": {
                "status": "observed",
                "subagent": "reviewer",
                "system_prompt": text_record("Follow repository policies"),
                "external_sources": "unknown",
                "runtime_additions": "unknown",
            },
            "tool_restrictions": {
                "status": "observed",
                "bound_tools": ["read_file"],
                "inherited_fence": [],
                "enforcement": {"enabled": False, "disallowed_tools": [], "rate_limits": {}},
                "external_policy": "unknown",
            },
        }
        receipts[sid] = {**identity, "capture_status": "captured", "capture_supported": True, "entries": []}
    result.update(
        run_id=rid,
        review_context={"schema_version": 1, "execution_id": eid, "steps": contexts},
        tool_receipts={"schema_version": 1, "source": "native_audit", "execution_id": eid, "steps": receipts},
    )
    return result


def native_runner(runner):
    """Give existing policy tests native construction; legacy tests opt out explicitly."""

    @wraps(runner)
    async def run(*args, **kwargs):
        result = await runner(*args, **kwargs)
        if "review_context" in result:
            return result
        output_steps = result.get("steps", {})
        result = observed_result({**result, "steps": {**(kwargs.get("seed_outputs") or {}), **output_steps}})
        result["steps"] = output_steps
        # Simulate persisted, explicitly linked originals for fixture seed inputs.
        seeds = kwargs.get("seed_outputs") or {}
        if seeds:
            source = observed_result({"steps": seeds})
            for sid in seeds:
                if sid not in result["review_context"]["steps"]:
                    continue
                old = source["review_context"]["steps"][sid]
                env = result["review_context"]["steps"][sid]
                env.update(
                    status="not_executed",
                    conversation="unknown",
                    parent_history="unknown",
                    inputs="unknown",
                    instruction_sources="unknown",
                    tool_restrictions="unknown",
                    workflow_state="seeded",
                    origin={k: old[k] for k in ("run_id", "step_id", "execution_id")},
                )
                # Resume history must belong to the same run, as in the producer.
                old["run_id"] = result["run_id"]
                env["origin"]["run_id"] = result["run_id"]
                source["tool_receipts"]["steps"][sid]["run_id"] = result["run_id"]
                result["tool_receipts"]["steps"][sid]["capture_status"] = "not_executed"
            result["review_context"]["previous_executions"] = [source["review_context"]]
            result["tool_receipts"]["previous_executions"] = [source["tool_receipts"]]
        return result

    return run
