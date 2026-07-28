# BugHound Mini Model Card (Reflection)

Completed after running BugHound in both Heuristic and Gemini modes against the
sample snippets in `sample_code/`, and after adding/testing several guardrails
this session (see `bughound_agent.py`, `reliability/risk_assessor.py`, and
`tests/`).

---

## 1) What is this system?

**Name:** BugHound

**Core purpose:** BugHound is a small agentic code-review assistant. Given a
Python snippet, it (1) detects reliability/maintainability issues, (2)
proposes a fix, and (3) runs a deterministic, non-LLM risk assessment on the
proposed fix before deciding whether it's safe enough to auto-apply. Its real
purpose isn't "auto-fix code" — it's to demonstrate a pattern for putting a
trustworthy, explainable safety net around an untrustworthy LLM-driven change,
so that a bad or malformed model response degrades gracefully instead of
silently corrupting code.

**Intended users:** Students learning agentic workflows and AI reliability
engineering concepts (this is a CodePath ai110 module exercise). More broadly,
it models the concerns any team would face wiring an LLM into an automated
code-fixing pipeline: how much do you trust the model's output, and what
guardrails do you need before letting it touch code unattended?

---

## 2) How does it work?

`BugHoundAgent.run()` (`bughound_agent.py`) executes five steps, each logged to
an "Agent trace":

1. **PLAN** — a fixed log line; there's no real planning logic, it just marks
   the start of the run.
2. **ANALYZE** — detects issues. If an LLM client is configured, it sends the
   code to `prompts/analyzer_system.txt` / `analyzer_user.txt` and expects a
   JSON array of `{type, severity, msg}` objects back.
3. **ACT** — proposes a fixed version of the code the same way, via
   `prompts/fixer_system.txt` / `fixer_user.txt`.
4. **TEST** — runs `assess_risk()` (`reliability/risk_assessor.py`), a
   deterministic scorer (0–100) with no model involvement at all.
5. **REFLECT** — if the risk level is `"low"`, the agent would auto-apply the
   fix; otherwise it flags the result for human review.

**Heuristics vs. Gemini:** `bughound_agent.py`'s `_can_call_llm()` decides this
per call — it returns `True` whenever `client is not None and hasattr(client,
"complete")`. When `False`, ANALYZE/ACT use the regex-based
`_heuristic_analyze` / `_heuristic_fix` directly. When `True`, they call the
model, but fall back to the same heuristic functions if the LLM call fails or
its output doesn't clear the following bars (mostly added/tightened this
session):

- Analyzer output must parse as a JSON array, and every issue must have a
  non-empty `type`/`msg` and a `severity` that's exactly `Low`/`Medium`/`High`
  — otherwise the whole batch is rejected and heuristics run instead.
- Fixer output must be non-empty and must parse as syntactically valid Python
  (`ast.parse`) — otherwise the heuristic fixer runs instead.
- Any exception raised by the client (timeout, API error, rate limit) falls
  back to heuristics immediately.

