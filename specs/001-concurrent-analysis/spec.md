# Feature Specification: Concurrent Analysis Without Waiting

**Feature Branch**: `001-concurrent-analysis`

**Created**: 2026-08-12

**Status**: Draft

**Input**: User description: "Several people must be able to analyse their logs at the same time without waiting on each other. Today one person's analysis makes the site unresponsive for everyone else, and re-opening a report or downloading the PDF silently repeats the whole computation even when nothing has changed. The application will be shared with a 3000-member ABS support group, so the number of people arriving at once is unpredictable and could spike sharply just after the announcement. It must degrade gracefully rather than fail: when an analysis cannot start immediately, the person should see where they are in the queue and roughly how long they are waiting, then receive their results when ready. Re-requesting a report or PDF with unchanged settings should return immediately instead of recomputing. A person's uploaded data must remain available to them for their whole session no matter how the application is scaled to handle load, and must be discarded when that session expires. Logs covering a year or more of daily entries must be handled without degrading the experience for others, and someone uploading an unreasonably large file should get a clear message rather than affecting anyone else."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Analyse my log while other people are using the site (Priority: P1)

Someone from the support group uploads their log and runs an analysis. At the same
moment, several other people are doing exactly the same thing. Each person's analysis
progresses on its own; nobody's page hangs, stalls, or times out because of what
someone else is doing.

**Why this priority**: This is the core defect. Today a single analysis makes the
site unresponsive for everyone, which means the application cannot be shared with a
group of any size. Fixing only this already turns an unusable shared tool into a
usable one.

**Independent Test**: Have several people (or simulated people) upload logs and start
analyses within the same few seconds. Each must receive their own correct results, and
each page must stay responsive throughout — measurable without any of the other
stories being implemented.

**Acceptance Scenarios**:

1. **Given** two people have each uploaded their own log, **When** both start an
   analysis at the same time, **Then** both receive their own correct results and
   neither is made to wait for the other to finish.
2. **Given** one person is running a long analysis, **When** another person opens the
   site and uploads a file, **Then** the second person's pages load and respond
   normally throughout.
3. **Given** several analyses are running at once, **When** each completes, **Then**
   every person receives results computed only from their own uploaded log.
4. **Given** a person is running an analysis, **When** they navigate to another page of
   the application, **Then** that page responds normally and the analysis continues.

---

### User Story 2 - Re-open a report or download the PDF without recomputing (Priority: P2)

Having run an analysis, a person goes back to the results page, switches between views,
or downloads the PDF version of the same report. Because nothing about their data or
their settings has changed, the results appear immediately rather than being calculated
again from scratch.

**Why this priority**: This is the single largest source of avoidable load. Every
avoided recomputation is capacity given back to everyone else, and it removes the most
visible frustration for the individual — waiting again for something they already
waited for. It is also required by the project's request-path discipline, which forbids
recomputing a result when its inputs have not changed.

**Independent Test**: Run an analysis, note the wait. Re-open the same report and
download its PDF without changing any setting. Both must return promptly and contain
identical content to the first run.

**Acceptance Scenarios**:

1. **Given** a person has just completed an analysis, **When** they re-open the same
   report without changing any setting, **Then** the report appears immediately and its
   content is identical to the first result.
2. **Given** a person is viewing a completed report, **When** they download the PDF of
   that report, **Then** the download starts promptly without repeating the analysis.
3. **Given** a person has downloaded the PDF once, **When** they download it a second
   time with nothing changed, **Then** the second download starts promptly and the file
   is identical.
4. **Given** a person has a completed report, **When** they change an analysis setting
   (for example the look-back window), **Then** results are recalculated and reflect the
   new setting rather than returning the earlier result.
5. **Given** a person has a completed report, **When** they upload a different log,
   **Then** results are recalculated from the new log and the earlier results are not
   shown.

---

### User Story 3 - See where I am in the queue when the site is busy (Priority: P3)

Just after the tool is announced to the support group, many people arrive at once and
the application cannot start every analysis immediately. Rather than failing, timing
out, or appearing frozen, each person is told that their analysis is queued, where they
stand in that queue, and roughly how long they can expect to wait. When their turn
comes, the analysis runs and they get their results.

