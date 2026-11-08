# Contribution instructions

Campaign Studio is a local Python application with the Motif browser interface. Keep module responsibilities clear and changes focused on the supported workflow.

- Use FastAPI, Jinja2, plain JavaScript, Pillow, SQLite, and the official OpenAI SDK. Do not add a frontend build, ORM, job framework, or speculative provider abstraction.
- Preserve one version-checked current editing document in SQLite, autosave in Campaign and Studio, verified source reuse, deliberate request accounting, and offline export integrity. Ordinary edits save instructions; finished PNGs are rendered only for a ZIP or individual PNG download. Temporary visual previews are allowed. Local corrections and exports must not trigger paid requests.
- Capture one saved composition for each export, verify its source bytes, render and check its finished PNGs in temporary storage, and keep at most one temporary export job. Continue after an individual local render failure, then retry failed images once automatically; explicit retry reuses verified successes. Close temporary files after delivery, cancellation, stale-version rejection, or bounded idle expiry. Do not add permanent Outputs/Exports directories, working JSON mirrors, edit-history rows, or internal ZIP archives. Preserve earlier registered deliveries during upgrade.
- Keep credentials, runtime campaigns, raw provider responses, private discussions, personal paths, and internal reviews out of Git, logs, and CI artifacts.
- Treat the lockfile as authoritative. Export hashed requirements from that lock and keep both documented installation paths working.
- Use key-free fixtures and mocked transport in tests and CI. Distinguish synthetic checks, genuine saved-source replay, and live generation. Passing technical checks does not confer human publication approval.
- Preserve the selected interface and meaningful keyboard behavior. Use EN dashes and the Oxford comma in new prose.

Run Ruff, its format check, and tests appropriate to the change. Inspect staged files and commit metadata. Provider calls, publication, and an original-code license grant require applicable authorization.
