# Rules playbook schema

A corpus's rules playbook is a YAML file referenced by `Corpus.rules_path`
(resolved against `settings.CORPUS_ROOT` -- see `backend/app/config.py`;
`corpus/demo/rules.yaml` is the starter playbook for the demo corpus). It
is loaded and evaluated by `backend/app/services/rules.py`, and run by the
`examine` node (`backend/app/agent/nodes/examine.py`, plan 8.4) against the
claims extracted so far in the current run (`state.claims`).

A corpus with no `rules_path` set has nothing to check: `examine` produces
zero findings, same as a corpus whose rules all pass.

## Top level

```yaml
rules:
  - id: <string, unique within the file>
    description: <string, human-readable, also used as LLM-evaluator context>
    severity: info | warning | error
    deterministic: <DeterministicSpec>   # exactly one of
    llm: <LLMSpec>                       # `deterministic` or `llm`
```

Every rule has exactly one evaluator. A rule with both or neither set fails
to load (`Rule` validation in `services/rules.py` raises).

`severity` becomes the `findings.severity` of every finding the rule
produces.

## Grouping: subjects are features

Both evaluators work over this run's `state.claims` grouped by
`Claim.subject` -- in this codebase a claim's subject is the feature name
(e.g. `"checkout flow"`), so "every subject" means "every feature this run
extracted at least one claim about." A feature no claim mentions this run
is invisible to the rules engine; nothing here reaches into
`register_entries` for features from a *previous* run (that is an
`update`-run diffing question, out of scope for `examine`).

## `deterministic`: predicate DSL

```yaml
deterministic:
  op: every_subject_has | no_subject_has | at_least_one
  predicate: <string, matched against Claim.predicate>
  value: <string, required only when op is no_subject_has, matched against Claim.object>
```

No LLM call, no cost, evaluated in Python. Three operations:

- **`every_subject_has(predicate)`** (YAML: `op: every_subject_has`,
  `predicate: <predicate>`) -- for every subject with at least one claim
  this run, at least one of its claims must have this `predicate` (any
  object value). One finding per subject that has none, citing that
  subject's other claims as the evidence that it's a tracked feature at
  all.

- **`no_subject_has(predicate)=value`** (YAML: `op: no_subject_has`,
  `predicate: <predicate>`, `value: <value>`) -- no subject may have a
  claim with this exact `predicate`/`object` pair. One finding per subject
  that has a match, citing the matching claim(s).

- **`at_least_one(predicate)`** (YAML: `op: at_least_one`,
  `predicate: <predicate>`) -- across *all* of this run's claims (not
  per-subject), at least one must have this `predicate`. A corpus-level
  sanity check, not a per-feature one: at most one finding, `subject: null`,
  with no claim sources (there is nothing to cite for an absence).

## `llm`: prompt-based evaluator

```yaml
llm:
  select_predicate: <string, matched against Claim.predicate>
  select_object: <string, optional, matched against Claim.object>
  prompt: <filename under backend/app/agent/prompts/>
```

For rules that can't be decided from `(subject, predicate, object)` triples
alone -- this playbook's example is "no feature has status=shipped without
a linked release-notes source," which needs to know which *document*
backs each claim, not just what the claim says.

Evaluation: group claims by subject, then select every subject that has at
least one claim matching `select_predicate` (and `select_object`, if
given). If nothing is selected, the rule is skipped entirely -- no LLM
call, no cost, matching CLAUDE.md behavior 10. Otherwise, `call_claude` is
invoked once for the whole rule (`stage="examine"`) with:

- the system prompt loaded from `backend/app/agent/prompts/<prompt>`
- one user message containing, for every claim of every selected subject:
  `subject`, `predicate`, `object`, `claim_id`, and `source_doc_types` (the
  `documents.doc_type` of every document backing that claim, looked up via
  `claim_sources -> chunks -> documents`), plus the claims' verbatim source
  quotes wrapped via `injection_guard.wrap_sources` (CLAUDE.md behavior 8 --
  quotes are raw source text, same as in `extract`)

The prompt must instruct the model to respond with ONLY a strict JSON list,
one element per selected subject:

```json
[{"subject": "<subject>", "result": true | false, "reason": "<string>"}]
```

`result: false` becomes one finding for that subject, `message` set to
`reason`, citing every claim of that subject as sources. A raw response
that trips `injection_guard.scan_response` produces a
`possible_prompt_injection` finding, same as `extract` -- it never silently
changes what the rule concludes.

## Findings and sources

Every violation becomes one `findings` row (`rule_id`, `severity` from the
rule, `subject`, `message`) plus one `finding_sources` row per cited
`claim_id` (`finding_sources.chunk_id` is left null here -- claims, not
individual chunks, are what a rule violation is "about"). A violation that
inherently can't cite a claim (`at_least_one` against zero matching claims)
gets zero `finding_sources` rows rather than a fabricated one (CLAUDE.md
behavior 5: never invent a source).

If every rule passes (or there were no rules to evaluate), `examine` writes
one `examine_clean` audit event instead of any findings.

## Example: `corpus/demo/rules.yaml`

```yaml
rules:
  - id: every_feature_has_owner
    description: Every feature must have an assigned owner.
    severity: warning
    deterministic:
      op: every_subject_has
      predicate: owner

  - id: every_feature_has_target_release
    description: Every feature must have a target release.
    severity: warning
    deterministic:
      op: every_subject_has
      predicate: target_release

  - id: shipped_requires_release_notes
    description: >-
      No feature may be marked status=shipped unless at least one of its
      claims is sourced from a release-notes document.
    severity: error
    llm:
      select_predicate: status
      select_object: shipped
      prompt: examine_shipped_requires_release_notes.txt
```
