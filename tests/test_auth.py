from itertools import count
from typing import Any

from bson import ObjectId
from fastapi.testclient import TestClient
from pymongo.errors import DuplicateKeyError

import app.main as main

FRONTEND_ORIGIN = "http://localhost:3000"
_test_client_ids = count()


class FakeCollection:
    def __init__(self) -> None:
        self.documents: list[dict[str, Any]] = []
        self.unique_fields: set[str] = set()

    async def create_index(
        self,
        field: str,
        unique: bool = False,
        **options: Any,
    ) -> str:
        if unique:
            self.unique_fields.add(field)
        return field

    async def insert_one(self, document: dict[str, Any]) -> Any:
        for field in self.unique_fields:
            if any(item.get(field) == document.get(field) for item in self.documents):
                raise DuplicateKeyError(f"Duplicate value for {field}")
        inserted = document.copy()
        inserted["_id"] = ObjectId()
        self.documents.append(inserted)
        return type("InsertResult", (), {"inserted_id": inserted["_id"]})()

    async def find_one(self, query: dict[str, Any]) -> dict[str, Any] | None:
        for document in self.documents:
            if all(self._matches(document, key, value) for key, value in query.items()):
                return document.copy()
        return None

    def find(self, query: dict[str, Any]) -> "FakeCursor":
        documents = [
            document.copy()
            for document in self.documents
            if all(self._matches(document, key, value) for key, value in query.items())
        ]
        return FakeCursor(documents)

    async def delete_one(self, query: dict[str, Any]) -> Any:
        for index, document in enumerate(self.documents):
            if all(self._matches(document, key, value) for key, value in query.items()):
                self.documents.pop(index)
                return type("DeleteResult", (), {"deleted_count": 1})()
        return type("DeleteResult", (), {"deleted_count": 0})()

    async def update_one(
        self,
        query: dict[str, Any],
        update: dict[str, Any],
        upsert: bool = False,
    ) -> Any:
        for document in self.documents:
            if all(self._matches(document, key, value) for key, value in query.items()):
                document.update(update["$set"])
                return type("UpdateResult", (), {"matched_count": 1})()
        if upsert:
            return await self.insert_one(update["$set"])
        return type("UpdateResult", (), {"matched_count": 0})()

    @staticmethod
    def _matches(document: dict[str, Any], key: str, expected: Any) -> bool:
        actual = document.get(key)
        if isinstance(expected, dict) and "$gt" in expected:
            return actual > expected["$gt"]
        return actual == expected


class FakeDatabase:
    def __init__(self) -> None:
        self.users = FakeCollection()
        self.sessions = FakeCollection()
        self.sites = FakeCollection()
        self.assets = FakeCollection()
        self.gridfs_files: dict[str, dict[str, Any]] = {}


class FakeCursor:
    def __init__(self, documents: list[dict[str, Any]]) -> None:
        self.documents = documents

    def sort(self, key: str, direction: int) -> "FakeCursor":
        self.documents.sort(
            key=lambda item: item[key],
            reverse=direction < 0,
        )
        return self

    def __aiter__(self):
        self.iterator = iter(self.documents)
        return self

    async def __anext__(self) -> dict[str, Any]:
        try:
            return next(self.iterator)
        except StopIteration:
            raise StopAsyncIteration from None


class FakeMongoClient:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.database = FakeDatabase()
        self.admin = type(
            "FakeAdmin",
            (),
            {"command": self._ping},
        )()

    async def _ping(self, command: str) -> dict[str, int]:
        return {"ok": 1}

    def __getitem__(self, name: str) -> FakeDatabase:
        return self.database

    async def close(self) -> None:
        return None


def _client(monkeypatch) -> TestClient:
    monkeypatch.setattr(main, "AsyncMongoClient", FakeMongoClient)
    return TestClient(
        main.app,
        client=(f"testclient-{next(_test_client_ids)}", 50000),
    )


