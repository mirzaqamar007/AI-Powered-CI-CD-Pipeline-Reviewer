# AI-Powered CI/CD Pipeline Reviewer

A GitHub Actions workflow that reviews pull requests with Claude. When a PR is
opened or updated, it captures the diff, sends it to the Claude API, and posts
the review back as a comment on the PR.

## How it works

```
pull_request event
      │
      ├─ capture the diff against the merge base (lockfiles and build output excluded)
      ├─ skip if the PR is a draft, docs-only, or has no reviewable files
      ├─ send the diff to Claude  ──►  scripts/review_pr.py
      └─ post or update a single review comment on the PR
```

Two files do the work:

| File | Role |
| --- | --- |
| [`.github/workflows/ai-pr-review.yml`](.github/workflows/ai-pr-review.yml) | Triggers, collects the diff, posts the comment |
| [`scripts/review_pr.py`](scripts/review_pr.py) | Trims the diff, calls the Claude API, writes the review |

## Setup

1. **Add the API key.** In the repository, go to *Settings → Secrets and
   variables → Actions* and add a secret named `ANTHROPIC_API_KEY`. Get a key
   from the [Claude Console](https://console.anthropic.com/).

2. **Check Actions permissions.** *Settings → Actions → General → Workflow
   permissions* must allow read and write, or the workflow cannot post its
   comment. The workflow itself requests only `contents: read` and
   `pull-requests: write`.

3. **Open a pull request.** The review appears as a comment within a minute or
   two. Pushing more commits updates that same comment rather than adding a new
   one.

Without the secret the workflow still runs — it logs a warning and skips the
review instead of failing the check.

## What gets reviewed

The workflow deliberately narrows what it sends to the model:

- **Excluded paths** — lockfiles (`package-lock.json`, `poetry.lock`, `go.sum`,
  and friends), `vendor/`, `node_modules/`, `dist/`, `build/`, minified assets,
  source maps, test snapshots, and generated protobuf code.
- **Skipped PRs** — drafts, documentation-only changes, and PRs where every
  changed file was excluded.
- **Size caps** — 20,000 characters per file and 240,000 characters overall.
  Anything trimmed is named in a note at the bottom of the review, so a partial
  review never reads like a complete one.

## Configuration

The knobs live at the top of `scripts/review_pr.py`:

| Constant | Default | Meaning |
| --- | --- | --- |
| `MODEL` | `claude-opus-5` | The model that writes the review |
| `PER_FILE_CHAR_LIMIT` | `20_000` | Longest diff accepted for a single file |
| `TOTAL_CHAR_LIMIT` | `240_000` | Total diff budget for one review |
| `MAX_TOKENS` | `32_000` | Output budget, covering thinking and the review |

`SYSTEM_PROMPT` in the same file defines the review's shape and tone — edit it
to change what the reviewer looks for or how it reports findings.

Path exclusions live in the `EXCLUDES` array in the workflow's *Collect the
diff* step.

## Running the reviewer locally

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...

git diff -M origin/main...HEAD > pr.diff
python scripts/review_pr.py --diff pr.diff --output review.md --title "My change"
cat review.md
```

## Cost

One review is one API call. The cost scales with the size of the diff — a small
PR is a fraction of a cent; a large refactor at the 240,000-character cap is a
few cents. See [Claude API pricing](https://claude.com/pricing#api).

## License

MIT — see [LICENSE](LICENSE).
