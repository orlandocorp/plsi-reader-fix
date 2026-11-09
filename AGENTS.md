# Agent and contributor rules

Start at [README.md](README.md). Grok Bot runtime behaviour lives in [plsi.md](plsi.md), [persona.md](persona.md) and `skills/`; `skills/shared/team` assigns every skill to one bot of the roster (PLSI, the core team and the opt-in specialists).

- **Layering:** `plsi.life` never imports `plsi.map` or `plsi.policy`; `plsi.policy` never imports `plsi.life` and sees the world only through `plsi.map` (care context via `plsi.map.mapper.context`).
- **One vocabulary:** every enum lives in `src/plsi/vocab/`; nothing inline elsewhere. One name per concept, State Map terms kept (State Map, registry, selector, mapper, storyline, correspondence types, claim ladder).
- **Models live with their layer:** shared types in `src/plsi/models.py`; layer models in `life/`, `map/`, `policy/` `models.py`; the event envelope in `events.py` (each layer registers its own payloads). `contracts/*.schema.json` come only from `plsi contracts export` (`schemas.py`); never hand-edit them.
- **Agents judge, code computes:** Python holds calculators, connectors, the log, gates and the scorer; judgment (choosing, interpreting, predicting) lives in skills. Code may record a bot's judgment and may only make it stricter.
- **Where a skill goes:** `skills/<bot>/<skill>/SKILL.md` is the default — a skill belongs to the bot whose job it is. `skills/shared/` is only for a page **three or more bots actually read** (today: `team` 31, `l-llm` 3, `research-graph` 3). A bot reads only its own folder plus `shared`, so craft filed under another bot is unreachable, and the same page must never be copied into two folders. Before choosing `shared`, name each bot that would read it and what it would do with it; a bot that already has a narrower rule sized for its own job is not a reader. One page per job: a page read on every run stays short, and craft read on demand gets its own page the move page points at. Frontmatter `name:` matches the folder; `description:` starts "Use as <Bot> when/on …" for a bot's own skill and "Use when …" for a shared one.
- **A skill carries judgment, code carries arithmetic and refusals.** An instruction with nothing that code checks or counts is decoration: pair it or drop it.
- **No grab-bag modules:** no `util`/`helpers`/`common`; name a module after the one thing it does.
- **No hard-coded interventions or medications** in code, maps, skills, persona or fixtures. Fixtures use placeholders such as `test-drug-a`. Evidence is collected at run time.
- **No pointer READMEs, shims, backups or legacy mirrors.** The only README is the root one.
- **Base URLs** come from `mothership/references/` (one file per kind); keys only from env / `.env` via `plsi.keys`; the one exception is the Mothership relay's install token, minted at enrollment and kept in `<instance>/mothership/install.json` (masked with `keys.hide`). Never print secret values.
- **HTTP** only through `plsi.net` (called as module attributes so tests can fake it).
- **Soft limit 300 lines per module.** Prefer the smallest correct implementation.
- **UTF-8:** every `open()`/`read_text()`/`write_text()` passes `encoding="utf-8"`; no BOMs; LF line endings.
- **Extend by tiers:** a user map, then a formula (`engine: expr`), then a code extension (`plsi ext install`). Extensions add operations and never replace core gates, deid, scoring or the event log. They are full-trust code: install one only with the user's explicit approval naming its source.
- **Never edit `events.jsonl`.** Corrections go through `plsi retract`; gates are never bypassed.
- **Tests** run offline: `python -m pytest -q` from the repo root. `tests/` is gitignored except the tracked MH contract tests and their fixtures, and `tests/test_apple_export.py` (synthetic Apple Health exports only) and `tests/test_oura_export.py` (synthetic Oura API v2 pages only) and `tests/test_mothership_minimize.py` (synthetic mothership packages only); mark network tests `live` and biolearn tests `clocks`.
