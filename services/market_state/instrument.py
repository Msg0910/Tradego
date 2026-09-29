"""
Tradego Instrument Identity and Resolution Abstraction.

Decouples internal trading models from transient external broker/feed tokens.
Provides strict validation rules for valid combinations of:
exchange, instrument_type, expiry, strike, and option_type.

RULE: Never silently fallback an unresolved provider token to Exchange.NSE.
Unknown instruments must remain explicitly unresolved until valid metadata is registered.
"""

from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Dict, List, Optional, Set, Tuple


class Exchange(str, Enum):
    NSE = "NSE"
    BSE = "BSE"
    MCX = "MCX"
    NFO = "NFO"
    CDS = "CDS"


class InstrumentType(str, Enum):
    EQUITY = "EQUITY"
    INDEX = "INDEX"
    FUTURES = "FUTURES"
    OPTIONS = "OPTIONS"
    COMMODITY = "COMMODITY"
    CURRENCY = "CURRENCY"


class OptionType(str, Enum):
    CE = "CE"
    PE = "PE"
    NONE = "NONE"


@dataclass(frozen=True, slots=True)
class InstrumentId:
    """
    Canonical, immutable instrument identifier.
    Hashable and safe for use as dictionary keys.
    """
    symbol: str
    exchange: Exchange
    instrument_type: InstrumentType
    expiry: Optional[date] = None
    strike: Optional[float] = None
    option_type: OptionType = OptionType.NONE

    def __post_init__(self) -> None:
        sym = self.symbol.strip().upper() if self.symbol else ""
        if not sym:
            raise ValueError("InstrumentId.symbol cannot be empty.")
        object.__setattr__(self, "symbol", sym)

        # Validation Rule 1: EQUITY
        if self.instrument_type == InstrumentType.EQUITY:
            if self.exchange not in (Exchange.NSE, Exchange.BSE):
                raise ValueError(
                    f"Equity instrument '{sym}' must be on NSE or BSE, got {self.exchange}."
                )
            if self.expiry is not None:
                raise ValueError(f"Equity instrument '{sym}' cannot have an expiry.")
            if self.strike is not None:
                raise ValueError(f"Equity instrument '{sym}' cannot have a strike price.")
            if self.option_type != OptionType.NONE:
                raise ValueError(f"Equity instrument '{sym}' cannot have an option_type.")

        # Validation Rule 2: INDEX
        elif self.instrument_type == InstrumentType.INDEX:
            if self.expiry is not None:
                raise ValueError(f"Index instrument '{sym}' cannot have an expiry.")
            if self.strike is not None:
                raise ValueError(f"Index instrument '{sym}' cannot have a strike price.")
            if self.option_type != OptionType.NONE:
                raise ValueError(f"Index instrument '{sym}' cannot have an option_type.")

        # Validation Rule 3: FUTURES
        elif self.instrument_type == InstrumentType.FUTURES:
            if self.expiry is None:
                raise ValueError(f"Futures contract '{sym}' must have an expiry date.")
            if self.strike is not None:
                raise ValueError(f"Futures contract '{sym}' cannot have a strike price.")
            if self.option_type != OptionType.NONE:
                raise ValueError(f"Futures contract '{sym}' cannot have an option_type.")

        # Validation Rule 4: OPTIONS
        elif self.instrument_type == InstrumentType.OPTIONS:
            if self.expiry is None:
                raise ValueError(f"Options contract '{sym}' must have an expiry date.")
            if self.strike is None or self.strike <= 0:
                raise ValueError(
                    f"Options contract '{sym}' must have a positive strike price, got {self.strike}."
                )
            if self.option_type not in (OptionType.CE, OptionType.PE):
                raise ValueError(
                    f"Options contract '{sym}' must have option_type CE or PE, got {self.option_type}."
                )

        # Validation Rule 5: COMMODITY (Cash/Spot or non-derivative)
        elif self.instrument_type == InstrumentType.COMMODITY:
            if self.exchange != Exchange.MCX:
                raise ValueError(
                    f"Commodity '{sym}' expected on MCX, got {self.exchange}."
                )
            if self.strike is not None:
                raise ValueError(f"Spot Commodity '{sym}' cannot have a strike price.")
            if self.option_type != OptionType.NONE:
                raise ValueError(f"Spot Commodity '{sym}' cannot have an option_type.")

        # Validation Rule 6: CURRENCY
        elif self.instrument_type == InstrumentType.CURRENCY:
            if self.exchange != Exchange.CDS:
                raise ValueError(
                    f"Currency '{sym}' expected on CDS, got {self.exchange}."
                )
            if self.strike is not None:
                raise ValueError(f"Spot Currency '{sym}' cannot have a strike price.")
            if self.option_type != OptionType.NONE:
                raise ValueError(f"Spot Currency '{sym}' cannot have an option_type.")

    @property
    def canonical_id(self) -> str:
        """
        Standard human-readable representation:
        - Equity/Index: NSE:360ONE, NSE:NIFTY
        - Futures: MCX:GOLD_20261005_FUT
        - Options: NFO:NIFTY_20260924_24000_CE
        """
        if self.instrument_type in (InstrumentType.EQUITY, InstrumentType.INDEX):
            return f"{self.exchange.value}:{self.symbol}"
        if self.instrument_type == InstrumentType.FUTURES:
            exp_str = self.expiry.strftime("%Y%m%d") if self.expiry else ""
            return f"{self.exchange.value}:{self.symbol}_{exp_str}_FUT"
        if self.instrument_type == InstrumentType.OPTIONS:
            exp_str = self.expiry.strftime("%Y%m%d") if self.expiry else ""
            strike_str = f"{self.strike:g}" if self.strike is not None else ""
            return f"{self.exchange.value}:{self.symbol}_{exp_str}_{strike_str}_{self.option_type.value}"
        return f"{self.exchange.value}:{self.symbol}"


