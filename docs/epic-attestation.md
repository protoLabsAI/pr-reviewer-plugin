# Epic attestation: an epic is not reviewed twice

Large features land on long-lived `epic/*` branches. Each slice PR (base `epic/<name>`) gets the
full panel and is squash-merged into the epic. The epic → default-branch PR then carries the
**whole** epic diff, every line already reviewed slice by slice.

For a PR whose head is this repo's `epic/*` and whose base is the default branch, every commit on
the epic's first-parent chain (`base..head`, read from a real checkout) is attributed:

| Attribution | When | Reviewed again? |
|---|---|---|
| **slice** | The merge commit of a merged PR into this epic (`GET /commits/{sha}/pulls`, `merge_commit_sha` = the commit) whose strictest panel round at its final head is a **complete, verified PASS**, read from our own verdict markers exactly as the `QA panel` gate reads them. A WARN, an incomplete pass (the check's ⚪ neutral), an unverified pass or no verdict does not attest. A slice merged with a merge commit must also have merged its reviewed head cleanly | no |
| **sync** | A merge whose second parent is on the base branch and whose tree is exactly `git merge-tree --write-tree` of its parents; it adds nothing of its own | no |
| **residual** | Everything else: direct pushes, conflict-resolution merges, merges of any other branch, unattested slices | **yes** |

## Outcomes

- **Every commit attested.** A PASS is posted at the head through the normal verdict path (a real
  marker, so `Review at head` and the `QA panel` gate see a verdict). Its body is the attestation
  table: commit → slice PR → that PR's verdict. **No panel runs.** A standing FAIL block from an
  earlier round on the epic PR is not dismissed by it.
- **Some residual.** The structural panel runs with a `<review_scope>` block: the attested commits
  (out of scope) and the residual commits' combined diff (a conflict merge contributes only its
  resolution, the diff from git's automatic merge to what was committed). In-diff confinement holds
  findings to the residual files, and the body lists what was attested and what was reviewed.
  The structural pass is scoped the same way, so attested slices never fill its feature cap. The
  scope is handed over server-side, keyed by the head SHA (the tool the model calls carries only
  `pr` / `repo`).
- **Fails closed.** A failed `gh` read, an unreadable slice history, a failed checkout or git
  command, or more than **300** commits means the normal full review. An `@vera review` summon
  **always** reviews the whole PR.

`epic_attestation: false` reviews the whole epic diff.
