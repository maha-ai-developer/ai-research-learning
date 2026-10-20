# Architecture: source-aware AI learning

## End-to-end flow

```text
Sources
  ├─ Hinton website
  ├─ Bengio website
  ├─ Wikipedia / MediaWiki API (later)
  └─ arXiv API (later)
       ↓
Source connectors and policy checks
       ↓
Fetch metadata → parse/normalize → deduplicate → record provenance
       ↓
Document metadata + permitted text/chunks + source citations
       ↓
Keyword search + vector retrieval (later)
       ↓
Tutor / concept explorer / research timeline / comparison view
```

## Core modules

- **Source registry:** stable source identifiers, allowed domains/path prefixes, source type, policy review state, collection cadence, and reuse notes.
- **Connectors:** one implementation per source. Web scraping and APIs are separate connector types, but they emit the same normalized document schema.
- **Ingestion:** fetch approved resources, parse only expected content, canonicalize URLs, hash metadata, detect changed records, and retain fetch logs.
- **Storage:** SQLite is fine for a local prototype. For a multi-user service, move operational metadata to PostgreSQL and store only content that can be retained under applicable terms. Use a separate table for chunks and embeddings.
- **Retrieval:** combine keyword search and semantic retrieval; every returned chunk should keep `document_id`, source URL, date, and chunk locator.
- **Answering:** LLM answers must distinguish source statements, research evidence, and generated explanations. Surface citations and limitations.
- **UI:** source explorer, concept explorer, question-answering, research timeline, and comparisons.
- **Lab service (later):** run user experiments outside the app/API process, behind an isolated execution service.

## Suggested canonical document fields

`document_id`, `source_id`, `source_person`, `content_type`, `title`, `authors`, `published_at`, `canonical_url`, `external_identifier`, `topics`, `abstract_or_description`, `license_status`, `fetched_at`, `metadata_hash`, `text_storage_status`.

Store chunks in a related table with `document_id`, `chunk_index`, `text`, `page_or_section`, `embedding_model`, and `embedding`. Never discard the original URL or source context when chunking.

## Engineering sequence

1. Stabilize URL inventory and policy evidence.
2. Collect small batches of metadata and inspect failures.
3. Add source-specific parsers only where generic metadata is insufficient.
4. Add API-based Wikipedia and arXiv connectors.
5. Add search, then retrieval-grounded explanations with citations.
6. Add JupyterLab-backed coding lessons as a separate, security-reviewed subsystem.