@dataclass(slots=True)
class InstrumentMetadata:
    """
    Static metadata and trading specifications for an instrument.
    """
    instrument_id: InstrumentId
    lot_size: int = 1
    tick_size: float = 0.05
    price_precision: int = 2
    freeze_quantity: Optional[int] = None
    provider_tokens: Dict[str, str] = None  # provider_name -> token_id


class InstrumentRegistry:
    """
    Bidirectional mapping between canonical InstrumentId and provider-specific tokens.

    CRITICAL RULE:
    Never silently fallback an unresolved provider token to Exchange.NSE.
    Unknown tokens must remain explicitly unresolved until registered.
    """

    def __init__(self) -> None:
        self._token_to_id: Dict[Tuple[str, str], InstrumentId] = {}
        self._id_to_metadata: Dict[InstrumentId, InstrumentMetadata] = {}
        self._canonical_to_id: Dict[str, InstrumentId] = {}
        self._unresolved_tokens: Set[Tuple[str, str]] = set()

    def register(
        self,
        instrument_id: InstrumentId,
        provider_tokens: Optional[Dict[str, str]] = None,
        lot_size: int = 1,
        tick_size: float = 0.05,
        price_precision: int = 2,
        freeze_quantity: Optional[int] = None,
    ) -> InstrumentMetadata:
        """
        Registers an instrument and maps provider tokens to its canonical identity.
        """
        metadata = InstrumentMetadata(
            instrument_id=instrument_id,
            lot_size=lot_size,
            tick_size=tick_size,
            price_precision=price_precision,
            freeze_quantity=freeze_quantity,
            provider_tokens=dict(provider_tokens or {}),
        )

        self._id_to_metadata[instrument_id] = metadata
        self._canonical_to_id[instrument_id.canonical_id] = instrument_id

        if provider_tokens:
            for provider, token in provider_tokens.items():
                p_norm = provider.strip().upper()
                t_norm = str(token).strip()
                key = (p_norm, t_norm)
                self._token_to_id[key] = instrument_id
                self._unresolved_tokens.discard(key)

        return metadata

    def resolve(self, provider: str, token: str) -> Optional[InstrumentId]:
        """
        Resolves a provider token into an InstrumentId.
        Returns None if the token is not registered.
        DOES NOT silently fallback to NSE.
        """
        p_norm = provider.strip().upper()
        t_norm = str(token).strip()
        key = (p_norm, t_norm)

        inst_id = self._token_to_id.get(key)
        if inst_id is None:
            self._unresolved_tokens.add(key)
            return None
        return inst_id

    def get_metadata(self, instrument_id: InstrumentId) -> Optional[InstrumentMetadata]:
        """Returns registered metadata for an instrument."""
        return self._id_to_metadata.get(instrument_id)

    def get_by_canonical_id(self, canonical_id: str) -> Optional[InstrumentId]:
        """Returns an instrument by canonical ID (e.g. 'NSE:360ONE')."""
        return self._canonical_to_id.get(canonical_id)

    @property
    def unresolved_tokens(self) -> Set[Tuple[str, str]]:
        """Returns all tokens that have been queried but remain unresolved."""
        return set(self._unresolved_tokens)

    @property
    def registered_instruments(self) -> List[InstrumentId]:
        """Returns list of all registered canonical instrument IDs."""
        return list(self._id_to_metadata.keys())
