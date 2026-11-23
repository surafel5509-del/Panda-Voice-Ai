import os
import tempfile

os.environ.setdefault("MOTION_AGENT_DB", tempfile.mktemp(suffix=".sqlite3"))
os.environ.setdefault("MOTION_AGENT_API_KEY", "test-key")

from main import validate_movie, save_memory, search_memory, create_plan, update_plan

def test_valid_movie_with_optional_voiceover():
    movie = {"resolution":"full-hd","scenes":[{"duration":8,"elements":[
        {"type":"text","text":"Launch"},
        {"type":"voice","text":"Introducing the future.","model":"azure","voice":"en-US-EmmaMultilingualNeural"}
    ]}]}
    assert validate_movie(movie)["resolution"] == "full-hd"

def test_rejects_invalid_voice_model():
    movie = {"scenes":[{"duration":4,"elements":[{"type":"voice","text":"Hello","model":"fake-provider"}]}]}
    try:
        validate_movie(movie)
        assert False, "Expected unsupported voice model to fail"
    except ValueError as exc:
        assert "voice model" in str(exc).lower()

def test_memory_and_plan_persistence():
    from main import init_db
    init_db()
    saved = save_memory("test-workspace", {"category":"style","content":"Prefers cinematic typography","tags":["type"],"importance":4})
    found = search_memory("test-workspace","cinematic typography")
    assert saved["saved"] is True
    assert found and found[0]["content"] == "Prefers cinematic typography"
    plan = create_plan("test-workspace", {"title":"Launch film","goal":"Create a product film","steps":["Storyboard","Render"]})
    updated = update_plan("test-workspace", {"plan_id":plan["plan_id"],"step_index":0,"step_status":"completed"})
    assert updated["steps"][0]["status"] == "completed"
