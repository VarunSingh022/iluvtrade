"""Broker connections: the platform's half of a venue relationship.

AlphaLab owns the trading half — :class:`alphalab.broker.protocol.BrokerProtocol`
is the contract an adapter implements, and AlphaLab's own reconciliation, order
model and execution vocabulary sit above it. This package owns what AlphaLab
deliberately does not: whose account it is, where the credentials live, what
state the connection is in, and who authorized it.
"""
