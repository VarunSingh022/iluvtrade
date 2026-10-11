"""The one way this application derives a run seed when the caller gives none.

AlphaLab takes a run's seed as an input -- it seeds the identifier stream
(``RunConfig.seed``) -- and reports it beside, not inside, the run's
configuration and dataset identities (``lifecycle.digest_run``: "the seed and
the dataset are separate fields, so a change to either is attributable"). It
does not say how a seed is chosen. This module does, for every derived seed:

* **Input:** a mapping of the run's material inputs, by identity.
* **Encoding:** canonical JSON -- keys sorted, separators ``(",", ":")``,
  non-JSON values (``Decimal``) written by ``str`` -- encoded as UTF-8.
* **Digest:** SHA-256.
* **Integer:** the first 8 hex digits (32 bits), big-endian, unsigned:
  ``0 .. 2**32 - 1``. Stored as BIGINT (migration 0006).

It never uses Python's ``hash()``, which is randomized per interpreter process
(``PYTHONHASHSEED``): a session seeded from it was created with a number no
other process would derive from the same inputs. A derived seed is also stored
on the row it belongs to before anything runs, and every later execution reads
it back rather than deriving it again.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

__all__ = ["canonical_json", "derive_seed"]


def canonical_json(inputs: Mapping[str, Any]) -> str:
    """The canonical text a seed (or a fingerprint) is taken over."""

    return json.dumps(inputs, sort_keys=True, separators=(",", ":"), default=str)


def derive_seed(inputs: Mapping[str, Any]) -> int:
    """A 32-bit unsigned seed from ``inputs``, identical in every process."""

    digest = hashlib.sha256(canonical_json(inputs).encode("utf-8")).hexdigest()
    return int(digest[:8], 16)
