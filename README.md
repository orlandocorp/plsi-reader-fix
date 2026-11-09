# PLSI: Personal Longevity State Interface

PLSI turns any health record into a deidentified, code-normalized event log, projects it through versioned **State Maps** into a hashed state document, and lets Grok Bot propose screened actions with **explicit, scored next-state predictions**, including aging clocks.

On Grok Bot, PLSI is a team of Bots with separate chats, memories and contexts: **PLSI** (the orchestrator the user talks to), a core team that is always created (Data Processor, Health Consultant, Nostradamus, Challenger, Reviewer, Data Controller, Memory Hole, Visualizer) and the specialists the user picks (Wearables, Nutritionist, Coach, Sleep, Biohacker, Citizen Researcher, Research Autopilot, Explorer, Navigator, Trial Scout, Pharma Analyst, Engineer, Writer). A longevity physician also gets the section **PLSI Clinic** (Panel, Visit Prep, Clinical Evidence, Liaison) and a pro researcher the section **PLSI Lab** (Target Hunter, Landscape, Cohort Analyst, Grant Writer), chosen by role (`plsi role set`). The workers sit in the collapsible sidebar section **PLSI Workers** (the user creates it; PLSI places each new bot in it), the sections in **PLSI Clinic** and **PLSI Lab**; the roster is `skills/shared/team`. This repo supplies:

- State Map contracts (`contracts/*.schema.json`, `maps/*.yaml`)
- a deterministic CLI toolbelt (`plsi`, JSON in, one JSON envelope out)
- skills the bots follow (`skills/`; `shared/team` assigns them), a first-run file (`plsi.md`) and a versioned persona (`persona.md`)
- the handoff protocol between the bots (`contracts/Handoff.schema.json`, `plsi handoff check`)
- trusted references (`mothership/references/<kind>.yaml`, indexed by id; `plsi refs list [--kind]`)
- opt-in sharing with the Longevity Mothership, Insilico's longevity research initiative (`plsi mothership`,
  `mothership/consent.md`): a weekly coded (pseudonymous, not anonymous) package of coded fields only (codes, numbers, units, months, years;
  no free text, no value computed from age; the person only as sex, age band and ethnicity group), gate-checked, posted to Insilico's relay only after the user joins;
  registry updates; research notes only by the user's own Grok email, signed by their address; for invited
  physicians and researchers, the data catalog, releases and the findings ledger
- usage reports for the PLSI team (`plsi feedback`): setup stages timed as they run, frustrations and suggested
  improvements, sent only on the user's approval from their own Grok email, with no join
- autoresearch (`<PLSI_HOME>/research/<topic>/`, `plsi research`): one folder per topic with questions, hypotheses
  and findings as linked Markdown (opens as an Obsidian vault), a cursor and an append-only ledger. An optional
  daily routine works the active topic one move at a time inside the share of the weekly meter the user sets; code
  computes every verdict, and PLSI Visualizer draws a topic's dashboard from `plsi research status --full`
- target discovery for researchers (`<PLSI_HOME>/targets/<gene>/`, `plsi targets`): dossiers gathered from free
  sources, an assessment refused unless every claim is sourced, a dashboard drawn by PLSI Visualizer, and an
  evidence-based target file that can go to the Longevity Mothership as a research note
- local state storage (`instances/<name>/`)

## Disclaimer

Longevity Grok is AI software for personal longevity research and education. It does not provide medical care, does
not diagnose, treat or prescribe, and does not replace your clinician. Outputs are generated automatically, are not
reviewed by any clinician, scientist or team member before you see them, and may be wrong. Automated checks do not
remove that risk, and no health or longevity outcome is promised. Individual academic affiliations are shown for
identification and do not imply institutional endorsement. Talk to your clinician before starting, stopping or
changing any medication, supplement or health routine, and do not delay care because of an output. In an emergency,
contact local emergency services.

By installing, you agree to the [Terms of Service](legal/TERMS.md); see also the [Privacy Policy](legal/PRIVACY.md)
and the [Disclaimer](legal/DISCLAIMER.md). xAI processes the information in your Grok Bot account under its own
terms and privacy policy, and services you connect may also receive information. Insilico receives information from
that account only through contributions or emails you choose to send.

## Triad and layering

