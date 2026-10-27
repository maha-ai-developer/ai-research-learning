"""Unit and integration tests for the Task 029 local-first RAG pipeline and provider layer."""
from __future__ import annotations

from pathlib import Path
import shutil
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
    DeterministicHashEmbeddingProvider,
    GeminiEmbeddingProvider,
    GeminiGenerationProvider,
    LocalRAGPipeline,
    OpenAIEmbeddingProvider,
    OpenAIGenerationProvider,
    PolicyBlockedIndexingError,
    ProviderConfig,
    ProviderNotConfiguredError,
    RetrievalOnlyFallbackGenerationProvider,
    chunk_text,
    compute_chunk_id,
    create_embedding_provider,
    create_generation_provider,
    evaluate_the_batch_candidate_policy,
    extract_indexable_document_from_resource,
    normalize_text_for_chunking,
    register_candidate_source_the_batch,
    run_end_to_end_rag_proof,
)
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
                self.assertEqual(pilot_chroma.count(), indexed_count)
            finally:
                pilot_store.close()


if __name__ == "__main__":
    unittest.main()
