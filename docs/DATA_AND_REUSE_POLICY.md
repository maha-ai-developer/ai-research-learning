# Data collection and reuse checklist

This project begins with a metadata-first inventory. It does not assume that publicly reachable resources may be copied or republished.

Before each source connector collects data, record:

- Current `robots.txt` result and the date it was checked.
- Sitemap endpoint and whether the URLs it lists remain inside the approved scope.
- Terms of use and copyright/reuse policy, including rules for automated access.
- Whether a record is metadata-only, an abstract/short excerpt, a full text, a transcript, or a media file.
- The rights/licence basis for any content retained or displayed.
- Contact details for the crawler user-agent and a reasonable request rate.

Rules for the current metadata collector:

1. Default invocation is a dry run and does not access the network.
2. Network mode must explicitly be enabled with `--fetch`.
3. Policy checks must run before collection; if robots cannot be verified, fail closed.
4. Restrict fetches to specifically approved first-party domains and path prefixes.
5. Do not automatically fetch links discovered on pages.
6. Do not download PDFs, videos, audio, images, or full paper corpora in the metadata phase.
7. Save canonical source URLs and metadata retrieval timestamps.
8. Respect crawl delays; keep requests conservative and batch sizes small.
9. A robots rule is not a copyright licence. Do not republish article text, books, slides, papers, interviews, or transcripts without an appropriate basis.
10. Keep access tokens, credentials, and any private user data out of Git.

For Wikipedia and arXiv, prefer their official APIs where appropriate and follow the terms/licensing attached to specific content. API availability does not itself grant a blanket right to republish every returned field or paper.

This document is an engineering checklist, not legal advice. Confirm the current policies directly before live collection and before public redistribution.
