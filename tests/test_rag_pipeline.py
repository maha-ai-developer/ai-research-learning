"""Unit and integration tests for the Task 029 local-first RAG pipeline and provider layer."""
from __future__ import annotations

from pathlib import Path
import io
import json
import shutil
import sqlite3
import sys
import tempfile
from typing import Any
import unittest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from ai_research_learning import (  # noqa: E402
    AnthropicGenerationProvider,
    CANDIDATE_SOURCE_THE_BATCH,
    ChromaPersistentVectorStore,
    DEFAULT_RAG_EVAL_DATASET_PATH,
    DEFAULT_VERTEX_LOCATION,
    DEFAULT_VERTEX_PROJECT,
    DeterministicHashEmbeddingProvider,
    EmbeddingIncompatibilityError,
    GeminiEmbeddingProvider,
    GeminiGenerationProvider,
    LocalRAGPipeline,
    OpenAIEmbeddingProvider,
    OpenAIGenerationProvider,
    PolicyBlockedIndexingError,
    ProviderConfig,
    ProviderNotConfiguredError,
    RAGEvalCaseResult,
    RAGEvalSummary,
    ReadOnlySQLiteStore,
    RetrievalOnlyFallbackGenerationProvider,
    ask_research_question,
    build_cli_parser,
    build_vertex_ai_model_endpoint,
    chunk_text,
    compute_chunk_id,
    compute_lexical_claim_groundedness_proxy,
    create_embedding_provider,
    create_generation_provider,
    evaluate_the_batch_candidate_policy,
    extract_indexable_document_from_resource,
    format_rag_response,
    load_dotenv_file,
    normalize_text_for_chunking,
    parse_dotenv_content,
    redact_secrets,
    register_candidate_source_the_batch,
    resolve_cli_provider_config,
    run_end_to_end_rag_proof,
    run_offline_rag_evaluation,
    seed_eval_corpus_from_fixture,
)
from ai_research_learning import rag as rag_module  # noqa: E402
from knowledge_store import KnowledgeStore  # noqa: E402


