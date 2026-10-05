"""Session → lesson distillation tests (neural_mesh.session_distill)."""
import tempfile
from pathlib import Path

from neural_mesh import Mesh
from neural_mesh.session_distill import (
    SessionDistiller, extract_lessons, jaccard, signature, yantrikdb_turns,
)

TRANSCRIPT = """
Rule: always namespace code-graph ids before ingesting a second repo.
- Gotcha: the cron guard refuses a script that names a >1MiB artifact literally.
Note: this session couldn't run the scanner — no terminal tool available.
Pitfall: relative imports are not resolved by code_ingest.
Just some ordinary prose that should never become a lesson.
brain/01-campaigns/2026-09-27-alpha-digest.md
Important: never log credentials or paste them into a commit.
"""


def test_extract_lessons_picks_rules_and_drops_noise():
    got = extract_lessons(TRANSCRIPT)
    joined = " ".join(got)
    assert "namespace code-graph ids" in joined
    assert "cron guard refuses" in joined
    assert "credentials" in joined
    # dropped: status narration, bare path, plain prose
    assert "couldn't run the scanner" not in joined
    assert "ordinary prose" not in joined
    assert not any(g.strip().endswith(".md") for g in got)


def test_signature_is_order_insensitive():
    a = signature("never log credentials into a commit")
    b = signature("credentials commit log never into a")
    assert a == b


def test_jaccard_bounds():
    assert jaccard("alpha beta", "alpha beta") == 1.0
    assert jaccard("alpha", "gamma") == 0.0
    assert 0.0 < jaccard("alpha beta gamma", "alpha beta delta") < 1.0


def test_distill_adds_then_dedupes():
    with tempfile.TemporaryDirectory() as d:
        mesh = Mesh(db_path=str(Path(d) / "m.db"))
        dist = SessionDistiller(mesh)
        first = dist.distill([("sess-1", TRANSCRIPT)])
        assert first["added"] >= 3
        assert first["duplicates"] == 0
        # same content again -> nothing new
        second = dist.distill([("sess-2", TRANSCRIPT)])
        assert second["added"] == 0
        assert second["duplicates"] == first["added"]
        # lessons are real mesh nodes
        lessons = [n for n in mesh._load().values() if n.meta.get("lesson")]
        assert len(lessons) == first["added"]
        assert all(n.provenance == "session-distill" for n in lessons)


def test_report_surfaces_recurring_lessons():
    shared = "Rule: always namespace code-graph ids before a second repo ingest."
    other1 = "Rule: prefer precision floors over always-fill top_k retrieval."
    other2 = "Gotcha: the guard scans large artifacts and refuses."
    with tempfile.TemporaryDirectory() as d:
        mesh = Mesh(db_path=str(Path(d) / "m.db"))
        dist = SessionDistiller(mesh)
        dist.distill([("sess-a", shared + "\n" + other1),
                      ("sess-b", shared + "\n" + other2)])
        rows = dist.report(min_support=2)
        assert len(rows) == 1                      # only the shared lesson recurs
        assert rows[0]["support"] == 2
        assert "namespace code-graph ids" in rows[0]["example"]


def test_yantrikdb_turns_missing_file_is_empty():
    assert yantrikdb_turns("/nonexistent/path.db") == []