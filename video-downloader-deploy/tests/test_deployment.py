from fastapi.testclient import TestClient

from app.main import app


def test_public_origin_is_explicit_and_same_origin(monkeypatch):
    monkeypatch.setenv('PUBLIC_ORIGIN', 'https://video.example.com')
    with TestClient(app, base_url='https://video.example.com') as client:
        assert client.get('/api/health').status_code == 200
        assert client.get('/api/health', headers={'Host': 'evil.example'}).status_code == 403
        assert client.post('/api/analyze', json={'url': 'invalid'}).status_code == 403
        assert client.post('/api/analyze', json={'url': 'invalid'}, headers={'Origin': 'https://evil.example'}).status_code == 403
        assert client.post('/api/analyze', json={'url': 'invalid'}, headers={'Origin': 'https://video.example.com'}).status_code == 400


def test_invalid_public_configuration_fails_closed(monkeypatch):
    monkeypatch.setenv('PUBLIC_ORIGIN', 'https://video.example.com/not-an-origin')
    with TestClient(app, base_url='https://video.example.com') as client:
        assert client.get('/api/health').status_code == 403
