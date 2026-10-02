# Public companion movement check

The Render review companion has no patient sign-in integration. It uses the
separate `/api/pose/review-runner`, `/api/assessment/review/tasks` and
`/api/assessment/review/results` endpoints. These routes never access accounts,
recordings, credits or database history. They return browser-local preview reports.
The existing authenticated assessment routes and loopback preview guards remain.

`companion_review_runner.html` is the assembled camera runner exported from the
local companion's `assessment-service/backend/server.py` on 2 October 2026. Its
SHA-256 before the guest transport substitutions is
`a581e2aa0d835579efd59d4abd3ca7a24b916f7064413dcde06a353ce7415e33`.
`companion_review_catalog.json` contains the same core task definitions and rubrics.
`companion_review_quality.py` snapshots the matching local task-quality scorer,
including the existing combined face/shoulder evidence rule for trunk leaning.
This preserves the local calibration, target ladders, gesture checks, affected
side, speech gates and completion presentation without changing the separately
deployed patient app's runner.
The completion script and markup were refreshed from the local companion on
2 October 2026 to remove the technical Details disclosure. Scores, coaching,
Done, transport and camera behavior remain the same.

The guest speech transport now accepts only audio identified as ElevenLabs
Alira and retains caption/replay UI on a failure. It cannot switch to a device
voice. `companion_review_voice_lines.json` inventories its fixed prompts and
every ordered guest task transition; `bake_alira_voice` includes them in the
shipped Alira pack. Unrecorded corrections use the server's serialized,
retrying ElevenLabs queue. Credentials are never part of either catalog.

Regenerate both snapshots together when the local camera runner changes. Export only the assembled static template and task metadata;
never environment files or runtime records.

The guest adapter sets preview mode, clears account context, selects the guest
transport routes, and passes only a bounded goal/helper choice with the result.
It disables account recording uploads and server progress saving via the existing
preview guards, and disables the separate loopback debug recorder. The completion
postMessage carries the scored report back to the companion.
The existing pure function scorer and exercise selector are reused. Sitting support
is not inferred: exercise candidates retain the local preview's review gate.

Verification covers anonymous task JSON, bounded evidence scoring, the result
postMessage, refusal of account credentials and unsupported task options, unchanged
authenticated-route access, and JavaScript syntax of the served runner. Live camera
performance requires a visitor to grant their own camera permission.
