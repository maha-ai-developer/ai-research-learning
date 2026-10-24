# Validation report

Date: 2026-10-20

## Offline validation to run from this repository

```bash
python -m unittest discover -s tests -v
python -m compileall -q collect.py knowledge_store.py src tests
python collect.py
python knowledge_store.py init
python knowledge_store.py import-manifest
python knowledge_store.py status
```

The default collector invocation is offline. The manifest contains 52 records, currently including first-party, policy/control, and external reference URLs. The initial balanced dry-run selection is limited to 10 HTML URLs (up to five per researcher).

## Live-network status

A single bounded, policy-gated metadata pilot (`<= 2` resources per source, `<= 10` total) was executed for Task 012 and documented in [`reports/task-012-metadata-pilot-report.md`](reports/task-012-metadata-pilot-report.md). All default CLI and CI commands remain strictly offline.

The runtime database is created locally at `data/runtime/inventory.sqlite`; this path is ignored by Git. The historical inventory databases in `inventory/` are snapshots from earlier planning work and are not evidence of full pages being downloaded.

## Caveats

- Generic HTML metadata extraction may require source-specific parsers for publication listings and blog pagination.
- Sitemap endpoint availability does not mean every URL listed there is in scope or reusable.
- A robots policy is not a copyright licence.
- Offline tests verify code behavior against fixtures; they do not establish that a live website currently permits crawling.
