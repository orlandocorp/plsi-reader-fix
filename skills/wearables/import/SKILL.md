---
name: import
description: Use as PLSI Wearables when PLSI asks to import wearable or device data (sleep, resting heart rate, HRV, steps, exercise minutes, VO2max, blood pressure, weight, CGM) - from the user's Open Wearables server, a Google Health pull (wearables/google-health) or an export file - as calendar-month means on the sleep_activity and cardiometabolic maps.
---

# PLSI wearables

Follow persona.md norms and `shared/team`. Owner: **PLSI Wearables**. Reply only in handoffs to PLSI; never message the user.

Device data arrives by minute or by night; PLSI keeps it as **one calendar-month mean per metric per closed month**, so a map value is a month, not one
night, and predictions are scored on months. The raw data stays where it came from.

## When to use

- A `request` naming Open Wearables and a date range ("Import my wearable data", the first connect, or PLSI's check-in
  asking for a closed month not yet recorded).
- A `request` with an inbox path to a device export: zip, XML, JSON or CSV from a watch, ring, strap, CGM, cuff or
  scale. A FHIR bundle or a value stated in chat is not yours: say so in a `result` so PLSI hands it to the Data
  Processor (`processor/intake`).
- Daily rows from `wearables/google-health` step 4 or `wearables/apple-health` step 4 (a file under
  `<PLSI_HOME>/work/<thread>/`): start at step 3.

## Steps

1. **Read the data.**
   - **Open Wearables** (`plsi refs show open_wearables`; the user's own server, connected as an MCP server with
     their key from the secret store): list users and pick the one the request names (one Open Wearables user per
     instance, remembered by PLSI). Read the sleep summary, activity summary and time series (resting heart rate,
     weight, blood pressure, glucose) for the range in the request. Not connected or no user: say so in a `result`.
   - **Google Health** (no Open Wearables): pull with `wearables/google-health`, then continue here with its daily
     rows. **Apple Health** through the P.R.O. app: pull with `wearables/apple-health`, the same way. A source already linked to Open Wearables is read there only, never pulled twice.
   - **Apple Health export** (`export.zip`, any size): never open it yourself. Stream it:

     ```bash
     plsi apple export --file <zip> --out <PLSI_HOME>/work/<thread>/daily.csv --provenance <PLSI_HOME>/work/<thread>/apple-provenance.json
     ```

     `daily.csv` stays `date,metric,value,unit,source`. `source` is a kind (`watch`, `phone`, `other`; the next app of
     that kind is `other-2`). The opt-in sidecar is local only. It names the app (`sourceName`) behind each kind,
     the original start and end with the offset as written, the HRV method (`sdnn` or `rmssd`, from the HealthKit
     type, never from the number), and a blood-pressure `correlation` when the export's Correlation element paired
     the two sides. `copies` greater than 1 is the same record listed again. Two rows with the same value and
     different `start` are repeated readings. `unpaired` and `uncertain` stay in the preview: do not drop them, do
     not average them, and do not invent a pair because the timestamps match. `abnormal` means a non-physical
     number was kept; a high or low physical reading is kept with no clinical flag. The sidecar has no device
     serial, GPS, account id or filename. Do not ingest it, attach it, or send it. Without `--provenance` the CSV
     is unchanged and the app name cannot be recovered. Stdout `sha256` is the export hash for step 2 and does not
     repeat the sidecar. Continue at step 3 with `daily.csv`.

     Limits, also listed in the sidecar: step and exercise samples are a count plus the first and last timestamps,
     not every sample, and an exact repeat in those series is not collapsed. Sleep `date` is the local wake day and the minutes are asleep only (`InBed` and `Awake` are
     not added; a legacy `Asleep` record that overlaps stages is the coarse copy and is not added again). A night
     split across midnight stays one session when the stages touch, when they share an `InBed` record, or when an
     `Awake` record covers a gap of at most two hours and there is no `InBed`. `multi_session` means more than one
     session ended that local day (a nap and a night): the CSV value is their sum, and the sessions are in the
     sidecar. This command does not record or project anything, and it has not been checked against a real export.
   - **Export file:** open only the inbox path; list metrics, units, time zone, date range and sampling. Large,
     binary or recurring formats need an `importer` extension (`plsi ext list`); none installed: ask PLSI in a
     `result` for the PLSI Engineer (`engineer/extend`), then step 5 with `--ext <id> --input <file>`.

2. **Hash an export file** for `origin.file_sha256` (Open Wearables and Google Health reads need none):

   ```bash
   python -c "import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" export.zip
   ```

