"""INTEGRATION REQUIRED. Must use the existing unchanged A15 strategy engine.

DO NOT swap in GDXU V7 or another approximation: all A15 sub-strategies,
filters and 57-symbol indicator calculations must be evaluated as of timestamp.
"""

def evaluate(as_of_utc, state, original_a15_json):
    # Hook the actual source of truth on the A15 VPS. It should return
    # allocations such as {'GDXU': 0.35, 'BIL': 0.65} based ONLY on
    # market data available as of as_of_utc. Full A15 weights, not signals.
    # It MUST NOT submit any broker orders or invoke the production run.
    raise NotImplementedError("Connect the existing A15 read-only evaluation engine first")