Important nuance discovered this session (see §5, failure #1): `_can_call_llm`
checks the *client's shape*, not the UI's selected mode. The app's "Heuristic
only (no API)" mode still passes a `MockClient()`, which *has* a `.complete`
method — so ANALYZE correctly falls back to heuristics (because MockClient's
response isn't parseable JSON), but ACT does not automatically use
`_heuristic_fix`; it calls `MockClient.complete()` for the fixer prompt too.

---

## 3) Inputs and outputs

**Inputs tested** (all four snippets in `sample_code/`):

| File | Shape | Issue(s) present |
|---|---|---|
| `print_spam.py` | 4-line function | `print(` calls only |
| `flaky_try_except.py` | 6-line function | bare `except:` + manual `open()`/`.read()` with no `close()`/`with` |
| `mixed_issues.py` | 7-line function + module comment | `print(`, bare `except:`, and a `TODO` comment, combined |
| `cleanish.py` | 5-line function | none (control case — already uses `logging`, has a `return`) |

All four are short, single-function scripts (4–8 lines) — enough to exercise
each heuristic rule individually, in combination, and in a "nothing wrong"
control case.

**Outputs observed:**

- *Issue types:* heuristic mode only ever produces `Code Quality` (Low, for
  `print(`), `Reliability` (High, for bare `except:`), and `Maintainability`
  (Medium, for `TODO`) — exactly its three hardcoded checks, nothing else.
  Gemini mode (see the real transcript for `flaky_try_except.py` captured
  earlier this session) produced a fourth category heuristics have no rule for
  at all: `Resource Management` (Medium) — flagging that `f.close()` gets
  skipped if `f.read()` raises.
- *Fixes proposed:* Gemini's fix for `flaky_try_except.py` rewrote the manual
  `open()`/`.read()` into a `with open(path, "r") as f:` block and narrowed the
  bare `except:` to `except Exception:`. Heuristic mode's `_heuristic_fix`
  (when it actually runs) turns bare `except:` into `except Exception as e:` +
  a placeholder comment, and rewrites `print(` calls to `logging.info(`.
- *Risk reports observed* (scores from actually running the agent this
  session):

  | Run | Score | Level | Auto-fix? |
  |---|---|---|---|
  | `cleanish.py`, no issues | 100 | low | Yes |
  | `flaky_try_except.py`, Gemini | 55 | medium | No |
  | `print_spam.py`, MockClient (see §5 #1) | 45 | medium | No |
  | `flaky_try_except.py`, MockClient (see §5 #1) | 5 | high | No |
  | `mixed_issues.py`, MockClient (see §5 #1) | 0 | high | No |

---

## 4) Reliability and safety rules

**Rule A — severity-based deduction** (`High` −40, `Medium` −20, `Low` −5 per
issue):

- *What it checks:* sums a deduction per detected issue, scaled by the
  analyzer's own severity label.
- *Why it matters:* ties the numeric risk score to how bad each specific
  problem is, so a fix touching a serious reliability bug is scrutinized more
  than one touching a cosmetic style nit.
- *False positive it can cause:* severity labeling isn't consistent across
  modes — the heuristic analyzer hardcodes bare `except:` as `High`, while
  Gemini rated the *same underlying bug* `Medium` in our real test. A fix can
  be blocked from auto-apply purely because of which analyzer happened to run,
  not because the fix itself is actually risky.
- *False negative it can miss:* the rule trusts the severity label completely.
  If an issue is genuinely dangerous but mislabeled `Low`, it only costs 5
  points — nowhere near enough to stop an unsafe fix from reaching the
  low-risk / auto-fix band.

**Rule B — "fixed code is much shorter than original"** (fixed lines < 50% of
original lines → −20):

- *What it checks:* a length-ratio heuristic meant to catch a fix that
  silently dropped a large chunk of logic.
- *Why it matters:* it's a cheap, model-agnostic signal that doesn't require
  understanding the code's semantics — useful as a last line of defense.
- *False positive it can cause:* legitimate simplifications trip it too. We
  saw this directly: `MockClient`'s placeholder fixer response
  (`"# MockClient: no rewrite available in offline mode."`, one line) is far
  shorter than every multi-line sample, so this rule fired on every affected
  run in §3 — correctly in that case, but the same rule would equally penalize
  a *good* one-line simplification (e.g. collapsing manual
  open/read/close into one `with` line).
- *False negative it can miss:* a broken or unsafe rewrite padded to a similar
  or greater line count (extra comments, verbose logging, restructured but
  equally-long code) sails past this check entirely, since it only measures
  line-count ratio, never correctness.

**Rule C — dangerous-call introduction** (added this session; −35 if
`fixed_code` contains `eval(`, `exec(`, `os.system(`, `subprocess.call(`, or
`pickle.loads(` that wasn't in `original_code`):

- *What it checks:* a diff between original and fixed code against a small
  denylist of risky call patterns.
- *Why it matters:* none of the other rules evaluate whether a fix makes
  *previously safe* code unsafe — a "correct" fix for the flagged issue could
  still introduce an unrelated security regression, and nothing else here
  would catch it (confirmed by the test added this session: without this
  rule, that exact scenario scores 100/low/auto-fixable).
- *False positive it can cause:* purely substring-based, so a legitimate,
  intentional use of e.g. `subprocess.call([...])` (no `shell=True`, no
  untrusted input) gets the same penalty as an actually dangerous call — and
  even a comment or docstring merely *mentioning* `"os.system("` as an example
  would incorrectly trip it.
- *False negative it can miss:* only matches five exact, hardcoded substrings.
  Equivalent danger spelled differently — `os.popen(`, `subprocess.run(...,
  shell=True)`, a dynamically built string passed to `exec`, `__import__
  ('os').system(...)` — passes through completely undetected.

---

## 5) Observed failure modes

**1. "Heuristic only" mode doesn't actually run the heuristic fixer.**

The Streamlit app's "Heuristic only (no API)" mode sets `client =
MockClient()`. Since `MockClient` exposes a `.complete()` method,
`_can_call_llm()` returns `True` for it. `analyze()` still falls back to
`_heuristic_analyze` correctly (MockClient's analyzer response isn't
parseable JSON), but `propose_fix()` takes the LLM branch whenever issues
exist, calls `MockClient.complete()`, and gets back the literal string `"#
MockClient: no rewrite available in offline mode.\n"` — which is syntactically
valid Python (a bare comment), so it passes the syntax guardrail and is
returned as the "fixed code," silently discarding the entire function body.
Verified directly: running the agent this way on `print_spam.py`,
`flaky_try_except.py`, and `mixed_issues.py` all produced exactly that stub as
`fixed_code`, with scores 45/5/0 respectively. The risk assessor's structural
checks (§4 Rule B, plus "return removed") happened to catch every case and
correctly blocked auto-apply — but the "Proposed fix" panel shown to the user
in heuristic mode isn't a heuristic fix at all. This is a case of the agent
misreading a placeholder response as acceptable output, rooted in inferring
"are we in LLM mode?" from the client's shape rather than the app's actual
selected mode.

**2. Silently accepting an unrecognized severity value (fixed this session).**

Before this session's validation was added, an LLM returning syntactically
valid JSON with a severity value that wasn't exactly `"Low"`/`"Medium"`/`"High"`
(e.g. `"Critical"`) was accepted as-is by `_normalize_issues`. `assess_risk`'s
severity loop only matches those three exact (lowercased) strings, so an
unrecognized severity contributed a $0$ deduction — the issue still appeared
in "Detected issues," but invisibly didn't affect the risk score, as if it
were risk-free. Reproduced directly this session with a fake client returning
`severity: "critical"`: score stayed at 100/low/auto-fixable despite a
reported issue. Fixed by rejecting the whole analyzer batch (triggering
heuristic fallback) when any issue's severity isn't recognized.

**3. A fix that resolves the flagged issue while introducing a new one.**

Constructed and tested this session: given code with one `Low`-severity
issue, a fixer can return code that's syntactically valid, genuinely
addresses the flagged issue, and *additionally* swaps in `os.system(cmd)`
where it wasn't present before. Nothing in the agent workflow's format checks
(JSON validity, severity validity, Python syntax) objects, since the response
is well-formed on every axis those checks examine — this is exactly the gap
Rule C (§4) was added to close, and without it this scenario resolves to
`should_autofix: True`. Line counts and `return` presence look completely
normal, so this failure mode is invisible to every check except the
dangerous-call one.

---

## 6) Heuristic vs Gemini comparison

Using the one real Gemini transcript captured this session
(`flaky_try_except.py`) against our own heuristic-mode run of the same file:

| | Heuristic (via app's actual wiring) | Gemini |
|---|---|---|
| Issues found | 1: `Reliability`/`High` — bare `except:` | 2: `Error Handling`/`Medium` (bare except) + `Resource Management`/`Medium` (file not closed on read error) |
| Fix produced | `"# MockClient: no rewrite..."` stub (see §5 #1) | Real rewrite to `with open(path, "r") as f:` |
| Risk score | 5 (high) | 55 (medium) |
| Auto-fix? | No | No |

- **Coverage discrepancy:** Gemini caught a whole bug class (resource leak on
  exception) that heuristic mode is structurally incapable of seeing —
  heuristics only check for `print(`, bare `except:`, and `TODO`; anything
  outside that vocabulary is invisible regardless of how severe it is.
- **Severity discrepancy:** the identical underlying bug (bare `except:`) was
  rated `High` by the hardcoded heuristic rule and `Medium` by Gemini's
  judgment — same code, different severity, which changes the deterministic
  risk math (−40 vs. −20) for reasons unrelated to the actual danger of the
  code.
- **Fix-quality discrepancy:** Gemini's fix is a real, working improvement;
  heuristic mode's "fix" (as actually wired through the app) wasn't a fix at
  all — see §5 #1.
- **Agreement with intuition:** Gemini's medium/no-autofix call feels right —
  a good fix that still slightly broadens the exception type, worth a glance.
  Heuristic mode's high/no-autofix call was *accidentally* right — it's
  correctly cautious, but only because it was scoring a meaningless stub, not
  because it reasoned about a real fix.

---

## 7) Human-in-the-loop decision

**Scenario:** an LLM proposes a fix that resolves the reported issue(s) but
introduces a call like `os.system(...)`, `eval(...)`, or `pickle.loads(...)`
that wasn't present in the original code (§4 Rule C, §5 #3). Even though the
fix is syntactically valid and on-topic, BugHound should refuse to auto-apply
and require a human to look at it — a fix that trades one bug for a new
security-relevant one is exactly the case where automation should stop.

- **Trigger:** the dangerous-call-introduction check already added to
  `reliability/risk_assessor.py` this session — it drops the score by 35 and
  adds a `"Fix introduces potentially dangerous call(s)..."` reason, which is
  normally enough on its own to push a would-be low-risk result into
  `medium`, flipping `should_autofix` to `False`.
- **Why this layer:** `risk_assessor.py` is the only place that sees both
  `original_code` and `fixed_code` together, and it's the deterministic
  guardrail the rest of the agent already defers to for the auto-fix decision
  — applying the check here protects against dangerous fixes regardless of
  whether they came from the LLM or the heuristic fixer.
- **Message shown to the user:** already surfaced today via the risk report's
  "Reasons" list (`"Fix introduces potentially dangerous call(s) not present
  in the original: os.system("`) plus the REFLECT trace line ("Fix is not
  safe enough to auto-apply. Human review recommended.") — both are visible
  in the existing UI without further changes.

---

## 8) Improvement idea

**Give the agent an explicit offline flag instead of inferring "LLM mode" from
the client's shape.**

This directly targets the real, reproducible bug in §5 #1: `_can_call_llm()`
currently returns `True` for *any* client with a `.complete` method, including
`MockClient` — so selecting "Heuristic only (no API)" in the UI still lets
`propose_fix()` call the mock's fixer stub and return it as a nonsensical
"fix," relying on the risk assessor to catch the fallout rather than avoiding
it in the first place.

**Change:** add an explicit `use_llm: bool = True` parameter to
`BugHoundAgent.__init__`, have `bughound_app.py` pass `use_llm=(mode ==
"Gemini (requires API key)")` when constructing the agent, and change
`_can_call_llm()` to `return self.use_llm and self.client is not None and
hasattr(self.client, "complete")`.

This is a one-parameter, three-line change (constructor, one method, one
call site) — no new files, no new dependencies — and it measurably improves
reliability by making "Heuristic only" mode actually run `_heuristic_fix` for
every snippet with issues, instead of silently returning a placeholder string
that happens to get blocked downstream for the wrong reason.
