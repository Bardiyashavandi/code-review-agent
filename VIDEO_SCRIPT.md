# Video script — AI Code Review Agent (target: 4:30, hard cap 5:00)

Recording notes: one take if you can manage it — screen-record your terminal/editor, talk over it live. The lines below are things to say in your own words, not a teleprompter. Run both demo commands for real, on camera — they were just verified working end-to-end today, with the exact output shown below.

Verified demo repo: `https://github.com/anxolerd/dvpwa` (branch `master`) — finds real SQL injection, weak MD5 password hashing, missing rate limiting, and disabled HTML autoescaping (XSS risk). Good range of severities for the demo.

---

## Before you hit record — checklist

- [ ] `git status` clean, everything pushed (`git log --oneline -3` shows your latest commit on top)
- [ ] `.env` has real `GITHUB_TOKEN` and `GEMINI_API_KEY` — keep this file/tab off-screen
- [ ] Terminal font bumped up (Cmd+ a few times) so text reads on a recording
- [ ] Run `clear` in your terminal right before recording so the screen starts empty
- [ ] README.md open in a tab/editor, scrolled to the architecture diagram
- [ ] `tests/` folder open in your editor for a 2-second cutaway
- [ ] Screen recorder ready (Cmd+Shift+5 → Record Selected Portion, or QuickTime → File → New Screen Recording)

---

## 0:00–0:30 — The hook

**Say:**

"Code review doesn't scale with how fast teams ship. Static analyzers like Semgrep catch real patterns, but their output is rule-ID jargon with no judgment behind it. Ask an LLM to review your code with no grounding, and it just makes up plausible-sounding feedback, because it's reviewing a paste, not your actual repo. I built an agent that closes that gap: give it a GitHub URL, it fetches the real code, runs real static analysis, and asks Gemini to turn both into a prioritized review a human can actually act on."

**On screen:** title card or README open — "AI Code Review Agent" / Agents for Business track.

---

## 0:30–1:10 — Architecture walkthrough

**Do:** show the architecture diagram in README.md.

**Say:**

"Three stages, each its own tested module. `github_fetcher` pulls every Python file from the repo via the GitHub API. `semgrep_runner` writes those into an isolated sandbox and runs Semgrep — that's the deterministic, ground-truth half. `gemini_reviewer` takes the source plus the Semgrep findings and asks Gemini for a structured, severity-ranked review — that's the judgment half. `agent.py` wires all three together and also exposes the same pipeline as a Google ADK agent tool."

"Only a fetch failure is fatal — there's nothing to review without files. If Semgrep or Gemini has a bad moment, the pipeline degrades gracefully and keeps going instead of crashing the whole run."

---

## 1:10–2:10 — Live demo: real end-to-end run

**Do:** switch to your terminal and type this command live, exactly as written:

```bash
python3 main.py https://github.com/anxolerd/dvpwa --branch master --out review_report.md -v
```

**While it runs (takes about 9 seconds — don't talk too long here), say:**

"This is a live run against a small, intentionally-vulnerable Flask app — not staged, not pre-recorded. It's fetching files from GitHub, running Semgrep, then sending the code and findings to Gemini."

**When the summary line prints (`Files fetched: 10 ... Report written to: review_report.md`), say:**

"Ten files fetched, three Semgrep findings, all turned into a structured review in under ten seconds."

**Do:** run `cat review_report.md` and read 2–3 issues out loud from the actual output:

"Here's the critical one — SQL injection in the student-creation DAO, because the query's built with string formatting instead of parameters. It doesn't just say 'this is bad,' it gives the actual parameterized-query fix. There's also MD5 used for password hashing, and the login endpoint has no rate limiting at all. All real issues, found by the actual pipeline doing its actual job — I picked this repo because it's a known, deliberately-vulnerable practice app, not because I cherry-picked the output."

---

## 2:10–3:10 — Agent / ADK tool-calling

**Do:** type this command live:

```bash
python3 adk_demo.py
```

This script hands the agent one plain-English sentence and nothing else — no function name, no arguments, no code telling it what to do.

**Say, while it's running:**

"Up to now I've been calling the review function directly — that's just a script. This part is the actual agent. I'm handing it one plain-English sentence: review this repo and summarize the top issues. I'm not telling it which function to call or what arguments to pass. The model reads that sentence, decides on its own that it needs to call the review tool, figures out the right arguments itself, and then turns the structured result back into a normal summary."

**When `[agent decided to call tool: review_repo_tool]` prints, point at it:**

"There — that's the model choosing to call the tool on its own, not my code dispatching it."

**When the final summary prints, say:**

"And that's the agent's own write-up of the same findings — critical SQL injection, weak password hashing, missing rate limiting — generated from one sentence of input, with no manual function dispatch anywhere in the loop."

---

## 3:10–3:50 — Security, by design

**Say:**

"Security wasn't bolted on after — it shaped the architecture from the start. Every subprocess call uses explicit argument lists, never `shell=True`. File paths from a fetched repo are validated against path traversal before they ever touch disk. Semgrep's config argument is allow-listed by regex against injection. And the system prompt explicitly tells Gemini to treat all code and Semgrep output as untrusted data, not instructions — so a malicious commit can't talk its way past the review with something like 'ignore previous instructions.' No credentials are ever hardcoded, and there's a dedicated test that asserts a key never leaks into a log line or error message."

**Do:** flash the `tests/` folder for 2 seconds.

---

## 3:50–4:20 — Deployability + close

**Say:**

"The pipeline is fully stateless — one repo URL in, one report out, no persistent storage — so it drops into a CI step, a scheduled job, or a containerized service behind a webhook with no architectural changes. Eighty-three tests pass in about a second with everything mocked, and I ran this against real repositories with real credentials and it found real issues — including, while building it, several integration bugs in my own code that only a real run could have surfaced. Code, tests, and writeup are all linked below."

**On screen:** GitHub URL + "Agents for Business — Kaggle 5-Day AI Agents Capstone."

---

## After recording

1. Trim dead air if your recorder supports it.
2. Upload to YouTube — visibility **Public** (required by the rules, not Unlisted).
3. Copy the YouTube link for the Kaggle Writeup.