def test_register_login_me_and_logout(monkeypatch) -> None:
    with _client(monkeypatch) as client:
        registered = client.post(
            "/api/auth/register",
            headers={"Origin": FRONTEND_ORIGIN},
            json={"email": "Builder@example.com", "password": "a-very-long-password"},
        )

        assert registered.status_code == 201
        assert registered.json()["user"]["email"] == "builder@example.com"
        assert main.settings.session_cookie_name in registered.cookies
        csrf_token = registered.json()["csrf_token"]

        user_document = client.app.state.database.users.documents[0]
        assert "password" not in user_document
        assert user_document["password_hash"] != "a-very-long-password"

        current_user = client.get("/api/auth/me")
        assert current_user.status_code == 200
        assert current_user.json()["csrf_token"] == csrf_token

        logged_out = client.post(
            "/api/auth/logout",
            headers={
                "Origin": FRONTEND_ORIGIN,
                "X-CSRF-Token": csrf_token,
            },
        )
        assert logged_out.status_code == 204
        assert client.get("/api/auth/me").status_code == 401

        incorrect_password = client.post(
            "/api/auth/login",
            headers={"Origin": FRONTEND_ORIGIN},
            json={"email": "builder@example.com", "password": "incorrect-password"},
        )
        assert incorrect_password.status_code == 401
        assert incorrect_password.json()["detail"] == "Email or password is incorrect"

        logged_in = client.post(
            "/api/auth/login",
            headers={"Origin": FRONTEND_ORIGIN},
            json={"email": "builder@example.com", "password": "a-very-long-password"},
        )
        assert logged_in.status_code == 200
        assert logged_in.json()["user"]["email"] == "builder@example.com"


def test_authentication_requires_session_and_allowed_origin(monkeypatch) -> None:
    with _client(monkeypatch) as client:
        assert client.get("/api/auth/me").status_code == 401

        rejected = client.post(
            "/api/auth/register",
            headers={"Origin": "https://example.invalid"},
            json={"email": "builder@example.com", "password": "a-very-long-password"},
        )

    assert rejected.status_code == 403


