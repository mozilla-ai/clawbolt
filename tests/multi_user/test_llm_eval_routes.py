"""Admin endpoints for the model-swap evaluator.

Covers the things that gate real behavior: the consent requirement, the
server-side baseline resolution (an operator cannot pick what they are
comparing against), the one-run-per-user guard, cancellation, and the
report's worst-first ordering and PII redaction.

``launch_run`` is patched throughout. Letting it fire would start a real
background evaluation against real providers.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Generator
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.auth.admin_dep import get_current_admin
from backend.app.config import settings
from backend.app.models import LLMEvalRun, LLMEvalTurnResult, Subscription, User
from backend.app.services.llm_eval.metrics import MIN_TURNS_FOR_VERDICT
from backend.app.services.llm_eval.types import AgreementClass, RunStatus

BASE = "/api/admin/llm-eval"


@pytest.fixture()
def admin_client(client: TestClient, test_user: User) -> Generator[TestClient]:
    from tests.multi_user.conftest import MULTI_USER_APP as app

    app.dependency_overrides[get_current_admin] = lambda: test_user
    yield client
    app.dependency_overrides.pop(get_current_admin, None)


@pytest.fixture()
def consenting_user(db_session: Session, test_user: User) -> User:
    user = db_session.get(User, test_user.id)
    assert user is not None
    user.data_sharing_consent = True
    db_session.commit()
    return test_user


@pytest.fixture()
def _launch() -> Generator[MagicMock]:
    with patch("backend.app.routers.admin_llm_eval.launch_run") as mock:
        yield mock


@pytest.fixture(autouse=True)
def _global_model() -> Generator[None]:
    with (
        patch.object(settings, "llm_provider", "anthropic"),
        patch.object(settings, "llm_model", "incumbent-model"),
    ):
        yield


def _payload(**overrides: object) -> dict:
    body = {
        "candidate_provider": "anthropic",
        "candidate_model": "candidate-model",
        "sample_count": 50,
        "judge_enabled": True,
    }
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# Consent gate
# ---------------------------------------------------------------------------


def test_starting_a_run_requires_consent(
    admin_client: TestClient, test_user: User, _launch: MagicMock
) -> None:
    response = admin_client.post(f"{BASE}/users/{test_user.id}/runs", json=_payload())
    assert response.status_code == 403
    assert "consent" in response.json()["detail"].lower()
    _launch.assert_not_called()


def test_unknown_user_is_404(admin_client: TestClient, _launch: MagicMock) -> None:
    response = admin_client.post(f"{BASE}/users/{uuid.uuid4()}/runs", json=_payload())
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Starting a run
# ---------------------------------------------------------------------------


def test_start_run_creates_a_pending_row_and_launches(
    admin_client: TestClient, consenting_user: User, db_session: Session, _launch: MagicMock
) -> None:
    response = admin_client.post(f"{BASE}/users/{consenting_user.id}/runs", json=_payload())
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == RunStatus.PENDING
    assert body["candidate_model"] == "candidate-model"
    assert body["requested_samples"] == 50
    _launch.assert_called_once()

    # ``body["id"]`` is the public id, so the row is found by that column.
    row = db_session.execute(
        select(LLMEvalRun).filter_by(public_id=body["id"])
    ).scalar_one_or_none()
    assert row is not None
    assert row.user_id == consenting_user.id


def test_baseline_comes_from_the_server_not_the_client(
    admin_client: TestClient, consenting_user: User, _launch: MagicMock
) -> None:
    """A report must compare against the model the user is actually on."""
    response = admin_client.post(
        f"{BASE}/users/{consenting_user.id}/runs",
        json=_payload(baseline_model="something-else"),
    )
    assert response.status_code == 201
    assert response.json()["baseline_model"] == "incumbent-model"


def test_baseline_prefers_the_users_subscription_override(
    admin_client: TestClient,
    consenting_user: User,
    db_session: Session,
    _launch: MagicMock,
) -> None:
    db_session.add(
        Subscription(
            user_id=consenting_user.id,
            role="user",
            plan="free",
            status="active",
            llm_model_override="pinned-model",
        )
    )
    db_session.commit()

    response = admin_client.post(f"{BASE}/users/{consenting_user.id}/runs", json=_payload())
    assert response.status_code == 201
    assert response.json()["baseline_model"] == "pinned-model"


def test_sample_count_above_the_cap_is_rejected(
    admin_client: TestClient, consenting_user: User, _launch: MagicMock
) -> None:
    with patch.object(settings, "llm_eval_max_samples", 10):
        response = admin_client.post(
            f"{BASE}/users/{consenting_user.id}/runs", json=_payload(sample_count=500)
        )
    assert response.status_code == 422
    _launch.assert_not_called()


def test_second_concurrent_run_for_the_same_user_conflicts(
    admin_client: TestClient, consenting_user: User, _launch: MagicMock
) -> None:
    first = admin_client.post(f"{BASE}/users/{consenting_user.id}/runs", json=_payload())
    assert first.status_code == 201
    second = admin_client.post(f"{BASE}/users/{consenting_user.id}/runs", json=_payload())
    assert second.status_code == 409
    assert _launch.call_count == 1


def test_global_concurrency_cap_returns_429(
    admin_client: TestClient, consenting_user: User, db_session: Session, _launch: MagicMock
) -> None:
    """Runs share the provider rate limit with live traffic, so the per-user
    guard is not enough on its own."""
    other = User(id=str(uuid.uuid4()), user_id=f"google_{uuid.uuid4().hex[:8]}")
    db_session.add(other)
    db_session.commit()
    db_session.add(
        LLMEvalRun(
            user_id=other.id,
            baseline_provider="anthropic",
            baseline_model="incumbent-model",
            candidate_provider="anthropic",
            candidate_model="candidate-model",
            requested_samples=10,
            status=str(RunStatus.RUNNING),
        )
    )
    db_session.commit()

    with patch.object(settings, "llm_eval_max_concurrent_runs", 1):
        response = admin_client.post(f"{BASE}/users/{consenting_user.id}/runs", json=_payload())
    assert response.status_code == 429
    assert "limit is 1" in response.json()["detail"]
    _launch.assert_not_called()


def test_judge_disabled_leaves_the_judge_model_empty(
    admin_client: TestClient, consenting_user: User, _launch: MagicMock
) -> None:
    response = admin_client.post(
        f"{BASE}/users/{consenting_user.id}/runs", json=_payload(judge_enabled=False)
    )
    assert response.status_code == 201
    assert response.json()["judge_model"] == ""


# ---------------------------------------------------------------------------
# Listing, reporting, cancelling
# ---------------------------------------------------------------------------


def _make_run(db: Session, user_id: str, **overrides: object) -> LLMEvalRun:
    run = LLMEvalRun(
        user_id=user_id,
        baseline_provider="anthropic",
        baseline_model="incumbent-model",
        candidate_provider="anthropic",
        candidate_model="candidate-model",
        requested_samples=10,
        status=str(RunStatus.COMPLETED),
        recommendation="safe_to_switch",
    )
    for key, value in overrides.items():
        setattr(run, key, value)
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def test_list_runs_spans_every_user_by_default(
    admin_client: TestClient, consenting_user: User, db_session: Session
) -> None:
    """Unfiltered is the useful default: it is how a run is found again.

    An operator looking for last week's evaluation does not necessarily
    remember which user it was against.
    """
    other = User(id=str(uuid.uuid4()), user_id="other-user")
    db_session.add(other)
    db_session.commit()
    # The email lives on Subscription, and the base fixture user has no row,
    # so both sides of the join are covered: one with an email, one without.
    db_session.add(Subscription(user_id=other.id, email="other@example.com"))
    db_session.commit()
    _make_run(db_session, consenting_user.id)
    _make_run(db_session, other.id)

    body = admin_client.get(f"{BASE}/runs").json()
    assert body["total"] == 2
    owners = {r["user_id"] for r in body["runs"]}
    assert owners == {consenting_user.id, other.id}
    # The listing names whose run each row is, or the table cannot be read.
    # A user with no subscription row reports an empty email rather than
    # dropping out of the listing.
    assert {r["user_email"] for r in body["runs"]} == {"other@example.com", ""}


def test_list_runs_filters_to_one_user(
    admin_client: TestClient, consenting_user: User, db_session: Session
) -> None:
    other = User(id=str(uuid.uuid4()), user_id="other-user")
    db_session.add(other)
    db_session.commit()
    _make_run(db_session, consenting_user.id)
    _make_run(db_session, other.id)

    body = admin_client.get(f"{BASE}/runs?user_id={consenting_user.id}").json()
    assert body["total"] == 1
    assert body["runs"][0]["user_id"] == consenting_user.id


def test_list_runs_404s_an_unknown_user_filter(admin_client: TestClient) -> None:
    """An empty page would read as "never evaluated" rather than "no such user"."""
    assert admin_client.get(f"{BASE}/runs?user_id={uuid.uuid4()}").status_code == 404


def test_list_runs_pages(
    admin_client: TestClient, consenting_user: User, db_session: Session
) -> None:
    for _ in range(3):
        _make_run(db_session, consenting_user.id)

    body = admin_client.get(f"{BASE}/runs?limit=2").json()
    assert len(body["runs"]) == 2
    assert body["total"] == 3
    assert len(admin_client.get(f"{BASE}/runs?limit=2&offset=2").json()["runs"]) == 1


def test_list_runs_keeps_a_run_whose_user_withdrew_consent_and_says_so(
    admin_client: TestClient, consenting_user: User, db_session: Session
) -> None:
    """A row is run metadata, not conversation content, so it survives.

    The report does not: it is consent-gated, which is why the row carries the
    flag rather than the console offering a link that 403s.
    """
    run = _make_run(db_session, consenting_user.id)
    user = db_session.get(User, consenting_user.id)
    assert user is not None
    user.data_sharing_consent = False
    db_session.commit()

    body = admin_client.get(f"{BASE}/runs").json()
    assert [r["id"] for r in body["runs"]] == [run.public_id]
    assert body["runs"][0]["user_consented"] is False
    # And the evidence really is refused.
    assert admin_client.get(f"{BASE}/runs/{run.public_id}").status_code == 403


def test_a_run_is_addressed_by_a_public_id_not_its_row_id(
    admin_client: TestClient, consenting_user: User, db_session: Session
) -> None:
    """The report URL is pasted and bookmarked, so it must not be a row counter.

    The integer primary key stays internal: the worker and the turn rows use
    it, and it must not be reachable from the API.
    """
    run = _make_run(db_session, consenting_user.id)
    assert admin_client.get(f"{BASE}/runs/{run.id}").status_code == 404

    body = admin_client.get(f"{BASE}/runs/{run.public_id}").json()
    assert body["run"]["id"] == run.public_id
    assert body["run"]["id"] != str(run.id)


def test_list_runs_reports_the_bounds_the_run_form_has_to_respect(
    admin_client: TestClient, consenting_user: User
) -> None:
    """The sample control cannot hold its own ceiling.

    ``LLM_EVAL_MAX_SAMPLES`` is configurable and ``start_run`` enforces it, so
    a client guessing the cap offers sizes the API rejects with a bare 422.
    """
    with patch.object(settings, "llm_eval_max_samples", 40):
        response = admin_client.get(f"{BASE}/runs?user_id={consenting_user.id}")
    assert response.status_code == 200
    body = response.json()
    assert body["max_samples"] == 40
    assert body["min_turns_for_verdict"] == MIN_TURNS_FOR_VERDICT


def test_report_orders_the_most_concerning_turns_first(
    admin_client: TestClient, consenting_user: User, db_session: Session
) -> None:
    run = _make_run(db_session, consenting_user.id)
    db_session.add_all(
        [
            LLMEvalTurnResult(
                run_id=run.id,
                message_seq=1,
                user_message="matched turn",
                agreement=str(AgreementClass.IDENTICAL),
            ),
            LLMEvalTurnResult(
                run_id=run.id,
                message_seq=2,
                user_message="quiet divergence",
                agreement=str(AgreementClass.SAME_TOOLS_DIFFERENT_ARGS),
            ),
            LLMEvalTurnResult(
                run_id=run.id,
                message_seq=3,
                user_message="unsafe turn",
                agreement=str(AgreementClass.DIFFERENT_TOOLS),
                safety_issues=json.dumps([{"finding": "unknown_tool", "tool_name": "nope"}]),
            ),
            LLMEvalTurnResult(
                run_id=run.id,
                message_seq=4,
                user_message="stopped acting",
                agreement=str(AgreementClass.REPLIED_INSTEAD_OF_ACTING),
            ),
        ]
    )
    db_session.commit()

    response = admin_client.get(f"{BASE}/runs/{run.public_id}")
    assert response.status_code == 200
    order = [t["message_seq"] for t in response.json()["turns"]]
    # Safety finding first, then the silent no-op, then the quiet
    # divergence, with the matched turn last.
    assert order == [3, 4, 2, 1]


def test_report_pages_turns_and_reports_the_total(
    admin_client: TestClient, consenting_user: User, db_session: Session
) -> None:
    """Every text column on a turn is decrypted and redacted per request, so a
    run's evidence is paged rather than shipped whole."""
    run = _make_run(db_session, consenting_user.id)
    db_session.add_all(
        [
            LLMEvalTurnResult(
                run_id=run.id,
                message_seq=i,
                user_message=f"turn {i}",
                agreement=str(AgreementClass.IDENTICAL),
            )
            for i in range(1, 8)
        ]
    )
    db_session.commit()

    first = admin_client.get(f"{BASE}/runs/{run.public_id}?limit=3").json()
    assert len(first["turns"]) == 3
    assert first["total_turns"] == 7

    second = admin_client.get(f"{BASE}/runs/{run.public_id}?limit=3&offset=3").json()
    assert len(second["turns"]) == 3
    # Pages must not overlap, or "show more" would repeat turns.
    assert {t["message_seq"] for t in first["turns"]}.isdisjoint(
        t["message_seq"] for t in second["turns"]
    )


