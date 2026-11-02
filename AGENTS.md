# Contribution instructions

Campaign Studio is a local Python application with the Motif browser interface. Keep module responsibilities clear and changes focused on the supported workflow.

- Use FastAPI, Jinja2, plain JavaScript, Pillow, SQLite, and the official OpenAI SDK. Do not add a frontend build, ORM, job framework, or speculative provider abstraction.
- Preserve version-checked drafts, immutable image revisions, verified source reuse, deliberate request accounting, and offline export integrity. Local corrections must not trigger paid requests.
- Keep credentials, runtime campaigns, raw provider responses, private discussions, personal paths, and internal reviews out of Git, logs, and CI artifacts.
- Treat the lockfile as authoritative. Export hashed requirements from that lock and keep both documented installation paths working.
- Use key-free fixtures and mocked transport in tests and CI. Distinguish synthetic checks, genuine saved-source replay, and live generation. Passing technical checks does not confer human publication approval.
- Preserve the selected interface and meaningful keyboard behavior. Use EN dashes and the Oxford comma in new prose.

Run Ruff, its format check, and tests appropriate to the change. Inspect staged files and commit metadata. Provider calls, publication, and an original-code license grant require applicable authorization.
