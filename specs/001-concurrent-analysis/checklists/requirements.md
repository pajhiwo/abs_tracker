# Specification Quality Checklist: Concurrent Analysis Without Waiting

**Purpose**: Validate specification completeness and quality before proceeding to planning
**Created**: 2026-08-12
**Feature**: [spec.md](../spec.md)

## Content Quality

- [x] No implementation details (languages, frameworks, APIs)
- [x] Focused on user value and business needs
- [x] Written for non-technical stakeholders
- [x] All mandatory sections completed

## Requirement Completeness

- [x] No [NEEDS CLARIFICATION] markers remain
- [x] Requirements are testable and unambiguous
- [x] Success criteria are measurable
- [x] Success criteria are technology-agnostic (no implementation details)
- [x] All acceptance scenarios are defined
- [x] Edge cases are identified
- [x] Scope is clearly bounded
- [x] Dependencies and assumptions identified

## Feature Readiness

- [x] All functional requirements have clear acceptance criteria
- [x] User scenarios cover primary flows
- [x] Feature meets measurable outcomes defined in Success Criteria
- [x] No implementation details leak into specification

## Constitution Alignment

Checked against `.specify/memory/constitution.md` v2.0.0:

- [x] **I. Advisory, Never Diagnostic** — not engaged; this feature changes no analytical
      output or wording.
- [x] **II. Deterministic Core, Optional Intelligence** — request-path discipline is made
      explicit in FR-016 through FR-020 (no recomputation when inputs are unchanged) and
      FR-001 through FR-004 (one person's work never blocks another's).
- [x] **III. Honest Statistics** — FR-018 requires reused results to be identical to
      freshly computed ones, so reuse cannot silently change what a person is told.
- [x] **IV. Health Data Is Special Category** — FR-021 through FR-025 cover session-scoped
      retention, discard on expiry, disclosure, and the prohibition on any cross-person
      data flow. Re-checked against v2.0.0, separating current compliance from future duty:
      **today** the implementation satisfies FR-024 as written — the interface describes
      actual behaviour (memory-only retention, the upload path's temporary file, an upper
      bound rather than a guarantee, early discard under capacity pressure), with the window
      substituted from `SESSION_TTL` on the only route that serves the page. Session-scoped
      disk is permitted by v2.0.0 but is **not implemented**, so correctly it is not
      disclosed. **When it ships**, R12 obliges a reworded disclosure covering disk storage
      and deletion at session end; that is a future duty, not an open gap in FR-024 now.
      **Scope caveat**: FR-024 asks only for a
      retention statement. A GDPR Article 13 privacy notice for special-category data is a
      separate and larger obligation that does not exist yet, and no requirement in this spec
      covers it.
- [x] **V. Evidence-Backed Changes** — SC-001 through SC-014 give the measurable targets
      that any implementation must be demonstrated against.
- [x] **VI. Anonymous Use Is a First-Class Path** — FR-023 and SC-010 keep the full
      journey available with no account.

**Numbering note**: this section originally cited pre-renumbering identifiers, so every
reference above was off by roughly four after iteration 2's insertions. Corrected in
iteration 3 against the spec as it now stands (FR-001 to FR-030, SC-001 to SC-014).

## Iteration Log

**Iteration 1 (2026-08-12)**

Two items failed and were fixed in place:

- *No implementation details* — FR-017 and SC-008 described scaling as "application
  processes or instances", and the Assumptions section referred to the "workbook format".
  Reworded to "scaled up to handle load" and "the spreadsheet format the application
  accepts".
- *Success criteria are technology-agnostic* — same SC-008 wording; resolved by the above.

One item remained open and was raised with the user as Q1 and Q2:

- *No [NEEDS CLARIFICATION] markers remain* — 2 markers concerned queue-limit policy and
  whether a queued person may leave the page. Neither had a defensible default: the first
  determines what the at-capacity message can promise, the second determines whether the
  queue is a background facility or tied to a live page.

**Iteration 2 (2026-08-12)**

Both clarifications resolved by the user; all checklist items now pass.

- *Q1 — queue limit*: both a cap on waiting people and a cap on estimated wait, whichever
  is reached first. Written up as FR-010 and FR-011, with starting values recorded as an
  assumption rather than a requirement.
- *Q2 — leaving the page while queued*: keep the place while away, but drop the analysis
  if the person is still absent when their turn arrives. Written up as FR-012 through
  FR-014, with two new edge cases, four new acceptance scenarios on User Story 3, and
  SC-013/SC-014.

Requirements after FR-011 were renumbered to accommodate the insertions; the spec now
runs FR-001 to FR-030 and SC-001 to SC-014 with no gaps or duplicates.

**Iteration 3 (2026-08-13) — external adversarial review of the plan**

