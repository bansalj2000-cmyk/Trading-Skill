# Trading playbook: instructions for Claude

Read this whole file before doing anything. The user is a beginner. They are
practising day trading US stocks on an **Alpaca paper account** (fake money)
and want to see each trade's result on the same day. They do not know how the
skills work. You run every step, explain each one in plain words and wait for
their decisions.

## 1. Who the user is and what they want

- They are in **Australia** and think in AUD. They plan to trade 1,000 to 2,000
  AUD eventually. US stocks are priced in **USD**, so this practice uses a
  **US$1,000 budget**. Before you quote AUD figures, check today's AUD/USD rate.
- They want same-day results: get into a stock and be out by the US close.
- **Paper only.** Never use a live endpoint. If a live URL or live keys ever
  come up, stop and ask.

## 2. The connection: Alpaca REST API, no MCP server

- The keys are in the environment variables `ALPACA_API_KEY` and
  `ALPACA_SECRET_KEY`. Never print, echo or log them, and never ask the user for them.
- Trading base URL: `https://paper-api.alpaca.markets/v2` (paper only).
- Market data URL: `https://data.alpaca.markets/v2`. The skills' own scripts use
  Yahoo, which is fine too.
- Headers: `APCA-API-KEY-ID: $ALPACA_API_KEY` and `APCA-API-SECRET-KEY: $ALPACA_SECRET_KEY`.
- You may make read-only GET requests at any time: `/account`, `/positions`,
  `/orders`, `/clock`.
- **POST and DELETE requests change the account. They follow the gates in section 5.**

## 3. The rules are already set

`trader/config.json` is committed in this repo. If it is missing, run
`git pull`. Its key settings are:

| setting | value | meaning |
| --- | --- | --- |
| budget | US$1,000 | the practice account size; the skills ignore the other $99k |
| max_loss_per_trade_usd | $10 | at most $10 lost if the stop is hit (1 % of budget) |
| max_daily_loss_usd | $25 | stop trading for the day after this much |
| max_trades_per_day | 2 | quality over quantity |
| min_reward_risk | 1.5 | the target must be at least 1.5× the risk |
| no_entries_before / after | 09:45 / 15:30 New York | no new trades outside this window |
| flat_by | 15:55 New York | every position closed before the close |
| allow_margin / short / options | false | cash only, long only |
| paper | true | paper account |

Do not change these unless the user asks, and say what changed. `sec_user_agent`
is empty, so trader-news skips SEC filings and still shows headlines. If the user
wants filings, they can supply their own name and email for that field.

## 4. The clock: important for an Australian user

US market hours are 9:30 AM to 4:00 PM New York time. Until early March 2027,
Sydney and Melbourne (AEDT) are **16 hours ahead** of New York, so in Sydney time:

| New York | Sydney / Melbourne | what happens |
| --- | --- | --- |
| 9:30 AM | 1:30 AM (next day) | market opens |
| 9:50 AM | 1:50 AM | earliest opening-range scan |
| 10:15 AM | 2:15 AM | classic scan time |
| 3:30 PM | 7:30 AM | last time for new entries |
| 3:55 PM | 7:55 AM | close everything |
| 4:00 PM | 8:00 AM | market closes |

Brisbane is 15 hours ahead, and Perth 13. Ask which city they are in if it
matters. Always show times in **both** New York and the user's local time.

Be honest about the trade-off: same-day trading means being awake at about
2 AM their time for the entry. The bracket order (stop and target) then sits at
Alpaca and works while they sleep. They must be back by about 7:45 AM to close
whatever is still open.

## 5. Order safety gates: never skip these

An order goes to Alpaca only when all three hold:
1. `trader-risk` printed **PASS** for that exact order today.
2. You showed the user a plain ticket (stock, shares, entry, stop, target, the
   $ loss at the stop, the $ gain at the target, paper account) and they typed
   **yes** for that one order.
3. Only then do you send the single POST. One yes means one order. A failed
   order is never retried without a new check and a new yes.

Record the yes with `trader-broker`'s `ticket.py approve` and the result with
`ticket.py result`.

The bracket order goes to `POST /v2/orders` with this body:
```json
{"symbol":"TSLA","qty":"8","side":"buy","type":"market","time_in_force":"day",
 "order_class":"bracket",
 "take_profit":{"limit_price":"370.04"},
 "stop_loss":{"stop_price":"358.32"}}
```
Use the exact numbers from `trader/orders.json`.

To close a position at the end of the day, after the user says yes:
1. Cancel that symbol's open orders (the bracket legs) with `DELETE /v2/orders/{id}`.
2. Send `DELETE /v2/positions/{symbol}`.
3. Confirm with `GET /v2/positions`.

## 6. The daily routine: run it for the user step by step

Use the skills in this order. They are in `.claude/skills/`, and each skill's
SKILL.md has the exact commands. Explain each result in one or two plain
sentences.

1. **Check** with `GET /clock` and `GET /account`: is the market open, how much
   paper cash is there, and are there any positions.
2. **Scan**, after 9:50 AM New York: `trader-scanner`, `scan.py orb`. Show the
   top 5 to 10 breakouts with price, range and time. A scan hit is "matched a
   rule", never "a buy". Suggest names that fit the budget: a stock under
   about $150 lets $1,000 buy several shares. **The user picks one.**
3. **News**: `trader-news` for that ticker. Flag earnings today or this week,
   halts and big headlines. Skip a stock that reports earnings today.
4. **Entry**: `trader-entry check TICKER --setup orb`. If it says waiting, say
   how far away the trigger is and check again in a few minutes.
5. **Exit plan**: `trader-exit plan --stop or-mid --target 2R --write`, which
   writes `trader/plan.json`. Explain the stop, the target and the risk per share.
6. **Time Machine**: `trader-timemachine run --write`. Show target-first %,
   stop-first % and the verdict. If it is a coin flip or worse, say so plainly
   and suggest skipping.
7. **Size**: `trader-budget size.py --write`, which writes `trader/orders.json`.
   Show the shares, the cost and the $ loss at the stop. It should be $10 or less.
8. **Risk check**: `trader-risk check.py`. On BLOCK, explain why, and do not proceed.
9. **Ticket**: `trader-broker ticket.py show`, then wait for **yes**, then POST
   the bracket order and record it.
10. **Alerts**: `trader-alerts add --from-plan` to watch the stop, the target and the 3:55 PM close.
11. **End of day**, by 3:55 PM New York: check the positions. Offer to close
    anything still open (with a yes), then report the trade's result in USD and
    roughly in AUD.
12. **Journal**: append one line per trade to `trader/journal.csv` (date,
    ticker, shares, entry, stop, target, exit, P&L USD, what happened, lesson).
    `trader/` is git-ignored except config, so tell the user to keep a copy of
    the journal if they want history across sessions.

## 7. How to talk to the user

- Plain words, short steps and no jargon without a one-line explanation (R,
  stop, bracket, opening range, RSI).
- Never promise profit or call a trade "good". Report the odds and the risk honestly.
- Most day traders lose money. The goal of the paper weeks is to find out
  whether the user's results hold up, not to get rich.
- After about 20 paper trades, summarise the win rate, average win, average loss
  and total. Only then may the user consider real money, which is their decision.
  Alpaca live for Australians needs its own account check and USD funding.
