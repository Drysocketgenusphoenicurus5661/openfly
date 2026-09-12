# NIFTY and current-week straddle facts

Measured on Saturday 2026-09-12 through the local OpenAlgo server (broker
data, live mode). Market closed; values reflect Friday 2026-09-11.

## Index and volatility

| Field | Value |
| --- | --- |
| NIFTY (NSE_INDEX) close | 23,398.1 (open 23,270.3, high 23,448.1, low 23,231.4, prev close 23,477.8) |
| INDIAVIX close | 12.29 (open 11.8, high 12.57) |
| INDIAVIX, last 271 trading days | mean 13.67, min 9.15, max 27.89 |

## Contracts

| Field | Value |
| --- | --- |
| NIFTY option expiries | 15-SEP-26, 22-SEP-26, 29-SEP-26, 06-OCT-26, 13-OCT-26, 27-OCT-26, 23-NOV-26, then quarterly and half-yearly |
| Weekly expiry weekday | Tuesday |
| Futures expiries | 29-SEP-26, 27-OCT-26, 23-NOV-26 |
| Lot size | 65 |
| Freeze quantity per order | 1,800 (27 lots) |
| Strike step | 50 |
| Tick size | 0.05 |
| Symbol format | NIFTY15SEP2623400CE, NIFTY15SEP2623400PE (broker symbol NIFTY2691523400CE) |

## Current-week ATM straddle (expiry 15-SEP-26, strike 23400)

| Field | Call | Put |
| --- | --- | --- |
| LTP | 133.6 | 70.4 |
| Bid / ask | 133.65 / 135.0 | 70.55 / 71.25 |
| Day range | 44.9 to 156.15 | 67.5 to 199.0 |
| Open interest | 6,543,875 | 9,096,880 |
| Volume (contracts) | 387,587,655 | 279,680,570 |
| Implied volatility | 10.88 percent | 10.88 percent |
| Delta | 0.603 | -0.397 |
| Gamma | 0.00156 | 0.00156 |
| Theta (points per day) | -13.88 | -13.88 |
| Vega | 8.79 | 8.79 |
| Days to expiry | 3.44 | 3.44 |

Combined premium 204.0 points = INR 13,260 per lot. The greeks endpoint
used a spot of 23,463.2 (synthetic forward), which is why 23400 shows a
0.60 delta call; the true ATM at that forward is 23450.

Derived: the straddle decays about 27.8 points (INR 1,800) per calendar day
if the index stays put; a 100 point move against the straddle costs about
0.5 x 0.0031 x 100 x 100 = 15.5 points (INR 1,000) before decay. A 25
percent combined stop is 51 points (INR 3,300) per lot.

## Margins (from the margin endpoint, 1 lot)

| Structure | Product | SPAN | Exposure | Total |
| --- | --- | --- | --- | --- |
| Short straddle | NRML | 141,125.25 | 60,835.06 | 188,700.31 |
| Short straddle | MIS | 141,125.25 | 60,835.06 | 188,700.31 |
| Long straddle | NRML | 0 | 0 | 13,260 |

## NIFTY index behaviour (1m bars, 400 calendar days, 271 trading days)

| Statistic | Value |
| --- | --- |
| Bars | 101,310; 375 per day (09:15 to 15:29) |
| 1m log-return std | 0.0426 percent |
| 1m lag-1 autocorrelation | 0.005 |
| Forward move std, 5 / 15 / 30 / 60 / 120 min | 0.072 / 0.115 / 0.158 / 0.216 / 0.300 percent |
| Forward move std, full day (375 min) | 0.879 percent (about 206 points) |
| Mean absolute full-day move | 0.617 percent |
| Daily range mean | 0.891 percent |
| Open-to-close absolute move mean | 0.454 percent |
| Mean absolute overnight gap | 0.378 percent |

Intraday 1m return std by window:

| Window | Std |
| --- | --- |
| 09:15 to 09:30 | 0.163 percent |
| 09:30 to 09:45 | 0.038 |
| 09:45 to 10:00 | 0.032 |
| 10:00 to 10:30 | 0.028 |
| 10:30 to 12:30 | 0.023 to 0.026 |
| 12:30 to 13:00 | 0.027 |
| 13:00 to 14:00 | 0.024 to 0.027 |
| 14:00 to 15:00 | 0.028 to 0.030 |
| 15:00 to 15:15 | 0.038 |
| 15:15 to 15:30 | 0.032 |

Reading: the full-day realized move is almost exactly the straddle premium,
so on average a short straddle is fairly priced and its edge has to come
from choosing days and hours, which is the question the fly is asked.

## History coverage

| Series | Availability |
| --- | --- |
| NIFTY index 1m | 400 days in one call, tz Asia/Kolkata, columns close/high/low/oi/open/volume |
| INDIAVIX daily and 1m | available (271 daily bars; 1m confirmed for recent days) |
| NIFTY15SEP2623400CE 1m and 5m | from 2026-09-02 (listed about two weeks before expiry) |
| Expired contracts | not available from the broker; hence the recorder job and the synthetic straddle model |

## Costs per straddle round trip (1 lot, short, discount broker calculator)

Reference from a discount broker's NFO options brokerage calculator (verified
2026-09-12): buy 100, sell 100, quantity 400 (turnover 80,000) gives
brokerage 40 (flat INR 20 per executed order), STT 60 (0.15 percent of the
sell-side premium value), exchange transaction charge 28.42 (0.03553 percent
of turnover), GST 12.33 (18 percent of brokerage plus exchange plus SEBI),
SEBI 0.08 (0.0001 percent of turnover), stamp duty 1 (0.003 percent of the
buy value, rounded to the rupee), total 141.83, 0.35 points to breakeven.

For one lot of the 204 point straddle sold and bought back at the same
premium (sell value 13,260, turnover 26,520): brokerage 80, STT 19.89,
exchange 9.42, SEBI 0.03, GST 16.10, stamp 0, total about INR 126, or 0.95
percent of the credit.

## Endpoint notes from this session

`optionchain`, `optionsymbol` and `syntheticfuture` returned HTTP 404 "No
strikes found for NIFTY expiring 15-SEP-26" although `search`, `symbol`,
`quotes` and `optiongreeks` resolved the same contracts. OpenFly resolves
the chain itself from `expiry` and `search`.

## How these were produced

Using the `openalgo` Python SDK against http://127.0.0.1:5000 with the API
key read from a git-ignored environment file: `expiry`, `search`, `symbol`,
`quotes`, `optiongreeks`, `margin`, `history` (index 1m, VIX daily and 1m,
option 1m and 5m), `timings`. Statistics computed with pandas on the
returned frames. The scripts will be committed under `backend/scripts/`
once the backend package exists.
