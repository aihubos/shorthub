import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from app.config import config
from app.services import llm, oauth_login


ROOT_DIR = Path(__file__).parent.parent.parent
WEBUI_MAIN = ROOT_DIR / "webui" / "Main.py"


class TestOauthLogin(unittest.TestCase):
    def setUp(self):
        self.original_app_config = dict(config.app)

    def tearDown(self):
        config.app.clear()
        config.app.update(self.original_app_config)

    def test_chatgpt_status_reads_local_login_without_exposing_full_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            auth_path = Path(tmp) / "auth.json"
            auth_path.write_text(
                json.dumps(
                    {
                        "auth_mode": "chatgpt",
                        "tokens": {
                            "access_token": "tok-secret",
                            "account_id": "acct_1234567890",
                        },
                    }
                ),
                encoding="utf-8",
            )
            status = oauth_login.chatgpt_status(auth_path=auth_path)

        self.assertTrue(status["connected"])
        self.assertEqual(status["account_id"], "acct_1234567890")
        self.assertEqual(oauth_login.mask_account(status["account_id"]), "acct...7890")

    def test_openai_generation_uses_chatgpt_login_when_api_key_missing(self):
        config.app["llm_provider"] = "openai"
        config.app["openai_api_key"] = ""
        config.app["openai_base_url"] = ""
        config.app["openai_model_name"] = "gpt-4o-mini"

        class FakeCompletions:
            def create(self, **kwargs):
                self.kwargs = kwargs
                message = type("Message", (), {"content": "OK"})()
                choice = type("Choice", (), {"message": message})()
                return type("ChatCompletion", (), {"choices": [choice]})()

        class FakeClient:
            def __init__(self, **kwargs):
                FakeClient.kwargs = kwargs
                self.chat = type("Chat", (), {"completions": FakeCompletions()})()

        with (
            patch.object(
                oauth_login,
                "resolve_openai_credentials",
                return_value=("chatgpt-access-token", ""),
            ),
            patch.object(llm, "OpenAI", FakeClient),
            patch.object(llm, "ChatCompletion", object),
        ):
            # isinstance check uses ChatCompletion; make Fake response pass by patching extract
            with patch.object(
                llm,
                "_extract_chat_completion_text",
                return_value="OK",
            ):
                result = llm._generate_response("test")

        self.assertEqual(result, "OK")
        self.assertEqual(FakeClient.kwargs["api_key"], "chatgpt-access-token")

    def test_settings_dialog_shows_chatgpt_login_status(self):
        app_config = dict(config.app, llm_provider="openai", openai_api_key="")
        ui_config = dict(config.ui, language="ko")
        with (
            patch.object(config, "app", app_config),
            patch.object(config, "ui", ui_config),
            patch.object(config, "try_save_config", return_value=True),
            patch.object(
                oauth_login,
                "chatgpt_status",
                return_value={
                    "connected": True,
                    "account_id": "acct_abcdef1234",
                    "access_token": "hidden",
                    "mode": "chatgpt",
                    "auth_path": "/tmp/auth.json",
                    "last_refresh": "",
                    "provider": "openai",
                },
            ),
        ):
            app = AppTest.from_file(str(WEBUI_MAIN), default_timeout=60)
            app.session_state["ui_language"] = "ko"
            app.session_state["settings_dialog_open"] = True
            app.run()

        self.assertEqual([str(item.value) for item in app.exception], [])
        joined = " ".join(str(item.value) for item in app.success)
        self.assertIn("ChatGPT", joined)
