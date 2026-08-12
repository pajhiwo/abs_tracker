<!--
Sync Impact Report
==================
Version change: (none) -> 1.0.0
Bump rationale: Initial ratification of the project constitution.

Principles defined:
  I.   Advisory, Never Diagnostic
  II.  Deterministic Core, Optional Intelligence
  III. Honest Statistics
  IV.  Health Data Is Special Category
  V.   Evidence-Backed Changes

Sections added:
  - Security, AI Integration & Scale Constraints
  - Development Workflow & Quality Gates
  - Governance

Sections removed: none (initial version).

Dependent templates and skills read this constitution at runtime; none were
modified by this update.

Deferred TODOs: none.
-->

# ABS Tracker Constitution

## Core Principles

### I. Advisory, Never Diagnostic

ABS Tracker analyses diet, breath-alcohol readings, and medication periods for people living
with auto-brewery syndrome. Every output is correlational and advisory. The system MUST NOT
present a diagnosis, prescribe or adjust medication or dosing, or claim that a food causes an
episode. Language shown to users MUST describe association ("BAC was higher when this was eaten
recently"), never causation. Any report, PDF, or prediction MUST state that it exists to inform
a conversation with a clinician, not to replace one.

Rationale: ABS is a serious medical condition, users act on what they read here, and a
correlation across a few dozen readings is not clinical evidence.

### II. Deterministic Core, Optional Intelligence

`ai/template_engine.py` is the default analysis path and MUST remain fully functional with no
API keys, no network access, and no trained model. LLM- and ML-backed features are strictly
additive: each MUST declare a deterministic fallback and MUST degrade to it on error, timeout,
missing credentials, or exhausted quota. A failure in an optional intelligence layer MUST NOT
block upload, parsing, lift scores, or report generation.

Rationale: someone checking a reading mid-episode needs an answer, not an outage.

### III. Honest Statistics

Uncertainty MUST travel with every number shown to a user. Minimum-observation thresholds MUST
be enforced, and the `low_confidence` and `always_present` flags produced by `core/analysis.py`
MUST reach the interface rather than being filtered away silently. Predictive models MUST be
scored against the naive and ridge baselines in `ml/train.py` using temporal cross-validation --
never a random split, which leaks later meals into training. Models below the configured readings
threshold MUST be labelled preliminary. No lift score, risk level, or prediction may be displayed
without its observation count.

Rationale: a confident-looking number derived from five readings is worse than no number at all.

### IV. Health Data Is Special Category

Diet logs, breath-alcohol readings, and medication histories are special-category personal data
under GDPR Article 9. Collection MUST be limited to what the analysis requires. Every user MUST
be able to export and permanently delete all of their data. Retention periods MUST be documented
and enforced in code. Personal data MUST NOT be sent to any third-party service without explicit,
feature-specific consent and prior redaction of free-text fields. Real user logs MUST NOT be
committed to the repository; `example/example_log.xlsx` is the only workbook in version control
and contains no real patient data.

Rationale: this is the most sensitive category the regulation defines, held on behalf of people
with a rare and frequently disbelieved condition.

### V. Evidence-Backed Changes

Changes to parsing, correlation, or model code MUST ship with tests, and the full suite MUST pass
before merge. `example/example_log.xlsx` is the integration fixture for real-world spreadsheet
quirks -- aggregate rows, blank padding, partially filled nutrient columns, compound dishes -- and
parser changes MUST be exercised against it, not only against synthetic workbooks. Claims that
work is complete MUST be backed by observed command output.

Rationale: the parser silently shapes every downstream result, so a regression yields plausible
wrong numbers rather than a visible error.

## Security, AI Integration & Scale Constraints

Untrusted input: meal comments, medication notes, and product names all originate in user-supplied
spreadsheets and MUST be treated as untrusted. Model output derived from them MUST NOT drive
control flow, shell execution, file writes, or database mutations.

Cost and egress control: every external AI call MUST enforce a per-user rate and spend cap, a
timeout, and redaction of free-text fields before egress. Credentials MUST be read from the
environment; secrets MUST NOT appear in the repository, in logs, or in error responses.

Request-path discipline: analysis endpoints serve concurrent users. Model training and Excel
parsing MUST NOT run synchronously inside request handlers, and MUST NOT be recomputed per
request when their inputs are unchanged. Blocking CPU-bound work MUST be moved off the event loop.

Identity and durability: any feature that retains user data across visits MUST rely on
authenticated accounts and durable storage rather than in-memory session state.

## Development Workflow & Quality Gates

Dependencies are managed exclusively with `uv`; `uv.lock` is committed, and deployments MUST
install with `--locked`. Work proceeds on numbered feature branches merged through pull requests.
Feature work follows the Spec Kit flow -- specify, clarify, plan, tasks, implement -- and plan
output MUST be checked against this constitution before implementation begins. Commits are
authored deliberately: automated tooling MUST NOT commit on a contributor's behalf without an
explicit request.

## Governance

This constitution supersedes conflicting conventions and ad-hoc practice. Amendments MUST be
proposed as a pull request stating the rationale and the intended version bump, and MUST be
recorded in the version line below.

Versioning follows semantic versioning: MAJOR for removing or redefining a principle in a
backward-incompatible way, MINOR for adding a principle or materially expanding guidance, PATCH
for clarifications and wording. Every pull request MUST verify compliance with the principles
above. Deviations MUST be justified in the pull request description, and a deviation that cannot
be justified MUST block the merge. Added complexity MUST be justified against Principle II.

**Version**: 1.0.0 | **Ratified**: 2026-08-12 | **Last Amended**: 2026-08-12
