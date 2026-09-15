"""ThetaData-derived enrichment providers (Phase 3.6).

Self-derived confluence: provider implementations that compute the
confluence enrichment axes (dealer gamma / GEX first) from the ThetaData
option chain we already own, instead of from Unusual Whales. They satisfy
the same provider Protocols the M21-M28 fusion stages already consume, so
the stages are untouched — only the data source changes.
"""
