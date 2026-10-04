from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from pydantic import ValidationError

from platform_api import (
    AutomationRuleInput,
    CampaignInput,
    CampaignScheduleInput,
    router,
)


class FakeQuery:
    def __init__(self, db, table, operation="select", payload=None):
        self.db, self.table, self.operation, self.payload = db, table, operation, payload
        self.equals, self.members = {}, {}

    def select(self, *_args, **_kwargs): return self
    def eq(self, key, value): self.equals[key] = value; return self
    def in_(self, key, value): self.members[key] = set(value); return self
    def order(self, *_args, **_kwargs): return self
    def limit(self, *_args, **_kwargs): return self
    def gte(self, *_args, **_kwargs): return self
    def lte(self, *_args, **_kwargs): return self
    def neq(self, *_args, **_kwargs): return self
    def is_(self, *_args, **_kwargs): return self
    def insert(self, payload): self.operation, self.payload = "insert", payload; return self
    def update(self, payload): self.operation, self.payload = "update", payload; return self
    def upsert(self, payload, **_kwargs): self.operation, self.payload = "upsert", payload; return self

    def execute(self):
        if self.operation != "select":
            return SimpleNamespace(data=[self.payload])
        rows = self.db.rows.get(self.table, [])
        rows = [row for row in rows if all(row.get(k) == v for k, v in self.equals.items())]
        rows = [row for row in rows if all(row.get(k) in v for k, v in self.members.items())]
        return SimpleNamespace(data=rows)


class FakeRpc:
    def __init__(self, db, name, params): self.db, self.name, self.params = db, name, params
    def execute(self):
        self.db.rpc_calls.append((self.name, self.params))
        return SimpleNamespace(data=self.db.rpc_results[self.name])


class FakeDB:
    def __init__(self):
        self.rows = {"location_profiles": []}
        self.rpc_calls = []
        self.rpc_results = {
            "create_content_campaign": {"status": "draft", "campaign_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "location_count": 2},
            "schedule_content_campaign": {"status": "scheduled", "credits_charged": 10},
        }
    def table(self, name): return FakeQuery(self, name)
    def rpc(self, name, params): return FakeRpc(self, name, params)


@pytest.fixture
def client():
    app = FastAPI()
    app.state.db = FakeDB()

    @app.middleware("http")
    async def signed_in(request: Request, call_next):
        request.state.user = SimpleNamespace(id="owner")
        return await call_next(request)

    app.include_router(router)
    return TestClient(app), app.state.db


def test_campaign_requires_post_type_details():
    with pytest.raises(ValidationError):
        CampaignInput(title="Event", topic_type="EVENT", location_ids=["locations/1"])
    with pytest.raises(ValidationError):
        CampaignInput(title="Offer", topic_type="OFFER", location_ids=["locations/1"], event_details={"start": "tomorrow"})


def test_schedule_requires_timezone_and_future():
    with pytest.raises(ValidationError):
        CampaignScheduleInput(publish_at=datetime.now() + timedelta(days=1))
    with pytest.raises(ValidationError):
        CampaignScheduleInput(publish_at=datetime.now(timezone.utc) - timedelta(seconds=1))


def test_automation_rating_range_is_ordered():
    with pytest.raises(ValidationError):
        AutomationRuleInput(name="Invalid", min_rating=5, max_rating=2)


def test_campaign_creation_is_owner_scoped_and_uses_rpc(client):
    http, db = client
    db.rows["location_profiles"] = [
        {"user_id": "owner", "location_id": "locations/1"},
        {"user_id": "owner", "location_id": "locations/2"},
    ]
    response = http.post("/api/platform/campaigns", json={
        "title": "Admissions",
        "topic_type": "STANDARD",
        "summary": "Admissions are open",
        "location_ids": ["1", "locations/2"],
    })
    assert response.status_code == 200
    assert response.json()["location_count"] == 2
    name, params = db.rpc_calls[-1]
    assert name == "create_content_campaign"
    assert params["p_user"] == "owner"
    assert params["p_location_ids"] == ["locations/1", "locations/2"]


def test_campaign_creation_rejects_unowned_location(client):
    http, db = client
    db.rows["location_profiles"] = [{"user_id": "owner", "location_id": "locations/1"}]
    response = http.post("/api/platform/campaigns", json={
        "title": "Admissions",
        "topic_type": "STANDARD",
        "location_ids": ["locations/1", "locations/2"],
    })
    assert response.status_code == 403
    assert db.rpc_calls == []


def test_campaign_schedule_passes_authenticated_owner(client):
    http, db = client
    publish_at = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    response = http.post(
        "/api/platform/campaigns/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa/schedule",
        json={"publish_at": publish_at},
    )
    assert response.status_code == 200
    assert response.json()["credits_charged"] == 10
    assert db.rpc_calls[-1][1]["p_user"] == "owner"


def test_review_reply_approval_is_owner_scoped(client):
    http, db = client
    job_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    db.rows["review_reply_jobs"] = [{
        "id": job_id, "user_id": "owner", "status": "pending_approval",
        "draft_text": "Thank you for your feedback.", "location_id": "locations/1",
    }]
    response = http.post(f"/api/platform/review-jobs/{job_id}/approve", json={"publish_at": None})
    assert response.status_code == 200
    assert response.json()["status"] == "scheduled"
    assert response.json()["credits_due"] == 2.5


def test_review_reply_approval_hides_another_accounts_job(client):
    http, db = client
    job_id = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    db.rows["review_reply_jobs"] = [{
        "id": job_id, "user_id": "another", "status": "pending_approval",
        "draft_text": "Thanks.", "location_id": "locations/9",
    }]
    response = http.post(f"/api/platform/review-jobs/{job_id}/approve", json={"publish_at": None})
    assert response.status_code == 404
