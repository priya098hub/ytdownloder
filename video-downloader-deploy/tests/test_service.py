import asyncio
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main
from app.media import MAX_BYTES, MAX_DURATION, PublicError, normalize_url, select_options, validate_info

VIDEO_ID = "Abcde_12345"
URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATA_ROOT", tmp_path / "data")
    main.analyses.clear()
    main.jobs.clear()
    main.worker_tasks.clear()
    main.analysis_semaphore = asyncio.Semaphore(1)
    main.download_semaphore = asyncio.Semaphore(1)
    with TestClient(main.app, base_url="http://127.0.0.1:8000") as test_client:
        yield test_client


@pytest.mark.parametrize("url", [
    URL, f"https://youtu.be/{VIDEO_ID}",
    f"https://m.youtube.com/shorts/{VIDEO_ID}/", f"https://music.youtube.com/embed/{VIDEO_ID}",
    f"https://www.youtube.com/live/{VIDEO_ID}",
])
def test_normalize_url_accepts_supported_youtube_forms(url):
    assert normalize_url(url) == URL


@pytest.mark.parametrize("url", [
    "http://youtube.com/watch?v=Abcde_12345", "https://localhost/watch?v=Abcde_12345",
    "https://127.0.0.1/watch?v=Abcde_12345", "https://[::1]/watch?v=Abcde_12345",
    "https://169.254.169.254/latest/meta-data", "https://youtube.com.evil.test/watch?v=Abcde_12345",
    "https://youtube.com@evil.test/watch?v=Abcde_12345", "https://youtube.com:443/watch?v=Abcde_12345",
    "https://www.youtube.com/watch?v=Abcde_12345&v=Otherid_1234", "https://youtu.be/not-an-id",
    " https://youtu.be/Abcde_12345", "https://youtube.com/playlist?list=abc",
])
def test_normalize_url_rejects_invalid_or_nonpublic_targets(url):
    with pytest.raises(PublicError):
        normalize_url(url)


def test_validate_info_public_and_restricted_cases():
    validate_info({"id": VIDEO_ID, "availability": "public", "duration": 42}, VIDEO_ID)
    for updates in (
        {"availability": "private"}, {"availability": "unlisted"}, {"availability": "premium_only"},
        {"age_limit": 18}, {"is_private": True}, {"is_unlisted": True}, {"has_drm": True},
        {"is_live": True}, {"is_upcoming": True}, {"live_status": "post_live"},
        {"duration": 0}, {"duration": MAX_DURATION + 1}, {"duration": float("inf")},
    ):
        info = {"id": VIDEO_ID, "availability": "public", **updates}
        with pytest.raises(PublicError):
            validate_info(info, VIDEO_ID)
    with pytest.raises(PublicError):
        validate_info({"id": "different", "availability": "public"}, VIDEO_ID)
    with pytest.raises(PublicError):
        validate_info({"id": VIDEO_ID, "availability": "public", "_type": "playlist"}, VIDEO_ID)


def test_select_options_pairs_audio_and_filters_bad_or_oversize_formats():
    formats = [
        {"format_id": "v1", "ext": "mp4", "vcodec": "avc1.4d", "acodec": "none", "height": 720,
         "protocol": "https", "filesize": 100},
        {"format_id": "a1", "ext": "m4a", "vcodec": "none", "acodec": "mp4a.40.2",
         "protocol": "http", "filesize": 50, "abr": 128},
        {"format_id": "v2", "ext": "mp4", "vcodec": "avc1", "acodec": "none", "height": 1080,
         "protocol": "https", "filesize": MAX_BYTES},
        {"format_id": "a2", "ext": "m4a", "vcodec": "none", "acodec": "aac", "protocol": "http",
         "filesize": 1},
        {"format_id": "bad", "ext": "mp4", "vcodec": "avc1", "acodec": "mp4a", "height": 480,
         "protocol": "file", "filesize": 1},
        {"format_id": "drm", "ext": "mp4", "vcodec": "avc1", "acodec": "mp4a", "height": 360,
         "protocol": "https", "has_drm": True, "filesize": 1},
    ]
    options = select_options({"duration": 10, "formats": formats})
    assert [o["format"] for o in options] == ["v1+a1"]
    assert options[0]["ext"] == "mp4" and options[0]["size_bytes"] == 150


def test_health_home_static_and_security_headers(client):
    health = client.get("/api/health")
    assert health.status_code == 200 and health.json() == {"status": "ok", "local_only": True}
    assert health.headers["cache-control"] == "no-store"
    assert "YouTube link." in client.get("/").text
    assert client.get("/static/app.js").status_code == 200


