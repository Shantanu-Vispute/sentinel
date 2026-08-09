import sqlite3

from digest.search import build_fts_query, lexical_rankings, reciprocal_rank_fusion


def test_build_fts_query_quotes_terms_and_uses_prefix_matching():
    assert build_fts_query("voice transcription") == '"voice"* AND "transcription"*'
    assert build_fts_query("MCP / agents") == '"MCP"* AND "agents"*'


def test_lexical_rankings_returns_bm25_ordered_story_ids():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE VIRTUAL TABLE stories_fts USING fts5(story_id UNINDEXED, title, summary)"
    )
    conn.executemany(
        "INSERT INTO stories_fts(story_id, title, summary) VALUES (?, ?, ?)",
        [
            ("one", "Voice transcription model", "Offline speech recognition"),
            ("two", "A gardening note", "Plants and soil"),
            ("three", "Voice model", "A short mention of transcription"),
        ],
    )

    result = lexical_rankings(conn, "voice transcription")

    assert result[0] == "one"
    assert set(result) == {"one", "three"}
    conn.close()


def test_reciprocal_rank_fusion_rewards_results_present_in_both_rankings():
    scores = reciprocal_rank_fusion(
        ["shared", "lexical-only"],
        ["shared", "semantic-only"],
    )

    assert scores["shared"] > scores["lexical-only"]
    assert scores["shared"] > scores["semantic-only"]
