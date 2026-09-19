"""Provider symbols → AlphaLab instrument identity.

A CSV says ``RELIANCE``. AlphaLab's core refuses that: ``Fill.asset_id`` must be
a UUID, because a provider symbol is not an identity — two venues can list the
same ticker for different companies, and one company's ticker changes.

AlphaLab already owns the resolution of this, in :mod:`alphalab.instrument`: an
:class:`~alphalab.instrument.record.InstrumentRecord` *derives* a UUID with
``uuid5`` over the canonical key ``(symbol, asset_type, exchange, currency)``.
Deriving rather than minting is what lets this application and any other agree
on an instrument's identity with no shared table.

So this module does **not** invent a symbol-to-id mapping. It declares the
instruments a dataset contains, registers them with AlphaLab, and keeps the
reverse map so a results screen can show ``RELIANCE`` rather than
``5e828721-…``. The identity itself is AlphaLab's, derived by AlphaLab's rule.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from alphalab.core.enums import AssetType
from alphalab.instrument.record import InstrumentRecord
from alphalab.instrument.registry import InstrumentRegistry, register_instruments

__all__ = ["InstrumentUniverse", "asset_id_for", "universe_for"]

#: The provider name every symbol from an uploaded or fetched dataset is
#: registered under. A future vendor feed registers its own aliases beside this
#: one against the same derived identity.
DATASET_PROVIDER = "iluvtrade-dataset"


def asset_id_for(symbol: str, *, exchange: str, currency: str, asset_type: AssetType) -> str:
    """AlphaLab's derived identity for one instrument declaration."""

    return InstrumentRecord(symbol, asset_type, exchange, currency).asset_id


@dataclass(frozen=True, slots=True)
class InstrumentUniverse:
    """The instruments one run trades, with identity resolved both ways."""

    registry: InstrumentRegistry
    #: provider symbol → AlphaLab asset_id
    by_symbol: dict[str, str]
    #: AlphaLab asset_id → provider symbol, for display
    by_asset_id: dict[str, str]
    exchange: str
    currency: str

    @property
    def asset_ids(self) -> frozenset[str]:
        return frozenset(self.by_asset_id)

    def symbol_of(self, asset_id: str) -> str:
        """The human symbol for an engine identity, or the id when unknown."""

        return self.by_asset_id.get(asset_id, asset_id)

    def to_dict(self) -> dict[str, Any]:
        """The mapping, stored on a run so its results stay readable later."""

        return {
            "exchange": self.exchange,
            "currency": self.currency,
            "provider": DATASET_PROVIDER,
            "symbols": self.by_symbol,
        }


def universe_for(
    symbols: list[str],
    *,
    exchange: str = "XNSE",
    currency: str = "INR",
    asset_type: AssetType = AssetType.EQUITY,
) -> InstrumentUniverse:
    """Declare and register the instruments in a dataset.

    ``exchange``, ``currency`` and ``asset_type`` are part of the canonical key,
    so they are part of the identity: the same ticker declared on two exchanges
    is two instruments, which is correct and is why none of the three is
    guessed from the data. They default to NSE equities in rupees because that
    is this deployment's first market, and a dataset from elsewhere states its
    own.
    """

    records = tuple(
        InstrumentRecord(
            symbol=symbol,
            asset_type=asset_type,
            exchange=exchange,
            currency=currency,
            aliases={DATASET_PROVIDER: symbol},
        )
        for symbol in sorted(set(symbols))
    )
    registry = register_instruments(InstrumentRegistry(), records)
    by_symbol = {record.symbol: record.asset_id for record in records}
    return InstrumentUniverse(
        registry=registry,
        by_symbol=by_symbol,
        by_asset_id={asset_id: symbol for symbol, asset_id in by_symbol.items()},
        exchange=exchange,
        currency=currency,
    )