class TestRAGPipelineAndArchitecture(unittest.TestCase):
    """Verify all Phase 2 & Phase 3 requirements for Task 029."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.db_path = self.tmp_path / "test_rag.sqlite"
        self.chroma_dir = self.tmp_path / "chroma_store"
        self.store = KnowledgeStore(self.db_path)

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def _seed_sample_resources(self) -> dict[str, dict[str, Any]]:
        """Seed permitted Wikipedia and arXiv resources plus a restricted Hinton PDF resource."""
        wiki_backprop = self.store.upsert_resource(
            source_id="SRC-WIKIPEDIA",
            canonical_url="https://en.wikipedia.org/wiki/Backpropagation",
            title="Backpropagation",
            authors="Wikipedia contributors",
            published_at="2026-03-01",
            resource_type="Encyclopedia article",
            abstract_or_description=(
                "Backpropagation is a gradient estimation method used to train neural network "
                "models. It computes the gradient of a loss function with respect to the weights "
                "of the network by applying the chain rule backward from the output layer to "
                "earlier hidden layers."
            ),
            collection_decision="COLLECTED",
            verification_status="HTTP_200_VERIFIED",
            license_status="CC_BY_SA_4_0",
            license_spdx="CC-BY-SA-4.0",
            permitted_storage_scope="WIKIPEDIA_OPEN_LICENSE_TEXT_PERMITTED",
            permits_full_text_storage=1,
            permits_verbatim_excerpts=1,
            legal_uncertainty_status="CLEARED_OPEN_LICENSE",
        )
        wiki_boltzmann = self.store.upsert_resource(
            source_id="SRC-WIKIPEDIA",
            canonical_url="https://en.wikipedia.org/wiki/Boltzmann_machine",
            title="Boltzmann machine",
            authors="Wikipedia contributors",
            published_at="2026-03-02",
            resource_type="Encyclopedia article",
            abstract_or_description=(
                "A Boltzmann machine is a stochastic recurrent neural network invented by "
                "Geoffrey Hinton and Terry Sejnowski. It is an energy-based model that learns "
                "probability distributions over binary units reaching thermal equilibrium."
            ),
            collection_decision="COLLECTED",
            verification_status="HTTP_200_VERIFIED",
            license_status="CC_BY_SA_4_0",
            license_spdx="CC-BY-SA-4.0",
            permitted_storage_scope="WIKIPEDIA_OPEN_LICENSE_TEXT_PERMITTED",
            permits_full_text_storage=1,
            permits_verbatim_excerpts=1,
            legal_uncertainty_status="CLEARED_OPEN_LICENSE",
        )
        arxiv_dropout = self.store.upsert_resource(
            source_id="SRC-ARXIV",
            canonical_url="https://arxiv.org/abs/1207.0580",
            title="Improving neural networks by preventing co-adaptation of feature detectors",
            authors="Geoffrey E. Hinton, Nitish Srivastava, Alex Krizhevsky, Ilya Sutskever, Ruslan R. Salakhutdinov",
            published_at="2012-07-03",
            resource_type="Preprint paper",
            abstract_or_description=(
                "When a large feedforward neural network is trained on a small training set, "
                "it typically performs poorly on held-out test data. This overfitting is "
                "greatly reduced by randomly omitting half of the feature detectors on each "
                "training case, a technique called dropout that prevents complex co-adaptations."
            ),
            collection_decision="COLLECTED",
            verification_status="HTTP_200_VERIFIED",
            license_status="ARXIV_NON_EXCLUSIVE_DISTRIB",
            permitted_storage_scope="ARXIV_METADATA_AND_ABSTRACT_PERMITTED",
            permits_full_text_storage=0,
            permits_verbatim_excerpts=0,
            legal_uncertainty_status="CLEARED_METADATA_AND_ABSTRACT",
        )
        restricted_hinton = self.store.upsert_resource(
            source_id="SRC-HINTON",
            canonical_url="https://www.cs.toronto.edu/~hinton/absps/naturedeepreview.pdf",
            title="Deep Learning (Nature Review)",
            authors="Yann LeCun, Yoshua Bengio, Geoffrey Hinton",
            published_at="2015-05-28",
            resource_type="Journal article PDF",
            abstract_or_description="Deep learning allows computational models composed of multiple processing layers.",
            collection_decision="METADATA_ONLY",
            verification_status="HTTP_200_VERIFIED",
            license_status="ALL_RIGHTS_RESERVED_METADATA_ONLY",
            permitted_storage_scope="METADATA_ONLY",
            permits_full_text_storage=0,
            permits_verbatim_excerpts=0,
            legal_uncertainty_status="UNVERIFIED_REQUIRES_HUMAN_REVIEW",
        )
        return {
            "backprop": wiki_backprop,
            "boltzmann": wiki_boltzmann,
            "dropout": arxiv_dropout,
            "restricted_hinton": restricted_hinton,
        }

    # ------------------------------------------------------------------
    # 1. Normalization, Deterministic Chunking, and Rights Gate
    # ------------------------------------------------------------------

    def test_normalization_and_deterministic_chunking(self) -> None:
        raw_text = (
            "Paragraph one explains how gradient descent updates network weights.\r\n\r\n\r\n"
            "Paragraph two explains how the chain rule propagates error derivatives backward "
            "through hidden layers in a neural network.\n\n"
            "Paragraph three discusses stochastic minibatch optimization and learning rates."
        )
        norm = normalize_text_for_chunking(raw_text)
        self.assertNotIn("\r", norm)
        self.assertNotIn("\n\n\n", norm)

        spans_first = chunk_text(norm, chunk_size_chars=120, chunk_overlap_chars=25)
        spans_second = chunk_text(norm, chunk_size_chars=120, chunk_overlap_chars=25)
        self.assertGreaterEqual(len(spans_first), 2)
        self.assertEqual(spans_first, spans_second)

        for idx, span in enumerate(spans_first):
            self.assertEqual(span.chunk_index, idx)
            self.assertEqual(norm[span.char_start : span.char_end], span.chunk_text)
            self.assertEqual(len(span.content_hash), 64)

        cid1 = compute_chunk_id("res_123", "ver_1", "RESOURCE_FULL_TEXT", 0)
        cid2 = compute_chunk_id("res_123", "ver_1", "RESOURCE_FULL_TEXT", 0)
        cid3 = compute_chunk_id("res_123", "ver_1", "RESOURCE_FULL_TEXT", 1)
        self.assertEqual(cid1, cid2)
        self.assertNotEqual(cid1, cid3)
        self.assertTrue(cid1.startswith("chk_"))

    def test_rights_and_licensing_gate_for_indexing(self) -> None:
        seeded = self._seed_sample_resources()

        # Wikipedia CC BY-SA 4.0 permits full-text indexing (PRIMARY_SOURCE_TEXT)
        wiki_doc = extract_indexable_document_from_resource(
            self.store,
            seeded["backprop"]["resource_id"],
            full_text="Full open-licensed article text on backpropagation and chain rule.",
        )
        self.assertEqual(wiki_doc.content_origin, "RESOURCE_FULL_TEXT")
        self.assertEqual(wiki_doc.epistemic_layer, "PRIMARY_SOURCE_TEXT")

        # arXiv permits abstract indexing (AUTHORIZED_ABSTRACT_OR_DESCRIPTION)
        arxiv_doc = extract_indexable_document_from_resource(
            self.store,
            seeded["dropout"]["resource_id"],
        )
        self.assertEqual(arxiv_doc.content_origin, "RESOURCE_ABSTRACT_METADATA")
        self.assertEqual(arxiv_doc.epistemic_layer, "AUTHORIZED_ABSTRACT_OR_DESCRIPTION")

        # arXiv full-text indexing is blocked when permits_full_text_storage=0
        with self.assertRaises(PolicyBlockedIndexingError):
            extract_indexable_document_from_resource(
                self.store,
                seeded["dropout"]["resource_id"],
                full_text="Full PDF text without verified open license.",
            )

        # All-rights-reserved metadata-only resource is blocked from semantic chunk indexing
        with self.assertRaises(PolicyBlockedIndexingError):
            extract_indexable_document_from_resource(
                self.store,
                seeded["restricted_hinton"]["resource_id"],
            )

    # ------------------------------------------------------------------
    # 2. Candidate Source SRC-THE-BATCH Policy Evaluation
    # ------------------------------------------------------------------

    def test_candidate_source_the_batch_policy_and_indexing_gate(self) -> None:
        registered = register_candidate_source_the_batch(self.store)
        self.assertEqual(registered["source_id"], "SRC-THE-BATCH")
        self.assertEqual(registered["status"], "CANDIDATE_PENDING_REVIEW")
        self.assertEqual(registered["permits_full_text_storage"], 0)
        self.assertEqual(registered["max_requests_per_run"], 0)

        sample_robots = (
            "User-agent: GPTBot\nDisallow: /\n"
            "User-agent: ClaudeBot\nDisallow: /\n"
            "User-agent: CCBot\nDisallow: /\n"
            "User-agent: *\nAllow: /\n"
            "Sitemap: https://www.deeplearning.ai/sitemap.xml\n"
        )
        policy = evaluate_the_batch_candidate_policy(sample_robots)
        self.assertEqual(policy["source_id"], CANDIDATE_SOURCE_THE_BATCH["source_id"])
        self.assertTrue(policy["general_user_agent_allowed"])
        self.assertTrue(policy["sitemap_advertised"])
        self.assertIn("GPTBot", policy["disallowed_bulk_ai_crawlers"])
        self.assertEqual(policy["technical_feasibility"], "FEASIBLE_METADATA_ONLY")
        self.assertEqual(policy["source_approval_status"], "CANDIDATE_PENDING_REVIEW")
        self.assertFalse(policy["ingestion_enabled"])

        # Attempting to index a resource under SRC-THE-BATCH while CANDIDATE_PENDING_REVIEW is blocked
        batch_res = self.store.upsert_resource(
            source_id="SRC-THE-BATCH",
            canonical_url="https://www.deeplearning.ai/the-batch/issue-290/",
            title="The Batch Issue 290",
            resource_type="Newsletter issue",
            abstract_or_description="Summary of recent AI developments.",
            collection_decision="METADATA_ONLY",
            verification_status="DISCOVERED_NOT_VERIFIED",
            license_status="ALL_RIGHTS_RESERVED_METADATA_ONLY",
            permitted_storage_scope="METADATA_ONLY",
            permits_full_text_storage=0,
            permits_verbatim_excerpts=0,
        )
        with self.assertRaises(PolicyBlockedIndexingError):
            extract_indexable_document_from_resource(self.store, batch_res["resource_id"])

    # ------------------------------------------------------------------
    # 3. Interchangeable Provider Configuration & Embedding Providers
    # ------------------------------------------------------------------

    def test_provider_config_secret_redaction_and_embedding_providers(self) -> None:
        cfg = ProviderConfig.from_env(
            {
                "AI_LEARNING_EMBEDDING_PROVIDER": "openai",
                "AI_LEARNING_EMBEDDING_MODEL": "text-embedding-3-small",
                "AI_LEARNING_EMBEDDING_DIMENSIONS": "64",
                "AI_LEARNING_GENERATION_PROVIDER": "anthropic",
                "AI_LEARNING_GENERATION_MODEL": "claude-3-5-haiku-latest",
                "OPENAI_API_KEY": "sk-secret-do-not-expose",
                "ANTHROPIC_API_KEY": "sk-ant-secret-do-not-expose",
            }
        )
        safe = cfg.to_safe_dict()
        self.assertTrue(safe["has_openai_api_key"])
        self.assertTrue(safe["has_anthropic_api_key"])
        self.assertFalse(safe["has_gemini_api_key"])
        self.assertTrue(safe["embedding_ready"])
        self.assertTrue(safe["generation_ready"])
        self.assertNotIn("sk-secret-do-not-expose", str(safe))
        self.assertNotIn("sk-secret-do-not-expose", repr(cfg))
        self.assertNotIn("sk-ant-secret-do-not-expose", repr(cfg))

        # Local deterministic embeddings have unit norm and separate related vs unrelated texts
        det_emb = DeterministicHashEmbeddingProvider(dimensions=128)
        v_backprop = det_emb.embed_query("backpropagation chain rule gradient neural network")
        v_backprop_doc = det_emb.embed_texts(
            ["Backpropagation computes gradients of the loss function using the chain rule."]
        )[0]
        v_unrelated = det_emb.embed_texts(
            ["Sourdough bread fermentation requires wild yeast and lactic acid bacteria."]
        )[0]
        norm = sum(x * x for x in v_backprop)
        self.assertAlmostEqual(norm, 1.0, places=5)
        sim_related = sum(a * b for a, b in zip(v_backprop, v_backprop_doc))
        sim_unrelated = sum(a * b for a, b in zip(v_backprop, v_unrelated))
        self.assertGreater(sim_related, 0.30)
        self.assertGreater(sim_related, sim_unrelated + 0.25)

        # OpenAI & Gemini embedding providers raise ProviderNotConfiguredError without credentials
        unconfigured_openai = OpenAIEmbeddingProvider(api_key=None, dimensions=16)
        with self.assertRaises(ProviderNotConfiguredError):
            unconfigured_openai.embed_query("test query")

        unconfigured_gemini = GeminiEmbeddingProvider(api_key="", dimensions=16)
        with self.assertRaises(ProviderNotConfiguredError):
            unconfigured_gemini.embed_query("test query")

        # OpenAI & Gemini embedding providers work with injected HTTP transport
        openai_emb = OpenAIEmbeddingProvider(
            api_key="sk-test-key",
            dimensions=4,
            http_post_json=lambda url, headers, payload: {
                "data": [{"index": 0, "embedding": [0.5, 0.5, 0.5, 0.5]}]
            },
        )
        self.assertEqual(openai_emb.embed_query("hello"), [0.5, 0.5, 0.5, 0.5])

        gemini_emb = GeminiEmbeddingProvider(
            api_key="gm-test-key",
            dimensions=4,
            http_post_json=lambda url, headers, payload: {
                "embeddings": [{"values": [0.25, 0.25, 0.25, 0.25]}]
            },
        )
        self.assertEqual(gemini_emb.embed_query("hello"), [0.25, 0.25, 0.25, 0.25])

    # ------------------------------------------------------------------
    # 4. Persistent Chroma Storage, Semantic Retrieval, Re-indexing, & SQLite Links
    # ------------------------------------------------------------------

    def test_persistent_chroma_indexing_deduplication_and_sqlite_linkage(self) -> None:
        seeded = self._seed_sample_resources()
        config = ProviderConfig.from_env({})
        emb_provider = create_embedding_provider(config)
        gen_provider = create_generation_provider(config)
        vector_store = ChromaPersistentVectorStore(
            self.chroma_dir,
            collection_name=config.chroma_collection,
        )
        pipeline = LocalRAGPipeline(
            store=self.store,
            vector_store=vector_store,
            embedding_provider=emb_provider,
            generation_provider=gen_provider,
            config=config,
        )

        r1 = pipeline.index_resource(seeded["backprop"]["resource_id"])
        r2 = pipeline.index_resource(seeded["boltzmann"]["resource_id"])
        r3 = pipeline.index_resource(seeded["dropout"]["resource_id"])
        self.assertEqual(r1["operation"], "indexed")
        self.assertEqual(r2["operation"], "indexed")
        self.assertEqual(r3["operation"], "indexed")
        self.assertEqual(vector_store.count(), 3)

        # Re-indexing unchanged resource is idempotent and does not duplicate records
        r1_repeat = pipeline.index_resource(seeded["backprop"]["resource_id"])
        self.assertEqual(r1_repeat["operation"], "unchanged")
        self.assertEqual(vector_store.count(), 3)
        active_chunks = pipeline.list_indexed_chunks(only_active=True)
        self.assertEqual(len(active_chunks), 3)

        # Reopen a fresh ChromaPersistentVectorStore instance on the same directory to prove persistence
        reopened_store = ChromaPersistentVectorStore(
            self.chroma_dir,
            collection_name=config.chroma_collection,
        )
        self.assertEqual(reopened_store.count(), 3)

        # Semantic query retrieves relevant chunks ranked accurately
        hits_backprop = pipeline.retrieve(
            "How does backpropagation apply the chain rule to compute gradients?"
        )
        self.assertGreaterEqual(len(hits_backprop), 1)
        self.assertEqual(hits_backprop[0].resource_id, seeded["backprop"]["resource_id"])
        self.assertEqual(hits_backprop[0].epistemic_layer, "PRIMARY_SOURCE_TEXT")

        hits_dropout = pipeline.retrieve(
            "How does dropout prevent overfitting and co-adaptation in neural networks?"
        )
        self.assertGreaterEqual(len(hits_dropout), 1)
        self.assertEqual(hits_dropout[0].resource_id, seeded["dropout"]["resource_id"])
        self.assertEqual(
            hits_dropout[0].epistemic_layer,
            "AUTHORIZED_ABSTRACT_OR_DESCRIPTION",
        )

        # Updating a resource creates a new version_id; re-indexing marks old chunk SUPERSEDED
        updated_backprop = self.store.upsert_resource(
            source_id="SRC-WIKIPEDIA",
            canonical_url="https://en.wikipedia.org/wiki/Backpropagation",
            title="Backpropagation",
            authors="Wikipedia contributors",
            published_at="2026-03-15",
            resource_type="Encyclopedia article",
            abstract_or_description=(
                "Backpropagation is reverse-mode automatic differentiation applied to neural "
                "networks, computing exact loss gradients layer by layer via the chain rule."
            ),
            collection_decision="COLLECTED",
            verification_status="HTTP_200_VERIFIED",
            license_status="CC_BY_SA_4.0",
            license_spdx="CC-BY-SA-4.0",
            permitted_storage_scope="WIKIPEDIA_OPEN_LICENSE_TEXT_PERMITTED",
            permits_full_text_storage=1,
            permits_verbatim_excerpts=1,
            legal_uncertainty_status="CLEARED_OPEN_LICENSE",
            change_reason="updated_summary_v2",
        )
        self.assertEqual(updated_backprop["operation"], "updated")
        self.assertNotEqual(
            updated_backprop["current_version_id"],
            seeded["backprop"]["current_version_id"],
        )

        r1_v2 = pipeline.index_resource(updated_backprop["resource_id"])
        self.assertEqual(r1_v2["operation"], "updated")
        self.assertEqual(r1_v2["superseded_chunk_ids"], r1["chunk_ids"])
        # Total active vectors in Chroma remains 3 (old version vector was pruned)
        self.assertEqual(vector_store.count(), 3)

        all_backprop_rows = pipeline.list_indexed_chunks(
            resource_id=updated_backprop["resource_id"],
            only_active=False,
        )
        states = {row["version_id"]: row["lifecycle_state"] for row in all_backprop_rows}
        self.assertEqual(states[seeded["backprop"]["current_version_id"]], "SUPERSEDED")
        self.assertEqual(states[updated_backprop["current_version_id"]], "ACTIVE")

        # Removing a resource from the index tombstones its SQLite chunks and deletes its Chroma vector
        removed = pipeline.remove_resource_from_index(seeded["boltzmann"]["resource_id"])
        self.assertEqual(removed["removed_chunk_ids"], r2["chunk_ids"])
        self.assertEqual(vector_store.count(), 2)
        hits_boltzmann = pipeline.retrieve("Boltzmann machine thermal equilibrium")
        self.assertEqual(
            [h for h in hits_boltzmann if h.resource_id == seeded["boltzmann"]["resource_id"]],
            [],
        )

    # ------------------------------------------------------------------
    # 5. Multi-Provider RAG Answering, Citation Verification, Fallback, & Refusal
    # ------------------------------------------------------------------

    def test_multi_provider_generation_citation_verification_and_fallbacks(self) -> None:
        seeded = self._seed_sample_resources()
        emb_provider = DeterministicHashEmbeddingProvider(dimensions=128)
        vector_store = ChromaPersistentVectorStore(self.chroma_dir)
        fallback_pipeline = LocalRAGPipeline(
            store=self.store,
            vector_store=vector_store,
            embedding_provider=emb_provider,
            generation_provider=RetrievalOnlyFallbackGenerationProvider(),
        )
        idx_bp = fallback_pipeline.index_resource(seeded["backprop"]["resource_id"])
        fallback_pipeline.index_resource(seeded["dropout"]["resource_id"])
        valid_bp_chunk_id = idx_bp["chunk_ids"][0]

        # A. Retrieval-only fallback when no generation credentials are configured
        fb_resp = fallback_pipeline.answer_question(
            "How does backpropagation compute gradients?"
        )
        self.assertEqual(fb_resp.status, "retrieval_only_fallback")
        self.assertTrue(fb_resp.sufficient_evidence)
        self.assertIsNone(fb_resp.generated_answer)
        self.assertIsNotNone(fb_resp.fallback_reason)
        self.assertGreaterEqual(len(fb_resp.retrieved_chunks), 1)
        self.assertEqual(fb_resp.citations[0].chunk_id, valid_bp_chunk_id)

        # B. Insufficient evidence for out-of-domain question
        insuff_resp = fallback_pipeline.answer_question(
            "What is the orbital period of Jupiter's moon Europa?"
        )
        self.assertEqual(insuff_resp.status, "insufficient_evidence")
        self.assertFalse(insuff_resp.sufficient_evidence)
        self.assertIsNone(insuff_resp.generated_answer)
        self.assertEqual(insuff_resp.citations, [])
        self.assertEqual(insuff_resp.retrieved_chunks, [])

        # C. OpenAI generation provider with fabricated citation stripping
        openai_gen = OpenAIGenerationProvider(
            api_key="sk-test-openai",
            model_name="gpt-4o-mini",
            http_post_json=lambda url, headers, payload: {
                "choices": [
                    {
                        "message": {
                            "content": (
                                f"Backpropagation applies the chain rule backward "
                                f"[{valid_bp_chunk_id}] and [chk_deadbeef00000000]."
                            )
                        }
                    }
                ]
            },
        )
        openai_pipeline = LocalRAGPipeline(
            store=self.store,
            vector_store=vector_store,
            embedding_provider=emb_provider,
            generation_provider=openai_gen,
        )
        oa_resp = openai_pipeline.answer_question(
            "How does backpropagation compute gradients?"
        )
        self.assertEqual(oa_resp.status, "answered")
        self.assertTrue(oa_resp.sufficient_evidence)
        assert oa_resp.generated_answer is not None
        self.assertEqual(oa_resp.generated_answer.provider_name, "openai")
        self.assertEqual(oa_resp.generated_answer.epistemic_status, "GENERATED_INTERPRETATION")
        # Fabricated chk_deadbeef00000000 must be stripped from cited_chunk_ids and citations
        self.assertEqual(oa_resp.generated_answer.cited_chunk_ids, [valid_bp_chunk_id])
        self.assertEqual([c.chunk_id for c in oa_resp.citations], [valid_bp_chunk_id])
        # Original source text remains intact and separate from generated_answer
        self.assertNotEqual(
            oa_resp.retrieved_chunks[0].chunk_text,
            oa_resp.generated_answer.answer_text,
        )

        # D. Anthropic generation provider
        anthropic_gen = AnthropicGenerationProvider(
            api_key="sk-ant-test",
            model_name="claude-3-5-haiku-latest",
            http_post_json=lambda url, headers, payload: {
                "content": [
                    {
                        "type": "text",
                        "text": f"The chain rule propagates error derivatives [{valid_bp_chunk_id}].",
                    }
                ]
            },
        )
        ant_pipeline = LocalRAGPipeline(
            store=self.store,
            vector_store=vector_store,
            embedding_provider=emb_provider,
            generation_provider=anthropic_gen,
        )
        ant_resp = ant_pipeline.answer_question("Explain backpropagation gradients.")
        self.assertEqual(ant_resp.status, "answered")
        assert ant_resp.generated_answer is not None
        self.assertEqual(ant_resp.generated_answer.provider_name, "anthropic")
        self.assertEqual(ant_resp.generated_answer.cited_chunk_ids, [valid_bp_chunk_id])

        # E. Gemini generation provider
        gemini_gen = GeminiGenerationProvider(
            api_key="gm-test-key",
            model_name="gemini-2.5-flash",
            http_post_json=lambda url, headers, payload: {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": f"Gradients are computed backward via the chain rule [{valid_bp_chunk_id}]."
                                }
                            ]
                        }
                    }
                ]
            },
        )
        gem_pipeline = LocalRAGPipeline(
            store=self.store,
            vector_store=vector_store,
            embedding_provider=emb_provider,
            generation_provider=gemini_gen,
        )
        gem_resp = gem_pipeline.answer_question("Explain backpropagation gradients.")
        self.assertEqual(gem_resp.status, "answered")
        assert gem_resp.generated_answer is not None
        self.assertEqual(gem_resp.generated_answer.provider_name, "gemini")
        self.assertEqual(gem_resp.generated_answer.cited_chunk_ids, [valid_bp_chunk_id])

    # ------------------------------------------------------------------
    # 6. Integration Proof with Pilot SQLite Copy and CLI Runner
    # ------------------------------------------------------------------

    def test_pilot_database_permitted_resources_and_cli_proof_runner(self) -> None:
        summary = run_end_to_end_rag_proof()
        self.assertEqual(
            summary["candidate_source_the_batch"]["status"],
            "CANDIDATE_PENDING_REVIEW",
        )
        self.assertFalse(summary["candidate_source_the_batch"]["ingestion_enabled"])
        self.assertEqual(summary["indexing"]["backprop_operation"], "indexed")
        self.assertEqual(summary["indexing"]["backprop_reindex_operation"], "unchanged")
        self.assertEqual(summary["retrieval_only_fallback"]["status"], "retrieval_only_fallback")
        self.assertEqual(summary["configured_provider_answer"]["status"], "answered")
        self.assertEqual(summary["insufficient_evidence"]["status"], "insufficient_evidence")

        # Also verify against a disposable copy of data/runtime/task012_pilot.sqlite if present
        pilot_db = REPO_ROOT / "data" / "runtime" / "task012_pilot.sqlite"
        if pilot_db.exists():
            copied_db = self.tmp_path / "pilot_copy.sqlite"
            shutil.copy2(pilot_db, copied_db)
            pilot_store = KnowledgeStore(copied_db)
            try:
                pilot_chroma = ChromaPersistentVectorStore(self.tmp_path / "pilot_chroma")
                pilot_pipeline = LocalRAGPipeline(
                    store=pilot_store,
                    vector_store=pilot_chroma,
                    embedding_provider=DeterministicHashEmbeddingProvider(dimensions=128),
                    generation_provider=RetrievalOnlyFallbackGenerationProvider(),
                )
                indexed_count = 0
                blocked_count = 0
                for res in pilot_store.list_resources():
                    try:
                        pilot_pipeline.index_resource(res["resource_id"])
                        indexed_count += 1
                    except PolicyBlockedIndexingError:
                        blocked_count += 1
                # Pilot DB has permitted Wikipedia (CC BY-SA 4.0) and arXiv (abstract-permitted)
                # resources alongside restricted metadata-only resources
                self.assertGreater(indexed_count, 0)
                self.assertGreater(blocked_count, 0)
                active_chunks = pilot_pipeline.list_indexed_chunks(only_active=True)
                self.assertEqual(pilot_chroma.count(), len(active_chunks))
                self.assertGreaterEqual(pilot_chroma.count(), indexed_count)
            finally:
                pilot_store.close()

    # ------------------------------------------------------------------
    # 7. Task 030 Phase 1 & 2: .env Loading Boundary, Precedence, & Redaction
    # ------------------------------------------------------------------

    def test_dotenv_loading_precedence_and_secret_redaction(self) -> None:
        dotenv_file = self.tmp_path / ".env.test"
        dotenv_file.write_text(
            "# Local test .env\n"
            "GEMINI_API_KEY=gm-dotenv-secret-111\n"
            "export ANTHROPIC_API_KEY='ant-dotenv-secret-222'\n"
            'OPENAI_API_KEY="oa-dotenv-secret-333" # inline comment\n'
            "AI_LEARNING_EMBEDDING_PROVIDER=gemini\n"
            "AI_LEARNING_EMBEDDING_MODEL=gemini-embedding-2\n"
            "AI_LEARNING_EMBEDDING_DIMENSIONS=768\n"
            "AI_LEARNING_GENERATION_PROVIDER=gemini\n"
            "AI_LEARNING_GENERATION_MODEL=gemini-3.8-flash\n",
            encoding="utf-8",
        )
        parsed = load_dotenv_file(dotenv_file)
        self.assertEqual(parsed["GEMINI_API_KEY"], "gm-dotenv-secret-111")
        self.assertEqual(parsed["ANTHROPIC_API_KEY"], "ant-dotenv-secret-222")
        self.assertEqual(parsed["OPENAI_API_KEY"], "oa-dotenv-secret-333")

        # 1. Load from .env with empty explicit environ override
        cfg = ProviderConfig.from_env({}, dotenv_path=dotenv_file, load_dotenv=True)
        self.assertEqual(cfg.embedding_provider, "gemini")
        self.assertEqual(cfg.embedding_model, "gemini-embedding-2")
        self.assertEqual(cfg.embedding_dimensions, 768)
        self.assertEqual(cfg.generation_provider, "gemini")
        self.assertEqual(cfg.generation_model, "gemini-3.8-flash")
        self.assertEqual(
            cfg.credential_status(),
            {
                "GEMINI_API_KEY": "CONFIGURED",
                "ANTHROPIC_API_KEY": "CONFIGURED",
                "OPENAI_API_KEY": "CONFIGURED",
            },
        )
        safe = cfg.to_safe_dict()
        self.assertEqual(safe["credential_status"]["GEMINI_API_KEY"], "CONFIGURED")
        for secret in ("gm-dotenv-secret-111", "ant-dotenv-secret-222", "oa-dotenv-secret-333"):
            self.assertNotIn(secret, str(safe))
            self.assertNotIn(secret, repr(cfg))

        # 2. Explicit environment variables override .env values
        cfg_override = ProviderConfig.from_env(
            {
                "AI_LEARNING_GENERATION_PROVIDER": "anthropic",
                "AI_LEARNING_GENERATION_MODEL": "claude-3-5-haiku-latest",
                "GEMINI_API_KEY": "gm-env-override-999",
                "OPENAI_API_KEY": "",
            },
            dotenv_path=dotenv_file,
            load_dotenv=True,
        )
        self.assertEqual(cfg_override.generation_provider, "anthropic")
        self.assertEqual(cfg_override.generation_model, "claude-3-5-haiku-latest")
        self.assertEqual(cfg_override.gemini_api_key, "gm-env-override-999")
        self.assertIsNone(cfg_override.openai_api_key)
        self.assertEqual(
            cfg_override.credential_status(),
            {
                "GEMINI_API_KEY": "CONFIGURED",
                "ANTHROPIC_API_KEY": "CONFIGURED",
                "OPENAI_API_KEY": "MISSING",
            },
        )

        # 3. Secret redaction helper and HTTP error redaction
        redacted_msg = redact_secrets(
            "Request failed with key gm-dotenv-secret-111 and Bearer oa-dotenv-secret-333",
            ["gm-dotenv-secret-111", "oa-dotenv-secret-333"],
        )
        self.assertNotIn("gm-dotenv-secret-111", redacted_msg)
        self.assertNotIn("oa-dotenv-secret-333", redacted_msg)
        self.assertIn("[REDACTED]", redacted_msg)

        # 4. Dimension mismatch check in GeminiEmbeddingProvider & OpenAIEmbeddingProvider
        bad_gemini = GeminiEmbeddingProvider(
            api_key="gm-test-key",
            model_name="gemini-embedding-2",
            dimensions=8,
            http_post_json=lambda url, headers, payload: {
                "embeddings": [{"values": [0.1, 0.2, 0.3, 0.4]}]
            },
        )
        with self.assertRaises(RuntimeError):
            bad_gemini.embed_query("dimension mismatch check")

        bad_openai = OpenAIEmbeddingProvider(
            api_key="oa-test-key",
            model_name="text-embedding-3-small",
            dimensions=8,
            http_post_json=lambda url, headers, payload: {
                "data": [{"index": 0, "embedding": [0.1, 0.2]}]
            },
        )
        with self.assertRaises(RuntimeError):
            bad_openai.embed_query("dimension mismatch check")

        # 5. ArxivSourceAdapter-normalized scope (METADATA_AND_ABSTRACT + ARXIV_METADATA_AND_ABSTRACT_PERMITTED)
        arxiv_adapter_res = self.store.upsert_resource(
            source_id="SRC-ARXIV",
            canonical_url="https://arxiv.org/abs/1706.03762",
            title="Attention Is All You Need",
            authors="Ashish Vaswani et al.",
            published_at="2017-06-12",
            resource_type="Preprint paper",
            abstract_or_description=(
                "The dominant sequence transduction models are based on complex recurrent or "
                "convolutional neural networks. We propose a new simple network architecture, "
                "the Transformer, based solely on attention mechanisms."
            ),
            collection_decision="COLLECTED",
            verification_status="HTTP_200_VERIFIED",
            license_status="ARXIV_METADATA_AND_ABSTRACT_PERMITTED",
            permitted_storage_scope="METADATA_AND_ABSTRACT",
            permits_full_text_storage=0,
            permits_verbatim_excerpts=0,
            legal_uncertainty_status="CLEARED_PERMITTED_SCOPE",
        )
        doc = extract_indexable_document_from_resource(
            self.store, arxiv_adapter_res["resource_id"]
        )
        self.assertEqual(doc.content_origin, "RESOURCE_ABSTRACT_METADATA")
        self.assertEqual(doc.epistemic_layer, "AUTHORIZED_ABSTRACT_OR_DESCRIPTION")
        self.assertIn("Transformer", doc.text)

    # ------------------------------------------------------------------
    # 8. Task 030 Vertex AI + ADC Configuration (Without GEMINI_API_KEY)
    # ------------------------------------------------------------------

    def test_vertex_ai_adc_embedding_and_generation_without_gemini_api_key(self) -> None:
        # Configure Gemini embedding + generation with NO GEMINI_API_KEY
        cfg = ProviderConfig.from_env(
            {
                "AI_LEARNING_EMBEDDING_PROVIDER": "gemini",
                "AI_LEARNING_EMBEDDING_MODEL": "gemini-embedding-2",
                "AI_LEARNING_EMBEDDING_DIMENSIONS": "4",
                "AI_LEARNING_GENERATION_PROVIDER": "gemini",
                "AI_LEARNING_GENERATION_MODEL": "gemini-3.8-flash",
                "GEMINI_API_KEY": "",
            }
        )
        self.assertTrue(cfg.use_vertex_ai)
        self.assertEqual(cfg.vertex_project, "kerala-lottery-intel-dev")
        self.assertEqual(cfg.vertex_location, DEFAULT_VERTEX_LOCATION)
        self.assertIsNone(cfg.gemini_api_key)
        self.assertEqual(cfg.vertex_status()["vertex_project"], "kerala-lottery-intel-dev")
        self.assertFalse(cfg.vertex_status()["requires_gemini_api_key"])

        captured_emb_requests: list[tuple[str, dict[str, str], dict[str, Any]]] = []

        def fake_vertex_emb_post(
            url: str, headers: dict[str, str], payload: dict[str, Any]
        ) -> dict[str, Any]:
            captured_emb_requests.append((url, headers, payload))
            return {"embedding": {"values": [0.5, 0.5, 0.5, 0.5]}}

        emb_provider = create_embedding_provider(
            cfg,
            adc_token_provider=lambda: "ya29.fake-adc-token-do-not-expose",
            http_post_json=fake_vertex_emb_post,
        )
        vec = emb_provider.embed_query("backpropagation chain rule")
        self.assertEqual(vec, [0.5, 0.5, 0.5, 0.5])
        self.assertEqual(len(captured_emb_requests), 1)
        emb_url, emb_headers, emb_payload = captured_emb_requests[0]
        self.assertEqual(
            emb_url,
            "https://aiplatform.googleapis.com/v1/projects/kerala-lottery-intel-dev"
            "/locations/global/publishers/google/models/gemini-embedding-2:embedContent",
        )
        self.assertEqual(
            emb_headers["Authorization"], "Bearer ya29.fake-adc-token-do-not-expose"
        )
        self.assertEqual(emb_headers["x-goog-user-project"], "kerala-lottery-intel-dev")
        self.assertNotIn("x-goog-api-key", emb_headers)
        self.assertEqual(emb_payload["outputDimensionality"], 4)

        # Also test Vertex AI :predict format for gemini-embedding-001
        emb_001 = GeminiEmbeddingProvider(
            api_key=None,
            model_name="gemini-embedding-001",
            dimensions=4,
            vertex_project="kerala-lottery-intel-dev",
            vertex_location="us-central1",
            use_vertex_ai=True,
            adc_token_provider=lambda: "ya29.fake-adc-token",
            http_post_json=lambda url, headers, payload: {
                "predictions": [{"embeddings": {"values": [0.1, 0.2, 0.3, 0.4]}}]
            },
        )
        self.assertEqual(emb_001.embed_query("test"), [0.1, 0.2, 0.3, 0.4])

        # Test Vertex AI generation with gemini-3.8-flash and no GEMINI_API_KEY
        seeded = self._seed_sample_resources()
        det_emb = DeterministicHashEmbeddingProvider(dimensions=128)
        vector_store = ChromaPersistentVectorStore(self.chroma_dir / "vertex_test")
        captured_gen_requests: list[tuple[str, dict[str, str], dict[str, Any]]] = []

        def fake_vertex_gen_post(
            url: str, headers: dict[str, str], payload: dict[str, Any]
        ) -> dict[str, Any]:
            captured_gen_requests.append((url, headers, payload))
            return {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"thought": True, "text": "Internal reasoning step."},
                                {
                                    "text": f"Backpropagation computes gradients via the chain rule [{bp_chunk_id}]."
                                },
                            ]
                        }
                    }
                ]
            }

        gen_provider = create_generation_provider(
            cfg,
            adc_token_provider=lambda: "ya29.fake-adc-token-do-not-expose",
            http_post_json=fake_vertex_gen_post,
        )
        pipeline = LocalRAGPipeline(
            store=self.store,
            vector_store=vector_store,
            embedding_provider=det_emb,
            generation_provider=gen_provider,
            config=cfg,
        )
        bp_idx = pipeline.index_resource(seeded["backprop"]["resource_id"])
        bp_chunk_id = bp_idx["chunk_ids"][0]
        resp = pipeline.answer_question("How does backpropagation compute gradients?")
        self.assertEqual(resp.status, "answered")
        assert resp.generated_answer is not None
        self.assertEqual(resp.generated_answer.provider_name, "gemini")
        self.assertEqual(resp.generated_answer.model_name, "gemini-3.8-flash")
        self.assertEqual(resp.generated_answer.cited_chunk_ids, [bp_chunk_id])
        self.assertNotIn("Internal reasoning step", resp.generated_answer.answer_text)
        gen_url, gen_headers, gen_payload = captured_gen_requests[0]
        self.assertEqual(
            gen_url,
            "https://aiplatform.googleapis.com/v1/projects/kerala-lottery-intel-dev"
            "/locations/global/publishers/google/models/gemini-3.8-flash:generateContent",
        )
        self.assertEqual(
            gen_headers["Authorization"], "Bearer ya29.fake-adc-token-do-not-expose"
        )
        self.assertEqual(gen_headers["x-goog-user-project"], "kerala-lottery-intel-dev")
        self.assertNotIn("x-goog-api-key", gen_headers)
        self.assertNotIn("generationConfig", gen_payload)

        # Missing ADC token in Vertex AI mode raises ProviderNotConfiguredError
        missing_adc_emb = GeminiEmbeddingProvider(
            api_key=None,
            model_name="gemini-embedding-2",
            dimensions=4,
            vertex_project="kerala-lottery-intel-dev",
            use_vertex_ai=True,
            adc_token_provider=lambda: "",
        )
        with self.assertRaises(ProviderNotConfiguredError):
            missing_adc_emb.embed_query("test")

        # Even if GEMINI_API_KEY is present in .env / passed, Vertex AI mode ignores it completely
        vertex_emb_128 = GeminiEmbeddingProvider(
            api_key="gm-ignored-key-in-vertex-mode",
            model_name="gemini-embedding-2",
            dimensions=128,
            vertex_project="kerala-lottery-intel-dev",
            use_vertex_ai=True,
            adc_token_provider=lambda: "ya29.fake-adc-token",
            http_post_json=lambda url, headers, payload: {
                "embedding": {"values": [1.0 / (128**0.5)] * 128}
            },
        )
        self.assertIsNone(vertex_emb_128._api_key)

        # Attempting to index or retrieve against the existing local_deterministic collection
        # with an incompatible embedding model (gemini-embedding-2, even with matching 128d)
        # raises EmbeddingIncompatibilityError instead of silently mixing vectors
        mismatched_pipeline = LocalRAGPipeline(
            store=self.store,
            vector_store=vector_store,
            embedding_provider=vertex_emb_128,
            generation_provider=gen_provider,
            config=cfg,
        )
        with self.assertRaises(EmbeddingIncompatibilityError):
            mismatched_pipeline.index_resource(seeded["boltzmann"]["resource_id"])
        with self.assertRaises(EmbeddingIncompatibilityError):
            mismatched_pipeline.retrieve("How does backpropagation compute gradients?")
        # Original collection count remains unchanged at 1
        self.assertEqual(vector_store.count(), 1)

    # ------------------------------------------------------------------
    # 9. Task 032: Research Question CLI, Citations, Abstention, & Safety
    # ------------------------------------------------------------------

    def test_research_question_cli_answerable_and_citation_resolution(self) -> None:
        seeded = self._seed_sample_resources()
        det_emb = DeterministicHashEmbeddingProvider(dimensions=128)
        vector_store = ChromaPersistentVectorStore(self.chroma_dir / "cli_test")
        pipeline = LocalRAGPipeline(
            store=self.store,
            vector_store=vector_store,
            embedding_provider=det_emb,
            generation_provider=RetrievalOnlyFallbackGenerationProvider(),
        )
        bp_idx = pipeline.index_resource(seeded["backprop"]["resource_id"])
        do_idx = pipeline.index_resource(seeded["dropout"]["resource_id"])
        bp_chunk_id = bp_idx["chunk_ids"][0]
        self.assertEqual(vector_store.count(), len(bp_idx["chunk_ids"]) + len(do_idx["chunk_ids"]))

        def fake_vertex_gen(
            url: str, headers: dict[str, str], payload: dict[str, Any]
        ) -> dict[str, Any]:
            return {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": (
                                        f"Backpropagation computes gradients of the loss function "
                                        f"with respect to network weights by applying the chain rule "
                                        f"backward from the output layer [{bp_chunk_id}]."
                                    )
                                }
                            ]
                        }
                    }
                ]
            }

        # 1. Markdown output via CLI with Vertex AI Gemini (ADC)
        out_md = io.StringIO()
        err_md = io.StringIO()
        rc_md = rag_module.main(
            [
                "How does backpropagation compute gradients in a neural network?",
                "--db",
                str(self.db_path),
                "--chroma-dir",
                str(self.chroma_dir / "cli_test"),
                "--generation-provider",
                "gemini",
                "--format",
                "markdown",
            ],
            environ={},
            load_dotenv=False,
            adc_token_provider=lambda: "ya29.fake-cli-adc-secret-token",
            http_post_json=fake_vertex_gen,
            stdout=out_md,
            stderr=err_md,
        )
        self.assertEqual(rc_md, 0, msg=err_md.getvalue())
        self.assertEqual(err_md.getvalue(), "")
        md_text = out_md.getvalue()
        self.assertIn("## 1. Generated Interpretation (`GENERATED_INTERPRETATION`)", md_text)
        self.assertIn("## 2. Verified Source Citations (`SourceCitation`)", md_text)
        self.assertIn(
            "## 3. Retrieved Source Material (`PRIMARY_SOURCE_TEXT` / `AUTHORIZED_ABSTRACT_OR_DESCRIPTION`)",
            md_text,
        )
        self.assertIn(bp_chunk_id, md_text)
        self.assertIn("https://en.wikipedia.org/wiki/Backpropagation", md_text)
        self.assertIn(seeded["backprop"]["resource_id"], md_text)
        self.assertIn(seeded["backprop"]["current_version_id"], md_text)
        self.assertNotIn("ya29.fake-cli-adc-secret-token", md_text)

        # 2. Plain-text output via CLI (-q / --question flag)
        out_txt = io.StringIO()
        err_txt = io.StringIO()
        rc_txt = rag_module.main(
            [
                "--question",
                "How does backpropagation compute gradients in a neural network?",
                "--db",
                str(self.db_path),
                "--chroma-dir",
                str(self.chroma_dir / "cli_test"),
                "--generation-provider",
                "gemini",
                "--format",
                "text",
            ],
            environ={},
            load_dotenv=False,
            adc_token_provider=lambda: "ya29.fake-cli-adc-secret-token",
            http_post_json=fake_vertex_gen,
            stdout=out_txt,
            stderr=err_txt,
        )
        self.assertEqual(rc_txt, 0, msg=err_txt.getvalue())
        txt_output = out_txt.getvalue()
        self.assertIn("1. GENERATED INTERPRETATION (GENERATED_INTERPRETATION)", txt_output)
        self.assertIn("2. VERIFIED SOURCE CITATIONS (SourceCitation)", txt_output)
        self.assertIn("3. RETRIEVED SOURCE MATERIAL", txt_output)
        self.assertIn(bp_chunk_id, txt_output)

        # 3. JSON output via CLI and verification that citation IDs resolve to SQLite rows
        out_json = io.StringIO()
        err_json = io.StringIO()
        rc_json = rag_module.main(
            [
                "How does backpropagation compute gradients in a neural network?",
                "--db",
                str(self.db_path),
                "--chroma-dir",
                str(self.chroma_dir / "cli_test"),
                "--generation-provider",
                "gemini",
                "--format",
                "json",
            ],
            environ={},
            load_dotenv=False,
            adc_token_provider=lambda: "ya29.fake-cli-adc-secret-token",
            http_post_json=fake_vertex_gen,
            stdout=out_json,
            stderr=err_json,
        )
        self.assertEqual(rc_json, 0, msg=err_json.getvalue())
        payload = json.loads(out_json.getvalue())
        self.assertEqual(payload["status"], "answered")
        self.assertTrue(payload["sufficient_evidence"])
        self.assertEqual(
            payload["generated_answer"]["epistemic_status"],
            "GENERATED_INTERPRETATION",
        )
        self.assertEqual(payload["generated_answer"]["cited_chunk_ids"], [bp_chunk_id])
        self.assertGreaterEqual(len(payload["citations"]), 1)

        # Resolve every returned citation against SQLite resources and knowledge_chunks
        for cit in payload["citations"]:
            db_row = self.store.conn.execute(
                """
                SELECT kc.chunk_id, kc.resource_id, kc.version_id, kc.source_id,
                       kc.char_start, kc.char_end, kc.epistemic_layer, kc.content_origin,
                       kc.chunk_text, r.title, r.canonical_url, r.license_status
                  FROM knowledge_chunks kc
                  JOIN resources r ON r.resource_id = kc.resource_id
                 WHERE kc.chunk_id = ? AND kc.lifecycle_state = 'ACTIVE'
                """,
                (cit["chunk_id"],),
            ).fetchone()
            self.assertIsNotNone(db_row)
            assert db_row is not None
            self.assertEqual(cit["resource_id"], db_row["resource_id"])
            self.assertEqual(cit["version_id"], db_row["version_id"])
            self.assertEqual(cit["source_id"], db_row["source_id"])
            self.assertEqual(cit["title"], db_row["title"])
            self.assertEqual(cit["canonical_url"], db_row["canonical_url"])
            self.assertEqual(cit["char_start"], int(db_row["char_start"]))
            self.assertEqual(cit["char_end"], int(db_row["char_end"]))
            self.assertEqual(cit["epistemic_layer"], db_row["epistemic_layer"])
            self.assertEqual(cit["content_origin"], db_row["content_origin"])
            self.assertEqual(cit["license_status"], db_row["license_status"])

    def test_research_question_cli_abstention_on_unanswerable_question(self) -> None:
        seeded = self._seed_sample_resources()
        det_emb = DeterministicHashEmbeddingProvider(dimensions=128)
        vector_store = ChromaPersistentVectorStore(self.chroma_dir / "cli_abstain")
        pipeline = LocalRAGPipeline(
            store=self.store,
            vector_store=vector_store,
            embedding_provider=det_emb,
            generation_provider=RetrievalOnlyFallbackGenerationProvider(),
        )
        pipeline.index_resource(seeded["backprop"]["resource_id"])

        llm_called = False

        def should_not_be_called(
            url: str, headers: dict[str, str], payload: dict[str, Any]
        ) -> dict[str, Any]:
            nonlocal llm_called
            llm_called = True
            return {}

        out_md = io.StringIO()
        err_md = io.StringIO()
        rc = rag_module.main(
            [
                "What fermentation temperature is best for sourdough rye bread starter?",
                "--db",
                str(self.db_path),
                "--chroma-dir",
                str(self.chroma_dir / "cli_abstain"),
                "--generation-provider",
                "gemini",
                "--format",
                "markdown",
            ],
            environ={},
            load_dotenv=False,
            adc_token_provider=lambda: "ya29.fake-adc-token",
            http_post_json=should_not_be_called,
            stdout=out_md,
            stderr=err_md,
        )
        # Legitimate abstention must exit with code 0 and never call the generation LLM
        self.assertEqual(rc, 0, msg=err_md.getvalue())
        self.assertFalse(llm_called)
        md_output = out_md.getvalue()
        self.assertIn("ABSTAINED — INSUFFICIENT EVIDENCE", md_output)
        self.assertIn("`insufficient_evidence`", md_output)
        self.assertIn("No answer was generated and no citations were fabricated.", md_output)

        # Verify JSON abstention structure as well
        out_json = io.StringIO()
        rc_json = rag_module.main(
            [
                "What fermentation temperature is best for sourdough rye bread starter?",
                "--db",
                str(self.db_path),
                "--chroma-dir",
                str(self.chroma_dir / "cli_abstain"),
                "--format",
                "json",
            ],
            environ={},
            load_dotenv=False,
            stdout=out_json,
            stderr=io.StringIO(),
        )
        self.assertEqual(rc_json, 0)
        abstain_data = json.loads(out_json.getvalue())
        self.assertEqual(abstain_data["status"], "insufficient_evidence")
        self.assertFalse(abstain_data["sufficient_evidence"])
        self.assertIsNone(abstain_data["generated_answer"])
        self.assertEqual(abstain_data["citations"], [])
        self.assertEqual(abstain_data["retrieved_chunks"], [])

    def test_research_question_cli_errors_secret_redaction_and_compatibility(self) -> None:
        seeded = self._seed_sample_resources()
        det_emb = DeterministicHashEmbeddingProvider(dimensions=128)
        chroma_path = self.chroma_dir / "cli_errors"
        vector_store = ChromaPersistentVectorStore(chroma_path)
        pipeline = LocalRAGPipeline(
            store=self.store,
            vector_store=vector_store,
            embedding_provider=det_emb,
            generation_provider=RetrievalOnlyFallbackGenerationProvider(),
        )
        pipeline.index_resource(seeded["backprop"]["resource_id"])
        self.assertEqual(vector_store.count(), 1)

        # 1. ReadOnlySQLiteStore blocks write operations at the SQLite engine level
        with ReadOnlySQLiteStore(self.db_path) as ro_store:
            with self.assertRaises(sqlite3.OperationalError):
                ro_store.conn.execute("DELETE FROM knowledge_chunks")

        # 2. Provider/network error returns non-zero exit code (1) and redacts API keys
        secret_openai_key = "sk-live-super-secret-openai-key-99999"

        def failing_network_post(
            url: str, headers: dict[str, str], payload: dict[str, Any]
        ) -> dict[str, Any]:
            raise RuntimeError(
                f"HTTP 401 Unauthorized calling {url} with token {secret_openai_key}"
            )

        out_net = io.StringIO()
        err_net = io.StringIO()
        rc_net = rag_module.main(
            [
                "How does backpropagation compute gradients?",
                "--db",
                str(self.db_path),
                "--chroma-dir",
                str(chroma_path),
                "--generation-provider",
                "openai",
            ],
            environ={"OPENAI_API_KEY": secret_openai_key},
            load_dotenv=False,
            http_post_json=failing_network_post,
            stdout=out_net,
            stderr=err_net,
        )
        self.assertEqual(rc_net, 1)
        self.assertNotIn(secret_openai_key, err_net.getvalue())
        self.assertNotIn(secret_openai_key, out_net.getvalue())
        self.assertIn("[REDACTED]", err_net.getvalue())
        self.assertIn("ERROR [RuntimeError]", err_net.getvalue())

        pattern_redacted = redact_secrets(
            "Failed with ya29.a0AfH6SMB_secret_oauth_token and AIzaSyD1234567890abcdefghijklmnop and Bearer secret_bearer_xyz123",
            [],
        )
        self.assertNotIn("ya29.a0AfH6SMB_secret_oauth_token", pattern_redacted)
        self.assertNotIn("AIzaSyD1234567890abcdefghijklmnop", pattern_redacted)
        self.assertNotIn("secret_bearer_xyz123", pattern_redacted)
        self.assertIn("[REDACTED]", pattern_redacted)

        # 3. Unconfigured explicit LLM generation provider returns exit code 2
        err_unconf = io.StringIO()
        rc_unconf = rag_module.main(
            [
                "How does backpropagation compute gradients?",
                "--db",
                str(self.db_path),
                "--chroma-dir",
                str(chroma_path),
                "--generation-provider",
                "openai",
            ],
            environ={"OPENAI_API_KEY": ""},
            load_dotenv=False,
            stdout=io.StringIO(),
            stderr=err_unconf,
        )
        self.assertEqual(rc_unconf, 2)
        self.assertIn("ERROR [ProviderNotConfiguredError]", err_unconf.getvalue())

        # With --allow-retrieval-fallback, unconfigured LLM falls back cleanly with exit code 0
        out_fb = io.StringIO()
        rc_fb = rag_module.main(
            [
                "How does backpropagation compute gradients?",
                "--db",
                str(self.db_path),
                "--chroma-dir",
                str(chroma_path),
                "--generation-provider",
                "openai",
                "--allow-retrieval-fallback",
                "--format",
                "json",
            ],
            environ={"OPENAI_API_KEY": ""},
            load_dotenv=False,
            stdout=out_fb,
            stderr=io.StringIO(),
        )
        self.assertEqual(rc_fb, 0)
        self.assertEqual(json.loads(out_fb.getvalue())["status"], "retrieval_only_fallback")

        # 4. Embedding compatibility check enforced via CLI (returns exit code 2 without modifying collection)
        err_compat = io.StringIO()
        rc_compat = rag_module.main(
            [
                "How does backpropagation compute gradients?",
                "--db",
                str(self.db_path),
                "--chroma-dir",
                str(chroma_path),
                "--embedding-provider",
                "gemini",
                "--embedding-dimensions",
                "768",
            ],
            environ={},
            load_dotenv=False,
            adc_token_provider=lambda: "ya29.fake-token",
            http_post_json=lambda u, h, p: {"embedding": {"values": [0.1] * 768}},
            stdout=io.StringIO(),
            stderr=err_compat,
        )
        self.assertEqual(rc_compat, 2)
        self.assertIn("ERROR [EmbeddingIncompatibilityError]", err_compat.getvalue())
        self.assertEqual(vector_store.count(), 1)

        # 5. Missing database or missing Chroma collection returns exit code 1 without creating files
        missing_db = self.tmp_path / "nonexistent_store.sqlite"
        err_db = io.StringIO()
        rc_missing_db = rag_module.main(
            [
                "How does backpropagation compute gradients?",
                "--db",
                str(missing_db),
                "--chroma-dir",
                str(chroma_path),
            ],
            environ={},
            load_dotenv=False,
            stdout=io.StringIO(),
            stderr=err_db,
        )
        self.assertEqual(rc_missing_db, 1)
        self.assertFalse(missing_db.exists())
        self.assertIn("ERROR [FileNotFoundError]", err_db.getvalue())

        err_coll = io.StringIO()
        rc_missing_coll = rag_module.main(
            [
                "How does backpropagation compute gradients?",
                "--db",
                str(self.db_path),
                "--chroma-dir",
                str(chroma_path),
                "--collection",
                "nonexistent_collection_v999",
            ],
            environ={},
            load_dotenv=False,
            stdout=io.StringIO(),
            stderr=err_coll,
        )
        self.assertEqual(rc_missing_coll, 1)
        self.assertIn("ERROR [FileNotFoundError]", err_coll.getvalue())

        # 6. CLI help, --proof, missing question, and conflicting question arguments
        out_help = io.StringIO()
        rc_help = rag_module.main(["--help"], stdout=out_help, stderr=io.StringIO())
        self.assertEqual(rc_help, 0)
        self.assertIn("--generation-provider", out_help.getvalue())
        self.assertIn("Examples:", out_help.getvalue())

        err_no_q = io.StringIO()
        rc_no_q = rag_module.main([], stdout=io.StringIO(), stderr=err_no_q)
        self.assertEqual(rc_no_q, 2)
        self.assertIn("ERROR: A research question (or --proof) is required.", err_no_q.getvalue())

        err_conflict = io.StringIO()
        rc_conflict = rag_module.main(
            ["Question one?", "--question", "Question two?"],
            stdout=io.StringIO(),
            stderr=err_conflict,
        )
        self.assertEqual(rc_conflict, 2)
        self.assertIn("ERROR [ValueError]", err_conflict.getvalue())

    def test_15_task033_citation_integrity_and_provider_sanitization(self) -> None:
        """Verify Task 033 Phase 2 citation integrity across all providers and LocalRAGPipeline."""
        seeded = self._seed_sample_resources()
        vector_store = ChromaPersistentVectorStore(
            persist_directory=self.chroma_dir,
            collection_name="test_task033_citations",
        )
        embedder = DeterministicHashEmbeddingProvider(dimensions=128)
        base_pipeline = LocalRAGPipeline(
            store=self.store,
            vector_store=vector_store,
            embedding_provider=embedder,
            generation_provider=RetrievalOnlyFallbackGenerationProvider(),
        )
        idx_wiki = base_pipeline.index_resource(seeded["backprop"]["resource_id"])
        idx_arxiv = base_pipeline.index_resource(seeded["dropout"]["resource_id"])
        valid_wiki_cid = idx_wiki["chunk_ids"][0]
        valid_arxiv_cid = idx_arxiv["chunk_ids"][0]
        fabricated_cid_1 = "chk_deadbeef12345678"
        fabricated_cid_2 = "chk_cafebabe87654321"

        retrieved_chunks = base_pipeline.retrieve(
            "How does backpropagation compute gradients using the chain rule?",
            top_k=5,
        )
        self.assertGreaterEqual(len(retrieved_chunks), 1)
        prompt_input = rag_module.RAGPromptInput(
            question="How does backpropagation compute gradients using the chain rule?",
            retrieved_chunks=retrieved_chunks,
        )

        # (1) Valid cited answer across OpenAI, Anthropic, and Gemini providers
        openai_valid = OpenAIGenerationProvider(
            api_key="sk-test-openai",
            http_post_json=lambda u, h, p: {
                "choices": [
                    {
                        "message": {
                            "content": f"Backpropagation applies the chain rule [{valid_wiki_cid}]."
                        }
                    }
                ]
            },
        ).generate_grounded_answer(prompt_input)
        self.assertEqual(openai_valid.cited_chunk_ids, [valid_wiki_cid])
        self.assertIn(f"[{valid_wiki_cid}]", openai_valid.answer_text)

        anthropic_valid = AnthropicGenerationProvider(
            api_key="sk-ant-test",
            http_post_json=lambda u, h, p: {
                "content": [
                    {
                        "type": "text",
                        "text": f"Backpropagation applies the chain rule [{valid_wiki_cid}].",
                    }
                ]
            },
        ).generate_grounded_answer(prompt_input)
        self.assertEqual(anthropic_valid.cited_chunk_ids, [valid_wiki_cid])
        self.assertIn(f"[{valid_wiki_cid}]", anthropic_valid.answer_text)

        gemini_valid = GeminiGenerationProvider(
            api_key="AIzaSyTest",
            use_vertex_ai=False,
            http_post_json=lambda u, h, p: {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": f"Backpropagation applies the chain rule [{valid_wiki_cid}]."
                                }
                            ]
                        }
                    }
                ]
            },
        ).generate_grounded_answer(prompt_input)
        self.assertEqual(gemini_valid.cited_chunk_ids, [valid_wiki_cid])
        self.assertIn(f"[{valid_wiki_cid}]", gemini_valid.answer_text)

        # (2) Answer with NO citations: providers must NOT silently attach valid_ids[:1],
        #     and LocalRAGPipeline.answer_question must abstain instead of attaching retrieved[0].
        for provider in (
            OpenAIGenerationProvider(
                api_key="sk-test-openai",
                http_post_json=lambda u, h, p: {
                    "choices": [
                        {
                            "message": {
                                "content": "Backpropagation computes gradients layer by layer."
                            }
                        }
                    ]
                },
            ),
            AnthropicGenerationProvider(
                api_key="sk-ant-test",
                http_post_json=lambda u, h, p: {
                    "content": [
                        {
                            "type": "text",
                            "text": "Backpropagation computes gradients layer by layer.",
                        }
                    ]
                },
            ),
            GeminiGenerationProvider(
                api_key="AIzaSyTest",
                use_vertex_ai=False,
                http_post_json=lambda u, h, p: {
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {"text": "Backpropagation computes gradients layer by layer."}
                                ]
                            }
                        }
                    ]
                },
            ),
        ):
            payload_no_cit = provider.generate_grounded_answer(prompt_input)
            self.assertEqual(payload_no_cit.cited_chunk_ids, [])
            pipeline_no_cit = LocalRAGPipeline(
                store=self.store,
                vector_store=vector_store,
                embedding_provider=embedder,
                generation_provider=provider,
            )
            resp_no_cit = pipeline_no_cit.answer_question(
                "How does backpropagation compute gradients using the chain rule?"
            )
            self.assertEqual(resp_no_cit.status, "insufficient_evidence")
            self.assertFalse(resp_no_cit.sufficient_evidence)
            self.assertEqual(resp_no_cit.citations, [])
            self.assertIsNone(resp_no_cit.generated_answer)
            self.assertGreaterEqual(len(resp_no_cit.retrieved_chunks), 1)
            self.assertIn("valid citations", resp_no_cit.insufficient_evidence_reason or "")

        # (3) Answer containing ONLY fabricated citation IDs (plus non-retrieved valid chunk ID):
        #     fabricated IDs are stripped from answer_text and cited_chunk_ids, and pipeline abstains.
        only_fabricated_text = (
            f"Backpropagation uses the chain rule [{fabricated_cid_1}] "
            f"and reverse accumulation ({fabricated_cid_2}) {valid_arxiv_cid}."
        )
        for provider in (
            OpenAIGenerationProvider(
                api_key="sk-test-openai",
                http_post_json=lambda u, h, p: {
                    "choices": [{"message": {"content": only_fabricated_text}}]
                },
            ),
            AnthropicGenerationProvider(
                api_key="sk-ant-test",
                http_post_json=lambda u, h, p: {
                    "content": [{"type": "text", "text": only_fabricated_text}]
                },
            ),
            GeminiGenerationProvider(
                api_key="AIzaSyTest",
                use_vertex_ai=False,
                http_post_json=lambda u, h, p: {
                    "candidates": [{"content": {"parts": [{"text": only_fabricated_text}]}}]
                },
            ),
        ):
            # Restrict prompt_input to only the backprop chunk so valid_arxiv_cid is not in retrieved
            single_chunk_prompt = rag_module.RAGPromptInput(
                question="How does backpropagation compute gradients?",
                retrieved_chunks=[c for c in retrieved_chunks if c.chunk_id == valid_wiki_cid],
            )
            payload_fab = provider.generate_grounded_answer(single_chunk_prompt)
            self.assertEqual(payload_fab.cited_chunk_ids, [])
            self.assertNotIn(fabricated_cid_1, payload_fab.answer_text)
            self.assertNotIn(fabricated_cid_2, payload_fab.answer_text)
            self.assertNotIn(valid_arxiv_cid, payload_fab.answer_text)

            pipeline_fab = LocalRAGPipeline(
                store=self.store,
                vector_store=vector_store,
                embedding_provider=embedder,
                generation_provider=provider,
            )
            resp_fab = pipeline_fab.answer_question(
                "How does backpropagation compute gradients using the chain rule?",
                source_ids=["SRC-WIKIPEDIA"],
            )
            self.assertEqual(resp_fab.status, "insufficient_evidence")
            self.assertFalse(resp_fab.sufficient_evidence)
            self.assertEqual(resp_fab.citations, [])
            self.assertIsNone(resp_fab.generated_answer)
            self.assertGreaterEqual(len(resp_fab.retrieved_chunks), 1)

        # (4) Answer containing BOTH valid and fabricated citation IDs:
        #     fabricated IDs are stripped from answer_text and citations, valid ID is preserved.
        mixed_text = (
            f"Backpropagation computes the gradient of a loss function [{valid_wiki_cid}, {fabricated_cid_1}] "
            f"by applying the chain rule backward [{fabricated_cid_2}] ({fabricated_cid_1})."
        )
        mixed_provider = OpenAIGenerationProvider(
            api_key="sk-test-openai",
            http_post_json=lambda u, h, p: {
                "choices": [{"message": {"content": mixed_text}}]
            },
        )
        pipeline_mixed = LocalRAGPipeline(
            store=self.store,
            vector_store=vector_store,
            embedding_provider=embedder,
            generation_provider=mixed_provider,
        )
        resp_mixed = pipeline_mixed.answer_question(
            "How does backpropagation compute gradients using the chain rule?",
            source_ids=["SRC-WIKIPEDIA"],
        )
        self.assertEqual(resp_mixed.status, "answered")
        self.assertTrue(resp_mixed.sufficient_evidence)
        self.assertIsNotNone(resp_mixed.generated_answer)
        assert resp_mixed.generated_answer is not None
        self.assertEqual(resp_mixed.generated_answer.cited_chunk_ids, [valid_wiki_cid])
        self.assertEqual([c.chunk_id for c in resp_mixed.citations], [valid_wiki_cid])
        self.assertIn(f"[{valid_wiki_cid}]", resp_mixed.generated_answer.answer_text)
        self.assertNotIn(fabricated_cid_1, resp_mixed.generated_answer.answer_text)
        self.assertNotIn(fabricated_cid_2, resp_mixed.generated_answer.answer_text)

        # (5) Exact citation metadata resolution against SQLite provenance
        cit = resp_mixed.citations[0]
        self.assertEqual(cit.chunk_id, valid_wiki_cid)
        self.assertEqual(cit.resource_id, seeded["backprop"]["resource_id"])
        self.assertEqual(cit.version_id, seeded["backprop"]["current_version_id"])
        self.assertEqual(cit.source_id, "SRC-WIKIPEDIA")
        self.assertEqual(cit.canonical_url, "https://en.wikipedia.org/wiki/Backpropagation")
        self.assertEqual(cit.title, "Backpropagation")
        self.assertEqual(cit.license_status, "CC_BY_SA_4_0")
        self.assertEqual(cit.license_spdx, "CC-BY-SA-4.0")
        self.assertEqual(cit.content_origin, "RESOURCE_FULL_TEXT")
        self.assertEqual(cit.epistemic_layer, "PRIMARY_SOURCE_TEXT")
        self.assertTrue(rag_module._verify_citation_metadata_against_sqlite(self.store, cit))

    def test_16_task033_post_retrieval_and_pre_retrieval_abstention(self) -> None:
        """Verify Task 033 Phase 3 pre-retrieval and post-retrieval abstention and non-rejection of valid answers."""
        seeded = self._seed_sample_resources()
        vector_store = ChromaPersistentVectorStore(
            persist_directory=self.chroma_dir,
            collection_name="test_task033_abstention",
        )
        embedder = DeterministicHashEmbeddingProvider(dimensions=128)
        base_pipeline = LocalRAGPipeline(
            store=self.store,
            vector_store=vector_store,
            embedding_provider=embedder,
            generation_provider=RetrievalOnlyFallbackGenerationProvider(),
        )
        idx_wiki = base_pipeline.index_resource(seeded["backprop"]["resource_id"])
        idx_arxiv = base_pipeline.index_resource(seeded["dropout"]["resource_id"])
        valid_wiki_cid = idx_wiki["chunk_ids"][0]
        valid_arxiv_cid = idx_arxiv["chunk_ids"][0]

        generator_calls: list[str] = []

        def _counting_http_post(response_text: str):
            def _post(url: str, headers: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
                generator_calls.append(url)
                return {"choices": [{"message": {"content": response_text}}]}

            return _post

        # 1. Pre-retrieval abstention: out-of-domain query and unindexed in-domain query
        #    must abstain before calling the generator.
        pre_provider = OpenAIGenerationProvider(
            api_key="sk-test-openai",
            http_post_json=_counting_http_post(f"Should never be called [{valid_wiki_cid}]."),
        )
        pre_pipeline = LocalRAGPipeline(
            store=self.store,
            vector_store=vector_store,
            embedding_provider=embedder,
            generation_provider=pre_provider,
        )
        ood_resp = pre_pipeline.answer_question(
            "What hydration percentage and oven temperature are best for baking sourdough bread?"
        )
        self.assertEqual(ood_resp.status, "insufficient_evidence")
        self.assertFalse(ood_resp.sufficient_evidence)
        self.assertEqual(ood_resp.retrieved_chunks, [])
        self.assertEqual(ood_resp.citations, [])
        self.assertIsNone(ood_resp.generated_answer)
        self.assertEqual(len(generator_calls), 0)

        unindexed_resp = pre_pipeline.answer_question(
            "How does Proximal Policy Optimization clip probability ratios in actor-critic reinforcement learning?"
        )
        self.assertEqual(unindexed_resp.status, "insufficient_evidence")
        self.assertFalse(unindexed_resp.sufficient_evidence)
        self.assertEqual(unindexed_resp.retrieved_chunks, [])
        self.assertEqual(unindexed_resp.citations, [])
        self.assertIsNone(unindexed_resp.generated_answer)
        self.assertEqual(len(generator_calls), 0)

        # 2. Post-retrieval abstention: partial-evidence query where chunks are retrieved above threshold,
        #    and generator explicitly reports insufficient evidence (even if referencing the chunk ID).
        for abstention_msg in (
            "The retrieved sources do not contain sufficient evidence to answer what exact learning rate or mini-batch size was used.",
            f"The retrieved chunks [{valid_arxiv_cid}] do not specify the exact GPU model or learning rate schedule.",
            "There is insufficient evidence in the retrieved sources to determine the exact weight decay hyperparameter.",
            "This question cannot be answered from the retrieved sources because only the high-level abstract is indexed.",
        ):
            abst_provider = OpenAIGenerationProvider(
                api_key="sk-test-openai",
                http_post_json=_counting_http_post(abstention_msg),
            )
            abst_pipeline = LocalRAGPipeline(
                store=self.store,
                vector_store=vector_store,
                embedding_provider=embedder,
                generation_provider=abst_provider,
            )
            partial_resp = abst_pipeline.answer_question(
                "In the dropout neural network training experiments, what exact learning rate and mini-batch size were used?"
            )
            self.assertEqual(partial_resp.status, "insufficient_evidence")
            self.assertFalse(partial_resp.sufficient_evidence)
            self.assertEqual(partial_resp.citations, [])
            self.assertIsNone(partial_resp.generated_answer)
            self.assertGreaterEqual(len(partial_resp.retrieved_chunks), 1)
            self.assertIn(
                "do not contain sufficient evidence",
                partial_resp.insufficient_evidence_reason or "",
            )

        # 3. Valid grounded answers containing words like "evidence", "insufficient", or "cannot"
        #    in domain explanations must NOT be falsely rejected as abstentions.
        for valid_domain_msg in (
            f"Empirical evidence shows that random dropout prevents complex co-adaptations of feature detectors [{valid_arxiv_cid}].",
            f"Training without regularization is insufficient to prevent overfitting on small datasets, whereas dropout reduces overfitting [{valid_arxiv_cid}].",
            f"Co-adapted feature detectors cannot generalize well alone without dropout regularization [{valid_arxiv_cid}].",
        ):
            valid_provider = OpenAIGenerationProvider(
                api_key="sk-test-openai",
                http_post_json=_counting_http_post(valid_domain_msg),
            )
            valid_pipeline = LocalRAGPipeline(
                store=self.store,
                vector_store=vector_store,
                embedding_provider=embedder,
                generation_provider=valid_provider,
            )
            valid_resp = valid_pipeline.answer_question(
                "How does dropout prevent co-adaptation and overfitting in neural networks?"
            )
            self.assertEqual(valid_resp.status, "answered")
            self.assertTrue(valid_resp.sufficient_evidence)
            self.assertIsNotNone(valid_resp.generated_answer)
            self.assertEqual([c.chunk_id for c in valid_resp.citations], [valid_arxiv_cid])

    def test_17_task033_offline_rag_evaluation_benchmark_harness(self) -> None:
        """Verify Task 033 Phase 4 offline evaluation dataset and deterministic metrics in an isolated fixture."""
        self.assertTrue(DEFAULT_RAG_EVAL_DATASET_PATH.is_file())
        dataset = json.loads(DEFAULT_RAG_EVAL_DATASET_PATH.read_text(encoding="utf-8"))
        self.assertEqual(dataset["dataset_version"], "1.0")
        self.assertEqual(len(dataset["corpus_resources"]), 6)
        self.assertEqual(len(dataset["cases"]), 16)

        eval_work_dir = self.tmp_path / "isolated_eval_run"
        summary = run_offline_rag_evaluation(
            DEFAULT_RAG_EVAL_DATASET_PATH,
            work_dir=eval_work_dir,
        )
        self.assertIsInstance(summary, RAGEvalSummary)
        self.assertEqual(summary.total_cases, 16)
        self.assertEqual(summary.answerable_cases, 11)
        self.assertEqual(summary.abstention_cases, 5)
        self.assertEqual(summary.indexed_resource_count, 6)
        self.assertEqual(summary.indexed_chunk_count, 18)

        # Retrieval metrics
        self.assertEqual(summary.resource_hit_rate_at_k, 1.0)
        self.assertEqual(summary.chunk_recall_at_k, 1.0)
        self.assertEqual(summary.mrr_at_k, 1.0)

        # Citation integrity metrics
        self.assertEqual(summary.citation_precision, 1.0)
        self.assertEqual(summary.citation_recall, 1.0)
        self.assertEqual(summary.fabricated_citation_rate, 0.0)
        self.assertEqual(summary.citation_metadata_resolution_accuracy, 1.0)

        # Answer & abstention metrics
        self.assertEqual(summary.required_concept_recall, 1.0)
        self.assertEqual(summary.abstention_precision, 1.0)
        self.assertEqual(summary.abstention_recall, 1.0)
        self.assertGreaterEqual(summary.lexical_claim_groundedness_proxy, 0.80)

        # Verify each case's expected status and category coverage
        categories = {c.category for c in summary.case_results}
        self.assertIn("answerable_single_chunk", categories)
        self.assertIn("answerable_multi_chunk", categories)
        self.assertIn("source_filtered_arxiv_multi_chunk", categories)
        self.assertIn("source_filtered_wikipedia_single_chunk", categories)
        self.assertIn("out_of_domain_negative_control", categories)
        self.assertIn("in_domain_unindexed_topic_control", categories)
        self.assertIn("partial_evidence_post_retrieval_abstention", categories)

        for case_res in summary.case_results:
            self.assertEqual(
                case_res.actual_status,
                case_res.expected_status,
                msg=f"Status mismatch on {case_res.case_id}",
            )
            self.assertEqual(
                case_res.actual_abstain,
                case_res.expected_abstain,
                msg=f"Abstention mismatch on {case_res.case_id}",
            )
            self.assertEqual(case_res.fabricated_citation_count, 0)
            self.assertTrue(case_res.citation_metadata_resolved)
            if case_res.category == "partial_evidence_post_retrieval_abstention":
                # Related chunks WERE retrieved above threshold, but pipeline abstained post-retrieval
                self.assertGreaterEqual(len(case_res.retrieved_chunk_ids), 1)
                self.assertEqual(case_res.cited_chunk_ids, [])
            elif case_res.expected_abstain:
                # Out-of-domain and unindexed in-domain abstained pre-retrieval
                self.assertEqual(case_res.retrieved_chunk_ids, [])
                self.assertEqual(case_res.cited_chunk_ids, [])


if __name__ == "__main__":
    unittest.main()