def test_report_orders_before_paging(
    admin_client: TestClient, consenting_user: User, db_session: Session
) -> None:
    """The first page has to be the worst turns, not an arbitrary three."""
    run = _make_run(db_session, consenting_user.id)
    rows = [
        LLMEvalTurnResult(
            run_id=run.id,
            message_seq=i,
            user_message=f"clean {i}",
            agreement=str(AgreementClass.IDENTICAL),
        )
        for i in range(1, 7)
    ]
    rows.append(
        LLMEvalTurnResult(
            run_id=run.id,
            message_seq=99,
            user_message="the bad one",
            agreement=str(AgreementClass.DIFFERENT_TOOLS),
            safety_issues=json.dumps([{"finding": "unknown_tool", "tool_name": "nope"}]),
        )
    )
    db_session.add_all(rows)
    db_session.commit()

    page = admin_client.get(f"{BASE}/runs/{run.public_id}?limit=1").json()
    assert page["turns"][0]["message_seq"] == 99


def test_report_redacts_pii_in_message_bodies(
    admin_client: TestClient, consenting_user: User, db_session: Session
) -> None:
    run = _make_run(db_session, consenting_user.id)
    db_session.add(
        LLMEvalTurnResult(
            run_id=run.id,
            message_seq=1,
            user_message="call me at +15555550123",
            agreement=str(AgreementClass.IDENTICAL),
            candidate_tool_calls=json.dumps(
                [{"name": "send_message", "arguments": {"to": "jane.doe@example.com"}}]
            ),
        )
    )
    db_session.commit()

    turn = admin_client.get(f"{BASE}/runs/{run.public_id}").json()["turns"][0]
    assert "+15555550123" not in turn["user_message"]
    assert "[PHONE]" in turn["user_message"]
    assert turn["candidate"]["tool_calls"][0]["arguments"]["to"] == "[EMAIL]"


