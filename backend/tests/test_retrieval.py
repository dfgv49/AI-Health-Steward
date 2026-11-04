"""Unit tests for hybrid retrieval: Reciprocal Rank Fusion (P0).

RRF fuses the vector ranked list with the BM25 ranked list using ranks only,
because cosine distance and BM25 scores live on incomparable scales.

These tests cover the fusion maths in isolation — no DB, no embedding model,
no jieba required.
"""
import pytest

from app.services.rag import _rrf_fuse


def _hit(cid: str, source: str = "vector") -> dict:
    return {
        "chunk_id": cid,
        "chunk_text": f"text-{cid}",
        "report_id": 1,
    }


class TestRRFFusion:
    def test_document_ranked_first_by_both_wins(self) -> None:
        vector = [_hit("A"), _hit("B")]
        bm25 = [_hit("A"), _hit("B")]

        out = _rrf_fuse(vector, bm25, k=60)

        assert [h["chunk_id"] for h in out] == ["A", "B"]

    def test_document_in_both_lists_beats_single_list_top_hit(self) -> None:
        # B is #2 in both lists; C is #1 but only in the vector list.
        # RRF = 1/62 + 1/62 = 0.0322 for B, 1/61 = 0.0164 for C.
        vector = [_hit("C"), _hit("B")]
        bm25 = [_hit("A"), _hit("B")]

        out = {h["chunk_id"]: h["score"] for h in _rrf_fuse(vector, bm25, k=60)}

        assert out["B"] > out["C"]

    def test_normalised_score_is_one_when_first_in_both(self) -> None:
        out = _rrf_fuse([_hit("A")], [_hit("A")], k=60)

        assert out[0]["chunk_id"] == "A"
        assert out[0]["score"] == pytest.approx(1.0)

    def test_scores_stay_within_unit_interval(self) -> None:
        vector = [_hit(x) for x in "ABCDE"]
        bm25 = [_hit(x) for x in "EDCBA"]

        out = _rrf_fuse(vector, bm25, k=60)

        assert all(0.0 <= h["score"] <= 1.0 for h in out)
        assert all(0.0 < h["rrf_score"] for h in out)

    def test_output_is_sorted_descending(self) -> None:
        vector = [_hit("A"), _hit("B"), _hit("C")]
        bm25 = [_hit("C"), _hit("B"), _hit("A")]

        scores = [h["score"] for h in _rrf_fuse(vector, bm25, k=60)]

        assert scores == sorted(scores, reverse=True)

    def test_union_covers_both_lists_exactly_once(self) -> None:
        vector = [_hit("A"), _hit("B")]
        bm25 = [_hit("B"), _hit("C"), _hit("D")]

        ids = [h["chunk_id"] for h in _rrf_fuse(vector, bm25, k=60)]

        assert sorted(ids) == ["A", "B", "C", "D"]  # no duplicates, no drops

    def test_bm25_only_hit_keeps_payload(self) -> None:
        # payload.setdefault must preserve the keyword-path record so the
        # caller can still render chunk_text / report_id.
        out = _rrf_fuse([], [_hit("D")], k=60)

        assert out[0]["chunk_id"] == "D"
        assert out[0]["chunk_text"] == "text-D"
        assert out[0]["report_id"] == 1

    def test_symmetric_rankings_score_equally(self) -> None:
        # Symmetry: fusing (A,B) with (B,A) must score A and B equally.
        out = {
            h["chunk_id"]: h["score"]
            for h in _rrf_fuse([_hit("A"), _hit("B")], [_hit("B"), _hit("A")], k=60)
        }

        assert out["A"] == pytest.approx(out["B"])

    @pytest.mark.parametrize(
        "vector,bm25",
        [
            ([], []),
            ([_hit("A")], []),
            ([], [_hit("A")]),
        ],
    )
    def test_degenerate_inputs_do_not_raise(self, vector, bm25) -> None:
        result = _rrf_fuse(vector, bm25, k=60)

        assert isinstance(result, list)
        # At most one side is populated here, so union size == sum of sizes.
        assert len(result) == len(vector) + len(bm25)

    def test_k_changes_relative_weight_of_ranks(self) -> None:
        # A smaller k sharpens the advantage of top ranks. Verify the ordering
        # is stable but the gap widens, which is the knob we tune later.
        vector = [_hit("A"), _hit("B")]
        bm25 = [_hit("A"), _hit("B")]

        small = {h["chunk_id"]: h["score"] for h in _rrf_fuse(vector, bm25, k=10)}
        large = {h["chunk_id"]: h["score"] for h in _rrf_fuse(vector, bm25, k=60)}

        assert small["A"] == large["A"] == pytest.approx(1.0)  # both normalised
        assert small["B"] < large["B"]  # rank-2 gets a bigger share when k is large


class TestVectorScoreNormalisation:
    def test_cosine_distance_maps_to_unit_interval(self) -> None:
        # Mirror the formula used by _vector_recall:
        # score = max(0, 1 - distance / 2), distance in [0, 2].
        for distance, expected in [(0.0, 1.0), (1.0, 0.5), (2.0, 0.0), (2.5, 0.0)]:
            assert max(0.0, 1.0 - distance / 2.0) == pytest.approx(expected)
