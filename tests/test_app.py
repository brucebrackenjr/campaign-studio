import pytest
from fastapi.testclient import TestClient

from campaign_studio.app import create_app
from campaign_studio.config import Settings


@pytest.fixture
def client(tmp_path):
    settings = Settings(data_dir=tmp_path / "runtime")
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8000") as test_client:
        yield test_client


def test_empty_state_and_resources_are_served_without_exposing_runtime(client):
    response = client.get("/")
    assert response.status_code == 200
    assert 'data-page="library"' in response.text
    assert "/static/motif/actions.js" in response.text
    assert "Load synthetic sample" not in response.text
    assert client.get("/static/motif/styles.css").status_code == 200
    assert client.get("/health").json() == {
        "status": "ok",
        "stage": "workflow",
    }
    assert client.get("/private/state.sqlite3").status_code == 404
    assert client.get("/static/../private/state.sqlite3").status_code == 404
    assert client.post("/health").status_code == 403
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"host": "untrusted.example"}, 400),
        ({"origin": "https://untrusted.example"}, 403),
        ({"origin": "http://127.0.0.1:9000"}, 403),
        ({"origin": "null"}, 403),
        ({"sec-fetch-site": "cross-site"}, 403),
        ({"origin": "http://127.0.0.1:8000"}, 200),
    ],
)
def test_local_request_boundary(client, headers, expected):
    assert client.get("/health", headers=headers).status_code == expected
