# Install

## Requirements

| Need | Why |
|---|---|
| protoAgent ≥ 0.170.0 | The bundled recipe takes its finder budget from an input (protoAgent#3555), and 0.170.0 carries the completion guard (protoAgent#3553) the structural relay relies on. The manifest pins this and refuses to load on an older core. |
| The `github` plugin enabled | Finders and verifiers read PRs, files and threads through it. |
| `git` and `gh` | Clones for the structural pass, PR reads. `gh` must be authenticated, or `GITHUB_TOKEN` / `GH_TOKEN` set. With App credentials configured the plugin keeps `GH_TOKEN` fresh itself. |
| `clawpatch` | The [protoPatch](https://github.com/protoLabsAI/protoPatch) CLI: `npm i -g @protolabsai/protopatch`. Missing it degrades the structural lane to a gap, never an error. See [structural](structural.md). |
| Gateway credentials in the host env | `GATEWAY_API_KEY` or `OPENAI_API_KEY`, plus `OPENAI_BASE_URL` (or `pr_reviewer.gateway_base_url`) for a non-default gateway. clawpatch reads these. |
| A GitHub App (recommended) | Reviews post as the App's bot identity and checks need App auth. A PAT or machine user works for a trial but cannot write check runs. |

## Install the plugin

```bash
python -m server plugin install https://github.com/protoLabsAI/pr-reviewer-plugin --ref main
```

The plugin ships **disabled** (`enabled: false` in the manifest): enabling it is your trust
decision. Turn it on in **Settings → Plugins → PR reviewer**; it hot-mounts, no restart.
Or in config:

```yaml
plugins:
  enabled: [github, pr-reviewer]
```

## Create the GitHub App

1. **Permissions** (repository):
   - Pull requests: **read & write** (post reviews, approve, dismiss)
   - Contents: **read** (clone, read blobs)
   - Checks: **read & write** (publish `QA panel` / `protoReview`; without it those writes log a warning naming the permission and the review still posts)
   - Issues: **read & write** (PR conversation comments: command replies, pause/resume markers)
   - Metadata: **read** (implicit)
2. **Subscribe to events.** This is the step that is easiest to miss:

   | Event | Needed for |
   |---|---|
   | Pull request | Every review trigger (`opened`, `synchronize`, `reopened`, `ready_for_review`) |
   | Issue comment | `@vera` commands in the PR conversation |
   | Pull request review comment | Required by `GET /summon/health`. The webhook does not yet act on inline-comment deliveries, so commands must go in the PR conversation |
   | Pull request review thread | Resolving a thread re-publishes the check ([checks](checks.md#clearing-by-resolving-threads)) |
   | Check run | **Re-run** on `protoReview`, and CI completing refreshing `QA panel` |

   The sweep is the backstop for anything a webhook misses.

   An App subscribed only to `pull_request` makes every command vanish with no error: the
   code is right and the event simply never arrives. Use [`GET /summon/health`](api.md#get-summonhealth)
   to see exactly which are missing.
3. **Webhook.** URL: `https://<your-agent>/plugins/pr-reviewer/webhook`. Content type
   `application/json`. Set a secret.
4. **Private key.** Generate one and keep the PEM.
5. **Install** the App on the repos you want reviewed.

## Configure

Secrets go through Settings (or env on headless deployments); never the main YAML.

| What | Setting | Env fallback |
|---|---|---|
| Webhook secret | `webhook_secret` | `PR_REVIEWER_WEBHOOK_SECRET` |
| App ID | `app_id` | `PROTOREVIEW_APP_ID` |
| App private key (PEM) | `app_private_key` | `PROTOREVIEW_APP_PRIVATE_KEY` |
| App installation ID (optional, auto-discovered) | `installation_id` | `PROTOREVIEW_APP_INSTALLATION_ID` |
| Our own login | `viewer_login` | `PR_REVIEWER_VIEWER_LOGIN` |

**`webhook_secret` is fail-closed:** with none configured every delivery returns 403.

**`viewer_login` is required on App auth.** An installation token is not a user, so
`GET /user` returns 403 and the plugin cannot discover its own identity. Set it to the App's
bot login (for example `my-reviewer[bot]`). It is how the reviewer refuses to review its
own PRs. With a PAT it is auto-detected.

`repos` narrows scope: empty (and `PR_REVIEWER_REPOS` empty) means **every repo the App
installation covers**, so installing or uninstalling the App is then the gate.

Every key is documented in [configuration](configuration.md).

## Start in shadow mode

`shadow_mode: true` is the default. Verdicts post as plain `COMMENT` reviews and never
block. Leave it on while you watch the [eval report](operations.md#the-eval-report) and
compare verdicts to what humans decided. To go live:

1. `shadow_mode: false`: a `FAIL` now posts `REQUEST_CHANGES`.
2. `promotion_owner: true`: this agent owns approve-on-green and writes the `QA panel` check.
   Exactly one reviewer per repo should own promotion; two promoters racing double-merge.
3. Add `QA panel` and/or `protoReview` to the branch's required checks ([checks](checks.md)).

## Smoke test

1. Open any PR on a watched repo and comment `@vera help`. It needs no admin and spends no
   panel; you should get the verb list back.
2. `GET /api/plugins/pr-reviewer/summon/health` should report `summon_reachable: true`.
3. Open a non-draft PR. Within a minute a `protoReview` check appears in progress, and a
   review posts when the panel finishes (5 to 9 minutes for a structural-sized diff).
4. `POST /api/plugins/pr-reviewer/dispatch` with `{"repo": "owner/name", "pr": 12}` runs the
   same path by hand ([api](api.md)).
