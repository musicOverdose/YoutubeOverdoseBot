import pytest
from httpx import ASGITransport, AsyncClient
from src.web.app import app


@pytest.mark.asyncio
async def test_health_and_metrics_endpoints():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Health
        resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "healthy"
        assert data["version"] == "1.0.0"

        # 2. Metrics
        m_resp = await client.get("/metrics")
        assert m_resp.status_code == 200
        text = m_resp.text
        assert "ytdl_jobs_completed_total" in text
        assert "ytdl_queue_active_jobs" in text

        # 3. Unauthorized access to protected route
        prot_resp = await client.get("/api/dashboard/stats")
        assert prot_resp.status_code == 401

        cache_clear_unauth = await client.post("/api/cache/clear")
        assert cache_clear_unauth.status_code == 401


@pytest.mark.asyncio
async def test_cache_clear_authorized(monkeypatch):
    from unittest.mock import AsyncMock, patch
    from src.web.auth import get_current_admin
    from src.core.database import get_db

    mock_session = AsyncMock()
    mock_result = AsyncMock()
    mock_result.rowcount = 5
    mock_session.execute.return_value = mock_result
    mock_session.commit.return_value = None

    async def override_get_db():
        yield mock_session

    async def override_get_admin():
        return {"sub": "admin", "role": "ADMIN"}

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_admin] = override_get_admin

    try:
        with patch("src.web.routes.cache_routes.get_redis_client") as mock_get_r, \
             patch("src.services.audit_service.AuditService.log_action", new_callable=AsyncMock) as mock_audit:
            mock_r = AsyncMock()
            async def _scan(pattern):
                if False:
                    yield None
            mock_r.scan_iter = _scan
            mock_get_r.return_value = mock_r

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post("/api/cache/clear")
                assert resp.status_code == 200
                data = resp.json()
                assert data["status"] == "success"
                assert data["deleted_db_entries"] == 5
                mock_session.commit.assert_awaited()
    finally:
        app.dependency_overrides.clear()
