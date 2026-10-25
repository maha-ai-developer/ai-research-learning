# ADR 0006 — Transcript Acquisition Methods, Policy Gate, and Three-Layer Storage Model

| Metadata | Value |
| :--- | :--- |
| **ADR ID** | `ADR-0006` |
| **Date** | `2026-10-25` |
| **Status** | `Proposed (For Architectural Review)` |
| **Policy Profile Namespace** | `TRANSCRIPT_INGESTION_POLICY_V1` |
| **Policy Version** | `ADR-0006/2026-10-25` |
| **Related Docs** | [`transcript-ingestion.md`](../transcript-ingestion.md), [`data-model.md`](../data-model.md), [`source-adapter-contract.md`](../source-adapter-contract.md), [`implementation-roadmap.md`](../implementation-roadmap.md), [`0002-knowledge-data-model.md`](0002-knowledge-data-model.md), [`0005-arxiv-api-access-policy.md`](0005-arxiv-api-access-policy.md) |

---

## 1. Context and Problem Statement

The AI Research & Learning System catalogs research papers, lecture series, keynote talks, interviews, and visual mathematics lessons from Geoffrey Hinton (`SRC-HINTON`), Yoshua Bengio (`SRC-BENGIO`), 3Blue1Brown (`SRC-3B1B`), arXiv (`SRC-ARXIV`), and English Wikipedia (`SRC-WIKIPEDIA`). During the Task 012/013 metadata pilot and Task 016 offline curation review, `181` candidate links were persisted in `discovered_link_candidates` (`fetch_permission_granted = 0`), including `26` outbound links to `www.youtube.com` and `youtu.be` alongside `56` internal lesson/topic links on `www.3blue1brown.com` and `32` internal links on `www.cs.toronto.edu/~hinton/`.

To support timestamp-grounded learning, concept navigation, and citation verification across video and lecture resources, the system needs a canonical transcript ingestion architecture. However, naive transcript collection pipelines frequently violate platform Terms of Service, copyright restrictions, or epistemic integrity by:

1. Assuming that a publicly playable video URL or an outbound link on a researcher's homepage grants permission to download captions, extract audio, run automated speech recognition (ASR), or redistribute full transcripts.
2. Conflating a human viewer's ability to read captions in a video platform's web UI (or embed a player via an iframe API) with programmatic download authorization—for example, ignoring that YouTube Data API v3 `captions.download` requires OAuth 2.0 authorization (`youtube.force-ssl` or `youtubepartner`) from the **video owner** and returns `403 Forbidden` for third-party videos, and resorting to undocumented `/api/timedtext` scrapers or stream-ripping tools (`youtube-transcript-api`, `yt-dlp`).
3. Treating a permissive `robots.txt` result (`RFC 9309`) as a substitute for a copyright license (`CC-BY`, `CC-BY-SA`, `CC0`, `MIT`) or written rights-holder permission.
4. Overwriting or mixing original transcript cues, normalized search text, and AI-generated summaries in the same database column, leading to fabricated quotes or unattributed hallucinations.

---

## 2. Official Normative References Consulted (Checked `2026-10-25`)

1. **YouTube Data API v3 — `Captions: list` & `Captions: download`**:
   - <https://developers.google.com/youtube/v3/docs/captions/list>
   - <https://developers.google.com/youtube/v3/docs/captions/download>
2. **YouTube API Services Terms of Service & IFrame Player API**:
   - <https://developers.google.com/youtube/terms/api-services-terms-of-service>
   - <https://developers.google.com/youtube/iframe_api_reference>
3. **W3C WebVTT: The Web Video Text Tracks Format**:
   - <https://www.w3.org/TR/webvtt1/>
4. **IETF RFC 9309 (Robots Exclusion Protocol) & IETF BCP 47 (Language Tags)**:
   - <https://www.rfc-editor.org/rfc/rfc9309.html>
   - <https://www.rfc-editor.org/info/bcp47>
5. **arXiv API Terms of Use, User Manual, and License Information**:
   - <https://info.arxiv.org/help/api/tou.html>
   - <https://info.arxiv.org/help/api/user-manual.html>
   - <https://info.arxiv.org/help/license/index.html>
