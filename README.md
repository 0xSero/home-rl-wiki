# Home RL Wiki

A Wikipedia-style wiki about RL post-training with LoRA on home hardware (4x DGX Spark, 4x RTX PRO 6000):
concepts, algorithms, hardware, models, frameworks, run-time estimates, and embedded videos and papers.

**Public copy:** https://0xsero.github.io/home-rl-wiki/ (read-only; rebuilt on every push to `main`)

## Run it locally (full editing)

    python3 server.py        # http://127.0.0.1:8765/wiki/Main_Page

No dependencies beyond Python 3. The local copy supports editing, talk pages, history, diffs and the questions form.

- Pages: `content/pages/<slug>.wiki` (talk pages: `talk__<slug>.wiki`)
- Local revisions: `content/history/`, change log: `content/changes.jsonl`, form answers: `content/answers.json` (not committed)
- `seed.wiki` holds the original articles. On start, missing pages are created and pages still matching their seed version are refreshed. Pages edited by hand, locally or on GitHub, are never overwritten.
- The server binds to 127.0.0.1 only and rejects cross-origin writes.

## Publishing

- `./publish.sh "message"` commits `content/pages` and pushes; GitHub Actions runs `build_static.py` and deploys to Pages.
- You can also edit any `content/pages/*.wiki` file directly on GitHub. The public site's "View source" tab links there.
- On the public site, history and recent changes come from git, and the questions form opens a prefilled GitHub issue.