**Why this priority**: Graceful degradation is what makes an unpredictable launch spike
survivable. It does not make the application faster, but it turns "the site is broken"
into "the site is busy, and I know when I'm up" — which protects trust with a patient
group who are unlikely to return after a failure.

**Independent Test**: Drive more simultaneous analyses than the application can run at
once. Every person beyond the limit must see a queued state with a position and a wait
estimate that updates, and must eventually receive correct results.

**Acceptance Scenarios**:

1. **Given** the application is already running as many analyses as it can, **When**
   another person starts an analysis, **Then** they are shown that their analysis is
   queued, their position in the queue, and an estimated wait.
2. **Given** a person is waiting in the queue, **When** analyses ahead of them finish,
   **Then** their displayed position and estimated wait update to reflect the change.
3. **Given** a person is waiting in the queue, **When** their turn arrives and the
   analysis completes, **Then** they receive their results without having to resubmit
   their log.
4. **Given** a person is waiting in the queue, **When** they choose to abandon the wait,
   **Then** they can cancel and their place is released to the people behind them.
5. **Given** the queue has reached either its maximum number of waiting people or its
   maximum acceptable wait, **When** another person starts an analysis, **Then** they
   receive a clear message explaining that the application is at capacity, a sense of
   when to return, and an invitation to try again — rather than a failure or an
   indefinite wait.
6. **Given** a person is waiting in the queue, **When** they reload the page or briefly
   lose their connection, **Then** they keep their place and see their position again on
   returning.
7. **Given** a queued person has left the site, **When** their turn arrives, **Then**
   their analysis is dropped and the capacity goes to the next person in the queue.
8. **Given** a person's queued analysis was dropped because they were absent at their
   turn, **When** they return within their session, **Then** they are told plainly what
   happened and can start the analysis again without re-uploading their log.

---

### User Story 4 - My uploaded data stays with me for my whole session (Priority: P4)

A person uploads a log, runs an analysis, reads the results, adjusts a setting, and
downloads a PDF — a sequence that may span many minutes and many separate interactions
with the site. Throughout that time their data remains theirs and remains available,
regardless of how the application has been scaled to cope with the number of people
using it. When their session ends, their data is gone.

**Why this priority**: This is the precondition for handling load at all: any way of
serving more people concurrently is unusable if a person's upload can become
unreachable partway through their visit. It also carries the privacy promise the
project makes — that data lives only for the session and is then discarded.

**Independent Test**: With the application scaled up to handle load, upload a log and
then perform a long sequence of interactions. The upload must remain available for every
one of them, and must be unreachable once the session has expired.

**Acceptance Scenarios**:

1. **Given** the application has been scaled up to handle load, **When** a person uploads
   a log and then makes many further requests over several minutes, **Then** every
   request finds their uploaded data available and never asks them to upload again.
2. **Given** a person's session has expired through inactivity, **When** they return to
   the site, **Then** their previous upload and results are no longer available and they
   are clearly told they need to upload again.
3. **Given** a person's session has expired, **When** the application handles any later
   request, **Then** the expired session's uploaded data and derived results are no
   longer retained anywhere.
4. **Given** two people are using the site at the same time, **When** each requests their
   own results, **Then** neither can see or download anything derived from the other
   person's log.
5. **Given** a person has never created an account, **When** they upload a log and run an
   analysis, **Then** the full analysis and report are available to them without being
   asked to register or sign in.

---

### User Story 5 - Very large and oversized logs are handled clearly (Priority: P5)

Someone who has logged their meals and readings every day for a year or more uploads
their file. It is much larger than a typical log, but it is analysed successfully and
without spoiling the experience of anyone else on the site. Someone who uploads a file
far beyond what the application is prepared to handle is told so straight away, in
plain language, rather than waiting through a failure.

**Why this priority**: Long-term loggers are precisely the people the analysis is most
valuable for, so their files must work. Oversized uploads are rarer, but an unbounded
one is the easiest way for a single person to spoil the site for everyone, so the
boundary must be explicit.

**Independent Test**: Upload a log covering a year of daily entries and confirm it
analyses successfully while other people's requests stay responsive. Separately, upload
a file beyond the accepted size and confirm a clear, immediate message.

**Acceptance Scenarios**:

1. **Given** a log covering a year or more of daily entries, **When** the person runs an
   analysis, **Then** it completes successfully and returns correct results.
2. **Given** a very large log is being analysed, **When** other people use the site at
   the same time, **Then** their pages remain responsive.
3. **Given** a file larger than the accepted upload size, **When** the person uploads it,
   **Then** they are told promptly and in plain language that the file is too large and
   what the limit is.
4. **Given** an oversized file has been rejected, **When** other people are using the
   site, **Then** their experience is unaffected by the rejected upload.
5. **Given** a log so large that analysing it would take an unreasonable amount of time,
   **When** the person runs an analysis, **Then** they are told what the limitation is
   rather than being left waiting indefinitely.

---

### Edge Cases

- **Session expires while queued**: A person's session ends while their analysis is
  still waiting for its turn. The queued work must be abandoned rather than run against
  data that should no longer exist, and the person must be told clearly if they return.
- **Person leaves while queued**: A person closes the tab, reloads, or loses their
  connection while waiting. They keep their place — a stray reload or a phone locking its
  screen must not cost them their turn — but if they are still absent when their turn
  arrives, the analysis is dropped and the capacity goes to the next person instead of
  being spent on results nobody is waiting for.
- **Person returns just as their turn passes**: A person comes back moments after their
  turn was given away. They must be told plainly what happened and be able to start again
  from their already-uploaded log, rather than finding a silent absence of results.
- **Same person submits twice**: A person starts an analysis, then starts another (for
  example by double-clicking or using two tabs) before the first finishes. They must not
  consume multiple places in the queue for identical work, and must not see results
  mixed between the two requests.
- **Settings changed mid-analysis**: A person changes a setting while an analysis is
  still running. The results they are eventually shown must correspond unambiguously to
  one set of settings, and it must be clear which.
- **Application restarted mid-analysis**: The application is redeployed or restarted
  while analyses are in flight. Affected people must receive a clear message and be able
  to restart their analysis, rather than waiting on results that will never arrive.
- **Unreadable or malformed log**: A file that cannot be parsed must produce a clear
  explanation quickly, must not occupy analysis capacity, and must not affect other
  people.
- **Empty or near-empty log**: A log with too few entries to support any analysis must
  produce an explanation rather than an empty or misleading report.
- **Everyone arrives at once**: Far more people arrive than the application can serve.
  Every one of them must receive either results, a queue position, or an explicit
  at-capacity message — never a hang, a timeout, or a blank error.
- **Queue estimate proves wrong**: The estimated wait shown to a queued person turns out
  to be substantially optimistic. The estimate must be corrected as it goes rather than
  counting down to zero and stalling there.

## Requirements *(mandatory)*

### Functional Requirements

#### Concurrency and responsiveness

- **FR-001**: The application MUST serve requests from one person while another person's
  analysis is running, with no person's page becoming unresponsive as a result of
  someone else's work.
- **FR-002**: The application MUST run more than one analysis at a time, up to a
  configured limit, so that people do not queue unnecessarily.
- **FR-003**: The application MUST keep the analysis of one person entirely separate from
  that of another: no person's results, report, or download may contain data derived from
  another person's log.
- **FR-004**: Pages that do not require a fresh analysis (the landing page, help content,
  and already-computed results) MUST remain responsive regardless of how many analyses
  are currently running.

#### Queueing and graceful degradation

- **FR-005**: When the application is already running its maximum number of concurrent
  analyses, it MUST queue further analysis requests rather than rejecting them or
  running them anyway.
- **FR-006**: A person whose analysis is queued MUST be shown that it is queued, their
  position in the queue, and an estimated wait time.
- **FR-007**: The displayed queue position and estimated wait MUST be updated as the
  queue advances, without the person needing to resubmit anything.
- **FR-008**: A queued analysis MUST run and deliver its results when its turn arrives,
  without the person having to re-upload their log or re-enter their settings.
- **FR-009**: A person MUST be able to cancel a queued analysis, and cancelling MUST
  release their place to the people behind them.
- **FR-010**: The application MUST treat the queue as full when either of two limits is
  reached: a maximum number of people waiting, or a maximum estimated wait for a
  newly-arriving person. Whichever limit is reached first applies.
