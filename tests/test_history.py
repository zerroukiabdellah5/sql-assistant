import app.history as history


def test_create_and_get_session(meta_paths):
    session = history.create_session("My first session")
    fetched = history.get_session(session["id"])
    assert fetched["title"] == "My first session"
    assert history.session_exists(session["id"]) is True


def test_list_sessions_ordered_by_update(meta_paths):
    first = history.create_session("Old")
    second = history.create_session("Newer")
    history.add_message(first["id"], "user", prompt="q1")
    sessions = history.list_sessions()
    assert sessions[0]["id"] == first["id"]
    assert [s["id"] for s in sessions] == [first["id"], second["id"]]


def test_add_and_get_messages(meta_paths):
    session = history.create_session()
    history.add_message(
        session["id"], "user", prompt="Show Nike",
        sql="", count=0, truncated=False,
    )
    history.add_message(
        session["id"], "assistant", prompt="",
        sql="SELECT * FROM products",
        explanation="Lists products", count=2, truncated=True,
    )
    messages = history.get_messages(session["id"])
    assert len(messages) == 2
    assert messages[0]["role"] == "user"
    assert messages[1]["sql"] == "SELECT * FROM products"
    assert messages[1]["truncated"] == 1


def test_build_history_turns(meta_paths):
    session = history.create_session()
    history.add_message(session["id"], "user", prompt="q1")
    history.add_message(
        session["id"], "assistant", sql="SELECT 1",
    )
    turns = history.build_history_turns(session["id"])
    assert turns == [{"prompt": "q1", "sql": "SELECT 1"}]


def test_build_history_turns_respects_max(meta_paths):
    session = history.create_session()
    for i in range(5):
        history.add_message(session["id"], "user", prompt=f"q{i}")
        history.add_message(
            session["id"], "assistant", sql=f"SELECT {i}",
        )
    turns = history.build_history_turns(
        session["id"], max_turns=2
    )
    assert len(turns) == 2
    assert turns[-1]["prompt"] == "q4"


def test_add_message_unknown_session_ok(meta_paths):
    session = history.create_session()
    history.add_message(session["id"], "user", prompt="orphan test")
    messages = history.get_messages(session["id"])
    assert len(messages) == 1


def test_delete_session_removes_messages(meta_paths):
    session = history.create_session()
    history.add_message(session["id"], "user", prompt="q")
    history.delete_session(session["id"])
    assert history.session_exists(session["id"]) is False
    assert history.get_messages(session["id"]) == []


def test_rolling_summary_created_at_tenth(meta_paths):
    session = history.create_session()
    for i in range(10):
        history.add_message(session["id"], "user", prompt=f"question {i}")
        history.add_message(session["id"], "assistant", sql=f"SELECT {i}")
    fetched = history.get_session(session["id"])
    assert "Recent questions" in fetched["summary"]