def test_report_for_unknown_run_is_404(admin_client: TestClient) -> None:
    assert admin_client.get(f"{BASE}/runs/{uuid.uuid4()}").status_code == 404


def test_cancel_flips_an_active_run(
    admin_client: TestClient, consenting_user: User, db_session: Session
) -> None:
    run = _make_run(db_session, consenting_user.id, status=str(RunStatus.RUNNING))
    response = admin_client.post(f"{BASE}/runs/{run.public_id}/cancel")
    assert response.status_code == 200
    assert response.json()["status"] == RunStatus.CANCELLED


def test_cancelling_a_finished_run_conflicts(
    admin_client: TestClient, consenting_user: User, db_session: Session
) -> None:
    run = _make_run(db_session, consenting_user.id, status=str(RunStatus.COMPLETED))
    assert admin_client.post(f"{BASE}/runs/{run.public_id}/cancel").status_code == 409


# ---------------------------------------------------------------------------
# Report ordering and accounting
# ---------------------------------------------------------------------------


def test_report_ranks_blocking_findings_above_advisory_ones(
    admin_client: TestClient, consenting_user: User, db_session: Session
) -> None:
    """An advisory badge must not outrank the turn that decided the verdict.

    ``unresolved_tool_name`` is a property of the replayed fixture and the
    summary says so, but keying the sort on "has any finding" put five of
    them on the first screen and pushed a genuine unrequested mutation below
    the fold.
    """
    run = _make_run(db_session, consenting_user.id, judge_model="incumbent-model")
    db_session.add_all(
        [
            LLMEvalTurnResult(
                run_id=run.id,
                message_seq=1,
                user_message="retired tool in the history",
                agreement=str(AgreementClass.DIFFERENT_TOOLS),
                safety_issues=json.dumps(
                    [{"finding": "unresolved_tool_name", "tool_name": "retired"}]
                ),
            ),
            LLMEvalTurnResult(
                run_id=run.id,
                message_seq=2,
                user_message="wrote something nobody asked for",
                agreement=str(AgreementClass.DIFFERENT_TOOLS),
                safety_issues=json.dumps(
                    [{"finding": "unrequested_mutation", "tool_name": "qb_update"}]
                ),
            ),
            LLMEvalTurnResult(
                run_id=run.id,
                message_seq=3,
                user_message="judge scored it against the candidate",
                agreement=str(AgreementClass.DIFFERENT_TOOLS),
                judge_verdict="candidate_worse",
            ),
            LLMEvalTurnResult(
                run_id=run.id,
                message_seq=4,
                user_message="quiet agreement",
                agreement=str(AgreementClass.IDENTICAL),
            ),
        ]
    )
    db_session.commit()

    response = admin_client.get(f"{BASE}/runs/{run.public_id}")
    assert response.status_code == 200
    assert [t["message_seq"] for t in response.json()["turns"]] == [2, 3, 1, 4]


