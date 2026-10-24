# Validation report

Date: 2026-10-20

## Offline validation to run from this repository

```bash
python -m unittest discover -s tests -v
python -m compileall -q collect.py knowledge_store.py tests
python collect.py
python knowledge_store.py init
python knowledge_store.py import-manifest
python knowledge_store.py status
```

The default collector invocation is offline. The manifest contains 52 records, currently including first-party, policy/control, and external reference URLs. The initial balanced dry-run selection is limited to 10 HTML URLs (up to five per researcher).

## Live-network status

No live pages have been fetched as part of assembling this Git repository. A previous browser check confirmed that University of Toronto's `robots.txt` includes a one-second crawl delay. Bengio's robots and sitemap policy must be rechecked from a network-enabled environment immediately before collection.

The runtime database is created locally at `data/runtime/inventory.sqlite`; this path is ignored by Git. The historical inventory databases in `inventory/` are snapshots from earlier planning work and are not evidence of full pages being downloaded.

## Caveats

- Generic HTML metadata extraction may require source-specific parsers for publication listings and blog pagination.
- Sitemap endpoint availability does not mean every URL listed there is in scope or reusable.
- A robots policy is not a copyright licence.
- Offline tests verify code behavior against fixtures; they do not establish that a live website currently permits crawling.
