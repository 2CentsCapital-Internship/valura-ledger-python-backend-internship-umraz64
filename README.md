# Ledger Arena: starter kit

You are building a double-entry book of record. We stream you a broker's event
feed; you post the journal legs each event produces and answer state
checkpoints. We score it continuously against a reference implementation.

Every awkward case in here is one that has cost a real back office real money,
and most of them still balanced perfectly while being wrong.

## Start here

```bash
git clone <your copy of this repo>
cd ledger-arena-starter
pip install -r requirements.txt

python client.py --key ak_your_key_here
```

Get your key from **https://hiring-arena.twocc.in** by entering the email your
invitation was sent to.

That first run will connect, stream, and score somewhere in the low tens. It is
meant to: `book.py` implements one event type as a worked example and raises on
the rest. Seeing the whole loop work before you have written a line means any
later failure is yours and not ours.

It does not score zero because roughly one event in seven correctly produces no
legs at all, and submitting nothing for those is the right answer. Treat the
number you get on that first run as the floor, not as progress.

It ends by printing what it could not post, which is your to-do list:

```
not implemented yet (1174 events skipped):
  order_filled                     287 events
  trade_settled                    281 events
  ...
```

Then read **`PROTOCOL.md`**. It is the entire specification: the accounts, all
twenty event types, every posting rule, and how the scoring works.

## What is already done for you

`client.py` is finished. It subscribes, survives the deliberate mid-run replay,
resumes from an offset, batches postings, and answers checkpoints on time. That
is transport, and it is not what we are assessing.

`book.py` is where you work. It hands you one event and takes back its legs.

## Two things to get right before anything else

**Use `Decimal`, never `float`.** Money here does not always divide evenly. A
float implementation will disagree with us by a cent in places that are very
hard to find afterwards.

**Key balances by `(customer, account)`, not by account.** At least one event
moves money between two customers on the same account. An account-level book
shows nothing wrong at all, and its trial balance agrees with it.

## Tiers

| Tier | Attempts | Score | What it is for |
| --- | --- | --- | --- |
| `practice` | unlimited | shown, with the correct legs on every event | develop here |
| `submission` | 3 | shown | scored; tuning against it is expected |
| `final` | 1 | withheld | this is what ranks you |

Practice returns the expected legs on every response. Use it hard: it is the
executable version of the specification, and anything ambiguous in the document
is settled by running against it.

Each attempt draws a fresh dataset, so a retry is a new problem rather than a
retake of one you have already seen scored.

## Rules

- **One address, one candidate.** Your key is your identity.
- **Use AI tools if you normally do.** We do. There is no penalty and no
  detection game. But you will walk us through the code in a live session and
  change it while we watch, so be able to defend every line of it.
- **Ask in Discord, not by DM.** Anything clarified there becomes canon for
  everyone, which is fairer than rewarding whoever thought to ask privately.
- **If you run out of time, stop and write down what is missing** and how you
  would have done it. That costs you nothing and reads far better than
  something half-built and unexplained.

## Things the stream will do to you

All deliberate, all in `PROTOCOL.md`, none of them bugs: duplicate delivery, a
forced disconnect that rewinds you several hundred events, fills that arrive
before their placement, oversells, reversals of events you never received, and
payloads that will not parse.

A server that rejects one bad event and keeps consuming beats one that stops.

## Running a graded tier

```bash
python client.py --key ak_... --mode submission
```

It will ask you to confirm, because attempts are limited. A run that cannot
finish before the deadline is refused rather than started, so you will not lose
an attempt to the clock.

# Solution Overview

This implementation completes the Ledger Arena book of record by consuming the
broker event stream, translating each supported event into balanced
double-entry journal legs, and submitting those postings back to the arena in
batches. The client maintains durable local state so it can reconnect, resume
from the correct stream offset, replay previously seen events, and answer
checkpoint requests with a deterministic snapshot of the ledger.

The accounting engine in `book.py` owns the domain logic. It applies events
idempotently, updates customer-level balances and inventory state, records the
legs produced for each event, and can reconstruct historical state for
as-of checkpoints by replaying the stored event history.

# Architecture

## Event-driven processing

`client.py` subscribes to the Ledger Arena server-sent event stream and routes
each ledger event into the `Book` processor. Events are handled one at a time,
with produced journal legs queued into bounded posting batches. Checkpoint
requests are handled inline by asking the book for either the current snapshot
or an as-of snapshot.

## Double-entry bookkeeping

Every accounting event is represented as one or more debit and credit legs.
The book validates that posted legs balance before accepting state mutations,
which keeps the ledger internally consistent even when events are duplicated,
malformed, or rejected.

## State management

Runtime state is split across the transport layer and the accounting layer.
`client.py` persists stream position, pending submissions, submitted event IDs,
run identity, and raw event history in a JSON state file. `book.py` maintains
balances, orders, holds, trades, settlements, reversals, FIFO lots, and posted
legs in deterministic in-memory structures that are rebuilt from the durable
event log on startup.

## FIFO inventory tracking

Inventory is tracked by customer and symbol using FIFO lots. Buy fills add lots
with total cost basis, while sell fills consume the oldest lots first and post
cost of goods sold from the consumed cost basis. This avoids unit-cost rounding
drift and keeps realized inventory accounting consistent across partial fills
and reversals.