def test_report_exposes_cache_creation_so_the_columns_can_be_read(
    admin_client: TestClient, consenting_user: User, db_session: Session
) -> None:
    """Without it the two token columns are unreadable side by side.

    An incumbent whose whole prompt is a fresh cache write reports nine
    thousand ``input_tokens`` next to a candidate reporting a hundred and
    forty-five thousand, for the same prompt.
    """
    run = _make_run(db_session, consenting_user.id)
    db_session.add(
        LLMEvalTurnResult(
            run_id=run.id,
            message_seq=1,
            user_message="a turn",
            agreement=str(AgreementClass.IDENTICAL),
            baseline_input_tokens=9329,
            baseline_cache_read_tokens=18288,
            baseline_cache_creation_tokens=223482,
            candidate_input_tokens=145270,
            candidate_cache_read_tokens=0,
            candidate_cache_creation_tokens=0,
        )
    )
    db_session.commit()

    response = admin_client.get(f"{BASE}/runs/{run.public_id}")
    turn = response.json()["turns"][0]
    assert turn["baseline"]["cache_creation_tokens"] == 223482
    assert turn["candidate"]["cache_creation_tokens"] == 0

    billed_baseline = (
        turn["baseline"]["input_tokens"]
        + turn["baseline"]["cache_read_tokens"]
        + turn["baseline"]["cache_creation_tokens"]
    )
    assert billed_baseline == 251099


