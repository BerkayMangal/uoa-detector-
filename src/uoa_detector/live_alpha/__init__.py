"""Live Alpha v1 (registry 5.25): the live recommendation engine behind ``/``.

Pure logic only: no I/O, no clock. The webapp side (``webapp/live_alpha``) reads
the database and Unusual Whales, calls into here, and stores what comes back.
Contract: ``docs/phase-5.25-live-alpha-acceptance.md``.
"""
