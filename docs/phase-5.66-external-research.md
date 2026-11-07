# Phase5.66 — external implementation review

Sources inspected2026-11-07. The linked GitHub files were actually retrieved
with HTTP200 through the configured proxy. They inform engineering choices;
their examples do not establish an edge for this strategy. No model weights,
paid feed, SEC response or production data were downloaded for this review.

## Semantic interpretation and source identity

[ProsusAI FinBERT](https://github.com/ProsusAI/finBERT/blob/3b4bfed383a8bb6b013db2f1ce9c766ff5100d24/README.md)
classifies financial text as positive/negative/neutral and describes a score
of P(positive)−P(negative). That score is neither a return forecast nor evidence
of issuer-specific surprise, novelty or price incorporation. The new semantic
layer therefore keeps issuer relation, event, certainty, direction, materiality
and exact source evidence separate. Unknown and mixed interpretations remain
explicit, and model/prompt/schema identities travel with the result.

FinBERT's current notice, effective12October2026, states that Apache2 covers
the training code/scripts/notebooks only, excluding pretrained weights and
FinancialPhraseBank. The weights' separate model-card terms were not verified.
No FinBERT weights are bundled; this implementation uses the owner's existing
Mahalle/Grok service within its separately registered bounds.

[QuantConnect's Benzinga adapter](https://github.com/QuantConnect/Lean.DataSource.BenzingaNews/blob/cf363d2cc3357a932cd7f57b5666c1c001088f1d/BenzingaNews.cs)
separates article identity, creation/update clocks, ticker mappings and content;
its availability field follows the update clock.
[Its Brain sentiment adapter](https://github.com/QuantConnect/Lean.DataSource.BrainSentiment/blob/ac16133abb165ad0172906b78ee3d66a5e37361e/BrainSentimentIndicatorBase.cs)
separates sentiment, article mentions, buzz and availability. These practices
support versioned source binding, duplicate-aware coverage and distinct slow
context versus fresh events. Public adapter code does not grant a data-feed
subscription or redistribution right. Neither feed was purchased or connected.

## Fundamental availability and multiple clocks

[Qlib's point-in-time documentation](https://github.com/microsoft/qlib/blob/54355232463878d2eebb91fe0ee5fa7fa1f5976c/docs/advanced/PIT.rst)
explicitly warns that using the latest restated accounts in historical tests
leaks later information. Publication date, fiscal period and revision identity
must remain distinct. Phase5.66 accordingly retains physical availability and
financial-period/basis evidence; a current undated response cannot become an
intraday historical feature merely because it describes an older quarter.

[Qlib's nested execution example](https://github.com/microsoft/qlib/blob/54355232463878d2eebb91fe0ee5fa7fa1f5976c/examples/nested_decision_execution/README.md)
separates daily/weekly decisions from minute execution. This supports treating
slow fundamental/regime context, chart cadence and holding horizon as separate
axes. The registered UOA experiment independently fixes temporal splits,
purging, sample gates and ticker shrinkage; those choices are not claims of
profitability borrowed from Qlib.

One possible additional primary source is SEC filings. The
[sec-edgar-downloader documentation](https://github.com/jadchaar/sec-edgar-downloader)
supports8-K/8-K-A/10-Q/10-K retrieval with date/quantity bounds and an identifying
User-Agent. Only that client documentation was inspected. SEC endpoint access
and integration remain unverified; filings also do not supply historical
consensus estimates by themselves.

## Execution evidence

[LEAN's equity fill model](https://github.com/QuantConnect/Lean/blob/cc98c42f5543256cea0a33e4521401e42303b7d7/Common/Orders/Fills/EquityFillModel.cs)
uses ask plus modeled slippage for buys, bid minus slippage for sells, and
explicit stale-data handling. The relevant lesson is to retain quote clocks,
both-side costs and missingness. Paired stock last-trade references do not
establish executable option returns. Option results require the actual
contracts/legs, direction, multipliers, contemporaneous quotes and costs;
replacing CALL with PUT is not PnL sign inversion.

[LEAN's NLTK sentiment example](https://github.com/QuantConnect/Lean/blob/cc98c42f5543256cea0a33e4521401e42303b7d7/Algorithm.Python/NLTKSentimentTradingAlgorithm.py)
literally checks sample text for Up/Down words. It is useful educational wiring,
not evidence of semantic understanding or validated alpha. No inspected
example or repository performance claim is used to promote a live UOA rule.