`plan.md`, `research.md`, `data-model.md` and the API contract were reviewed by two
independent models. The spec itself survived, but several Phase 0 conclusions did not, and
two premises the spec leaned on turned out to be false:

- **Retention claim was unachievable.** The framework spools uploads above 1 MiB to a
  temporary file before any handler runs, so "nothing on disk" could never have been true —
  and the constitution permitted Redis in the same sentence, which persists. FR-024's
  disclosure duty therefore cannot be satisfied by the wording the spec assumed. Resolved by
  the user sanctioning session-scoped disk with deletion at session end (R12), which requires
  a Principle IV amendment rather than a code fix.
- **FR-021 conflicts with current behaviour.** `InMemoryStore` evicts the oldest live session
  at 100, which destroys in-progress work at the spec's own 200-arrival design point. A
  correctness bug rather than a tuning value (R10).
- **Four endpoints depend on data the plan proposed to discard** (R4a), so the storage
  reduction needed an alternative data source, not deletion.
- **The likely bottleneck was misidentified.** Both reviewers rated a nested `iterrows` loop
  in `map_lookback` above model training as the dominant cost. Measurement now gates the
  executor design (R8a), and this bears directly on SC-006.

Two design decisions were also settled in the user's favour and recorded: Redis is not
adopted, with SQLite backing the queue instead, and the scaling path runs through added
workers on one machine rather than added machines.

No spec requirement was found to be wrong, so no requirement text changed in this iteration.
Checklist items all still pass; the corrections are to the plan and to the numbering above.

**Iteration 4 (2026-08-13) — constitution v2.0.0 and Redis removal**

Both actions the review recommended were approved and carried out:

- **Constitution amended to v2.0.0.** Principle IV's retention paragraph now permits
  session-scoped disk with deletion at session end, a startup sweep for data orphaned by an
  unclean shutdown, no backups or logs, and deletion described as unlinking rather than
  erasure. MAJOR rather than MINOR because the same amendment withdrew Redis as a sanctioned
  storage location: a plan that passed the Principle IV gate under v1.1.0 now fails it.
  Governance was clarified in the same edit to state that narrowing what a principle permits is
  MAJOR, and that code compliance is evidence of a change rather than grounds for a bump.
- **`RedisStore` deleted**, with the `REDIS_URL` branch, the `redis` optional dependency and
  the README instruction. Lock file regenerated; the suite still passes, now at 120 passing of 121 collected.

**Exposed by the amendment, then fixed**: the interface carried no retention disclosure
whatsoever. Under v1.1.0 that was a quiet omission; under v2.0.0, which requires retention to
be stated accurately *and* to be one the implementation can honour, it was an explicit breach.

FR-024 was implemented in the same session. Three choices are worth recording:

- **It describes today's behaviour, not what the constitution now permits.** The app does not
  yet write session data to disk, so the disclosure does not say it does. Disclosing sanctioned
  but unimplemented behaviour would be its own inaccuracy.
- **The retention window is substituted server-side from `SESSION_TTL`** rather than hardcoded,
  so an operator changing the environment variable cannot silently make the page lie. Sub-minute
  values round *up*, so the page never claims data is gone sooner than it is.
- **The disclosure ships in HTML, not injected by JavaScript**, because a notice that depends on
  a script running is not a disclosure.

It admits what earlier wording denied: uploads are briefly written to a temp file during
parsing, larger uploads are spooled to disk by the web server first, and removal means unlinking
rather than erasure. It also notes that the charting library loads from a public CDN, which sees
the visitor's IP. `tests/test_retention_disclosure.py` guards against drift and against
reintroducing overstated phrases.

**Obligation carried forward**: the wording is accurate only while sessions are memory-only. If
R12's disk-backed sessions ship, the disclosure must be revised in the same change.

**Iteration 5 (2026-08-13) — second external review round, two models**

The amendment and the disclosure were re-reviewed independently. Findings accepted and acted on:

- **The disclosure overstated three things.** It said "nothing here identifies you" while a
  session cookie and the CDN's view of the visitor's IP both contradict that; it implied data
  survives the full window when capacity eviction can discard it sooner; and it claimed
  restarting "erases everything immediately" when temp files are created with `delete=False`
  and unlinked in a `finally` block, so a crash between those points orphans a copy. All three
  are now stated accurately, with tests pinning each.
- **The essential facts were hidden behind a click.** A collapsed `<details>` satisfies "state
  retention wherever data is uploaded" only for whoever expands it, so an always-visible summary
  now carries retention, temp-file use, no pooling, and no egress.
- **FR-022 promised more than any medium delivers** ("MUST NOT be recoverable"), contradicting
  the constitution's own unlinking language. Reworded to "not retrievable through the
  application".