- **FR-011**: When the queue is full, the application MUST tell the person clearly that
  it is at capacity, give them a sense of when to return, and invite them to try again —
  rather than failing, hanging, or queueing them indefinitely.
- **FR-012**: A queued analysis MUST continue to hold its place while the person is away
  from the page, so that closing a tab, reloading, or a brief loss of connection does not
  cost them their place.
- **FR-013**: When a queued analysis reaches the front of the queue, the application MUST
  check whether the person is still waiting for it. If they are not, the analysis MUST be
  discarded and the capacity given to the next person rather than spent on results nobody
  is waiting for.
- **FR-014**: If a person returns within their session to find their queued analysis was
  discarded because they were absent, the application MUST explain that plainly and allow
  them to start it again without re-uploading their log.
- **FR-015**: If a queued analysis cannot be run (because the person's session has
  expired, they cancelled, or the application restarted), the application MUST discard
  the queued work and, if the person returns, explain why no results are available.

#### Avoiding repeated computation

- **FR-016**: Re-requesting a report whose underlying log and analysis settings are
  unchanged MUST return the previously computed result rather than recomputing it.
- **FR-017**: Re-requesting a PDF whose underlying log and analysis settings are
  unchanged MUST return the previously produced document rather than regenerating it.
- **FR-018**: A returned previously computed result MUST be identical in content to what
  a fresh computation from the same log and settings would produce.
- **FR-019**: Changing any analysis setting, or uploading a different log, MUST cause
  results to be recomputed rather than a stale result being returned.
- **FR-020**: Reuse of previously computed results MUST be confined to the person who
  produced them; a result computed for one person MUST NOT be served to another person
  under any circumstances, including when two people upload identical logs.

#### Session data and privacy

- **FR-021**: A person's uploaded log and derived results MUST remain available to them
  for the whole of their session, irrespective of how the application has been scaled to
  handle load.
- **FR-022**: When a session expires, the uploaded log, all derived results, and any
  produced documents belonging to that session MUST be discarded, and MUST NOT be
  retrievable through the application afterwards. Note this is deliberately weaker than
  "unrecoverable": no storage medium guarantees erasure, and the constitution (Principle IV,
  v2.0.0) describes deletion as unlinking for exactly that reason. Promising more than the
  medium can deliver is the overstatement that principle exists to prevent.
- **FR-023**: The application MUST continue to offer full analysis without requiring an
  account, sign-in, or any identifying information.
- **FR-024**: The interface MUST state plainly how long uploaded data is kept and that it
  is discarded when the session expires.
- **FR-025**: Nothing derived from one person's log may be used to compute, populate, or
  inform another person's results.

#### Large and oversized logs

- **FR-026**: The application MUST successfully analyse logs covering at least one year of
  daily entries.
- **FR-027**: Analysing a large log MUST NOT prevent other people from receiving timely
  responses.
- **FR-028**: The application MUST enforce a maximum accepted upload size and MUST state
  that limit to the person when an upload is rejected for exceeding it.
- **FR-029**: An oversized upload MUST be rejected promptly, before it can consume
  analysis capacity or degrade responsiveness for anyone else.
- **FR-030**: A log that is within the accepted upload size but too large to analyse
  within a reasonable time MUST produce a clear explanation of the limitation rather than
  an indefinite wait.

### Key Entities

- **Session**: The anonymous, time-limited context belonging to one person's visit. Holds
  their uploaded log, their chosen settings, and everything derived from them. Has an
  expiry, after which all of its contents are discarded. Not tied to any account or
  identity.
- **Uploaded Log**: The file a person provides for analysis, belonging to exactly one
  session. Has a size, and covers some span of dates.
- **Analysis Request**: One person's request to compute results from their uploaded log
  using a particular set of settings. Has a state (waiting, running, complete, failed,
  cancelled, dropped because the person was absent at their turn), a position while
  waiting, and an estimated wait.
- **Analysis Result**: The computed output of one analysis request, belonging to the
  session that requested it. Uniquely determined by the combination of uploaded log and
  settings, which is what allows an unchanged re-request to be answered without
  recomputing.
- **Report Document**: A downloadable rendering of an analysis result, belonging to the
  same session and likewise determined by the log and settings it was produced from.
