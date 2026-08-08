"""Outbound port definitions.

The workflow services depend only on these protocols, never on a concrete SDK. That is
what lets the whole ten-step flow -- including payment and NOC rendering -- run in a
test without a network.
"""
