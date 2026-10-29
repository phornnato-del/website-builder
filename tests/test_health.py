from fastapi.testclient import TestClient
from pymongo.errors import ConnectionFailure

import app.main as main


def test_liveness_does_not_depend_on_mongodb() -> None:
    with TestClient(main.app) as client:
        response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


def test_readiness_checks_mongodb(monkeypatch) -> None:
    class FakeAdmin:
        async def command(self, command: str) -> dict[str, int]:
            assert command == "ping"
            return {"ok": 1}

    class FakeClient:
        admin = FakeAdmin()

        def __getitem__(self, name: str) -> object:
            assert name == main.settings.mongodb_database
            return object()

        async def close(self) -> None:
            return None

    monkeypatch.setattr(main, "AsyncMongoClient", lambda *args, **kwargs: FakeClient())

    with TestClient(main.app) as client:
        response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "dependencies": {"mongodb": "available"},
    }


def test_readiness_reports_mongodb_unavailable(monkeypatch) -> None:
    class FakeAdmin:
        async def command(self, command: str) -> dict[str, int]:
            raise ConnectionFailure("MongoDB is unavailable")

    class FakeClient:
        admin = FakeAdmin()

        def __getitem__(self, name: str) -> object:
            return object()

        async def close(self) -> None:
            return None

    monkeypatch.setattr(main, "AsyncMongoClient", lambda *args, **kwargs: FakeClient())

    with TestClient(main.app) as client:
        response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "dependencies": {"mongodb": "unavailable"},
    }