def test_cors_allows_configured_frontend_with_credentials(monkeypatch) -> None:
    with _client(monkeypatch) as client:
        response = client.options(
            "/api/auth/login",
            headers={
                "Origin": FRONTEND_ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == FRONTEND_ORIGIN
    assert response.headers["access-control-allow-credentials"] == "true"


def test_registration_rejects_duplicate_email(monkeypatch) -> None:
    with _client(monkeypatch) as client:
        first = client.post(
            "/api/auth/register",
            headers={"Origin": FRONTEND_ORIGIN},
            json={"email": "builder@example.com", "password": "a-very-long-password"},
        )
        duplicate = client.post(
            "/api/auth/register",
            headers={"Origin": FRONTEND_ORIGIN},
            json={"email": "BUILDER@example.com", "password": "another-long-password"},
        )

    assert first.status_code == 201
    assert duplicate.status_code == 409


def test_logout_requires_csrf_token(monkeypatch) -> None:
    with _client(monkeypatch) as client:
        client.post(
            "/api/auth/register",
            headers={"Origin": FRONTEND_ORIGIN},
            json={"email": "builder@example.com", "password": "a-very-long-password"},
        )
        response = client.post(
            "/api/auth/logout",
            headers={"Origin": FRONTEND_ORIGIN, "X-CSRF-Token": "invalid"},
        )

    assert response.status_code == 403


def test_site_save_and_public_read(monkeypatch) -> None:
    with _client(monkeypatch) as client:
        registered = client.post(
            "/api/auth/register",
            headers={"Origin": FRONTEND_ORIGIN},
            json={"email": "builder@example.com", "password": "a-very-long-password"},
        )
        csrf_token = registered.json()["csrf_token"]
        design = {
            "blocks": [
                {
                    "id": "welcome",
                    "type": "heading",
                    "text": "Hello world",
                    "x": 50,
                    "y": 30,
                    "rotation": -12,
                    "width": 440,
                    "height": 84,
                    "color": "#172554",
                    "backgroundColor": "transparent",
                    "url": "",
                    "images": [],
                },
                {
                    "id": "accent",
                    "type": "circle",
                    "text": "",
                    "x": 80,
                    "y": 30,
                    "width": 120,
                    "height": 120,
                    "color": "#ffffff",
                    "backgroundColor": "#4f46e5",
                    "url": "",
                    "images": [],
                },
                {
                    "id": "gallery",
                    "type": "carousel",
                    "text": "Product gallery",
                    "x": 50,
                    "y": 70,
                    "width": 640,
                    "height": 360,
                    "color": "#1e293b",
                    "backgroundColor": "#e0e7ff",
                    "url": "",
                    "images": [
                        "/api/images/507f1f77bcf86cd799439011",
                        "https://example.com/second.png",
                    ],
                },
                {
                    "id": "site-nav",
                    "type": "navbar",
                    "text": "Brand",
                    "x": 50,
                    "y": 8,
                    "width": 1000,
                    "height": 72,
                    "color": "#172554",
                    "backgroundColor": "#ffffff",
                    "url": "",
                    "navItems": [
                        {"label": "Start", "pageSlug": "home"},
                        {"label": "Our story", "pageSlug": "about"},
                    ],
                },
            ],
            "pages": [
                {
                    "id": "home",
                    "slug": "home",
                    "title": "Home",
                    "blocks": [],
                },
                {
                    "id": "about",
                    "slug": "about",
                    "title": "About us",
                    "blocks": [
                        {
                            "id": "about-heading",
                            "type": "heading",
                            "text": "About us",
                            "x": 50,
                            "y": 30,
                            "width": 440,
                            "height": 84,
                            "color": "#172554",
                            "backgroundColor": "transparent",
                            "url": "",
                        }
                    ],
                },
            ],
            "page_width": 1200,
            "page_height": 900,
            "canvas_color": "#ffffff",
            "animation": "rise",
        }
        design["pages"][0]["blocks"] = design["blocks"]

        saved = client.put(
            "/api/sites/my-site",
            headers={"Origin": FRONTEND_ORIGIN, "X-CSRF-Token": csrf_token},
            json=design,
        )
        updated_design = {**design, "canvas_color": "#f8fafc"}
        updated = client.put(
            "/api/sites/my-site",
            headers={"Origin": FRONTEND_ORIGIN, "X-CSRF-Token": csrf_token},
            json=updated_design,
        )
        published = client.get("/api/sites/my-site")

    assert saved.status_code == 200
    assert saved.json()["slug"] == "my-site"
    assert saved.json()["animation"] == "rise"
    assert saved.json()["blocks"][0]["rotation"] == -12
    assert saved.json()["blocks"][1]["rotation"] == 0
    assert saved.json()["pages"][1]["slug"] == "about"
    assert saved.json()["pages"][0]["blocks"][3]["navItems"][1] == {
        "label": "Our story",
        "pageSlug": "about",
    }
    assert updated.status_code == 200
    assert published.status_code == 200
    assert published.json()["canvas_color"] == updated_design["canvas_color"]
    assert published.json()["pages"][1]["title"] == "About us"
    assert published.json()["blocks"][2]["images"] == [
        "/api/images/507f1f77bcf86cd799439011",
        "https://example.com/second.png",
    ]
    assert published.json()["animation"] == "rise"


def test_site_rejects_carousels_with_more_than_three_or_unsafe_images(monkeypatch) -> None:
    with _client(monkeypatch) as client:
        registered = client.post(
            "/api/auth/register",
            headers={"Origin": FRONTEND_ORIGIN},
            json={"email": "builder@example.com", "password": "a-very-long-password"},
        )
        headers = {
            "Origin": FRONTEND_ORIGIN,
            "X-CSRF-Token": registered.json()["csrf_token"],
        }
        block = {
            "id": "gallery",
            "type": "carousel",
            "text": "Gallery",
            "x": 50,
            "y": 50,
            "width": 640,
            "height": 360,
            "color": "#1e293b",
            "backgroundColor": "#ffffff",
            "url": "",
            "images": [
                "https://example.com/one.png",
                "https://example.com/two.png",
                "https://example.com/three.png",
                "https://example.com/four.png",
            ],
        }
        oversized = client.put(
            "/api/sites/gallery",
            headers=headers,
            json={
                "blocks": [block],
                "page_width": 1200,
                "page_height": 900,
                "canvas_color": "#ffffff",
            },
        )
        unsafe = client.put(
            "/api/sites/gallery",
            headers=headers,
            json={
                "blocks": [{**block, "images": ["javascript:alert(1)"]}],
                "page_width": 1200,
                "page_height": 900,
                "canvas_color": "#ffffff",
            },
        )

    assert oversized.status_code == 422
    assert unsafe.status_code == 422


def test_site_accepts_https_map_embeds_and_rejects_unsafe_urls(monkeypatch) -> None:
    with _client(monkeypatch) as client:
        registered = client.post(
            "/api/auth/register",
            headers={"Origin": FRONTEND_ORIGIN},
            json={"email": "builder@example.com", "password": "a-very-long-password"},
        )
        headers = {
            "Origin": FRONTEND_ORIGIN,
            "X-CSRF-Token": registered.json()["csrf_token"],
        }
        block = {
            "id": "map",
            "type": "map",
            "text": "Find us",
            "x": 50,
            "y": 50,
            "width": 640,
            "height": 360,
            "color": "#1e293b",
            "backgroundColor": "#ffffff",
            "url": (
                '<iframe src="https://www.google.com/maps/embed?pb=example'
                '&amp;hl=en" width="600" height="450"></iframe>'
            ),
        }
        saved = client.put(
            "/api/sites/maps",
            headers=headers,
            json={
                "blocks": [block],
                "page_width": 1200,
                "page_height": 900,
                "canvas_color": "#ffffff",
            },
        )
        unsafe_responses = [
            client.put(
                "/api/sites/maps",
                headers=headers,
                json={
                    "blocks": [{**block, "url": url}],
                    "page_width": 1200,
                    "page_height": 900,
                    "canvas_color": "#ffffff",
                },
            )
            for url in (
                "http://maps.example.com/embed",
                "javascript:alert(1)",
                "https://user:password@maps.example.com/embed",
            )
        ]

    assert saved.status_code == 200
    assert saved.json()["blocks"][0]["url"] == (
        "https://www.google.com/maps/embed?pb=example&hl=en"
    )
    assert all(response.status_code == 422 for response in unsafe_responses)


def test_site_rejects_duplicate_pages_and_missing_navigation_targets(monkeypatch) -> None:
    with _client(monkeypatch) as client:
        registered = client.post(
            "/api/auth/register",
            headers={"Origin": FRONTEND_ORIGIN},
            json={"email": "builder@example.com", "password": "a-very-long-password"},
        )
        headers = {
            "Origin": FRONTEND_ORIGIN,
            "X-CSRF-Token": registered.json()["csrf_token"],
        }
        home_page = {
            "id": "home",
            "slug": "home",
            "title": "Home",
            "blocks": [
                {
                    "id": "nav",
                    "type": "navbar",
                    "text": "Brand",
                    "x": 50,
                    "y": 8,
                    "width": 1000,
                    "height": 72,
                    "color": "#172554",
                    "backgroundColor": "#ffffff",
                    "url": "",
                    "navItems": [{"label": "Missing", "pageSlug": "missing"}],
                }
            ],
        }
        duplicate_slugs = client.put(
            "/api/sites/navigation",
            headers=headers,
            json={
                "blocks": [],
                "pages": [
                    home_page,
                    {"id": "other", "slug": "home", "title": "Another", "blocks": []},
                ],
                "page_width": 1200,
                "page_height": 900,
                "canvas_color": "#ffffff",
            },
        )
        missing_target = client.put(
            "/api/sites/navigation",
            headers=headers,
            json={
                "blocks": [],
                "pages": [home_page],
                "page_width": 1200,
                "page_height": 900,
                "canvas_color": "#ffffff",
            },
        )

    assert duplicate_slugs.status_code == 422
    assert missing_target.status_code == 422


def test_site_save_rejects_unauthenticated_or_invalid_slug(monkeypatch) -> None:
    with _client(monkeypatch) as client:
        unauthenticated = client.put("/api/sites/my-site", json={})
        registered = client.post(
            "/api/auth/register",
            headers={"Origin": FRONTEND_ORIGIN},
            json={"email": "builder@example.com", "password": "a-very-long-password"},
        )
        missing_csrf = client.put(
            "/api/sites/my-site",
            headers={"Origin": FRONTEND_ORIGIN},
            json={
                "blocks": [],
                "page_width": 1200,
                "page_height": 900,
                "canvas_color": "#ffffff",
            },
        )
        invalid_slug = client.put(
            "/api/sites/Not_Valid",
            headers={
                "Origin": FRONTEND_ORIGIN,
                "X-CSRF-Token": registered.json()["csrf_token"],
            },
            json={},
        )

    assert unauthenticated.status_code == 401
    assert missing_csrf.status_code == 403
    assert invalid_slug.status_code == 422


def test_site_slug_is_unique_across_users(monkeypatch) -> None:
    with _client(monkeypatch) as client:
        first = client.post(
            "/api/auth/register",
            headers={"Origin": FRONTEND_ORIGIN},
            json={"email": "first@example.com", "password": "a-very-long-password"},
        )
        site_design = {
            "blocks": [],
            "page_width": 1200,
            "page_height": 900,
            "canvas_color": "#ffffff",
        }
        saved = client.put(
            "/api/sites/shared",
            headers={
                "Origin": FRONTEND_ORIGIN,
                "X-CSRF-Token": first.json()["csrf_token"],
            },
            json=site_design,
        )
        client.post(
            "/api/auth/logout",
            headers={
                "Origin": FRONTEND_ORIGIN,
                "X-CSRF-Token": first.json()["csrf_token"],
            },
        )
        second = client.post(
            "/api/auth/register",
            headers={"Origin": FRONTEND_ORIGIN},
            json={"email": "second@example.com", "password": "a-very-long-password"},
        )
        conflict = client.put(
            "/api/sites/shared",
            headers={
                "Origin": FRONTEND_ORIGIN,
                "X-CSRF-Token": second.json()["csrf_token"],
            },
            json=site_design,
        )

    assert saved.status_code == 200
    assert conflict.status_code == 409


def test_image_metadata_is_saved_under_the_logged_in_user(monkeypatch) -> None:
    with _client(monkeypatch) as client:
        registered = client.post(
            "/api/auth/register",
            headers={"Origin": FRONTEND_ORIGIN},
            json={"email": "horm.sophorn@example.com", "password": "a-very-long-password"},
        )
        csrf_token = registered.json()["csrf_token"]
        user_id = registered.json()["user"]["id"]
        user_folder = f"horm-sophorn-{user_id[-8:]}"
        upload_metadata = {
            "pathname": f"users/{user_folder}/1234567890abcdef-profile.png",
            "filename": "profile.png",
            "content_type": "image/png",
            "size": 28,
        }
        saved = client.post(
            "/api/assets",
            headers={
                "Origin": FRONTEND_ORIGIN,
                "X-CSRF-Token": csrf_token,
            },
            json=upload_metadata,
        )

        listed = client.get("/api/assets")
        metadata = client.get(f"/api/assets/{saved.json()['id']}/metadata")
        client.post(
            "/api/auth/logout",
            headers={
                "Origin": FRONTEND_ORIGIN,
                "X-CSRF-Token": csrf_token,
            },
        )
        another_user = client.post(
            "/api/auth/register",
            headers={"Origin": FRONTEND_ORIGIN},
            json={"email": "another.user@example.com", "password": "a-very-long-password"},
        )
        other_user_id = another_user.json()["user"]["id"]
        another_upload = client.post(
            "/api/assets",
            headers={
                "Origin": FRONTEND_ORIGIN,
                "X-CSRF-Token": another_user.json()["csrf_token"],
            },
            json={
                **upload_metadata,
                "pathname": (
                    f"users/another-user-{other_user_id[-8:]}/"
                    "abcdef1234567890-profile.png"
                ),
            },
        )
        another_users_images = client.get("/api/assets")

    assert saved.status_code == 201
    assert saved.json()["filename"] == "profile.png"
    assert saved.json()["folder"].startswith("horm-sophorn-")
    assert listed.status_code == 200
    assert len(listed.json()) == 1
    assert metadata.status_code == 200
    assert metadata.json() == {
        "pathname": upload_metadata["pathname"],
        "content_type": "image/png",
    }
    assert another_upload.status_code == 201
    assert another_upload.json()["folder"].startswith("another-user-")
    assert another_upload.json()["id"] != saved.json()["id"]
    assert [asset["id"] for asset in another_users_images.json()] == [
        another_upload.json()["id"]
    ]


def test_image_metadata_rejects_foreign_paths_and_unsupported_types(monkeypatch) -> None:
    with _client(monkeypatch) as client:
        registered = client.post(
            "/api/auth/register",
            headers={"Origin": FRONTEND_ORIGIN},
            json={"email": "builder@example.com", "password": "a-very-long-password"},
        )
        headers = {
            "Origin": FRONTEND_ORIGIN,
            "X-CSRF-Token": registered.json()["csrf_token"],
        }
        foreign_path = client.post(
            "/api/assets",
            headers=headers,
            json={
                "pathname": (
                    f"users/wrong-name-{registered.json()['user']['id'][-8:]}/"
                    "1234567890abcdef-profile.png"
                ),
                "filename": "profile.png",
                "content_type": "image/png",
                "size": 28,
            },
        )
        unsupported_type = client.post(
            "/api/assets",
            headers=headers,
            json={
                "pathname": f"users/{registered.json()['user']['id']}/1234567890abcdef-profile.svg",
                "filename": "profile.svg",
                "content_type": "image/svg+xml",
                "size": 28,
            },
        )

    assert foreign_path.status_code == 422
    assert unsupported_type.status_code == 415
