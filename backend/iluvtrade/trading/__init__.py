"""Paper and live trading sessions.

A session is a deployment of one strategy version to one environment. The
environment differs in exactly two things, which is AlphaLab's own parity
statement rather than this package's claim: where records come from, and where an
accepted order executes. Everything between — strategy, allocation, risk, OMS,
fills, accounting, analytics — is one code path.
"""