def test_report_says_why_an_unjudged_turn_was_skipped(
    admin_client: TestClient, consenting_user: User, db_session: Session
) -> None:
    """A judged count that does not reach the turn count reads as a broken judge."""
    run = _make_run(db_session, consenting_user.id, judge_model="incumbent-model")
    db_session.add_all(
        [
            LLMEvalTurnResult(
                run_id=run.id,
                message_seq=1,
                user_message="same call both times",
                agreement=str(AgreementClass.IDENTICAL),
            ),
            LLMEvalTurnResult(
                run_id=run.id,
                message_seq=2,
                user_message="disqualified already",
                agreement=str(AgreementClass.DIFFERENT_TOOLS),
                safety_issues=json.dumps([{"finding": "unrequested_mutation"}]),
            ),
            LLMEvalTurnResult(
                run_id=run.id,
                message_seq=3,
                user_message="could not be measured",
                agreement=str(AgreementClass.NOT_COMPARED),
                candidate_error="RateLimitError: slow down",
            ),
        ]
    )
    db_session.commit()

    response = admin_client.get(f"{BASE}/runs/{run.public_id}")
    by_seq = {t["message_seq"]: t for t in response.json()["turns"]}
    assert by_seq[1]["judge_skip_reason"] == "identical"
    assert by_seq[2]["judge_skip_reason"] == "blocking_finding"
    assert by_seq[3]["judge_skip_reason"] == "call_failed"


def test_a_run_with_the_judge_off_says_so_rather_than_looking_broken(
    admin_client: TestClient, consenting_user: User, db_session: Session
) -> None:
    run = _make_run(db_session, consenting_user.id, judge_model="")
    db_session.add(
        LLMEvalTurnResult(
            run_id=run.id,
            message_seq=1,
            user_message="a divergence nobody adjudicated",
            agreement=str(AgreementClass.DIFFERENT_TOOLS),
        )
    )
    db_session.commit()

    response = admin_client.get(f"{BASE}/runs/{run.public_id}")
    assert response.json()["turns"][0]["judge_skip_reason"] == "judge_disabled"
