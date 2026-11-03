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

## Task 037 Source-to-Knowledge Ingestion Policy Rules (`src/ai_research_learning/ingestion.py`)

1. **Wikipedia (`SRC-WIKIPEDIA`):** Open-license `CC_BY_SA_4_0` text extracts retrieved via the official MediaWiki API (`explaintext=1`) are permitted for primary-source text extraction (`WIKIPEDIA_OPEN_LICENSE_TEXT_PERMITTED` / `PRIMARY_SOURCE_TEXT`) with mandatory CC BY-SA 4.0 attribution, revision ID (`oldid`), and canonical URL preservation.
2. **arXiv (`SRC-ARXIV`):** Official Atom API metadata and abstracts are stored as `AUTHORIZED_ABSTRACT_OR_DESCRIPTION` (`ABSTRACT_OR_METADATA`) with the mandatory `Thank you to arXiv for use of its open access interoperability` acknowledgment and `>= 3.0s` rate limiting. Full-text PDF extraction is permitted **only** when the individual work carries an explicit open license (`CC_BY_4_0`, `CC_BY_SA_4_0`, `CC0_1_0`) and `permits_full_text_storage = 1`.
3. **Geoffrey Hinton (`SRC-HINTON`) & Yoshua Bengio (`SRC-BENGIO`):** Restricted to `METADATA_ONLY` (`ALL_RIGHTS_RESERVED_METADATA_ONLY`) unless an individual resource has explicit full-text or excerpt permissions. Linked PDFs on `/~hinton/absps/` and blog post bodies on `yoshuabengio.org` are catalogued as bibliographic metadata and canonical links without unlicensed full-text redistribution.
4. **3Blue1Brown (`SRC-3B1B`):** Lesson and video metadata is separated strictly from transcript storage. Video or lesson access never implies transcript storage permission (`video_access_permits_transcript_storage = False`); transcript extraction (`TRANSCRIPT_AUTHORIZED_TEXT`) requires both `permits_transcript_storage = 1` and a verified non-expired `TranscriptAuthorizationAttestation`.
5. **DeepLearning.AI — The Batch (`SRC-THE-BATCH`):** Strictly scoped to `https://www.deeplearning.ai/the-batch*` (general `deeplearning.ai` URLs are rejected with `REJECTED_THE_BATCH_PATH_SCOPE_VIOLATION`), requires explicit curation approval before collection, and is classified exclusively as `AI news and reports` (`AI_NEWS_AND_DEVELOPMENTS` / `NEWS_AND_COMMENTARY`), never as foundational primary research proof.
6. **Discovered Link Candidates (`discovered_link_candidates`):** Unreviewed or unapproved discovered links (`review_status = 'PENDING_REVIEW'`, `fetch_permission_granted = 0`) are blocked from automatic collection (`REJECTED_UNAPPROVED_DISCOVERED_CANDIDATE`).

This document is an engineering checklist, not legal advice. Confirm the current policies directly before live collection and before public redistribution.
