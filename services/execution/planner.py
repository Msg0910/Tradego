"""
Tradego Phase 7 Execution Layer — Execution Planner.

Transforms risk-approved ApprovedTradeIntent instances into canonical OrderRequests.
Derives OrderSide, OrderPurpose, and PositionEffect deterministically.
Synthesizes stable client_order_id and computes SHA-256 idempotency keys.
"""

from datetime import datetime, timezone
import hashlib
from typing import Optional, Tuple

from services.execution.models import (
    OrderPurpose,
    OrderRequest,
    OrderSide,
    OrderType,
    PositionEffect,
    TimeInForce,
)
from services.risk.models import ApprovedTradeIntent, PositionSnapshot
from services.signals.models import SignalType


def derive_order_side_and_position_effect(
    signal_type: SignalType, direction: int, is_position_open: bool = False
) -> Tuple[OrderSide, OrderPurpose, PositionEffect]:
    """
    Deterministically maps ApprovedTradeIntent signal_type and direction to canonical
    OrderSide, OrderPurpose, and PositionEffect. Guarantees that exits never invert side.
    """
    match signal_type:
        case SignalType.ENTRY_LONG:
            effect = PositionEffect.INCREASE if is_position_open else PositionEffect.OPEN
            return OrderSide.BUY, OrderPurpose.ENTRY, effect

        case SignalType.ENTRY_SHORT:
            effect = PositionEffect.INCREASE if is_position_open else PositionEffect.OPEN
            return OrderSide.SELL, OrderPurpose.ENTRY, effect

        case SignalType.EXIT_LONG:
            # Exiting long position requires a SELL order with CLOSE effect
            return OrderSide.SELL, OrderPurpose.EXIT, PositionEffect.CLOSE

        case SignalType.EXIT_SHORT:
            # Exiting short position requires a BUY order with CLOSE effect
            return OrderSide.BUY, OrderPurpose.EXIT, PositionEffect.CLOSE

        case SignalType.SCALE_IN:
            side = OrderSide.BUY if direction > 0 else OrderSide.SELL
            return side, OrderPurpose.SCALE, PositionEffect.INCREASE

        case SignalType.SCALE_OUT:
            # Scaling out of long requires SELL; scaling out of short requires BUY
            side = OrderSide.SELL if direction > 0 else OrderSide.BUY
            return side, OrderPurpose.SCALE, PositionEffect.REDUCE

        case _:
            raise ValueError(f"Unsupported SignalType for execution planning: {signal_type}")


class ExecutionPlanner:
    """
    Translates ApprovedTradeIntent into canonical, broker-agnostic OrderRequest.
    Validates temporal horizons, ensures quantity conservation, enforces reference-price safety,
    and produces globally stable client_order_id values.
    """

    @staticmethod
    def plan_order(
        intent: ApprovedTradeIntent,
        current_position: Optional[PositionSnapshot] = None,
        order_type: OrderType = OrderType.MARKET,
        limit_price: Optional[float] = None,
        time_in_force: TimeInForce = TimeInForce.DAY,
        current_time: Optional[datetime] = None,
        metadata: Optional[Tuple[Tuple[str, str], ...]] = None,
    ) -> OrderRequest:
        """
        Creates an immutable, validated OrderRequest from risk authorization.
        """
        # 1. Temporal Horizon & Expiry Check
        now = current_time or datetime.now(timezone.utc)
        if intent.expiry_timestamp is not None:
            # Normalize timezones for comparison
            intent_expiry = intent.expiry_timestamp
            if intent_expiry.tzinfo is None and now.tzinfo is not None:
                intent_expiry = intent_expiry.replace(tzinfo=timezone.utc)
            elif intent_expiry.tzinfo is not None and now.tzinfo is None:
                now = now.replace(tzinfo=timezone.utc)

            if now >= intent_expiry:
                raise ValueError(
                    f"ApprovedTradeIntent {intent.intent_id} has expired at {intent_expiry} (current time {now})."
                )

        # 2. Risk Boundary Validation
        if intent.permitted_quantity <= 0:
            raise ValueError(f"Invalid permitted_quantity in intent: {intent.permitted_quantity}")
        if intent.approved_entry_price <= 0.0:
            raise ValueError(f"Invalid approved_entry_price in intent: {intent.approved_entry_price}")

        # 3. Derive OrderSide, OrderPurpose, PositionEffect
        is_position_open = current_position is not None and current_position.net_quantity != 0
        side, purpose, position_effect = derive_order_side_and_position_effect(
            signal_type=intent.signal_type,
            direction=intent.direction,
            is_position_open=is_position_open,
        )

        # 4. Synthesize Globally Stable client_order_id across retries
        ts = intent.market_timestamp or intent.signal_generated_timestamp or now
        date_str = ts.strftime("%Y%m%d")
        strat_prefix = intent.strategy_id[:4] if len(intent.strategy_id) >= 4 else intent.strategy_id.ljust(4, "X")
        intent_prefix = intent.intent_id[:12] if len(intent.intent_id) >= 12 else intent.intent_id.ljust(12, "0")
        client_order_id = f"TG-{strat_prefix}-{date_str}-{intent_prefix}"

        # 5. Compute Deterministic Idempotency Key
        idempotency_raw = f"{client_order_id}:{intent.fingerprint}:{intent.permitted_quantity}:{intent.reaffirmation_key}"
        idempotency_key = hashlib.sha256(idempotency_raw.encode("utf-8")).hexdigest()

        # 6. Price Parameters according to Canonical Reference-Price Policy
        if order_type == OrderType.LIMIT:
            price = limit_price if limit_price is not None else intent.approved_entry_price
            if price <= 0.0:
                raise ValueError(f"LIMIT order requires a positive price, got {price}")
        else:
            price = None

        return OrderRequest(
            client_order_id=client_order_id,
            intent_id=intent.intent_id,
            signal_id=intent.signal_id,
            strategy_id=intent.strategy_id,
            instrument_id=intent.instrument_id,
            side=side,
            position_effect=position_effect,
            order_type=order_type,
            quantity=intent.permitted_quantity,
            price=price,
            reference_price=intent.approved_entry_price,
            time_in_force=time_in_force,
            order_purpose=purpose,
            order_version=1,
            creation_timestamp=now,
            expiry_timestamp=intent.expiry_timestamp,
            idempotency_key=idempotency_key,
            metadata=metadata,
        )