- **Analysis Queue**: The ordered set of analysis requests waiting for capacity. Bounded
  three ways: how many analyses may run at once, how many people may wait, and how long a
  newly-arriving person may be told to wait before being turned away instead.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: When 50 people submit an analysis within the same minute, every one of them
  receives either results or an acknowledged queue position within 5 seconds of
  submitting.
- **SC-002**: While an analysis is running, other people's non-analysis pages respond in
  under 2 seconds, and no page fails to load.
- **SC-003**: Re-requesting a report or PDF with an unchanged log and unchanged settings
  returns in under 2 seconds and produces content identical to the first request.
- **SC-004**: Across a session in which a person views their report and downloads the PDF
  repeatedly, the analysis is computed exactly once for each distinct combination of log
  and settings.
- **SC-005**: A burst of 200 people arriving within 10 minutes produces zero failed or
  dropped requests: every person receives results, a queue position, or an explicit
  at-capacity message.
- **SC-006**: A log covering 12 months of daily entries completes analysis within 60
  seconds, and while it runs, other people's response times increase by no more than 20%.
- **SC-007**: Under simultaneous use by multiple people, zero instances occur of a person
  seeing, downloading, or receiving results derived from another person's log.
- **SC-008**: With the application scaled up to handle load, zero people are asked to
  re-upload their log during an unexpired session.
- **SC-009**: After a session expires, none of that session's uploaded data, results, or
  documents can be retrieved by any request.
- **SC-010**: A person can complete the full journey — upload, analyse, read the report,
  download the PDF — without creating an account or providing identifying information.
- **SC-011**: An upload exceeding the accepted size is rejected within 5 seconds with a
  message stating the limit, and other people's response times are unaffected while it is
  rejected.
- **SC-012**: For a queued person, the estimated wait shown is within 50% of the wait they
  actually experience, in at least 80% of cases.
- **SC-013**: A queued person who reloads the page or is briefly disconnected keeps their
  place in 100% of cases.
- **SC-014**: When a queued person has left the site, no analysis capacity is spent on
  their request; their turn passes to the next person within 10 seconds of being reached.

## Assumptions

- **Session lifetime**: A session expires after 30 minutes of inactivity, matching the
  application's existing behaviour. This is assumed adequate for reading a report and
  downloading a PDF; it can be revisited if people report losing their data mid-visit.
- **Maximum upload size**: 10 MB is assumed as the accepted limit, which comfortably
  exceeds several years of daily entries in the spreadsheet format the application
  accepts, while bounding what any single person can submit.
- **What "a year of daily entries" means**: Roughly 4,000 meal rows and 2,000 readings,
  extrapolated from the pattern in the example workbook bundled with the application.
- **Queue limits**: A maximum of 50 people waiting and a maximum estimated wait of 5
  minutes are assumed as the starting values for the two caps in FR-010. Both are working
  defaults to be tuned against real behaviour; the requirement is that both caps exist,
  not that these particular numbers are correct.
- **Judging whether a queued person is still waiting**: The application is assumed to be
  able to tell that a person is still interested in their queued analysis while their page
  is open, and to treat a person as absent after a short grace period with no sign of
  them. The length of that grace period is a tuning decision, not a requirement.
- **Peak concurrency is unknown**: The 3,000-member group size is an upper bound on
  interest, not a prediction of simultaneous use. The targets above (50 simultaneous
  submissions, 200 arrivals in 10 minutes) are working design points chosen to be
  survivable; the requirement is graceful degradation beyond them, not a guaranteed
  capacity.
- **Deterministic results**: An analysis result is fully determined by the uploaded log
  and the chosen settings, which is what makes it safe to return an unchanged re-request
  without recomputing.
- **Anonymous use remains the only required path**: Accounts, sign-in, and any
  persistence of data beyond the session are out of scope for this feature.
- **No cross-user data**: Pooling or comparing data between people is out of scope, so no
  analysis needs data from more than one session.
- **AI-assisted analysis is out of scope**: This feature concerns the existing statistical
  analysis only; later AI-based interpretation is expected to reuse the same queueing and
  result-reuse behaviour but is not specified here.
- **Deployment environment**: The application will move off its current free hosting tier
  before the group announcement, so the targets above assume an environment with
  sufficient resources rather than the current one.