| Layer | Package | Owns |
|---|---|---|
| Life | `plsi.life` | intake (FHIR, canonical CSV, methylation, Grok-extracted JSON), LOINC/RxNorm/UCUM normalization, the append-only event log |
| Map | `plsi.map` | registry, selector, mapper (projection, units), storyline; derived coordinates (native blood PhenoAge, map formulas such as BMI and HOMA-IR, biolearn clocks); state documents |
| Policy | `plsi.policy` | evidence search and verification, blocking screening, plan gates, scoring and calibration, oracles |

Rules: `plsi.policy` never imports `plsi.life` and reads the world only through `plsi.map`; `plsi.life` never imports `plsi.map` or `plsi.policy`. The triad keeps a fully local deployment possible.

## The team, the protocol and the toolbelt

The bots do the judgment; `plsi` does what an agent should not: calculators (clocks, risk equations, unit
conversion), connectors (PubMed, ClinicalTrials.gov, openFDA, RxNav, Open Targets, MyGene.info, Pharos, RCSB PDB,
AlphaFold, ChEMBL, UniProt, Open Genes, oracles), the append-only log, the code gates and the scorer. Bot
folders and Python layers are separate axes. The Map is the protocol between the bots: Data Processor records and
projects state documents, the other bots cite them by hash, and every handoff is a `Handoff` message checked by
`plsi handoff check`.

```
                     user
                      |  (1:1 chat, question cards, approvals, routines)
                    PLSI  -- orchestrator: conducts threads, batches questions, urgent care
                      |      every handoff: Handoff JSON; cited states must be current
                      |      request/answer out, result/question back
   +------------------+--------------------------------------------+
   | PLSI Workers                                                  |
   | core: Data Processor, Health Consultant, Nostradamus,         |
   |       Challenger, Reviewer, Data Controller, Memory Hole,     |
   |       Visualizer                                              |
   | specialists (opt-in): Wearables, Nutritionist, Coach, Sleep,  |
   |       Biohacker, Citizen Researcher, Research Autopilot,      |
   |       Explorer, Navigator, Trial Scout, Pharma Analyst,       |
   |       Engineer, Writer                                        |
   +---------------------------------------------------------------+
   | PLSI Clinic (physician): Panel, Visit Prep, Clinical          |
   |       Evidence, Liaison                                       |
   | PLSI Lab (researcher): Target Hunter, Landscape, Cohort       |
   |       Analyst, Grant Writer                                   |
   +---------------------------------------------------------------+
   Life: ingest ---> Map: project / state show ---> Policy: screen, plan gates, score (referee)
          ^                                                     |
          +----------------- new measurements <-----------------+
```

- **Separate contexts.** Only Data Processor (and Wearables and Nutritionist for device and food-app exports) opens
  records; every other bot sees the world only through published state documents and the care context. PLSI saves
  attachments unopened and hands the owner the path.
- **Adversarial check.** Every plan goes to Challenger (`challenger/challenge`) before the user sees it; Nostradamus
  revises or rebuts once; the user sees both sides. Screening findings can only raise the label floor.
- **Propose, explore, then plan.** Domain specialists propose dosed options and the finders return ranked options;
  neither plans. Nostradamus merges what the user picks, and new insights from the digest, into the one plan; Reviewer
  reviews it once adopted.
- **Egress.** Anything leaving the computer passes the code gate, then Data Controller (`controller/egress`).
- **Opt-in specialists.** A specialist not on the team means its capability is off; PLSI offers to add it.
- **Daily research, metered.** With Research Autopilot, the user may give research a share of the weekly Grok limit
  per day; on each run of the "PLSI research" routine the Autopilot reads this account's meter, keeps at least 10% of
  the week for the user and stops at the share. Challenger (Note mode) decides which notes are worth sending.
- **No self-grading.** Predictions are scored by `plsi score`, not by the bot that made them.
- **Calculators, not mental arithmetic.** `plsi calc convert` and `plsi calc derive` (what-if clocks and scores from
  the published state) give every number a bot predicts or reports; absolute risks start from `base_rate` references.
  `plsi calc course` draws a measurement toward a cited steady state, and `plsi calc survival` turns a life table (the
  bundled UN WPP 2024 snapshot, CC BY 3.0 IGO, or a table from a `base_rate` reference) and cited hazard ratios into
  survival curves and expected lifespan per scenario.
- **One instance per person** (`plsi init --relation self|family|other|sample`, `plsi instances list`); every handoff
  names one. `plsi export` saves an instance as one JSON bundle or CSV tables.
