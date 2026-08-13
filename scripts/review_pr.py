#!/usr/bin/env python3
"""Send a pull request diff to Claude and write a Markdown review.

Reads a unified diff from a file, trims it to fit the model's context, asks
Claude to review it, and writes the review to another file. The GitHub Actions
workflow is responsible for producing the diff and posting the result.

Exit codes:
    0  A review was written (or the diff was empty and there was nothing to do).
    1  Something went wrong; stderr explains what.
"""

from __future__ import annotations

import argparse
import os
import sys

import anthropic

MODEL = "claude-opus-5"

# Guardrails on how much diff we send. A PR that blows past these is reviewed
# partially and the report says so, rather than failing or silently truncating.
PER_FILE_CHAR_LIMIT = 20_000
TOTAL_CHAR_LIMIT = 240_000

# Room for both thinking and the written review. Opus 5 thinks by default, and
# max_tokens caps thinking + response together.
MAX_TOKENS = 32_000

SYSTEM_PROMPT = """\
You are reviewing a GitHub pull request diff. You see only the diff, not the \
full repository, so reason from what the changed lines show and say when \
something depends on code you cannot see.

Report every issue you find, including ones you are uncertain about. Do not \
filter for importance — a human reads this and decides what matters. For each \
finding give the file and line, what goes wrong, and a concrete fix.

Write the review as GitHub-flavored Markdown in this shape:

## Summary
One or two sentences on what the PR does.

## Findings
A `###` section per finding, titled `path/to/file.py:42 — short description`, \
each opening with a **Severity:** line of `high`, `medium`, or `low`, followed \
by the explanation and the suggested fix. Write "No issues found." if the diff \
is clean.

## Notes
Anything worth mentioning that is not a defect — naming, structure, missing \
tests. Omit this section if you have nothing to add.

Skip praise and preamble. If the diff is trivial, keep the review short.\
"""

USER_TEMPLATE = """\
Review this pull request.

Title: {title}
Description:
{body}

Diff:
```diff
{diff}
```
{truncation_note}"""


def split_by_file(diff: str) -> list[tuple[str, str]]:
    """Split a unified diff into (path, chunk) pairs, one per file."""
    chunks: list[tuple[str, str]] = []
    current_path = "unknown"
    current: list[str] = []

    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git "):
            if current:
                chunks.append((current_path, "".join(current)))
            current = [line]
            # "diff --git a/path b/path" -> take the b-side path.
            parts = line.split(" b/", 1)
            current_path = parts[1].strip() if len(parts) == 2 else "unknown"
        else:
            current.append(line)

    if current:
        chunks.append((current_path, "".join(current)))
    return chunks


def trim(diff: str) -> tuple[str, list[str]]:
    """Trim the diff to the size limits. Returns the diff and a list of notes."""
    notes: list[str] = []
    kept: list[str] = []
    used = 0

    for path, chunk in split_by_file(diff):
        if len(chunk) > PER_FILE_CHAR_LIMIT:
            chunk = chunk[:PER_FILE_CHAR_LIMIT] + "\n... [file diff truncated]\n"
            notes.append(f"`{path}` was truncated — only the first part is shown.")

        if used + len(chunk) > TOTAL_CHAR_LIMIT:
            notes.append(f"`{path}` was omitted — the diff exceeded the size budget.")
            continue

        kept.append(chunk)
        used += len(chunk)

    return "".join(kept), notes


def build_prompt(diff: str, title: str, body: str) -> tuple[str, list[str]]:
    trimmed, notes = trim(diff)
    note_text = ""
    if notes:
        note_text = "\nThis diff was trimmed before you saw it:\n" + "\n".join(
            f"- {n}" for n in notes
        )
    prompt = USER_TEMPLATE.format(
        title=title or "(no title)",
        body=(body or "(no description)").strip(),
        diff=trimmed,
        truncation_note=note_text,
    )
    return prompt, notes


def request_review(client: anthropic.Anthropic, prompt: str):
    """Ask Claude for the review, streaming so long reviews don't hit a timeout."""
    params = dict(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        output_config={"effort": "high"},
        messages=[{"role": "user", "content": prompt}],
    )

    # Opus 5's safety classifiers can decline a request. `fallbacks: "default"`
    # re-serves it on Anthropic's recommended fallback model inside the same
    # call. An older SDK raises TypeError on the parameter and an account
    # without the beta gets a 400 — either way, retry without it rather than
    # failing the review.
    try:
        with client.beta.messages.stream(
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            **params,
        ) as stream:
            return stream.get_final_message()
    except (TypeError, anthropic.BadRequestError) as exc:
        print(f"note: server-side fallbacks unavailable ({exc}); retrying without", file=sys.stderr)

    with client.messages.stream(**params) as stream:
        return stream.get_final_message()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--diff", required=True, help="path to the unified diff")
    parser.add_argument("--output", required=True, help="path to write the review to")
    parser.add_argument("--title", default="", help="pull request title")
    parser.add_argument("--body", default="", help="pull request description")
    args = parser.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("error: ANTHROPIC_API_KEY is not set", file=sys.stderr)
        return 1

    try:
        with open(args.diff, encoding="utf-8", errors="replace") as handle:
            diff = handle.read()
    except OSError as exc:
        print(f"error: could not read {args.diff}: {exc}", file=sys.stderr)
        return 1

    if not diff.strip():
        print("note: diff is empty after filtering; nothing to review", file=sys.stderr)
        return 0

    prompt, notes = build_prompt(diff, args.title, args.body)
    for note in notes:
        print(f"note: {note}", file=sys.stderr)

    client = anthropic.Anthropic()

    try:
        counted = client.messages.count_tokens(
            model=MODEL,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        print(f"note: prompt is {counted.input_tokens} tokens", file=sys.stderr)
    except anthropic.APIError as exc:
        print(f"note: token count unavailable ({exc})", file=sys.stderr)

    try:
        message = request_review(client, prompt)
    except anthropic.APIError as exc:
        print(f"error: the Claude API call failed: {exc}", file=sys.stderr)
        return 1

    if message.stop_reason == "refusal":
        detail = getattr(message.stop_details, "explanation", None) or "no explanation given"
        print(f"error: the model declined to review this diff ({detail})", file=sys.stderr)
        return 1

    review = "\n".join(block.text for block in message.content if block.type == "text").strip()
    if not review:
        print("error: the model returned an empty review", file=sys.stderr)
        return 1

    if message.stop_reason == "max_tokens":
        review += "\n\n_This review was cut short at the output limit._"

    footer_notes = "".join(f"\n> - {n}" for n in notes)
    review += f"\n\n---\n> Reviewed by `{MODEL}`.{footer_notes}"

    try:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(review + "\n")
    except OSError as exc:
        print(f"error: could not write {args.output}: {exc}", file=sys.stderr)
        return 1

    print(f"note: wrote {len(review)} characters to {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
