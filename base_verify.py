"""The #300 pre-existing verifier — the one model call behind `scope: pre-existing`.

`dispatch.Dispatcher(base_verifier=…)` is a seam: an async
`(finding, base_sources) -> "yes" | "no" | "unsure"` handed the MERGE-BASE text of the
cited file(s) and the finding, asked whether the defect ALREADY exists there. Only `"yes"`
earns relief; the dispatcher's `_exists_at_base` treats everything else — and any raise — as
`"unsure"`, which keeps the finding gating (fail closed). The deterministic precondition
(`verdicts.base_reproduction_precondition`) has already proven the cited line is unchanged and
byte-identical at the base; this call makes the actual reproduces-at-base judgement.

`make_base_verifier` builds the PRODUCTION verifier `register()` wires. The host owns the model,
and the test suite runs host-free, so credentials resolve lazily (env first, then the host's own
`graph.sdk.config`, exactly as `protopatch._gateway_creds` does) and the gateway call is an
OpenAI-compatible `chat/completions` POST over the `httpx` the plugin already depends on. Missing
credentials, a transport error, a non-200, an unparseable body, or an answer that is not a clean
`yes`/`no` all read `"unsure"` — the verifier NEVER raises and NEVER guesses toward relief. The
model sees ONLY the base text (never the head), so the change under review cannot talk it into a
`"yes"`.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger("protoagent.plugins.pr_reviewer")

# The base text of one file is capped so a huge vendored file cannot blow the request budget; the
# cited line is always kept because the window is centred on it. Generous — a real source file is
# far smaller — and only ever TRIMS context, which can only move an answer away from "yes".
_MAX_LINES_PER_FILE = 600
_WINDOW = 200  # lines of base context kept on each side of the cited line when a file is over the cap

_SYSTEM = (
    "You are the merge-base verifier on protoAgent's PR-review panel. A finding cites a defect on a "
    "line this PR did NOT change, and that line is byte-identical at the pull request's merge-base. "
    "You are shown ONLY the merge-base (pre-PR) version of the cited file(s). Decide ONE thing: does "
    "the described defect ALREADY reproduce in this pre-PR code, independent of anything the PR adds? "
    "Answer 'yes' only if the defect is demonstrably present in the code shown. Answer 'no' if the "
    "defect depends on something not present here (so the PR introduced it). Answer 'unsure' if you "
    "cannot tell from the code shown. Reply with EXACTLY one word on the first line — yes, no, or "
    "unsure — then one short line of reasoning. When in doubt, answer 'unsure'."
)


def _trim(text: str, line: object) -> str:
    """`text` bounded to `_MAX_LINES_PER_FILE`, windowed around `line` when it must be cut so the
    cited line is never the part dropped."""
    lines = text.splitlines()
    if len(lines) <= _MAX_LINES_PER_FILE:
        return text
    if isinstance(line, int) and not isinstance(line, bool) and line > 0:
        lo = max(0, line - 1 - _WINDOW)
        hi = min(len(lines), line - 1 + _WINDOW)
        kept = lines[lo:hi]
        marker = f"# … merge-base lines {lo + 1}–{hi} of {len(lines)} (trimmed for length) …"
        return marker + "\n" + "\n".join(kept)
    return "\n".join(lines[:_MAX_LINES_PER_FILE])


def build_user_prompt(finding: dict, base_sources: dict[str, str]) -> str:
    """The user message: the finding, then each cited file AT THE MERGE-BASE. Pure — no I/O."""
    f = finding if isinstance(finding, dict) else {}
    head = [
        "Finding:",
        f"  file: {f.get('file') or '(none)'}",
        f"  line: {f.get('line')}",
        f"  severity: {f.get('severity') or '(none)'}",
        f"  claim: {f.get('claim') or '(none)'}",
    ]
    if f.get("evidence"):
        head.append(f"  evidence: {f.get('evidence')}")
    if f.get("note"):
        head.append(f"  note: {f.get('note')}")
    blocks = [
        f"\n--- merge-base {path} ---\n{_trim(str(text), f.get('line'))}"
        for path, text in (base_sources or {}).items()
        if isinstance(text, str) and text.strip()
    ]
    if not blocks:
        blocks = ["\n(no merge-base source available)"]
    return "\n".join(head) + "\n" + "".join(blocks)


def parse_answer(content: object) -> str:
    """The first word of the model's reply, normalised to `yes`/`no`/`unsure`. Anything else is
    `unsure` (fail closed)."""
    first = str(content or "").strip().lower().split()
    word = (first[0] if first else "").strip(".,:;!*`\"'")
    return word if word in ("yes", "no") else "unsure"


def resolve_endpoint(cfg: dict | None) -> tuple[str, str, str] | None:
    """`(api_key, chat_completions_url, model)` for the OpenAI-compatible gateway, or None when any
    piece is missing (⇒ the verifier is inert and fails closed). Env wins; the host's own model
    config (`graph.sdk.config`) is the fallback — the same resolution `protopatch._gateway_creds`
    uses, so a wizard-configured deployment that keeps its key in config is not starved."""
    cfg = cfg or {}
    key = os.environ.get("GATEWAY_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""
    base = str(cfg.get("gateway_base_url") or "") or os.environ.get("OPENAI_BASE_URL") or ""
    model = os.environ.get("PR_REVIEWER_BASE_VERIFIER_MODEL") or str(
        cfg.get("base_verifier_model") or cfg.get("model") or ""
    )
    if not key or not base or not model:
        try:
            from graph.sdk import config as host_config

            hc = host_config()
            key = key or str(getattr(hc, "api_key", "") or "")
            base = base or str(getattr(hc, "api_base", "") or "")
            model = model or str(getattr(hc, "model", "") or getattr(hc, "model_name", "") or "")
        except Exception:  # noqa: BLE001 — host-free (tests, standalone): no host config to borrow
            pass
    if not key or not base or not model:
        return None
    url = base if base.rstrip("/").endswith("/chat/completions") else base.rstrip("/") + "/chat/completions"
    return key, url, model


async def _httpx_post(url: str, headers: dict[str, str], payload: dict, timeout_s: float) -> str:
    """One OpenAI-compatible completion POST → the assistant's text. Raises on any failure; the
    caller turns every raise into `unsure`."""
    import httpx

    async with httpx.AsyncClient(timeout=timeout_s) as client:
        resp = await client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()
    return str(data["choices"][0]["message"]["content"] or "")


def make_base_verifier(cfg_provider=None, *, poster=None, endpoint=None, timeout_s: float = 30.0):
    """The production `base_verifier`. `cfg_provider()` yields the live config (for the gateway
    knobs); `poster`/`endpoint` are injection seams for tests. The returned coroutine answers
    `"yes"`/`"no"`/`"unsure"` and never raises."""
    post = poster or _httpx_post
    resolve = endpoint or resolve_endpoint

    async def verify(finding: dict, base_sources: dict[str, str]) -> str:
        try:
            cfg = (cfg_provider() if callable(cfg_provider) else cfg_provider) or {}
        except Exception:  # noqa: BLE001 — a config read must never fail the round
            cfg = {}
        resolved = resolve(cfg)
        if not resolved:
            return "unsure"  # no gateway wired ⇒ fail closed, nothing relieved
        key, url, model = resolved
        payload = {
            "model": model,
            "temperature": 0,
            "max_tokens": 200,
            "messages": [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": build_user_prompt(finding, base_sources)},
            ],
        }
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        try:
            content = await post(url, headers, payload, timeout_s)
        except Exception:  # noqa: BLE001 — a gateway/transport error must never fail the round
            log.exception("[pr-reviewer] #300 base verifier call failed")
            return "unsure"
        return parse_answer(content)

    return verify