3. **Only what a map uses.** Record only the metrics an installed map has a coordinate for (`plsi maps show
   sleep_activity`, `cardiometabolic`, any user map); everything else (active energy, stand,
   oxygen saturation, walking metrics, ...) stays in the export, listed by name in the `result`; one the user or a
   plan needs goes in the `result` as a `processor/add-map` request, and is imported once the map exists. A five-year export is then about 4 rows a month plus the weight and height readings.

   **Calendar-month means.** Per metric, group by calendar month (nights dated by wake-up day;
   steps and resting heart rate by calendar day). Use the device's own summary values; never average sleep stages into
   a total. Blood pressure, weight and VO2max stay one row per reading. Map coordinates (`plsi maps show
   sleep_activity`): sleep duration (min), resting heart rate (/min), HRV SDNN (ms), HRV RMSSD (ms, code
   `{system: free, code: sleep_activity.hrv_rmssd}`), daily steps (/d), exercise
   minutes (the device's moderate-or-harder minutes, e.g. Apple exercise time: the month's total / (days in the month
   / 7), min/wk), active days (days with 30 or more of those minutes, / (days in the month / 7), d/wk), VO2max
   estimate (mL/min/kg); `cardiometabolic` for blood pressure and weight. Sleep efficiency has no code: keep it as an uncoded row with a precise label. SDNN and RMSSD
   are different measures: record each under its own coordinate, never one as the other. `plsi apple export` already
   writes `hrv_sdnn` and `hrv_rmssd` from the HealthKit type; do not move a value from one to the other because the
   numbers look alike. An HRV that names neither
   stays uncoded unless the device's documentation says which. **One source per metric:** when a watch, a phone or another app report the same metric (an Apple
   Health export usually does), use the device's own merged value if the export has one, else one source per metric
   (for sleep the device worn at night); never add or average overlapping sources, and say the app and the kind of
   source in the label ("Apple Health, watch"). An overlap you cannot resolve is a question, not a row.

   **One app per metric across apps.** Google Health, Apple Health (P.R.O.), Open Wearables and export files often
   carry the same device's data, and their monthly means differ a little, so `plsi ingest` does not see them as
   duplicates. Each metric has ONE primary app, kept in your permanent memory ("Steps: Apple Health"): the app that
   reads the device worn at night for sleep, resting heart rate and HRV, the watch's app for steps and exercise
   minutes, else the phone's. Unclear which: put it in the step's `question` (per metric, the apps that carry it).
   Rows of another app for that metric are not recorded; list them as skipped in the `result`. Before recording a
   month, read what is there (`plsi events list --instance <instance> --kind measurement`): a row for that metric
   and month from another app means skip, never a second row. Two rows already recorded for one metric and month:
   keep the primary app's and ask PLSI in the `result` to have the Data Processor retract the other
   (`processor/intake`, Correct or retract), naming both rows.
   **Sleep duration is time asleep:** sum only the asleep segments (Apple Health `AsleepCore`, `AsleepDeep`,
   `AsleepREM`, `AsleepUnspecified`), never `InBed` or `Awake`, and merge overlapping segments before adding them up.
   A monthly mean outside 3-11 h, or a jump of more than 2 h between months, means the parsing is wrong: fix it or
   ask, never record it.

   Record **closed months only**: a month still under way is not recorded until it ends. One row per metric and
   month, only with at least **15** nights (sleep, HRV SDNN or RMSSD) or **15** days (resting heart rate, steps, exercise minutes, active days); fewer:
   skip it, never invent a mean. `effective_at` = the month's last day; `original_text` e.g. `Sleep duration
   (calendar-month mean of 30 nights, 2026-09)`. A month already recorded comes back as `duplicate`.

4. **Code once per metric, not per row.** One ObservationInput per distinct metric in `labels.json`, normalize, pick
   the candidate that matches the map coordinate when it clearly fits (label, unit, method), and copy its `code` into
   the `code_system,code` columns of every row of that metric. Units only via `plsi calc convert`. Equivalent
   spellings are the same unit and the number is not scaled: `mmHg` and `mm[Hg]`; Apple's `mL/min·kg` (and
   `mL/kg/min`, `ml/kg*min`) and `mL/min/kg`. The CSV and `unit_original` keep the spelling the export used. A
   different quantity (lb to kg, L to mL) is not an alias: convert only when `plsi calc convert` returns a factor.

   ```bash
   plsi normalize observations --in labels.json --out labels.norm.json
   plsi calc convert --map <id> --coordinate <id> --value <v> --unit <u>
   ```

   ```csv
   effective_at,text,value,unit,code_system,code,specimen,kind
   2026-09-30,Sleep duration (calendar-month mean of 30 nights, 2026-09),421,min,loinc,<loinc>,,measurement
   2026-09-30,Resting heart rate (calendar-month mean of 30 days, 2026-09),56,/min,loinc,<loinc>,,measurement
   ```

5. **Dry run, then ONE `question`** for the whole import ("Record 3 months of sleep, resting heart rate and steps,
   Jul to Sep 2026?" Record / Edit / Cancel) with every open code choice and the time-zone assumption; under a
   standing approval for imports, ingest at once.

   ```bash
   plsi ingest --instance <instance> --csv device.csv --dry-run
   plsi ingest --instance <instance> --csv device.csv
   ```

   `conflicts` in the dry run: a metric and month that already has a value (or two in this file). Drop the row
   unless it is the primary app's; then keep it and name the other row for retraction (step 3). Never ingest a
   conflict you did not resolve.

6. **Project and return.** Any ingest makes every saved state stale: project `sleep_activity` (and `cardiometabolic`
   for blood pressure or weight) and every map already listed in `plsi status` `states`, then ONE `result`: counts
   per metric, months covered, months skipped (fewer than 15 days), uncoded rows, every projected map in `states`
   with its hash and the gaps.

   ```bash
   plsi status --instance <instance>
   plsi project --instance <instance> --map sleep_activity
   ```

## Rules

- One import, one dry run, one question; never one per metric or per month. Re-imports of an overlapping range are
  fine: `duplicate` rows are already recorded; never set `repeat` for them.
- Never store device ids, account names or emails, GPS tracks, workout routes or file names; the CSV keeps only the
  canonical columns. The Apple provenance sidecar is the one local exception for app names and timestamps: it stays
  in the work folder, and it is not a record, not an attachment and not something to send. Open Wearables user ids stay in PLSI's memory, never in a record.
- Consumer devices are not diagnostic: say so in the `result` when a value looks abnormal, and flag it `uncertain`
  (`orchestrator/urgent`) instead of judging it; never treat a CGM, cuff or ring reading as a diagnosis.
- Never compare across devices or HRV methods; a device change is a `note` ("wearable changed 2026-10"). A new
  primary app for a metric starts with the next closed month; months already recorded stay.
