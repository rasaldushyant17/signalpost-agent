# Research and abstention policy

One rule dominates every other decision in this codebase:

**It is better to miss a fact than to publish it under the wrong
company.** A missed fact costs a small amount of coverage. A wrong-company
fact can disqualify the entire run.

## How identity is decided

See `src/signalpost_agent/identity.py` for the executable version of this.
In short:

1. Every BRREG-sourced fact is trusted by construction: BRREG returns a
   record keyed by the organisation number we asked for, so the only check
   needed is confirming the response's own key matches the request.
2. A website, social profile, job posting, or dated news item is only ever
   published when the target's own checksum-valid organisation number
   appears in a *labelled* context (e.g. "org.nr. 923 609 016", or "NO
   923609016 MVA") on a page belonging to the candidate's own registered
   domain.
3. Name similarity — even an exact full legal name match — is **never**
   sufficient on its own. It is retained as a candidate, not published.
4. A page that labels *more than one* organisation number in context
   (a group homepage listing its subsidiaries, a franchise directory) is
   `ambiguous` for every company mentioned on it, even the one whose
   number is actually present — because there's no reliable way to tell
   from the page alone whether the target is the operator or merely
   listed.
5. A redirect to a different registered domain is never trusted just
   because the *original* URL came from an official source. The final
   domain has to earn its own identity proof from scratch.

## What counts as abstention, and why it's not a failure

Six honest states, everywhere:

- `available` — a fact was found and its evidence checks out.
- `not_available` — checked, genuinely nothing there.
- `blocked` — a source's own access rules (robots.txt, etc.) disallowed
  the fetch.
- `not_applicable` — the field doesn't apply (e.g. accounts for a
  dissolved entity).
- `ambiguous` — a candidate exists but identity proof is insufficient or
  conflicting.
- `failed` — something on our end (network, parsing) went wrong; try
  again next run.

None of these is a defect to be "fixed" by guessing. A company profile
full of honest `not_available` states is a correct, complete profile.

## What this agent will never do

- Never use LLM output as the source of a factual claim. See
  `src/signalpost_agent/summary.py`: the synthesis layer only rephrases
  claims that already passed validation, and every number in the
  generated text is checked against an actual claim value before being
  shown.
- Never scrape LinkedIn, Meta, Glassdoor, Indeed, or similar
  access-restricted platforms.
- Never treat a fetch failure, a truncated response, or a re-ordered list
  as a real change during refresh (see `src/signalpost_agent/refresh.py`).
- Never let one company's failure — however unexpected — reduce the
  number of envelopes produced for the batch (see
  `src/signalpost_agent/batch.py` and its fault-injection tests).
