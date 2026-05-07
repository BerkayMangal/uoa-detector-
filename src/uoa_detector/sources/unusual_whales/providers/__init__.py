"""Unusual Whales provider implementations.

Phase 3.3.3.4 ships dealer_gamma, iv_history, dark_pool.
Phase 3.3.3.5 ships catalyst_calendar, open_interest, sector_peer.

All providers implement Protocols defined in Phase 2 stubs at
``uoa_detector.providers.*``. They are pure data-shipping: no
business logic. Module 21-28 stages (Phase 3.4) consume the typed
DTOs and apply scoring rules using profile-tunable thresholds.

Each provider holds an in-memory TTL cache keyed by request shape;
TTL comes from ``UnusualWhalesProviderCacheTTL`` in the profile.
0 disables the cache for that provider type.
"""
