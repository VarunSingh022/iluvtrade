"""The data workspace: how a CSV becomes a canonical, approved dataset.

One pipeline, two sources. :mod:`iluvtrade.data.fetch` retrieves bytes from an
approved URL and :mod:`iluvtrade.data.upload` accepts them from a browser; both
produce a :class:`~iluvtrade.db.models.data.DataSource` row and then hand the
same bytes to :func:`iluvtrade.data.ingest.ingest`. There is exactly one
canonicalization path after the source boundary, which is what PHASE 4 asks for.

The stages, and the module that owns each:

=========================  ==========================================
inspect / detect schema    :mod:`iluvtrade.data.schema`
validate + clean           :mod:`iluvtrade.data.cleaning`
quality report             :mod:`iluvtrade.data.quality`
canonical records          :mod:`iluvtrade.data.canonical`
orchestration              :mod:`iluvtrade.data.ingest`
=========================  ==========================================

**Nothing here mutates data silently.** Every change a row undergoes is a
:class:`~iluvtrade.data.cleaning.Transformation` with a reason, and every row
dropped is a :class:`~iluvtrade.data.cleaning.RejectedRow` carrying its line
number and the raw text. Both are persisted with the version.
"""
