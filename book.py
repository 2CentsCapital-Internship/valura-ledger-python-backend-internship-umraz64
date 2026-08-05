"""Ledger Arena — complete book implementation.

Key conventions (from the canonical protocol at /protocol):
  - Every amount rounded to the cent independently, half-away-from-zero.
  - Balances keyed by (customer_id, account), not just account.
  - Use Decimal everywhere; never float for money.
  - FIFO cost: round(lot_total × sold_qty / lot_qty) NOT unit_cost × qty.

Tariff (from the live protocol):
  BRK-A  equity/etf   brokerage=20bps  custody=4bps  broker_cost=9bps  custody_cost=2bps  min_fee=1.00  ticket=0.35
  BRK-B  equity/bond  brokerage=15bps  custody=5bps  broker_cost=8bps  custody_cost=3bps  min_fee=2.50  ticket=3.00
  BRK-C  etf/bond     brokerage=25bps  custody=3bps  broker_cost=12bps custody_cost=1bps  min_fee=0.50  ticket=0.20

Accounts:
  1100 Omnibus Cash            1150 Settlement Receivable  1200 Omnibus Custody
  2010 Customer Wallet         2100 Customer Securities     2300 Withdrawals In Transit
  2350 Unsettled Trade Payable 2400 Reg Fees Payable
  2411 Broker Fees Payable BRK-A  2412 BRK-B  2413 BRK-C
  2420 Custodian Fees Payable  2430 Partner Share Payable
  4000 Brokerage Revenue       4010 Custody Revenue
  4100 FX Spread Revenue       4200 Interest Income
  5000 Brokerage Cost          5010 Custody Cost            5100 Partner Revenue Share
"""
from __future__ import annotations

import copy
import logging
import os
from collections import defaultdict
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

D = Decimal
ZERO = D("0.00")
LOGGER = logging.getLogger("ledger_arena.book")
DEBUG_RAISE_UNEXPECTED = os.environ.get("LEDGER_ARENA_DEBUG_RAISE", "").lower() in {
    "1",
    "true",
    "yes",
    "on",
}

# ---------------------------------------------------------------------------
# Tariff table
# ---------------------------------------------------------------------------

# Each entry: (asset_classes, brokerage_bps, custody_bps,
#              broker_cost_bps, custody_cost_bps, min_fee, ticket, payable_account)
BROKERS: dict[str, dict] = {
    "BRK-A": {
        "asset_classes": {"equity", "etf"},
        "brokerage_bps": D("0.0020"),   # 20 bps
        "custody_bps":   D("0.0004"),   # 4 bps
        "broker_cost_bps":   D("0.0009"),  # 9 bps
        "custody_cost_bps":  D("0.0002"),  # 2 bps
        "min_fee":  D("1.00"),
        "ticket":   D("0.35"),
        "payable_account": "2411",
    },
    "BRK-B": {
        "asset_classes": {"equity", "bond"},
        "brokerage_bps": D("0.0015"),   # 15 bps
        "custody_bps":   D("0.0005"),   # 5 bps
        "broker_cost_bps":   D("0.0008"),  # 8 bps
        "custody_cost_bps":  D("0.0003"),  # 3 bps
        "min_fee":  D("2.50"),
        "ticket":   D("3.00"),
        "payable_account": "2412",
    },
    "BRK-C": {
        "asset_classes": {"etf", "bond"},
        "brokerage_bps": D("0.0025"),   # 25 bps
        "custody_bps":   D("0.0003"),   # 3 bps
        "broker_cost_bps":   D("0.0012"),  # 12 bps
        "custody_cost_bps":  D("0.0001"),  # 1 bps
        "min_fee":  D("0.50"),
        "ticket":   D("0.20"),
        "payable_account": "2413",
    },
}

REG_FEE_BPS = D("0.0008")   # 8 bps regulatory fee on every fill (both sides)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def money(x: Decimal) -> Decimal:
    """Round to 2 decimal places, half-away-from-zero."""
    return x.quantize(D("0.01"), rounding=ROUND_HALF_UP)


def leg(account: str, customer_id: str, debit=ZERO, credit=ZERO) -> dict:
    return {
        "account": account,
        "customer_id": customer_id,
        "debit":  str(money(D(debit))),
        "credit": str(money(D(credit))),
    }


def compute_charges(principal: Decimal, broker_name: str, asset_class: str,
                    partner_rate: Decimal) -> dict[str, Decimal]:
    """Compute all per-fill fee amounts from the tariff.

    Returns a dict with keys:
      brokerage, custody, reg_fee,
      broker_cost, custody_cost, partner_share
    Each is independently rounded to the cent.
    """
    b = BROKERS[broker_name]

    # Revenue charged to customer (each item rounded independently)
    raw_brokerage = principal * b["brokerage_bps"]
    brokerage = max(money(raw_brokerage), b["min_fee"])   # floor at minimum fee
    brokerage = money(brokerage)
    custody   = money(principal * b["custody_bps"])
    reg_fee   = money(principal * REG_FEE_BPS)

    # Cost charged to firm (gross, not netted) — include ticket then round
    broker_cost = money(principal * b["broker_cost_bps"] + b["ticket"])
    custody_cost  = money(principal * b["custody_cost_bps"])

    # Partner share: partner_rate × max(0, revenue - cost)
    revenue = brokerage + custody
    cost    = broker_cost + custody_cost
    margin  = revenue - cost
    if margin > ZERO:
        partner_share = money(partner_rate * margin)
    else:
        partner_share = ZERO    # no clawback when cost exceeds revenue

    return {
        "brokerage":    brokerage,
        "custody":      custody,
        "reg_fee":      reg_fee,
        "broker_cost":  broker_cost,
        "custody_cost": custody_cost,
        "partner_share": partner_share,
        "broker_payable": b["payable_account"],
    }