- **R2 still opened with a settled executor decision** while later text made it contingent.
  Now marked unresolved pending R8a.
- **The SQLite queue was hand-waved.** R5a now specifies the pragmas, the single-statement
  atomic claim, `BEGIN IMMEDIATE`, `SQLITE_BUSY` retry surfacing as `503`, lease reclaim as the
  crash path, and — the finding that mattered most — that `sqlite3` blocks, so queue calls from
  handlers violate the constitution's own request-path rule unless offloaded.
- **Rung 2 would have oversubscribed the CPU.** N workers × M pool processes was unaddressed;
  the global limit now belongs to the queue, capping leased jobs across processes.
- **Stale line citations** across all documents, refreshed again.

**Iteration 6 (2026-08-13) — reconciling the second reviewer**

The two reviews were run in parallel against the same tree, so much of the second overlapped
with Iteration 5. Four findings were genuinely new and one forced a change of policy:

- **The page was reachable unrendered.** `index.html` sat inside the directory mounted at
  `/static`, so `GET /static/index.html` returned a fully working upload UI whose retention
  window had never been substituted. The earlier fix — removing the number from the source file
  so a stale figure could not be served — traded a wrong figure for no figure, which fails
  FR-024's "state plainly how long" on a reachable page. The template now lives in
  `app/templates/`, outside the mount, so `GET /` is the only route that serves it and no path
  can bypass substitution. Pinned by a test asserting `/static/index.html` returns `404`.
- **Polling is a write path, not a read path.** The claim that "roughly 50 reads/second is
  nothing for WAL-mode SQLite" counted the wrong operation: if presence is recorded by updating
  `last_seen_at` per poll, every poll is a write, and SQLite serialises writers regardless of
  WAL. This is the finding most likely to have made the queue the new bottleneck. R5b now
  requires presence to be debounced or read from the session store instead, and the load check
  counts writes and `SQLITE_BUSY` rather than assuming.
- **The MAJOR bump was argued from the wrong premise.** The Sync Impact Report justified 2.0.0
  by the code that had to be deleted. Whether code complies is evidence of a change, not grounds
  for a version bump. The bump stands, on the correct premise: a plan proposing Redis passed the
  Principle IV gate under v1.1.0 and fails it now. The reviewer also noted that v1.1.0
  introduced the disk prohibition while bumping MINOR and claiming nothing was redefined
  incompatibly — under-bumped by this same standard. That history is left as recorded but noted
  in the constitution so it is not treated as precedent, and Governance now states the test
  explicitly.
- **`partial` was missing from the job status enum** while the staged flow it describes depends
  on it, and the `POST /upload` summary omitted parsing, contradicting R11. Both corrected.
- **Line citations, third failure — policy changed.** Rather than retarget them a third time,
  `app/main.py` and `ml/train.py` references now name symbols. The churn was self-inflicted and
  consumed review attention that should have gone to design.

Also corrected: R1 still described `summary` as computed from `bac_df` alone after later text had
corrected it; `data-model.md` understated uploaded-byte retention by omitting the application's
own temp file and `session.raw_bytes`; two stale rows in `plan.md` claimed the retention
disclosure was both resolved and outstanding; the constitution's follow-up bullet still said the
disclosure reflected withdrawn wording; the measurement check insisted it be run first while
numbered last; and the test count had drifted.

Findings recorded but *not* acted on:

- **No GDPR Article 13 privacy notice exists.** Correct, and larger than FR-024. It needs the
  operator's own decisions (controller identity, lawful basis, Article 9 condition) and warrants
  its own specification before the tool is announced.
- **"Trigger" remains as UI vocabulary** despite being mildly causal, because it is the
  established term in the ABS community and replacing it would cost more in comprehension than
  it gains in precision. Recorded as a deliberate choice rather than an oversight.
- **Capacity eviction is disclosed rather than removed.** One reviewer offered the alternative of
  not evicting live sessions at all before claiming a fixed window. R10 already commits to
  refusing new sessions instead of evicting live ones, which will make the caveat unnecessary —
  but that work has not landed, so the disclosure describes eviction as it exists today. The
  caveat should be removed when R10 ships, not before.

## Notes

- Items marked incomplete require spec updates before `/speckit-clarify` or `/speckit-plan`.
- The concurrency targets in SC-001, SC-005 and SC-006 are working design points, not
  commitments derived from measured demand — see Assumptions. They should be revisited
  once real usage data from the support group is available.
- SC-006's 60-second target is the one most at risk, because it depends entirely on the
  R8a measurement. If the `iterrows` loop dominates as both reviewers expect, vectorising it
  should clear the target comfortably; if training dominates, 60 seconds for a year-scale log
  may not be reachable and the criterion needs revisiting rather than quietly missing.
