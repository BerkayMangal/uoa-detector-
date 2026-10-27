# Phase 5.29 — personal desk and public introduction

Approved in chat, 2026-10-27: simplify the personal UI, remove excessive text,
improve design and SEO. Owner additionally chose a short public introduction.
Frozen before implementation. Reporting/presentation only, no strategy change.

- Give the shared shell a cohesive charcoal/copper design, local CSS, clear
  navigation, keyboard focus and responsive layouts. Keep existing routes/forms.
- Simplify the live desk: concise session strip, numeric entry/stop/target/risk,
  compact cards, collapsed evidence/cost explanations, searchable stock table,
  and collapsed historical/technical blocks. Preserve stale/no-data/withdrawn
  warnings, counterarguments, cost blockers and the immutable decision values.
- Add client-side stock focus filters (all/hot/peer-lag) using existing data;
  no new signal, threshold, upstream request or promise of profitable trades.
- Owner-authorized override to phase-5.0's health-only public surface: **GET**
  `/hakkinda`, `/robots.txt`, `/sitemap.xml`, `/assets/uoa.css`, `/favicon.svg`
  may also be public. Keep every trading/data/documentation route protected,
  including their methods, unknown paths and asset path traversal attempts.
- Public introduction contains static product/methodology text only, without
  database/API reads, live tickers, recommendations, positions, testimonials,
  performance claims or registration. It links to the authenticated desk.
- Turkish document language, per-page titles, public canonical/description/OG
  and truthful SoftwareApplication structured data. Sitemap lists only the
  introduction. Private pages/errors carry noindex and private no-store headers;
  public assets enable rendering without opening a directory mount.
- Update the old dynamic auth test enumeration only to exclude the explicit
  newly authorized GET routes. Retain all other assertions; add independent
  public-surface, private-indexing, auth-method and no-data-exposure tests.
- Verify browser rendering on desktop/mobile, search/filter/details interaction,
  accessible controls, no external asset dependency and no horizontal overflow.
  Use labelled local fixture data for previews, never call it production data.
- Run the repository gate, push a feature branch, verify PR CI, merge and verify
  Railway deployment SHA plus public SEO responses and private auth protection.
