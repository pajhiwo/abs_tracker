<!--
Sync Impact Report
==================
Version change: 1.0.0 -> 1.1.0
Bump rationale: MINOR -- one principle added and two materially expanded. No
principle was removed or redefined in a backward-incompatible way.

Principles added:
  VI.  Anonymous Use Is a First-Class Path

Principles modified:
  II.  Deterministic Core, Optional Intelligence
       Added priority ordering (normalisation before narrative reporting),
       a persistence/inspectability requirement for AI-derived mappings, and
       the rule that the unit of work is one unseen source string.
  IV.  Health Data Is Special Category
       Replaced the aspirational retention sentence with the actual anonymous
       retention behaviour (SESSION_TTL, memory/Redis, never disk) plus a
       disclosure obligation, and scoped cross-user pooling out with an
       explicit opt-in path for introducing it later.

Principles unchanged:
  I.   Advisory, Never Diagnostic
  III. Honest Statistics
  V.   Evidence-Backed Changes

Sections modified:
  - Security, AI Integration & Scale Constraints
    Added a derived-data caching clause limiting AI-derived mappings to
    product and ingredient name fields.

Sections removed: none.

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

Intelligence features are prioritised by their effect on the underlying statistics, not by how
impressive the output reads. Ingredient normalisation and fermentable-carbohydrate categorisation
come first, because collapsing name variants and deriving dense category features improves every
lift score already computed; narrative report generation comes second. AI-derived mappings
(canonical ingredient name, category assignment) MUST be persisted, keyed by the exact source
string, and MUST be inspectable and correctable by a human. The unit of work is one previously
unseen ingredient string, never one analysis request. The raw source string MUST remain the
fallback whenever a mapping is absent.

Rationale: someone checking a reading mid-episode needs an answer, not an outage -- and a
fabricated narrative is more dangerous than a plain table.

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
under GDPR Article 9. Collection MUST be limited to what the analysis requires.

Retention MUST be stated accurately wherever a user uploads data. Anonymous use holds the
uploaded workbook and its parsed derivatives in process memory, or in Redis when configured, for
at most the configured session lifetime (`SESSION_TTL`, 30 minutes by default); nothing is written
to disk, and the interface MUST disclose this rather than claiming that no data is retained. If an
authenticated storage path is added, users MUST be able to export and permanently delete all of
their data, and retention MUST be documented and enforced in code.

Personal data MUST NOT be sent to any third-party service without explicit, feature-specific
consent and prior redaction of free-text fields. Pooling or aggregating data across users is out
of scope; introducing it MUST require its own specification, explicit opt-in consent, and
documented anonymisation, and it MUST NOT be enabled by default. Real user logs MUST NOT be
committed to the repository; `example/example_log.xlsx` is the only workbook in version control
and contains no real patient data.

Rationale: this is the most sensitive category the regulation defines, held on behalf of people
with a rare and frequently disbelieved condition. A promise that overstates privacy is worse than
an accurate one.

### V. Evidence-Backed Changes

Changes to parsing, correlation, or model code MUST ship with tests, and the full suite MUST pass
before merge. `example/example_log.xlsx` is the integration fixture for real-world spreadsheet
quirks -- aggregate rows, blank padding, partially filled nutrient columns, compound dishes -- and
parser changes MUST be exercised against it, not only against synthetic workbooks. Claims that
work is complete MUST be backed by observed command output.

Rationale: the parser silently shapes every downstream result, so a regression yields plausible
wrong numbers rather than a visible error.

### VI. Anonymous Use Is a First-Class Path

Uploading a workbook and receiving a full analysis MUST work without an account, and MUST
continue to work for every feature that reaches the interface. Authentication and persistence, if
added, are strictly opt-in conveniences: no analysis capability, report, or export may be gated
behind creating an account. Features MUST NOT assume a durable user identity.

Rationale: users of this application are disclosing a stigmatised medical condition. Requiring
them to create an account before they can learn anything would exclude exactly the people the
tool exists to help.

## Security, AI Integration & Scale Constraints

Untrusted input: meal comments, medication notes, and product names all originate in user-supplied
spreadsheets and MUST be treated as untrusted. Model output derived from them MUST NOT drive
control flow, shell execution, file writes, or database mutations.

Cost and egress control: every external AI call MUST enforce a per-user rate and spend cap, a
timeout, and redaction of free-text fields before egress. Credentials MUST be read from the
environment; secrets MUST NOT appear in the repository, in logs, or in error responses.

Derived-data caching: AI-derived mappings MUST be limited to product and ingredient name fields.
Comments and medication notes MUST NOT be cached or shared across users, because free text can
carry personal narrative.

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

**Version**: 1.1.0 | **Ratified**: 2026-08-12 | **Last Amended**: 2026-08-12