6. **MediaWiki `Extension:TimedMediaHandler`, API Access Policy, and Wikimedia User-Agent Policy**:
   - <https://www.mediawiki.org/wiki/Extension:TimedMediaHandler>
   - <https://www.mediawiki.org/wiki/Wikimedia_APIs/Access_policy>
   - <https://foundation.wikimedia.org/wiki/Policy:Wikimedia_Foundation_User-Agent_Policy>
7. **3Blue1Brown Official Website & FAQ**:
   - <https://www.3blue1brown.com/>
   - <https://www.3blue1brown.com/faq>

---

## 3. Decision

### 3.1 Four Mutually Exclusive Transcript Acquisition Methods (`A`, `B`, `C`, `D`)

Every media or lecture resource evaluated for transcript ingestion must be classified into one of four explicit methods after passing a mandatory policy verification gate:

1. **Method A — Official Source Page Transcript (`METHOD_A_OFFICIAL_SOURCE_PAGE`)**:
   - Transcript or caption tracks published directly on an in-scope first-party source page permitted by `robots.txt` (`RFC 9309`) and rate limits.
   - **Storage Gate:** Full transcript/segment text may be stored **only** when the page carries a verified permissive copyright license (`CC-BY-4.0`, `CC-BY-SA-4.0`, `CC0-1.0`, `MIT`) or explicit written permission (`permits_transcript_storage = 1`). If the page is `ALL_RIGHTS_RESERVED` or `UNVERIFIED`, full text is **not** stored; only metadata and timestamp locators are recorded.
2. **Method B — Explicitly Permitted URL or Official API (`METHOD_B_PERMITTED_URL_OR_API`)**:
   - Caption or transcript files (`WebVTT`, `SRT`, `TTML`, `TimedText`) retrieved via an officially documented API or explicitly licensed download URL where **both** API terms/credentials (e.g., Wikimedia Commons `TimedText` API for CC-licensed media, or YouTube Data API v3 `captions.download` authenticated by the video owner) **and** content licensing (`permits_transcript_storage = 1`) are satisfied.
   - **Prohibition:** Undocumented player endpoints (`/api/timedtext`) and third-party caption scrapers (`youtube-transcript-api`, `yt-dlp`) are strictly prohibited.
3. **Method C — Authorized User-Supplied Media Transcription or Upload (`METHOD_C_AUTHORIZED_USER_SUPPLIED_ASR`)**:
   - Speech-to-text transcription (or user-supplied caption upload) of an audio/video file that the user/curator is explicitly authorized to supply and process, backed by a mandatory **Authorization Attestation** (`authorization_basis`, `license_spdx`, `supplied_media_sha256`, `attested_by`, `attested_at`) and full ASR model lineage (`asr_provider`, `asr_model_id`, `asr_model_version`, decoding parameters, and segment confidence scores).
   - **Prohibition:** The system never rips audio or video streams from unauthorized third-party URLs to feed into ASR.
4. **Method D — Transcript Unavailable or Inaccessible Through Permitted Methods (`METHOD_D_UNAVAILABLE`)**:
   - Default fail-closed classification whenever captions do not exist, require third-party video-owner OAuth (`PLATFORM_API_OWNER_ONLY_RESTRICTION`), or are governed by restrictive/unresolved copyright (`ALL_RIGHTS_RESERVED` or `UNVERIFIED_REQUIRES_HUMAN_REVIEW`).
   - Records availability status, reason code, and provenance in `transcripts` (`permits_transcript_storage = 0`, `segment_count = 0`) while preserving parent `resources` metadata and outbound deep links (`canonical_url`).

### 3.2 Mandatory Policy Gate State Machine and Auditable Authorization Attestations

Transcript ingestion is governed by a state machine (`DISCOVERED`, `TRANSCRIPT_AVAILABLE` / `RIGHTS_PENDING`, `POLICY_REVIEW_REQUIRED` / `NEEDS_REVIEW`, `READY_FOR_INGESTION`, `INGESTED`, `UNAVAILABLE` / `METADATA_ONLY_LINKED`, `BLOCKED_BY_POLICY`, `FAILED_RETRYABLE`, `WITHDRAWN_OR_TOMBSTONED`) enforcing three non-bypassable invariants:

1. **Discovery Never Grants Acquisition Permission:** Discovering a media URL or reviewing a candidate in `discovered_link_candidates` (`fetch_permission_granted = 0`) never transitions directly to `READY_FOR_INGESTION` or triggers network caption fetching.
2. **Caller-Supplied Permission Flags Require an Auditable Authorization Attestation:** The policy gate (`evaluate_transcript_storage_policy`), local importers (`import_local_transcript`, `import_local_transcript_file`), and SQLite triggers (hardened in forward-only Migration `8`) never trust caller-supplied `permits_transcript_storage=True` or `--permits-storage` flags in isolation. Storing transcript text requires a `VERIFIED`, non-withdrawn, non-expired record in `transcript_authorization_attestations` (`tatt_*`, introduced in forward-only SQLite Migration `7` and hardened at the trigger layer in Migration `8`) matching the target `resource_id`, `transcript_id`, `language_bcp47`, and `track_kind` with concrete evidence (`evidence_type`, `evidence_reference`, `reviewed_by`, `reviewed_at`).
3. **Granular Scope Separation (Storage vs. Verbatim Quoting vs. AI Synthesis vs. Redistribution) & SQLite Enforcement Boundary:** Each authorization attestation and transcript record tracks four distinct operational permissions:
   - `permits_transcript_storage`: Permission to persist `transcript_versions` and `transcript_segments` locally (enforced by both SQLite `BEFORE INSERT`/`BEFORE UPDATE` triggers and repository policy gates).
   - `permits_verbatim_excerpts`: Permission to return verbatim quotes (`get_verbatim_transcript_excerpt`). Because SQLite does not support `BEFORE SELECT` triggers on stored tables when `permits_transcript_storage = 1`, payload-level read gating is enforced in `get_verbatim_transcript_excerpt`, while SQLite triggers prevent unauthorized flag escalation on `transcripts` and prevent forging `TRANSCRIPT_VERBATIM_EXCERPT_ACCESSED` (`status='allowed'`) audit rows in `provenance_events`.
   - `permits_ai_synthesis`: Permission to store transformative AI study notes (`record_transcript_synthesis`), enforced by both SQLite triggers on `transcript_syntheses` and the repository method.
   - `permits_redistribution`: Permission to export/redistribute full transcript text (`export_transcript_for_redistribution`). Read-time payload export is enforced by `export_transcript_for_redistribution`, while SQLite triggers prevent unauthorized flag escalation on `transcripts` and prevent forging `TRANSCRIPT_REDISTRIBUTION_EXPORTED` (`status='exported'`) audit rows in `provenance_events`.
   Withdrawing, expiring, or narrowing an attestation (`withdraw_transcript_attestation` or direct SQL `UPDATE`) immediately revokes active permissions and cascades tombstone purging across `transcript_segments`, `transcript_syntheses`, `transcript_versions`, and `transcripts` while retaining provenance history. In addition, because SQLite triggers only fire on `INSERT`/`UPDATE`/`DELETE` and SQLite has no background daemon while idle on disk, `KnowledgeStore.expire_due_transcript_attestations` (plus live `_is_transcript_authorized_for_read` checks) runs automatically on `initialize()` and all transcript/attestation read and write entrypoints (as well as the `expire-transcripts` CLI command) so that natural expiration (`expires_at <= now`) immediately transitions the attestation to `EXPIRED`, purges `transcript_segments` and `transcript_syntheses`, tombstones `transcript_versions` and `transcripts`, and blocks all application read and write access without requiring a prior manual `UPDATE`.
4. **Retries Never Bypass the Policy Gate:** Any retry from `FAILED_RETRYABLE` transitions back to `TRANSCRIPT_AVAILABLE` and must re-execute the full policy gate (`validate_and_canonicalize_url`, `robots.txt` check, source circuit breaker, and attestation verification) before re-entering `READY_FOR_INGESTION`.

### 3.3 Three-Layer Separation of Source, Normalized, and AI-Generated Text

To prevent fabricated quotes and preserve primary-source integrity:

1. **Original Transcript Material (`transcript_segments.original_segment_text`)**: Immutable verbatim cue text as published by the source (`VERBATIM_SOURCE_QUOTE`) or emitted by authorized ASR (`ASR_TRANSCRIBED_SOURCE`). Null/unpersisted whenever `permits_transcript_storage = 0`.
2. **Normalized Transcript Text (`transcript_segments.normalized_segment_text`)**: Deterministic, non-LLM rule-normalized text (`normalizer_version = 'norm-v1.0'`) for search indexing, stored in a separate column with its own `normalized_text_sha256`.
3. **AI-Generated Summaries & Explanations (`transcript_syntheses`)**: Stored in a dedicated table with `epistemic_status = 'GENERATED_INTERPRETATION'`, `agent_run_id`, `model_id`, `prompt_version`, `cited_segment_ids_json`, and deterministic grounding checks (`TV-07`, `TV-08`), gated by `permits_ai_synthesis = 1`.

### 3.4 Local SQLite First, Cloud Storage & Cloud Run Deferred

- **Initial Local Runtime:** All structured transcript tables (`transcripts`, `transcript_versions`, `transcript_segments`, `transcript_syntheses`, `transcript_segment_links` in Migration `6`, plus `transcript_authorization_attestations` in Migration `7`) operate locally in SQLite on the Raspberry Pi with zero Firebase or Google Cloud dependency.
- **Future Cloud Partition:** Raw caption files (`.vtt`, `.srt`, `.ttml`) and full ASR JSON payloads where `permits_transcript_storage = 1` will later reside in Google Cloud Storage (`gs://<unprovisioned-artifacts-bucket>/transcripts/...`) and be processed by the `ingestion-worker` Cloud Run Job only after a cloud provisioning plan is separately approved.

---

## 4. Alternatives Considered

| Alternative | Pros | Cons / Reason for Rejection |
| :--- | :--- | :--- |
| **A. Use Unofficial Caption Scrapers (`youtube-transcript-api` / `yt-dlp`) on Public Lecture Videos** | High coverage of third-party YouTube lectures without video-owner OAuth. | **Rejected:** Uses undocumented internal endpoints, bypasses YouTube Data API v3 `captions.download` owner-authorization checks, and violates platform Terms of Service and copyright boundaries. |
| **B. Download Public Audio Streams and Run Local Whisper ASR Automatically** | Generates transcripts even for uncaptioned videos. | **Rejected:** Public streaming availability does not grant permission to download media streams, create derivative transcripts, or redistribute copyrighted lectures without rights-holder authorization. |
| **C. Store Only AI-Generated Summaries in Place of Original Transcripts** | Smaller storage footprint. | **Rejected:** Destroys primary-source provenance, prevents exact quote verification (`TV-07`), and violates the core architectural separation between source evidence and generated interpretation. |
| **D. Policy-Gated 4-Method Acquisition (`A`/`B`/`C`/`D`) + 3-Layer Local-First Data Model (Chosen)** | Strictly respects platform APIs, `robots.txt`, and copyright licenses; supports authorized ASR with full model lineage; preserves timestamp deep links even when full text cannot be stored. | Third-party `ALL_RIGHTS_RESERVED` videos remain `METADATA_ONLY` (`Method D`) unless rights-holder permission or open-licensed tracks are obtained. |

---

## 5. Consequences

### Positive
- **Zero Unauthorized Scraping:** Third-party video platforms and `ALL_RIGHTS_RESERVED` academic pages are protected by deterministic fail-closed policy gates (`TV-01`).
- **Complete Provenance & Epistemic Clarity:** Learners and evaluation harnesses can distinguish human-authored captions, authorized ASR outputs (with confidence scores and model versions), and AI-generated explanations.
- **Seamless Local-to-Cloud Evolution:** Extending [`../../../knowledge_store.py`](../../../knowledge_store.py) via a future forward-only Migration `6` reuses existing `resource_id`, `resource_version_id`, `source_id`, and `provenance_event_id` keys without data migration friction.

### Trade-offs & Open Questions
- **Unresolved Third-Party & `SRC-3B1B` Transcript Licensing:** Until explicit written permission or an SPDX open license is verified for `SRC-3B1B` captions (`github.com/3b1b/captions` / `3blue1brown.com/lessons/*`) and third-party hosted lectures on YouTube, those resources default to `POLICY_REVIEW_REQUIRED` / `UNAVAILABLE` (`Method D`) with outbound timestamp deep links rather than stored transcript text.
