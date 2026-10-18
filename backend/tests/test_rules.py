import pytest

from app.orchestrator.rules import detect_version, escalation_reason


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("Why was I charged twice on my card?", "billing"),
        ("Where can I download last month's invoice?", "billing"),
        ("I need a refund", "billing"),
        ("Can we negotiate an enterprise discount?", "billing"),
        ("I'm locked out of the admin console", "account"),
        ("The password reset email never shows up", "account"),
        ("Please close my Atlas account", "account"),
        ("Could you restart our prod cluster?", "account"),
        ("I want to report a security vulnerability", "security"),
        ("I think our database has been compromised", "security"),
        ("Can I talk to a human?", "human_request"),
        ("let me speak with an engineer", "human_request"),
        ("this is useless, get me someone who knows", "human_request"),
    ],
)
def test_requests_the_docs_cannot_answer_escalate(text: str, reason: str) -> None:
    assert escalation_reason(text) == reason


@pytest.mark.parametrize(
    "text",
    [
        "How do I model a payments collection with an orders reference?",
        "Can you fix my query? db.orders.find({status: 'A'}).sort({total: -1})",
        "How do I delete my data from a collection?",
        "What is a representative sample size for $sample?",
        "How do subscriptions to change streams resume after a failover?",
        "How do I restart mongod after changing the config file?",
        "E11000 duplicate key error collection: test.users index: email_1",
    ],
)
def test_technical_questions_do_not_escalate(text: str) -> None:
    assert escalation_reason(text) is None


@pytest.mark.parametrize(
    ("texts", "version"),
    [
        (["On 8.0 my transaction fails"], "8.0"),
        (["We're running MongoDB 7.0 on-prem.", "How do I compute a median?"], "7.0"),
        (["mongod v7.0.12 crashed"], "7.0"),
        (["Using MongoDB 8 with the Node driver"], "8.0"),
        (["I was on 7.0", "we upgraded to 8.0 last week"], "8.0"),
        (["How do I create an index?"], None),
        (["We run 7 nodes and 3 arbiters"], None),
        (["The Node.js driver 6.8 can't connect"], None),
    ],
)
def test_detect_version(texts: list[str], version: str | None) -> None:
    assert detect_version(texts) == version
