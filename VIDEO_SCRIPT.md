# Video script — AI Code Review Agent (target: 4:30, hard cap 5:00)

Recording notes: one take if you can manage it — screen-record your terminal/editor, talk over it live. The lines below are things to say in your own words, not a teleprompter. Run the demo command for real; don't fake or pre-record the output.

Verified demo repo: `https://github.com/anxolerd/dvpwa` (branch `master`) — already tested, finds real SQL injection, MD5 password hashing, disabled CSRF, missing `httponly` cookie flag, and debug mode left on. Good range of severities for the demo.

---

## Before you hit record — checklist

- [ ] `git status` clean, README pushed (`git log --oneline -3` shows it on top)
- [ ] `.env` has real `GITHUB_TOKEN` and `GEMINI_API_KEY` — keep this file/tab off-screen
- [ ] Terminal font bumped up (Cmd+ a few times) so text reads on a recording
- [ ] README architecture diagram open in a tab or editor
- [ ] ADK snippet (below) saved in a text file, ready to paste — don't type it live
- [ ] `tests/` folder open in your editor for a 2-second cutaway
- [ ] QuickTime Player → File → New Screen Recording, ready to go

---

## 0:00–0:30 — The hook

**Say:**

"Code review doesn't scale with how fast teams ship. Static analyzers like Semgrep catch real patterns, but their output is rule-ID jargon with no judgment behind it. Ask an LLM to review your code with no grounding, and it just makes up plausible-sounding feedback, because it's reviewing a paste, not your actual repo. I built an agent that closes that gap: give it a GitHub URL, it fetches the real code, runs real static analysis, and asks Gemini to turn both into a prioritized review a human can actually act on."

**On screen:** title card — "AI Code Review Agent" / Agents for Business track.

---

## 0:30–1:15 — Architecture walkthrough

**Do:** show the architecture diagram from the README.

**Say:**

"Three stages, each its own tested module. `github_fetcher` pulls every Python file from the repo via the GitHub API. `semgrep_runner` writes those into an isolated sandbox and runs Semgrep — that's the deterministic, ground-truth half. `gemini_reviewer` takes the source plus the Semgrep findings and asks Gemini 3.1 Flash Lite for a structured, severity-ranked review — that's the judgment half. `agent.py` wires all three together and also exposes the same pipeline as a Google ADK agent tool."

"Only a fetch failure is fatal — there's nothing to review without files. If Semgrep or Gemini has a bad moment, the pipeline degrades gracefully instead of crashing. You'll actually see that happen in a second — Gemini occasionally throws a transient 503 under load, and the agent retries automatically."

---

## 1:15–2:45 — Live demo: real end-to-end run

**Do:** run this for real, on camera:

```bash
python3 main.py https://github.com/anxolerd/dvpwa --branch master --out review_report.md -v
```

**While it runs (~70s), say:**

"This is a live run against a small intentionally-vulnerable Flask app — not staged, not a recording. Right now it's fetching files from GitHub, running Semgrep, and sending everything to Gemini."

If a `503` shows up in the log (it did in testing), point at it:

"There — that's a real transient error from Gemini, and you can see it just retried with backoff instead of failing the whole run. That's the non-fatal error handling working exactly as designed."

**When it finishes, open `review_report.md` and read 2–3 findings out loud:**

"Here's a critical one — SQL injection in the student-creation DAO, because the query's built with string formatting instead of parameters. It doesn't just say 'this is bad,' it gives the actual parameterized-query fix. There's also MD5 used for password hashing, and CSRF protection that's been commented out entirely. All real issues, found by the actual pipeline doing its actual job — I didn't pick this repo because I knew what was in it, I picked it because it's a known deliberately-vulnerable practice app, and the agent found the real things wrong with it on its own."

---

## 2:45–3:30 — Agent / ADK tool-calling

**Do:** in your terminal, run:

```bash
python3 adk_demo.py
```

This script just gives the agent one plain-English sentence — "review this repo and summarize the top issues" — and nothing else. No function name, no arguments, no code telling it what to do.

**Say:**

"Up to now I've been calling the review function directly — that's just a script. This part is the actual agent. I'm handing it one plain-English sentence: review this repo and summarize the issues. I'm not telling it which function to call or what arguments to pass. The model reads that sentence, decides on its own that it needs to call the review tool, figures out the right arguments itself, and then turns the result back into a normal summary. That decision-making step is what makes this an agent instead of a script with extra steps."

When the output prints a line like `[agent decided to call tool: review_repo_tool]`, point at it: "There — that's the model choosing to call the tool on its own, not my code dispatching it."

---

## 3:30–4:10 — Security, by design

**Say:**

"Security wasn't bolted on after — it shaped the architecture from the start. Every subprocess call uses explicit argument lists, never `shell=True`. File paths from a fetched repo are validated against path traversal before they ever touch disk. Semgrep's config argument is allow-listed by regex against injection. And the system prompt explicitly tells Gemini to treat all code and Semgrep output as untrusted data, not instructions — so a malicious commit can't talk its way past the review with something like 'ignore previous instructions.' No credentials are ever hardcoded, and there's a dedicated test that asserts a key never leaks into a log line or error message."

**Do:** flash the `tests/` folder for 2 seconds.

---

## 4:10–4:30 — Deployability + close

**Say:**

"The pipeline is fully stateless — one repo URL in, one report out, no persistent storage — so it drops into a CI step, a scheduled job, or a containerized service behind a webhook with no architectural changes. Eighty-three tests pass in about a second with everything mocked, and I ran this against real repositories with real credentials and it found real issues — including, while building it, three integration bugs in my own code that only a real run could have surfaced. Code, tests, and writeup are all linked below."

**On screen:** GitHub URL + "Agents for Business — Kaggle 5-Day AI Agents Capstone."

---

## After recording

1. Trim dead air in QuickTime (Edit → Trim) if needed.
2. Upload to YouTube — visibility **Public** (required by the rules, not Unlisted).
3. Copy the YouTube link for the Kaggle Writeup.
