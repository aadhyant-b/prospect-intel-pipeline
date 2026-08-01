# press-release-intel

Press-release prospect intelligence pipeline.

## Data sources

The pipeline ingests from public wire RSS feeds — currently **PR Newswire** and **GlobeNewswire**. Business Wire was evaluated and rejected: it exposes no general public RSS feed (their feeds are per-subscriber tokens issued through an authenticated portal, and their main site bot-blocks non-browser traffic). The architecture supports adding it, or any other wire, via a one-line feed URL entry if legitimate access is obtained.

Noisy aggregator workarounds (e.g. scraping a third-party mirror instead of the distributor's own feed) were rejected to protect distributor-label accuracy, which the switch detector depends on.