- **Fallback.** Without Bot creation, PLSI plays the core roles and the chosen specialists as separate passes.

Skills live in one folder per bot (`skills/<role>/<skill>/`); a bot uses only its own folder and `skills/shared/`.

| Skill | Bot | Drives |
|---|---|---|
| shared/team | all | roster, ownership, handoff format and flows |
| orchestrator/conduct | PLSI | thread ledger: dispatch, parallel rows, wait, collect, verify, re-prompt, chain; one card per decision |
| orchestrator/urgent | PLSI | red flags and out-of-scope requests: send to care at once, pause and resume threads |
| orchestrator/arrow | PLSI (relays to processor/history and nostradamus/risks) | Life (story -> events), Map (`project`), Policy (risks, predictions) -> `report` timeline |
| orchestrator/physicians | PLSI (relays to navigator/physicians) | `status`, `export`, `due` -> care team, visit summary, questions; approved messages only |
| orchestrator/nudge | PLSI | one reminders file and a one-time routine per reminder: follow-ups, windows opening, plan starts; the mid-plan adherence check |
| orchestrator/talk | PLSI (relays to processor/history) | the first "about you" talk and the weekly talk, voice or text -> the user's words recorded by Data Processor |
| orchestrator/answer | PLSI | status, due, records, threads, approvals, keys answered without a handoff |
| orchestrator/research | PLSI (relays to researcher/research) | topic and PandaOmics Agent budget cards -> research note or target file shown with what next; daily research on spare usage (the "PLSI research" routine); sharing via explorer/mothership and the user's Grok email |
| orchestrator/mothership | PLSI (relays to controller/egress) | `mothership join/status/leave` -> consent, one-time join code, weekly deidentified package via the relay, stop and delete |
| orchestrator/feedback | PLSI (relays to controller/egress) | `feedback timeline/report` -> a dated usage report (frustrations, time per setup stage, suggested improvements), sent to the PLSI team from the user's Grok email on approval; no join |
| processor/intake | PLSI Data Processor | Life: `ingest`, `normalize`, `retract`, `revise` (a new version of a document); adherence and side effects; lifestyle habits and reported amounts -> `lifestyle_context`; questionnaire totals -> `wellbeing`; body fat and grip -> `body_composition` |
| processor/history | PLSI Data Processor | life story -> dated events, `ingest`, `project` -> past and present for the arrow |
| processor/add-map | PLSI Data Processor | a quantity no map has -> LOINC-coded user map, `maps validate --live`, `maps install`, `project` |
| processor/add-score | PLSI Data Processor | Extend the triad with a formula (below) |
| consultant/assess | PLSI Health Consultant | Map: `maps select`, `project`, `maps install` -> states, changes, measurement requests |
| nostradamus/plan | PLSI Nostradamus | specialists' proposals -> Policy: `evidence`, `screen --findings`, `plan check/record/adopt` on the published state |
| nostradamus/trajectory | PLSI Nostradamus | `status`, `state show`, `calc derive/course/survival`, `refs list` -> current course vs with the plan, with intervals; or regimens vs a baseline for markers, weight and expected lifespan (Scenarios) |
| nostradamus/risks | PLSI Nostradamus | `state show`, `refs list`, `evidence` -> 3-7 future events with sourced absolute risks |
| challenger/challenge | PLSI Challenger | Map (`state show`, `project`), Life (`events list`) -> objections; Claim mode: `mothership analyze` rerun -> objections to a research claim; Note mode: worthy / not yet / not worth for a research note or a target file's candidates |
| reviewer/review | PLSI Reviewer | adherence and side effects, Map (`state show`), Policy (`screen`, `score`, `plan review`) |
| controller/egress | PLSI Data Controller | `mothership check` and the deid gate, then judgment that only adds rejections -> cleared or rejected payloads (packages, notes, professional messages, oracle asks, physician and doctor-patient messages); `mothership pack/send` |
| memoryhole/forget | PLSI Memory Hole | `export` first, then `instances delete --confirm` on the user's explicit words |
| memoryhole/prune | PLSI Memory Hole | research notes marked retire -> `research retire` (each removal logged in research/notes/retired.md) |
| visualizer/dashboard | PLSI Visualizer | `report --payload`: custom dashboards (trajectory brief, state now, plan review, life arrow, clinician brief, panel, research topic, target dossier and comparison) |
| visualizer/charts | PLSI Visualizer | `export`, `status`, `due` -> charts designed per question, with uncertainty and legends |
| wearables/import | PLSI Wearables | Open Wearables, Apple Health exports (`apple export`, optional local `--provenance` sidecar), saved Oura API v2 pages (`oura export`, offline) or device exports -> calendar-month means (sleep, heart rate, HRV SDNN and RMSSD, steps, exercise minutes, active days) and VO2max readings, `normalize`, `ingest --csv`, `sleep_activity` map |
| wearables/google-health | PLSI Wearables | Google Health API OAuth (`refs show google_health`) -> daily rows -> wearables/import |
| wearables/apple-health | PLSI Wearables | P.R.O. iPhone app as an MCP server (`refs show pro_app`) -> daily rows -> wearables/import |
| wearables/devices | PLSI Wearables | map gaps -> validated home devices and their import path |
| nutritionist/diet (placeholder) | PLSI Nutritionist | Map (`cardiometabolic`, `care_context`, `personal_context`) -> evidence -> proposal for nostradamus/plan |
| nutritionist/food-log | PLSI Nutritionist | Google Health, FatSecret diary or app exports -> mapped coordinates only -> processor/intake |
| coach/exercise (placeholder) | PLSI Coach | exercise doses -> proposal for nostradamus/plan |
| coach/substances | PLSI Coach | smoking and alcohol: screen and refer first, dose, predict on existing coordinates -> proposal for nostradamus/plan |
| sleep/sleep, sleep/stress | PLSI Sleep | screen and refer first, dose, predict on existing coordinates -> proposal for nostradamus/plan |
| biohacker/stack (placeholder) | PLSI Biohacker | Map (biomarker maps, `care_context`) -> web search and evidence -> ranked stacks |
| researcher/research | PLSI Citizen Researcher | `oracle`, `evidence` -> graded research notes on questions without personal data; proposes what the loop should work on next |
| autopilot/run | PLSI Research Autopilot | the unattended loop: `research next` -> one move -> `research save`, until the day's allocation is spent |
| autopilot/deep-search, autopilot/good-hypothesis | PLSI Research Autopilot | the craft behind two of the moves, read on demand: two retrieval routes, five query shapes and three kinds of silence; what a candidate has to rule out and the three grades of falsifier |
| autopilot/ask, gather, explain, test | PLSI Research Autopilot | the four moves: one answerable question or a split across angles; the five query families and findings with verbatim quotes; competing accounts each with its `if_false`; the evidence linked, the context question asked, the answer written with its citations |
| shared/research-graph | Autopilot writes; PLSI, Citizen Researcher, Challenger, Memory Hole read | the node kinds and their fields, what `research save` accepts and the eight things it refuses |
| explorer/digest | PLSI Explorer | `refs`, `evidence search/verify`, `state show` -> digest for the user, insights for Nostradamus and Reviewer |
| explorer/mothership | PLSI Explorer | research notes, target files and registry updates -> `mothership note` (cleared by Data Controller; a note sent by the user's Grok email, a registry update by the relay); reads its mail, the catalog and the findings ledger |
| navigator/physicians | PLSI Navigator | Map (`care_context`, `personal_context`) -> specialty, official directory and licence checks (`refs show us_npi`) -> ranked physicians |
| navigator/activities, navigator/clinics (placeholders) | PLSI Navigator | Map (`care_context`, `personal_context`, biomarker maps) -> web search -> ranked options |
| scout/trials | PLSI Trial Scout | `evidence search --source ctgov` -> eligibility screen, ranked studies |
| pharma/company | PLSI Pharma Analyst | filings, `evidence search --source ctgov` by sponsor, patents and exclusivity (`refs show`), market data and news -> dated, sourced company dossier |
| pharma/outlook | PLSI Pharma Analyst | the dossier -> catalyst calendar, base rates, bull/base/bear scenarios with rough probabilities (not investment advice) |
| engineer/extend | PLSI Engineer | Extend the triad with a code extension (below) |
| writer/prose (placeholder) | PLSI Writer | research notes and cited states -> deidentified report or manuscript prose |
| panel/roster | PLSI Panel | `role show`, `instances list` -> the physician's patients (the rows of `work/panel/roster.json`, each an instance created by Data Processor) and their status |
| panel/clinic | PLSI Panel | `clinic check/setup/sync/publish` (service account on a Workspace shared drive, or Grok's Drive connector) -> each patient's Drive inbox and shared folder: uploads routed by folder to processor/intake or wearables/import, cleared and approved reports to the patient |
| panel/overview | PLSI Panel | `due --all`, `status`, `state show` per patient -> who is due, out of range, off plan -> visualizer/dashboard (scope "panel") |
| visitprep/brief | PLSI Visit Prep | `status`, `export`, `due`, `events list` -> pre-visit brief for one patient -> visualizer/dashboard (clinician brief) |
| visitprep/notes | PLSI Visit Prep | the physician's visit notes -> items for processor/intake, follow-ups, plan changes, messages |
| clinev/review | PLSI Clinical Evidence | `evidence search/verify`, `refs list` -> graded, guideline-aware evidence review |
| clinev/protocol | PLSI Clinical Evidence | patient state and the physician's intent -> dosed, predicted proposals for nostradamus/plan |
| liaison/message | PLSI Liaison | doctor-patient message drafts for the known pipe -> controller/egress -> the physician's approval |
| liaison/education | PLSI Liaison | `refs list --kind education` -> longevity medicine courses and CME, patient handouts |
| liaison/mothership | PLSI Liaison | `mothership whoami/catalog/findings`, `mothership note` -> the physician's professional Mothership desk |
| targets/discover | PLSI Target Hunter | `targets unmet/discover/dossier/assess/show`, `evidence` -> a banded shortlist for a disease, a hallmark or both, written as a target file; PandaOmics only as optional enrichment |
| targets/evaluate | PLSI Target Hunter | `targets dossier/assess/trials/show`, `evidence` -> one target end to end on the free sources, with a verdict, three ratings and a falsifier, written as a target file |
| targets/landscape, targets/grounding, targets/report | PLSI Target Hunter | read on demand: harvesting every asset against a gene; what a claim must cite and what each refusal checks; the dossier file for the dashboard, and the target file the Mothership can receive |
| targets/pandaomics | PLSI Target Hunter | read on demand: using the user's own PandaOmics or PandaClaw token (capped per day, never a citation) |
| targets/hallmarks | PLSI Target Hunter | `targets hallmarks/dossier/brief`, `clocks list`, `evidence` -> a target framed against the twelve hallmarks of aging, with readouts and falsifiers |
| landscape/literature | PLSI Landscape | `evidence search/verify` -> field map: key papers, trials, open questions, contradictions |
| landscape/kol | PLSI Landscape | public publication and trial records -> key opinion leaders and groups |
| analyst/hypothesize | PLSI Cohort Analyst | `mothership whoami/catalog/findings` -> testable hypotheses with exact analysis specs |
| analyst/run | PLSI Cohort Analyst | `mothership prereg` -> Data Controller sends -> `mothership release/analyze` -> challenger/challenge (Claim mode) -> `mothership finding` |
| grants/proposal | PLSI Grant Writer | notes, findings, field maps, targets -> grant and project proposal drafts |

## Roles and the Longevity Mothership commands

| Command | Who | Does |
|---|---|---|
| `plsi role show`, `plsi role set --role <role>` | PLSI (Panel reads) | the user's role (individual, physician, researcher); it decides the section (`plsi.md` steps 6.1 and 7) |
| `plsi mothership join/status/leave` | PLSI | consent kinds `states`, `life_events`, `scores`; the one-time enrollment code; stop, or stop and delete |
| `plsi mothership receipt` | PLSI | the Mother's receipt for each sent message, by the relay ticket (`acks.jsonl`); `leave --delete` waits for them, and a deleted receipt drops the proof |
| `plsi mothership pack/check/send` | Data Controller | the weekly package; the gate on every message; the only sender (`--instance` given: that instance's install token; omitted: the professional token) |
| `plsi mothership note` | Explorer, Liaison | a research note or registry update -> its wire message, gate-checked; a note's data files checked and zipped |
| `plsi mothership email` | PLSI, Data Controller | a research note's email (to, subject, body in plain words, the message as a JSON attachment, the report as markdown and the data files as one zip), gate-checked and only for the approved `--hash`; the user's Grok email sends it (`send` refuses research notes) |
| `plsi research propose` | PLSI | candidate topics, each pointing at something in the record (a medication, a condition, a State Map gap, a moving marker); it chooses nothing |
| `plsi research discover` | PLSI, Citizen Researcher, Research Autopilot | what is well studied about a subject and where questions are still open, from PubTator3 annotations with their PMIDs; reads no store and chooses nothing |
| `plsi research topic/reframe/budget` | PLSI | create, switch or park a topic (its kind, what it rests on, how thorough it should be); reword its question and keep why; the daily share of the meter all research may spend |
| `plsi research next/save` | Research Autopilot | everything one move needs; then record the move, charge the meter and recompute the verdicts and the frontier |
| `plsi research status` | PLSI, Challenger | the answer so far with its verdict, the frontier and the budget; `--full` adds the log rows, the evidence lines and the source ids a dashboard is drawn from |
| `plsi research notes/retire` | PLSI, Challenger, Memory Hole (`retire`) | research notes, filtered; remove one marked retire |
| `plsi targets unmet/discover/dossier` | Target Hunter | what an indication already has; candidates for a disease banded `clinical`, `grounded` or `novel` by what has been tried, or the aging genes under a hallmark; one target gathered from the free sources into `<PLSI_HOME>/targets/<gene>/` |
| `plsi targets assess` | Target Hunter | the bot's judgment on a target, refused whole unless every claim cites a verified identifier and every superlative its source |
| `plsi targets trials` | Target Hunter | hand-harvested asset names -> their ClinicalTrials.gov records: phase, status and sponsor per indication, synonyms and near-misses |
| `plsi targets brief/hallmarks` | Target Hunter (PLSI carries a brief) | the citable rows of a dossier for a research move (database records, not quotes; writes nothing); the hallmark frame of a stored dossier |
| `plsi targets show/list` | Target Hunter | a stored dossier's identity, computed reads and assessment, offline (what its dashboard is drawn from); every stored dossier |
| `plsi mothership whoami` | PLSI, Data Controller, Liaison, Cohort Analyst, Grant Writer | the professional token's role, scopes and expiry, decoded locally (never printed) |
| `plsi mothership catalog`, `plsi mothership findings` | Explorer, Liaison, Cohort Analyst (Grant Writer: `findings` only) | the public data catalog; the findings ledger (professional, or public when published) |
| `plsi mothership release` | Cohort Analyst | a data release: aggregate cells of at least the minimum size |
| `plsi mothership analyze` | Cohort Analyst, Challenger | a `compare_groups` or `age_trend` spec on a release, with the same maths as the Mother |
| `plsi mothership prereg`, `plsi mothership finding` | Cohort Analyst | the preregistration and the finding messages for Data Controller to send |

**Longevity Mothership wire.** PLSI, the relay (`plsi refs show longevity_mothership`) and the Mother (Insilico's Grok
Bot with its own toolbelt and storage) share one contract, kept with the relay and the Mother in
[`plsi-mothership`](https://github.com/Insilico-org/plsi-mothership). Contributions carry a pseudonym made on the
user's computer (the hash of a local secret, which also proves deletion requests), never the install code; the relay
forwards bodies without the install code and keeps no IP logs; professional tokens come from a one-time emailed invite
after a steward's review and carry scopes. Message kinds: contribution, withdrawal, research note, registry update,
preregistration and finding, each gate-checked on both sides. Research notes never take the relay: they go by email to
the Mother's `+research` address (`plsi refs show longevity_mothership_research`), signed by the sender's address, and
the Mother replies there with a receipt and, after review, the decision.

## Extending PLSI

Grok can grow what PLSI does without changing the core. Use the lowest tier that closes the gap:

| Tier | Adds | How | Skill |
|---|---|---|---|
| Map | a new area: coordinates, codes, units | user map YAML, `plsi maps validate` / `install` | processor/add-map |
| Formula | a score, ratio or equation | `derive: {engine: expr, formula: ...}` in a map; a safe evaluator, never `eval` | processor/add-score |
| Extension | an input format (`importer`), a non-formula computation (`deriver`), an evidence source (`evidence_source`) | a tested package installed with `plsi ext install`, versioned under `<PLSI_HOME>/extensions/` | engineer/extend |

Extensions add and never replace. They are full-trust code, installed only with the user's explicit approval naming
the source; `install` runs the package's own tests with a minimal environment (no keys) and refuses imports outside
its layer or any patching of `plsi`, guard rails rather than a sandbox. Values they derive record
`extension:<id>@<version>` in provenance.

## Deidentification profile `plsi-limited-v1`

- The subject keeps only birth year (age 90 or over: `age_group: "90+"`, no birth year) and sex.
- Event dates are kept, so this is a limited data set, not HIPAA Safe Harbor.
- Observation origin keeps `file_sha256` and page, never a path or filename.
- FHIR import keeps codes, values, effective times, clinical status, components, dose text and the structured dose (doseQuantity); it drops identifiers, subject, performer, notes, narrative, contained resources, extensions and attachments.
- Patient name parts, identifier values, telecom and address are held in memory only; any observation whose text contains one is rejected with `IDENTIFIER_IN_TEXT`.
- CSV columns outside the canonical format are dropped and reported by name; methylation sample names are not stored.

## Consent

PLSI collects consent on its question cards (`persona.md`, Consent, batched); standing approvals replace a card until revoked. Joining the Longevity Mothership records the version of `mothership/consent.md` the user saw (`plsi mothership join --consent-version`), and `plsi mothership pack` refuses with `CONSENT_OUTDATED` once that text changes. Outbound oracle text is built in code from the saved state document only (values, item codes and months; never
`personal_context`) and every text sent to an oracle is kept in `egress/oracles.jsonl` (the instance's, or PLSI_HOME's).

## Differentiators

The State Map vocabulary: State Map; registry, selector, mapper, storyline; correspondence types terminology | expert | statistical | mechanistic | validated | unmapped; claim ladder designed | instantiated | exercised.

| | Differentiator | Claim level |
|---|---|---|
| a | Scored next-state predictions (interval, basis, horizon) with calibration and misses reported | exercised |
| b | Aging clocks as first-class coordinates and prediction targets (native blood PhenoAge; every biolearn methylation clock through one adapter) | exercised (PhenoAge), instantiated (biolearn) |
| c | Any-modality intake via Grok extraction into code-normalized observations; maps are data the bot can author and validate | instantiated |
| d | Live evidence collection with verification of every cited identifier | instantiated |
| e | Proactive blocking screening of every action | exercised |
| f | A personal companion inside Grok Bot, run as an orchestrator with a core team and opt-in specialists in separate contexts, a conducted hub-and-spoke protocol (one card per decision), an adversarial plan check and an egress check | designed |

## Quickstart

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
python -m pip install -e ".[dev]"                  # add ,clocks for methylation clocks
plsi doctor
plsi init --instance me --birth-year 1970 --sex F
plsi ingest --instance me --csv labs.csv --dry-run
plsi ingest --instance me --csv labs.csv
plsi maps select --instance me
plsi project --instance me --map blood_phenoage
plsi report --instance me --map blood_phenoage
```

Canonical CSV header: `effective_at,text,value,unit[,code_system,code,specimen,kind,fasting]`. Grok Bot users start with `plsi.md` instead.

Optional keys go in a gitignored `.env` (copy `.env.example`); the process environment overrides it. `plsi keys status` reports booleans only. A secret the user already has under another name is used through `plsi keys map --name <VAR> --from <their name>` (`plsi keys scan` lists the names); only the names are stored.

## Layout

```
plsi.md  persona.md  AGENTS.md      Grok first-run file, persona, contributor rules
contracts/*.schema.json             generated by `plsi contracts export`
maps/*.yaml                         shipped State Maps
mothership/references/<kind>.yaml  trusted references by id; API base URLs; the Mothership address (contact)
mothership/consent.md               what joining the Longevity Mothership means (versioned)
migration/v<X.Y.Z>.md               moving an earlier install to a version, one `# PLSI vA -> vB` section each (plsi.md step 2)
legal/                              Terms of Service, Privacy Policy and Disclaimer (plsi.md steps 0 and 1)
skills/<role>/<skill>/SKILL.md      shared/team (the roster); one folder per bot (orchestrator/, processor/, ...)
src/plsi/                           the toolbelt: cli + commands (the command table), vocab/ (every enum), models, events and one
                                    module per concern; life/ map/ policy/ (each with its models.py); mothership/ research/ targets/
                                    (each with its commands.py; its __init__.py docstring maps its modules)
instances/<name>/                   subject.json, events.jsonl, maps/user/, states/, reports/, exports/, artifacts/, mothership/
                                    (gitignored; under PLSI_HOME when set)
research/<topic>/                   one research topic: topic.md, log.jsonl, questions/ hypotheses/ findings/,
                                    with day.json and notes/ beside the topics (under PLSI_HOME)
targets/<gene>/dossier.json         one target dossier: facts gathered from the free sources, and the bot's
                                    assessment once it passes the refusals (under PLSI_HOME)
```

Contributor and agent rules: [AGENTS.md](AGENTS.md).