# ---------------------------------------------------------------------------
# Order routing (used for checkpoint open_order_routes)
# ---------------------------------------------------------------------------

def route_order(asset_class: str, principal: Decimal) -> str | None:
    """Return the broker id with the lowest total customer charge for this fill.

    Brokerage + custody, floored at min_fee, then ties break on broker id asc.
    Returns None if no broker covers this asset class (should not happen).
    """
    candidates = []
    for broker_id, b in sorted(BROKERS.items()):   # sorted → alphabetical tie-break
        if asset_class in b["asset_classes"]:
            brokerage = max(money(principal * b["brokerage_bps"]), b["min_fee"])
            custody   = money(principal * b["custody_bps"])
            total     = brokerage + custody
            candidates.append((total, broker_id))
    if not candidates:
        return None
    return min(candidates, key=lambda x: (x[0], x[1]))[1]


# ---------------------------------------------------------------------------
# Main Book
# ---------------------------------------------------------------------------

class Book:
    def __init__(self) -> None:
        # (customer_id, account) → debit-positive running balance
        self.balances: dict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO)

        # Already-processed event ids (idempotency guard)
        self.seen: set[str] = set()

        # Unimplemented or skipped event counts
        self.todo: dict[str, int] = defaultdict(int)

        # Full event store — needed for fee_refund, reversal, settlement lookups
        self.events: dict[str, dict] = {}

        # Legs posted per event — needed for reversal
        self.posted_legs: dict[str, list] = {}

        # fee event_id → {customer_id, amount}
        self.fees: dict[str, dict] = {}
        self.refunded_fees: set[str] = set()

        # withdrawal_id → {customer_id, amount}
        self.withdrawals: dict[str, dict] = {}
        self.withdrawal_status: dict[str, str] = {}

        # order_id → hold record
        # {customer_id, side, symbol, asset_class, cash_hold, qty_hold,
        #  total_quantity, remaining_quantity, broker (routing decision)}
        self.holds: dict[str, dict] = {}

        # trade_id → {customer_id, side, principal}
        self.trades: dict[str, dict] = {}
        self.used_trade_ids: set[str] = set()
        self.settled_trades: set[str] = set()
        self.closed_orders: set[str] = set()

        # FIFO lot book: (customer_id, symbol) → [{"quantity": D, "total_cost": D}, ...]
        # NOTE: we store total_cost per lot (not unit_cost) so FIFO rounding
        # matches: round(lot_total × sold_qty / lot_qty)
        self.lots: dict[tuple, list] = defaultdict(list)

        # lot-change audit per event (for reversal)
        self.lot_effects: dict[str, dict] = {}
        self.state_effects: dict[str, list[tuple]] = defaultdict(list)
        self.reversed_events: set[str] = set()

    # -----------------------------------------------------------------------
    # Dispatcher
    # -----------------------------------------------------------------------

    def apply(self, ev: dict) -> list[dict]:
        """Post one event, return its legs.  Idempotent on duplicate event_ids."""
        try:
            eid = ev["event_id"]
            etype = ev["type"]
            payload = ev["payload"]
        except (KeyError, TypeError):
            return []
        if eid in self.seen:
            return []
        self.seen.add(eid)
        self.events[eid] = ev

        handler = getattr(self, "on_" + etype, None)
        if handler is None:
            self.todo[etype] += 1
            return []
        state = self._copy_mutable_state()
        try:
            legs = handler(payload, ev) or []
        except Rejected:
            self._restore_mutable_state(state)
            return []
        except InvalidOperation:
            self._restore_mutable_state(state)
            return []
        except Exception as exc:
            self._restore_mutable_state(state)
            LOGGER.exception(
                "Book.apply failed event_id=%s type=%s exception=%s message=%s",
                eid,
                etype,
                type(exc).__name__,
                exc,
            )
            if DEBUG_RAISE_UNEXPECTED:
                raise
            return []

        try:
            self._post(legs)
        except Exception as exc:
            self._restore_mutable_state(state)
            LOGGER.exception(
                "Book.apply failed posting event_id=%s type=%s exception=%s message=%s",
                eid,
                etype,
                type(exc).__name__,
                exc,
            )
            if DEBUG_RAISE_UNEXPECTED:
                raise
            return []
        self.posted_legs[eid] = legs
        return legs

    def _copy_mutable_state(self) -> dict:
        return {
            "balances": defaultdict(lambda: ZERO, self.balances),
            "posted_legs": copy.deepcopy(self.posted_legs),
            "fees": copy.deepcopy(self.fees),
            "refunded_fees": set(self.refunded_fees),
            "withdrawals": copy.deepcopy(self.withdrawals),
            "withdrawal_status": dict(self.withdrawal_status),
            "holds": copy.deepcopy(self.holds),
            "trades": copy.deepcopy(self.trades),
            "used_trade_ids": set(self.used_trade_ids),
            "settled_trades": set(self.settled_trades),
            "closed_orders": set(self.closed_orders),
            "reversed_events": set(self.reversed_events),
            "lots": defaultdict(
                list,
                {k: [dict(lot) for lot in lots] for k, lots in self.lots.items()},
            ),
            "lot_effects": copy.deepcopy(self.lot_effects),
            "state_effects": defaultdict(list, copy.deepcopy(dict(self.state_effects))),
        }

    def _restore_mutable_state(self, state: dict) -> None:
        self.balances = state["balances"]
        self.posted_legs = state["posted_legs"]
        self.fees = state["fees"]
        self.refunded_fees = state["refunded_fees"]
        self.withdrawals = state["withdrawals"]
        self.withdrawal_status = state["withdrawal_status"]
        self.holds = state["holds"]
        self.trades = state["trades"]
        self.used_trade_ids = state["used_trade_ids"]
        self.settled_trades = state["settled_trades"]
        self.closed_orders = state["closed_orders"]
        self.reversed_events = state["reversed_events"]
        self.lots = state["lots"]
        self.lot_effects = state["lot_effects"]
        self.state_effects = state["state_effects"]

    def _post(self, legs: list[dict]) -> None:
        dr = sum((D(l["debit"])  for l in legs), ZERO)
        cr = sum((D(l["credit"]) for l in legs), ZERO)
        if money(dr) != money(cr):
            raise AssertionError(f"unbalanced: dr {dr} cr {cr}")
        for l in legs:
            self.balances[(l["customer_id"], l["account"])] += (
                D(l["debit"]) - D(l["credit"])
            )

    # -----------------------------------------------------------------------
    # FIFO lot helpers
    # -----------------------------------------------------------------------

    def _add_lot(self, cid: str, symbol: str, quantity: Decimal,
                 total_cost: Decimal, event_id: str | None = None) -> None:
        """Append a new lot.  total_cost is the full dollar cost of the lot."""
        lot = {"quantity": quantity, "total_cost": total_cost, "source_event_id": event_id}
        self.lots[(cid, symbol)].append(lot)
        if event_id:
            self.lot_effects[event_id] = {
                "type": "add", "cid": cid, "symbol": symbol, "lot": dict(lot),
            }

    def _consume_lots(self, cid: str, symbol: str, quantity: Decimal,
                      event_id: str | None = None) -> Decimal:
        """Consume lots FIFO, return total FIFO cost.

        Cost relief per partial lot = round(lot_total × sold_qty / lot_qty).
        Raises Rejected on oversell WITHOUT mutating the lot book.
        """
        lots = self.lots[(cid, symbol)]
        remaining = quantity
        total_cost = ZERO
        consumed: list[dict] = []

        for lot in list(lots):   # iterate a copy; we modify lots below
            if remaining <= ZERO:
                break
            lot_qty = lot["quantity"]
            lot_cost = lot["total_cost"]
            if lot_qty <= remaining:
                relief = money(lot_cost)           # consume whole lot
                consumed.append({"quantity": lot_qty, "total_cost": lot_cost,
                                 "source_event_id": lot.get("source_event_id"),
                                 "relief": relief})
                total_cost += relief
                remaining -= lot_qty
            else:
                # Partial: cost = round(lot_total × sold / lot_qty)
                relief = money(lot_cost * remaining / lot_qty)
                consumed.append({"quantity": remaining, "total_cost": lot_cost,
                                 "source_event_id": lot.get("source_event_id"),
                                 "partial_qty": lot_qty, "relief": relief})
                total_cost += relief
                remaining = ZERO

        if remaining > ZERO:
            raise Rejected(
                f"oversell: {cid} tried to sell {quantity} of {symbol}, "
                f"only {quantity - remaining} available"
            )

        # Apply the mutations now that we know it won't fail
        for entry in consumed:
            front = lots[0]
            if entry.get("partial_qty"):
                front["quantity"] -= entry["quantity"]
                front["total_cost"] -= entry["relief"]
            else:
                lots.pop(0)

        if event_id:
            self.lot_effects[event_id] = {
                "type": "consume", "cid": cid, "symbol": symbol,
                "consumed": consumed,
            }

        return money(total_cost)

    def _undo_lot_effect(self, event_id: str) -> None:
        effect = self.lot_effects.get(event_id)
        if not effect:
            return
        cid, symbol = effect["cid"], effect["symbol"]
        lots = self.lots[(cid, symbol)]

        if effect["type"] == "add":
            source_event_id = effect["lot"].get("source_event_id")
            for i, lot in enumerate(lots):
                if source_event_id and lot.get("source_event_id") == source_event_id:
                    lots.pop(i)
                    break
                if lot["total_cost"] == effect["lot"]["total_cost"] and \
                   lot["quantity"] == effect["lot"]["quantity"]:
                    lots.pop(i)
                    break

        elif effect["type"] == "consume":
            # Restore consumed lots using original total_cost values recorded
            for entry in reversed(effect["consumed"]):
                lots.insert(0, {
                    "quantity": entry["quantity"],
                    "total_cost": entry["total_cost"],
                    "source_event_id": entry.get("source_event_id"),
                })

        elif effect["type"] == "split":
            for lot, quantity in zip(lots, effect["quantities"]):
                lot["quantity"] = quantity

        elif effect["type"] == "symbol_change":
            old_key = (cid, effect["old_symbol"])
            new_key = (cid, effect["new_symbol"])
            if effect["old_lots"] is None:
                self.lots.pop(old_key, None)
            else:
                self.lots[old_key] = effect["old_lots"]
            if effect["new_lots"] is None:
                self.lots.pop(new_key, None)
            else:
                self.lots[new_key] = effect["new_lots"]
            for oid, symbol in effect.get("hold_symbols", {}).items():
                if oid in self.holds:
                    self.holds[oid]["symbol"] = symbol

    def _record_effect(self, event_id: str, *effect: Any) -> None:
        self.state_effects[event_id].append(tuple(effect))

    def _undo_state_effects(
        self, event_id: str, *, restore_order_lifecycle: bool = True
    ) -> None:
        for effect in reversed(self.state_effects.get(event_id, [])):
            kind = effect[0]
            if kind == "fee_charged":
                self.fees.pop(effect[1], None)
            elif kind == "fee_refund":
                self.refunded_fees.discard(effect[1])
            elif kind == "withdrawal_requested":
                wid = effect[1]
                self.withdrawals.pop(wid, None)
                self.withdrawal_status.pop(wid, None)
            elif kind == "withdrawal_status":
                _, wid, previous = effect
                self.withdrawal_status[wid] = previous
            elif kind == "order_placed":
                _, oid, previous_hold, was_closed = effect
                if previous_hold is None:
                    self.holds.pop(oid, None)
                else:
                    self.holds[oid] = previous_hold
                if was_closed:
                    self.closed_orders.add(oid)
                else:
                    self.closed_orders.discard(oid)
            elif kind == "order_closed":
                if not restore_order_lifecycle:
                    continue
                _, oid, previous_hold, was_closed = effect
                self.holds[oid] = previous_hold
                if was_closed:
                    self.closed_orders.add(oid)
                else:
                    self.closed_orders.discard(oid)
            elif kind == "fill":
                self.trades.pop(effect[1], None)
                self.used_trade_ids.discard(effect[1])
            elif kind == "trade_settled":
                self.settled_trades.discard(effect[1])
            elif kind == "reversal":
                self.reversed_events.discard(effect[1])

    # -----------------------------------------------------------------------
    # CASH EVENTS
    # -----------------------------------------------------------------------

    def on_deposit(self, p: dict, ev: dict) -> list[dict]:
        """Dr 1100  Cr 2010"""
        amount = money(D(str(p["amount"])))
        cid = p["customer_id"]
        return [leg("1100", cid, debit=amount), leg("2010", cid, credit=amount)]

    def on_fee_charged(self, p: dict, ev: dict) -> list[dict]:
        """Dr 2010  Cr 1100  — firm charges customer a fee from their wallet."""
        amount = money(D(str(p["amount"])))
        cid = p["customer_id"]
        self.fees[ev["event_id"]] = {"customer_id": cid, "amount": amount}
        self._record_effect(ev["event_id"], "fee_charged", ev["event_id"])
        return [leg("2010", cid, debit=amount), leg("1100", cid, credit=amount)]

    def on_fee_refund(self, p: dict, ev: dict) -> list[dict]:
        """Dr 1100  Cr 2010  — amount NOT in payload, look up from original fee."""
        source_id = p["refunds_source_id"]
        cid = p["customer_id"]
        if source_id in self.refunded_fees:
            raise Rejected(f"fee {source_id} already refunded")
        fee = self.fees.get(source_id)
        if fee is None:
            src = self.events.get(source_id)
            if src is None or source_id not in self.posted_legs:
                raise Rejected(f"fee_charged {source_id} unknown or not posted")
            amount = money(D(str(src["payload"]["amount"])))
        else:
            amount = fee["amount"]
        self.refunded_fees.add(source_id)
        self._record_effect(ev["event_id"], "fee_refund", source_id)
        return [leg("1100", cid, debit=amount), leg("2010", cid, credit=amount)]

    def on_interest_credited(self, p: dict, ev: dict) -> list[dict]:
        """Dr 1100 gross  Cr 2010 customer_share  Cr 4200 firm_share"""
        cid = p["customer_id"]
        gross = money(D(str(p["gross_amount"])))
        share = money(D(str(p["customer_share"])))
        firm  = money(gross - share)
        legs  = [leg("1100", cid, debit=gross), leg("2010", cid, credit=share)]
        if firm != ZERO:
            legs.append(leg("4200", cid, credit=firm))
        return legs

    def on_transfer_between_customers(self, p: dict, ev: dict) -> list[dict]:
        """Dr 2010 (from)  Cr 2010 (to)  — both legs on 2010, account nets zero."""
        amount   = money(D(str(p["amount"])))
        from_cid = p["from_customer_id"]
        to_cid   = p["to_customer_id"]
        return [leg("2010", from_cid, debit=amount), leg("2010", to_cid, credit=amount)]

    def on_fx_deposit(self, p: dict, ev: dict) -> list[dict]:
        """Dr 1100 market_rate  Cr 2010 customer_rate  Cr 4100 spread"""
        cid      = p["customer_id"]
        market   = money(D(str(p["usd_at_market_rate"])))
        customer = money(D(str(p["usd_at_customer_rate"])))
        spread   = money(market - customer)
        if spread < ZERO:
            raise Rejected("negative FX spread is bad data")
        legs = [leg("1100", cid, debit=market), leg("2010", cid, credit=customer)]
        if spread > ZERO:
            legs.append(leg("4100", cid, credit=spread))
        return legs

    def on_withdrawal_requested(self, p: dict, ev: dict) -> list[dict]:
        """Dr 2010  Cr 2300"""
        wid    = p["withdrawal_id"]
        cid    = p["customer_id"]
        amount = money(D(str(p["amount"])))
        if wid in self.withdrawals:
            raise Rejected(f"duplicate withdrawal_id {wid}")
        self.withdrawals[wid] = {"customer_id": cid, "amount": amount}
        self.withdrawal_status[wid] = "open"
        self._record_effect(ev["event_id"], "withdrawal_requested", wid)
        return [leg("2010", cid, debit=amount), leg("2300", cid, credit=amount)]

    def on_withdrawal_settled(self, p: dict, ev: dict) -> list[dict]:
        """Dr 2300  Cr 1100"""
        wid = p["withdrawal_id"]
        w   = self.withdrawals.get(wid)
        if w is None or self.withdrawal_status.get(wid) != "open":
            raise Rejected(f"unknown withdrawal {wid}")
        self._record_effect(ev["event_id"], "withdrawal_status", wid, "open")
        self.withdrawal_status[wid] = "settled"
        return [leg("2300", w["customer_id"], debit=w["amount"]),
                leg("1100", w["customer_id"], credit=w["amount"])]

    def on_withdrawal_rejected(self, p: dict, ev: dict) -> list[dict]:
        """Dr 2300  Cr 2010"""
        wid = p["withdrawal_id"]
        w   = self.withdrawals.get(wid)
        if w is None or self.withdrawal_status.get(wid) != "open":
            raise Rejected(f"unknown withdrawal {wid}")
        self._record_effect(ev["event_id"], "withdrawal_status", wid, "open")
        self.withdrawal_status[wid] = "rejected"
        return [leg("2300", w["customer_id"], debit=w["amount"]),
                leg("2010", w["customer_id"], credit=w["amount"])]

    # -----------------------------------------------------------------------
    # ORDER / TRADE EVENTS
    # -----------------------------------------------------------------------

    def on_order_placed(self, p: dict, ev: dict) -> list[dict]:
        """No legs.  Creates a hold; for buy: qty × limit_price + est_charges."""
        oid        = p["order_id"]
        cid        = p["customer_id"]
        side       = p["side"]
        symbol     = p["symbol"]
        asset_class = p.get("asset_class", "equity")
        quantity   = D(str(p["quantity"]))
        limit_price = D(str(p["limit_price"]))
        if oid in self.closed_orders:
            raise Rejected(f"order_id {oid} is already closed")
        if oid in self.holds and not self.holds[oid].get("out_of_order"):
            raise Rejected(f"duplicate order_id {oid}")
        if quantity <= ZERO or limit_price < ZERO:
            raise Rejected("invalid order quantity or price")

        if side == "buy":
            est_charges = D(str(p.get("est_charges", "0")))
            cash_hold   = money(quantity * limit_price + est_charges)
            qty_hold    = ZERO
        else:
            cash_hold = ZERO
            qty_hold  = quantity

        # Routing decision: choose cheapest broker for this asset_class
        principal_est = money(quantity * limit_price)
        broker = route_order(asset_class, principal_est)

        previous_hold = copy.deepcopy(self.holds.get(oid)) if oid in self.holds else None
        was_closed = oid in self.closed_orders
        if oid in self.holds and self.holds[oid].get("out_of_order"):
            hold = self.holds[oid]
            already_filled = hold.get("filled_quantity", ZERO)
            remaining_quantity = max(ZERO, quantity - already_filled)
            hold["out_of_order"] = False
            hold["customer_id"] = cid
            hold["side"] = side
            hold["symbol"] = symbol
            hold["asset_class"] = asset_class
            hold["limit_price"] = limit_price
            hold["total_quantity"] = quantity
            hold["remaining_quantity"] = remaining_quantity
            hold["broker"] = broker
            hold["original_cash_hold"] = cash_hold
            hold["original_qty_hold"] = qty_hold
            if side == "buy":
                hold["cash_hold"] = (
                    money(cash_hold * remaining_quantity / quantity)
                    if quantity > ZERO else ZERO
                )
                hold["qty_hold"] = ZERO
            else:
                hold["cash_hold"] = ZERO
                hold["qty_hold"] = remaining_quantity
        else:
            self.holds[oid] = {
                "customer_id":       cid,
                "side":              side,
                "symbol":            symbol,
                "asset_class":       asset_class,
                "limit_price":       limit_price,
                "cash_hold":         cash_hold,
                "original_cash_hold": cash_hold,
                "qty_hold":          qty_hold,
                "original_qty_hold":  qty_hold,
                "total_quantity":    quantity,
                "remaining_quantity": quantity,
                "filled_quantity":    ZERO,
                "broker":            broker,
                "out_of_order":      False,
            }
        self._record_effect(ev["event_id"], "order_placed", oid, previous_hold, was_closed)
        return []

    def on_order_partially_filled(self, p: dict, ev: dict) -> list[dict]:
        return self._handle_fill(p, ev, is_final=False)

    def on_order_filled(self, p: dict, ev: dict) -> list[dict]:
        return self._handle_fill(p, ev, is_final=True)

    def _handle_fill(self, p: dict, ev: dict, is_final: bool) -> list[dict]:
        """Core fill logic — implements the full tariff.

        BUY:
          Dr 2010  P + b + c + r       Cr 2350  P
          Dr 1200  P                   Cr 2100  P
          Dr 5000  bc                  Cr 4000  b
          Dr 5010  cc                  Cr 4010  c
          Dr 5100  ps                  Cr 2400  r
                                       Cr 241x  bc
                                       Cr 2420  cc
                                       Cr 2430  ps

        SELL: same firm economics; customer credited P net of charges; cost basis relieved.
        """
        oid        = p["order_id"]
        cid        = p["customer_id"]
        side       = p["side"]
        symbol     = p["symbol"]
        asset_class = p.get("asset_class", "equity")
        fill_qty   = D(str(p["quantity"]))
        principal  = money(D(str(p.get("principal", "0"))))
        broker_name = p["broker"]
        partner_rate = D(str(p.get("partner_rate", "0")))
        trade_id   = p.get("trade_id") or ev.get("trade_id") or ev["event_id"]
        eid        = ev["event_id"]

        if fill_qty <= ZERO or principal < ZERO:
            raise Rejected("invalid fill quantity or principal")
        if trade_id in self.used_trade_ids:
            raise Rejected(f"duplicate trade_id {trade_id}")
        if oid in self.closed_orders:
            raise Rejected(f"order {oid} is already closed")

        if oid not in self.holds:
            # Out-of-order fill arriving before order_placed
            self.holds[oid] = {
                "customer_id":       cid,
                "side":              side,
                "symbol":            symbol,
                "asset_class":       asset_class,
                "limit_price":       p.get("price", ZERO),
                "cash_hold":         ZERO,
                "original_cash_hold": ZERO,
                "qty_hold":          ZERO,
                "original_qty_hold":  ZERO,
                "total_quantity":    fill_qty,
                "remaining_quantity": fill_qty,
                "filled_quantity":    ZERO,
                "broker":            broker_name,
                "out_of_order":      True,
            }
        hold = self.holds[oid]

        if not hold.get("out_of_order"):
            if hold.get("customer_id") != cid or hold.get("side") != side:
                raise Rejected(f"fill does not match order {oid}")
            if hold.get("symbol") != symbol:
                raise Rejected(f"fill symbol does not match order {oid}")

        if not hold.get("out_of_order"):
            if hold.get("remaining_quantity", ZERO) <= ZERO:
                raise Rejected(f"order {oid} has no remaining quantity")
            if fill_qty > hold["remaining_quantity"]:
                raise Rejected(f"fill quantity {fill_qty} exceeds remaining order quantity {hold['remaining_quantity']}")

        # Store trade for settlement
        self.trades[trade_id] = {"customer_id": cid, "side": side, "principal": principal}
        self.used_trade_ids.add(trade_id)

        previous_hold = copy.deepcopy(hold)
        was_closed = oid in self.closed_orders
        filled_so_far = hold.get("filled_quantity", ZERO) + fill_qty
        if is_final:
            hold["cash_hold"]         = ZERO
            hold["qty_hold"]          = ZERO
            hold["remaining_quantity"] = ZERO
            hold["filled_quantity"] = filled_so_far
            self.closed_orders.add(oid)
        else:
            total_qty = hold["total_quantity"]
            if total_qty > ZERO:
                if side == "buy":
                    released = money(hold["original_cash_hold"] * fill_qty / total_qty)
                    hold["cash_hold"] = max(ZERO, hold["cash_hold"] - released)
                else:
                    hold["qty_hold"] = max(ZERO, hold["qty_hold"] - fill_qty)
            hold["remaining_quantity"] = max(ZERO, hold["remaining_quantity"] - fill_qty)
            hold["filled_quantity"] = filled_so_far
        self._record_effect(ev["event_id"], "order_closed", oid, previous_hold, was_closed)

        # ------------------------------------------------------------------
        # Compute all fee amounts from the tariff
        # ------------------------------------------------------------------
        ch = compute_charges(principal, broker_name, asset_class, partner_rate)
        b   = ch["brokerage"]
        c   = ch["custody"]
        r   = ch["reg_fee"]
        bc  = ch["broker_cost"]
        cc  = ch["custody_cost"]
        ps  = ch["partner_share"]
        brk_payable = ch["broker_payable"]

        self._record_effect(eid, "fill", trade_id)

        # ------------------------------------------------------------------
        # Build legs
        # ------------------------------------------------------------------
        if side == "buy":
            self._add_lot(cid, symbol, fill_qty, principal, event_id=eid)

            legs = [
                leg("2010", cid, debit=principal + b + c + r),
                leg("1200", cid, debit=principal),
                leg("5000", cid, debit=bc),
                leg("5010", cid, debit=cc),
                leg("5100", cid, debit=ps),
                leg("2350", cid, credit=principal),
                leg("2100", cid, credit=principal),
                leg("4000", cid, credit=b),
                leg("4010", cid, credit=c),
                leg("2400", cid, credit=r),
                leg(brk_payable, cid, credit=bc),
                leg("2420", cid, credit=cc),
                leg("2430", cid, credit=ps),
            ]
            # Drop any zero-amount legs (e.g. partner_share = 0)
            return [l for l in legs if l["debit"] != "0.00" or l["credit"] != "0.00"]

        else:  # sell
            fifo_cost = self._consume_lots(cid, symbol, fill_qty, event_id=eid)

            legs = [
                leg("1150", cid, debit=principal),
                leg("2100", cid, debit=fifo_cost),
                leg("5000", cid, debit=bc),
                leg("5010", cid, debit=cc),
                leg("5100", cid, debit=ps),
                leg("2010", cid, credit=principal - b - c - r),
                leg("1200", cid, credit=fifo_cost),
                leg("4000", cid, credit=b),
                leg("4010", cid, credit=c),
                leg("2400", cid, credit=r),
                leg(brk_payable, cid, credit=bc),
                leg("2420", cid, credit=cc),
                leg("2430", cid, credit=ps),
            ]
            return [l for l in legs if l["debit"] != "0.00" or l["credit"] != "0.00"]

    def on_trade_settled(self, p: dict, ev: dict) -> list[dict]:
        """Cash actually moves on settlement day.
        buy:  Dr 2350  Cr 1100
        sell: Dr 1100  Cr 1150
        """
        trade_id = p["trade_id"]
        trade = self.trades.get(trade_id)
        if trade is None or trade_id in self.settled_trades:
            raise Rejected(f"unknown trade_id {trade_id}")
        self.settled_trades.add(trade_id)
        self._record_effect(ev["event_id"], "trade_settled", trade_id)
        cid = trade["customer_id"]
        principal = trade["principal"]
        if trade["side"] == "buy":
            return [leg("2350", cid, debit=principal), leg("1100", cid, credit=principal)]
        else:
            return [leg("1100", cid, debit=principal), leg("1150", cid, credit=principal)]

    def on_order_cancelled(self, p: dict, ev: dict) -> list[dict]:
        """No legs.  Release the remaining hold."""
        oid = p["order_id"]
        hold = self.holds.get(oid)
        if hold is not None:
            previous_hold = copy.deepcopy(hold)
            was_closed = oid in self.closed_orders
            hold["cash_hold"] = ZERO
            hold["qty_hold"] = ZERO
            hold["remaining_quantity"] = ZERO
            self.closed_orders.add(oid)
            self._record_effect(ev["event_id"], "order_closed", oid, previous_hold, was_closed)
        return []

    def on_order_rejected(self, p: dict, ev: dict) -> list[dict]:
        return self.on_order_cancelled(p, ev)

    # -----------------------------------------------------------------------
    # SETTLEMENT PAYABLES
    # -----------------------------------------------------------------------

    def _settle_payable(self, account: str, cid: str) -> list[dict]:
        amount = money(-self.balances[(cid, account)])
        if amount <= ZERO:
            return []
        return [leg(account, cid, debit=amount), leg("1100", cid, credit=amount)]

    def on_broker_fees_settled(self, p: dict, ev: dict) -> list[dict]:
        broker = p["broker"]
        account = BROKERS[broker]["payable_account"]
        return self._settle_payable(account, p["customer_id"])

    def on_custodian_fees_settled(self, p: dict, ev: dict) -> list[dict]:
        return self._settle_payable("2420", p["customer_id"])

    def on_reg_fees_remitted(self, p: dict, ev: dict) -> list[dict]:
        return self._settle_payable("2400", p["customer_id"])

    def on_partner_payout(self, p: dict, ev: dict) -> list[dict]:
        return self._settle_payable("2430", p["customer_id"])

    # -----------------------------------------------------------------------
    # CORPORATE ACTIONS
    # -----------------------------------------------------------------------

    def on_dividend_cash(self, p: dict, ev: dict) -> list[dict]:
        """Dr 1100 net  Cr 2010 net  — tax withheld at source, no payable."""
        cid = p["customer_id"]
        net = money(D(str(p["net_amount"])))
        return [leg("1100", cid, debit=net), leg("2010", cid, credit=net)]

    def on_dividend_reinvested(self, p: dict, ev: dict) -> list[dict]:
        """Dr 1200 net  Cr 2100 net  — no cash; add a new lot."""
        cid    = p["customer_id"]
        net    = money(D(str(p["net_amount"])))
        symbol = p["symbol"]
        qty    = D(str(p["reinvest_quantity"]))
        self._add_lot(cid, symbol, qty, net, event_id=ev["event_id"])
        return [leg("1200", cid, debit=net), leg("2100", cid, credit=net)]

    def on_stock_split(self, p: dict, ev: dict) -> list[dict]:
        """No legs.  Per-customer: quantity × ratio, total cost unchanged."""
        cid        = p["customer_id"]
        symbol     = p["symbol"]
        ratio_from = D(str(p["ratio_from"]))
        ratio_to   = D(str(p["ratio_to"]))
        if ratio_from <= ZERO or ratio_to <= ZERO:
            raise Rejected("invalid split ratio")
        ratio      = ratio_to / ratio_from
        lots = self.lots.get((cid, symbol), [])
        self.lot_effects[ev["event_id"]] = {
            "type": "split",
            "cid": cid,
            "symbol": symbol,
            "quantities": [lot["quantity"] for lot in lots],
        }
        for lot in lots:
            lot["quantity"] = lot["quantity"] * ratio
            # total_cost is UNCHANGED; only quantity (and thus implied unit cost) shifts
        return []

    def on_symbol_change(self, p: dict, ev: dict) -> list[dict]:
        """No legs.  Per-customer: re-key the lot book."""
        cid     = p["customer_id"]
        old_sym = p["old_symbol"]
        new_sym = p["new_symbol"]
        key_old = (cid, old_sym)
        key_new = (cid, new_sym)
        old_lots = copy.deepcopy(self.lots.get(key_old)) if key_old in self.lots else None
        new_lots = copy.deepcopy(self.lots.get(key_new)) if key_new in self.lots else None
        if key_old in self.lots:
            moved = self.lots.pop(key_old)
            self.lots[key_new].extend(moved)
        self.lot_effects[ev["event_id"]] = {
            "type": "symbol_change",
            "cid": cid,
            "symbol": old_sym,
            "old_symbol": old_sym,
            "new_symbol": new_sym,
            "old_lots": old_lots,
            "new_lots": new_lots,
        }
        return []

    # -----------------------------------------------------------------------
    # CORRECTIONS
    # -----------------------------------------------------------------------

    def on_reversal(self, p: dict, ev: dict) -> list[dict]:
        """Post exact inverse of the original's legs + undo its lot-book effect."""
        target_id = p["reverses_event_id"]
        if target_id not in self.seen and target_id not in self.events:
            raise Rejected(f"original event {target_id} unknown; cannot reverse")
        if target_id not in self.posted_legs or target_id in self.reversed_events:
            raise Rejected(f"original event {target_id} has no posted legs or already reversed")
        original_legs = self.posted_legs.get(target_id, [])
        self.reversed_events.add(target_id)
        # Undo the lot-book change first (before we invert the accounting)
        self._undo_lot_effect(target_id)
        target_type = (self.events.get(target_id) or {}).get("type")
        self._undo_state_effects(
            target_id,
            restore_order_lifecycle=target_type not in {
                "order_partially_filled",
                "order_filled",
            },
        )
        self._record_effect(ev["event_id"], "reversal", target_id)
        # Swap debit ↔ credit on every leg
        return [
            leg(l["account"], l["customer_id"],
                debit=D(l["credit"]), credit=D(l["debit"]))
            for l in original_legs
        ]

    # -----------------------------------------------------------------------
    # REPORTING  (used by client.py for checkpoint responses)
    # -----------------------------------------------------------------------

    def snapshot(self) -> dict:
        """Full state for a checkpoint_request.

        Shape:
          {
            trial_balance: {account: str, ...},
            customers: {cid: {wallet_cash, cash_hold, positions: {sym: {quantity, cost_basis}}}},
            open_order_routes: {order_id: broker_id}
          }
        """
        # -- Trial balance --------------------------------------------------
        tb: dict[str, Decimal] = defaultdict(lambda: ZERO)
        for (_cid, acct), bal in self.balances.items():
            tb[acct] += bal

        # -- Customer records -----------------------------------------------
        customers: dict[str, dict[str, Any]] = {}

        def _cust(cid: str) -> dict:
            return customers.setdefault(
                cid, {"wallet_cash": ZERO, "cash_hold": ZERO, "positions": {}})

        # Wallet cash comes from 2010 (liability → credit-positive)
        for (cid, acct), bal in self.balances.items():
            if acct == "2010":
                _cust(cid)["wallet_cash"] += -bal

        def format_qty(q: Decimal) -> str:
            s = format(q, 'f')
            if '.' in s:
                s = s.rstrip('0').rstrip('.')
            return s if s else "0"

        # Positions come from the lot book (ground truth for share holdings)
        for (cid, symbol), lots in self.lots.items():
            total_qty  = sum(lot["quantity"]   for lot in lots)
            total_cost = sum(lot["total_cost"] for lot in lots)
            if total_qty > 0:
                _cust(cid)["positions"][symbol] = {
                    "quantity":   format_qty(total_qty),
                    "cost_basis": str(money(total_cost)),
                }

        # Cash holds from open buy orders (only active orders with remaining quantity)
        for oid, hold in self.holds.items():
            if oid not in self.closed_orders and hold.get("remaining_quantity", ZERO) > ZERO:
                ch = hold.get("cash_hold", ZERO)
                if ch > ZERO:
                    _cust(hold["customer_id"])["cash_hold"] += ch

        # -- Open order routes (still-open orders → assigned broker or optimal broker)
        open_order_routes = {}
        for oid, hold in self.holds.items():
            rem = hold.get("remaining_quantity", ZERO)
            if oid not in self.closed_orders and rem and rem > ZERO:
                broker = hold.get("broker")
                if not broker:
                    asset_class = hold.get("asset_class", "equity")
                    limit_price = hold.get("limit_price", ZERO)
                    total_qty = hold.get("total_quantity", rem)
                    broker = route_order(asset_class, money(total_qty * limit_price))
                if broker:
                    open_order_routes[oid] = broker

        return {
            "trial_balance": {
                a: str(money(v)) for a, v in sorted(tb.items())
            },
            "customers": {
                cid: {
                    "wallet_cash": str(money(c["wallet_cash"])),
                    "cash_hold":   str(money(c["cash_hold"])),
                    "positions":   c["positions"],
                }
                for cid, c in sorted(customers.items())
            },
            "open_order_routes": open_order_routes,
        }

    def snapshot_as_of(self, as_of_event_id: str) -> dict:
        replay = Book()
        for event_id, event in self.events.items():
            replay.apply(copy.deepcopy(event))
            if event_id == as_of_event_id:
                return replay.snapshot()
        return self.snapshot()


# ---------------------------------------------------------------------------

class Rejected(Exception):
    """Raise from a handler to submit legs=[] for this event and carry on."""
