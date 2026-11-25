# `verticals/` — the three kinds of business

One package per vertical. Each holds that trade's rules, queries and
reports; HTTP lives in `api/` (staff) and `public/` (no-login links), and
the shared writers stay in `services/` (stock, billing, orders, SMS).

| Package | Staff API | Public link | What it adds |
|---|---|---|---|
| `pharmacy/` | `/pharmacy/*` | `/o/<slug>/r/<refill_token>` | refill cycles and schedule, expiry radar, register report and PPB PDF |
| `distributor/` | `/distributor/*` | `/o/<slug>/d/<duka_token>` | credit ceiling, tier prices, containers, deni aging, reminders, eTIMS badge, Paybill by account code |
| `institution/` | `/institution/*` | `/o/<slug>/c/<campaign_slug>` | member lookup, campaign orders, instalments, pickup codes, campaign close, campaign report, Paybill by admission number |

Tests: `tests/test_pharmacy.py`, `tests/test_distributor.py`,
`tests/test_institution.py` (MySQL lane runs their concurrency tests).