def test_origin_host_and_request_validation(client):
    assert client.get("/api/health", headers={"host": "evil.test"}).status_code == 403
    assert client.get("/api/health", headers={"origin": "https://evil.test"}).status_code == 403
    assert client.post("/api/analyze", json={"url": URL}, headers={"origin": "null"}).status_code == 403
    assert client.post("/api/analyze", data="{}", headers={"content-type": "text/plain"}).status_code == 415
    assert client.post("/api/analyze", json={"url": URL, "extra": 1}).status_code == 422


def test_analyze_returns_opaque_options_and_keeps_formats_server_side(client, monkeypatch):
    async def fake_worker(payload):
        assert payload == {"mode": "analyze", "url": URL}
        return {"id": VIDEO_ID, "title": "Example", "duration": 12, "formats": [{"format_id": "secret"}],
                "choices": [{"format": "v+a", "ext": "mp4", "label": "720p", "size_bytes": 100},
                           {"format": "bad;expression", "ext": "mp4"},
                           {"format": "too-big", "ext": "mp4", "size_bytes": MAX_BYTES + 1}]}
    monkeypatch.setattr(main, "run_worker", fake_worker)
    response = client.post("/api/analyze", json={"url": URL})
    assert response.status_code == 200
    data = response.json()
    assert data["title"] == "Example" and len(data["options"]) == 1
    option_id = data["options"][0]["id"]
    assert len(option_id) == 32 and "format" not in data["options"][0]
    assert "secret" not in response.text
    assert main.analyses[data["analysis_id"]]["options"][option_id] == {"format": "v+a", "ext": "mp4"}


def _analyze_for_download(client, monkeypatch):
    async def analyze_worker(_payload):
        return {"id": VIDEO_ID, "formats": [], "choices": [{"format": "known", "ext": "mp4"}]}
    monkeypatch.setattr(main, "run_worker", analyze_worker)
    data = client.post("/api/analyze", json={"url": URL}).json()
    return data["analysis_id"], data["options"][0]["id"]


def test_download_requires_authorization_live_analysis_and_known_option(client, monkeypatch):
    aid, oid = _analyze_for_download(client, monkeypatch)
    base = {"analysis_id": aid, "option_id": oid, "authorized": True}
    assert client.post("/api/download", json={**base, "authorized": False}).status_code == 403
    assert client.post("/api/download", json={**base, "option_id": "0" * 32}).status_code == 400
    main.analyses[aid]["created"] = time.monotonic() - main.ANALYSIS_TTL - 1
    assert client.post("/api/download", json=base).status_code == 410
    assert client.post("/api/download", json={**base, "analysis_id": "bad"}).status_code == 422


def test_download_streams_fake_worker_file_with_safe_name(client, monkeypatch):
    aid, oid = _analyze_for_download(client, monkeypatch)
    async def fake_worker(payload):
        Path(payload["output_dir"], "video.mp4").write_bytes(b"fake video")
        return {"filename": "video.mp4"}
    monkeypatch.setattr(main, "run_worker", fake_worker)
    response = client.post("/api/download", json={"analysis_id": aid, "option_id": oid, "authorized": True})
    assert response.status_code == 202
    job_id = response.json()["job_id"]
    status = client.get(f"/api/jobs/{job_id}").json()
    assert status["status"] == "ready" and status["download_url"] == f"/api/files/{job_id}"
    file_response = client.get(status["download_url"])
    assert file_response.status_code == 200 and file_response.content == b"fake video"
    assert 'filename="youtube-video.mp4"' in file_response.headers["content-disposition"]


def test_invalid_expired_and_outside_job_files_are_not_served(client, tmp_path):
    assert client.get("/api/jobs/invalid").status_code == 404
    assert client.get("/api/files/invalid").status_code == 404
    job_id = "a" * 32
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"not to serve")
    directory = main.DATA_ROOT / job_id
    directory.mkdir(parents=True)
    main.jobs[job_id] = {"status": "ready", "created": time.monotonic(), "progress": 100,
                         "message": "ready", "filename": "youtube-video.mp4", "directory": directory, "path": outside}
    assert client.get(f"/api/files/{job_id}").status_code == 404
    assert client.get(f"/api/jobs/{'b' * 32}").status_code == 404


def test_worker_failure_sets_failed_and_releases_download_lock(client, monkeypatch):
    aid, oid = _analyze_for_download(client, monkeypatch)
    async def failing_worker(_payload):
        raise PublicError("test failure")
    monkeypatch.setattr(main, "run_worker", failing_worker)
    body = {"analysis_id": aid, "option_id": oid, "authorized": True}
    first = client.post("/api/download", json=body)
    assert first.status_code == 202
    failed = client.get(f"/api/jobs/{first.json()['job_id']}").json()
    assert failed["status"] == "failed" and failed["message"] == "test failure"
    assert not main.download_semaphore.locked()
    assert client.post("/api/download", json=body).status_code == 202
    second = client.get(f"/api/jobs/{list(main.jobs)[-1]}").json()
    assert second["status"] == "failed"
    assert not main.download_semaphore.locked()
