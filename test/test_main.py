import json
import runpy
from pathlib import Path
from unittest.mock import patch

from app import asgi
from app.config import config


ROOT_DIR = Path(__file__).resolve().parent.parent


def test_main_starts_uvicorn_with_runtime_config():
    """
    服务启动入口只负责把运行配置交给 Uvicorn。这里 mock 真正的服务器启动，
    既避免测试占用端口，也确认监听地址、端口和热重载配置不会在入口层丢失。
    """
    with (
        patch.object(config, "listen_host", "127.0.0.1"),
        patch.object(config, "listen_port", 8765),
        patch.object(config, "reload_debug", True),
        patch("uvicorn.run") as run_server,
    ):
        runpy.run_path(str(ROOT_DIR / "main.py"), run_name="__main__")

    run_server.assert_called_once_with(
        app="app.asgi:app",
        host="127.0.0.1",
        port=8765,
        reload=True,
        log_level="warning",
    )


def test_builders_lounge_healthz_reports_ready_with_boolean_checks_only():
    checks = {"renderTokenConfigured": True, "localMaterialsReady": True}

    with patch.object(
        asgi, "builders_lounge_renderer_readiness", return_value=checks
    ):
        response = asgi.builders_lounge_healthz()

    assert response.status_code == 200
    assert json.loads(response.body) == {
        "status": "ready",
        "contractVersion": "builders-lounge-renderer-v1",
        "checks": checks,
    }


def test_builders_lounge_healthz_reports_unavailable_when_a_check_fails():
    checks = {"renderTokenConfigured": True, "localMaterialsReady": False}

    with patch.object(
        asgi, "builders_lounge_renderer_readiness", return_value=checks
    ):
        response = asgi.builders_lounge_healthz()

    assert response.status_code == 503
    assert json.loads(response.body) == {
        "status": "unavailable",
        "contractVersion": "builders-lounge-renderer-v1",
        "checks": checks,
    }