## Checkpoint generation

Checkpoint responses are generated from ledger state rather than from submitted
postings alone. The snapshot includes customer balances, open order routes, open
positions, and trial balance information. As-of checkpoints are answered by
replaying the stored event stream through a fresh book until the requested event
ID is reached.

## Event sourcing approach

The client appends accepted stream events to a local JSONL event history. On
restart, the book is rebuilt by replaying that history, which makes recovery
deterministic and allows checkpoint state to be derived from the same source of
truth as live processing.

# Features Implemented

- Deposit processing
- Withdrawals
- Order placement
- Partial fills
- Full fills
- Trade settlement
- Dividends
- Interest accrual
- Stock splits
- Symbol changes
- Customer transfers
- Reversals
- Duplicate event handling
- Replay recovery
- Out-of-order fills
- Persistent state
- Checkpoint responses

# Accounting Principles

## Double-entry bookkeeping

Each event produces balanced debit and credit entries across the affected
accounts. The book rejects postings that do not balance, preventing partial or
inconsistent mutations from being committed.

## Decimal-based money calculations

Money and share quantities are handled with Python `Decimal` values instead of
floating-point arithmetic. This keeps fee calculations, allocations, and
rounding behavior stable and reproducible.

## Independent cent rounding

Monetary amounts are rounded independently at the point where the protocol
requires cents. This is especially important for charges, proportional
allocations, partial fills, and FIFO cost calculations where rounding one
aggregate value can differ from rounding each accounting component.

## Customer-level account balances

Balances are keyed by `(customer, account)`, so transfers between customers on
the same account are still visible in the ledger. This prevents account-level
netting from hiding customer-specific obligations.

## FIFO inventory costing

Inventory cost basis is maintained per customer and symbol. Sell-side fills
consume the oldest open lots first and post the consumed total cost, preserving
FIFO accounting through partial executions, stock splits, symbol changes, and
reversals.

# Reliability Improvements

- Improved checkpoint consistency by deriving snapshots from book state and
  replaying historical events for as-of checkpoint requests.
- Complete rollback of state mutations when an event is rejected, malformed, or
  fails validation.
- Enhanced duplicate detection for event IDs and domain identifiers such as
  withdrawals, orders, trades, settlements, and reversals.
- Replay-safe processing through durable event history, persisted stream
  offsets, pending submissions, and submitted event tracking.
- Better reconciliation through explicit trial balance reporting and
  customer-level account balances.
- Deterministic state restoration by rebuilding the book from the stored event
  log after reconnects or process restarts.
- Safer exception handling so malformed or unsupported events are rejected
  without stopping stream consumption.
- Improved resilience against malformed events, forced stream resets,
  duplicate delivery, out-of-order fills, and replayed offsets.

# Technologies

- Python
- Decimal
- JSON
- Event-driven architecture

# Project Structure

| File | Purpose |
| --- | --- |
| `README.md` | Official challenge description plus project-specific implementation notes. |
| `PROTOCOL.md` | Ledger Arena protocol specification, event definitions, account rules, and scoring behavior. |
| `client.py` | Durable SSE client that handles streaming, batching, checkpoint submission, replay recovery, persisted state, and practice feedback. |
| `book.py` | Accounting engine that converts events into double-entry journal legs, maintains ledger state, tracks FIFO inventory, supports reversals, and produces checkpoints. |
| `requirements.txt` | Python dependency list for running the client. |
| `.ledger_state_<mode>.json` | Generated local state file used to resume a run without losing offsets or pending submissions. |
| `.ledger_state_<mode>.json.events.jsonl` | Generated local event log used for replay and deterministic state reconstruction. |
| `.practice_feedback.jsonl` | Generated practice-mode feedback log used to inspect scoring differences during development. |

# Running

Install dependencies:

```bash
pip install -r requirements.txt
```

Run with the default mode:

```bash
python client.py --key YOUR_KEY
```

Run in practice mode:

```bash
python client.py --key YOUR_KEY --mode practice
```

Run in submission mode:

```bash
python client.py --key YOUR_KEY --mode submission
```

# Challenges Encountered

FIFO accounting required careful tracking of total lot cost rather than
rounded unit cost, especially when partial fills and sell-side cost basis were
involved. Reversals were also subtle because they had to undo both posted legs
and associated state mutations without restoring state that had already been
released by later events.

Duplicate events, replayed stream offsets, and out-of-order fills made
idempotency and state restoration central to the design. Maintaining ledger
consistency required balancing every posting, preserving customer-level account
detail, and ensuring checkpoint snapshots were generated from deterministic
book state.

# Final Result

Practice Score: 99.67/100

Breakdown:

| Category | Score |
| --- | ---: |
| Posting Correctness | 29.92/30 |
| Checkpoint Correctness | 39.96/40 |
| Resilience | 14.80/15 |
| Liveness | 10/10 |
| Final Reconciliation | 4.99/5 |

# Future Improvements

- Additional automated tests
- Property-based testing
- Performance benchmarking
- Metrics and monitoring
- Structured logging
