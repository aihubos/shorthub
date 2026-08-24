import asyncio
import os
import shutil
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.config import config
from app.controllers.manager.base_manager import TaskQueueFullError
from app.controllers.v1 import video as video_controller
from app.models import const
from app.models.exception import HttpException
from app.models.schema import TaskListResponse, TaskQueryResponse
from app.models.schema import BuildersLoungeVideoRequest
from app.services import state as sm
from app.utils import utils


class TestVideoControllerHelpers(unittest.TestCase):
    @staticmethod
    def _request(range_header=None):
        headers = {"x-task-id": "request-123"}
        if range_header is not None:
            headers["Range"] = range_header
        return SimpleNamespace(headers=headers)

    def test_sanitize_upload_filename_removes_client_path(self):
        """Windows 和 POSIX 客户端路径都只能保留最后一段安全文件名。"""
        for filename, expected in (
            (r"C:\videos\clip.MOV", "clip.MOV"),
            ("../../images/photo.png", "photo.png"),
        ):
            with self.subTest(filename=filename):
                self.assertEqual(
                    video_controller._sanitize_upload_filename(
                        filename, "request-123"
                    ),
                    expected,
                )

    def test_fastapi_startup_recovers_interrupted_cross_posts(self):
        """API 进程启动时必须执行一次发布遗留状态恢复。"""
        from app import asgi
        from app.services import task as task_service

        with patch.object(
            task_service, "recover_interrupted_cross_posts"
        ) as recover:
            async def run_lifespan():
                async with asgi.application_lifespan(asgi.app):
                    pass

            asyncio.run(run_lifespan())

        recover.assert_called_once_with()

    def test_sanitize_upload_filename_rejects_empty_name(self):
        """空文件名和目录占位符不能进入服务端存储路径。"""
        for filename in ("", ".", "..", "/"):
            with self.subTest(filename=filename):
                with self.assertRaises(HttpException) as raised:
                    video_controller._sanitize_upload_filename(
                        filename, "request-123"
                    )
                self.assertEqual(raised.exception.status_code, 400)

    def test_resolve_path_maps_missing_and_unsafe_files(self):
        """不存在文件返回 404，目录穿越等非法路径返回 403。"""
        for error, expected_status in (
            ("file does not exist", 404),
            ("path escapes base directory", 403),
        ):
            with self.subTest(error=error):
                with patch.object(
                    video_controller.file_security,
                    "resolve_path_within_directory",
                    side_effect=ValueError(error),
                ):
                    with self.assertRaises(HttpException) as raised:
                        video_controller._resolve_path_within_directory(
                            "/tasks", "../secret", "request-123"
                        )
                self.assertEqual(raised.exception.status_code, expected_status)

    def test_parse_byte_range_supports_common_player_requests(self):
        """播放器常见的闭区间、开放区间和后缀区间都应得到准确边界。"""
        cases = (
            (None, (0, 9)),
            ("bytes=2-5", (2, 5)),
            ("bytes=4-", (4, 9)),
            ("bytes=-4", (6, 9)),
            ("bytes=2-50", (2, 9)),
        )
        for header, expected in cases:
            with self.subTest(header=header):
                self.assertEqual(
                    video_controller._parse_byte_range(
                        header, 10, "request-123"
                    ),
                    expected,
                )

    def test_parse_byte_range_rejects_malformed_or_out_of_bounds_requests(self):
        """非法 Range 必须返回 416，不能因 split 或 int 转换异常变成 500。"""
        invalid_headers = (
            "items=0-1",
            "bytes=",
            "bytes=10-",
            "bytes=5-2",
            "bytes=0-1,3-4",
        )
        for header in invalid_headers:
            with self.subTest(header=header):
                with self.assertRaises(HttpException) as raised:
                    video_controller._parse_byte_range(
                        header, 10, "request-123"
                    )
                self.assertEqual(raised.exception.status_code, 416)


class TestVideoControllerTasks(unittest.TestCase):
    @staticmethod
    def _request():
        return SimpleNamespace(headers={"x-task-id": "request-123"})

    def test_create_task_queues_requested_pipeline_stage(self):
        """创建任务应持久化初始状态，并把原请求模型与停止阶段交给队列。"""
        body = MagicMock()
        body.model_dump.return_value = {"video_subject": "Coffee"}

        with (
            patch.object(video_controller.utils, "get_uuid", return_value="task-123"),
            patch.object(video_controller.sm.state, "update_task") as update_task,
            patch.object(video_controller.task_manager, "add_task") as add_task,
        ):
            response = video_controller.create_task(
                self._request(), body, stop_at="audio"
            )

        self.assertEqual(response["status"], 200)
        self.assertEqual(response["data"]["task_id"], "task-123")
        self.assertEqual(response["data"]["request_id"], "request-123")
        update_task.assert_called_once_with("task-123")
        add_task.assert_called_once_with(
            video_controller.tm.start,
            task_id="task-123",
            params=body,
            stop_at="audio",
        )

    def test_builders_lounge_create_is_idempotent_and_maps_korean_local_job(self):
        """같은 Lounge jobId는 한 번만 큐에 넣고 한국어 로컬 작업으로 고정한다."""
        job_id = "6c85c8cc-a77a-42b9-bc30-947815aa0558"
        body = BuildersLoungeVideoRequest.model_validate(
            {
                "jobId": job_id,
                "topic": "LLM 위키로 업무 정리하기",
                "detailedPrompt": "문제와 해결 장면을 대비한다.",
                "scenes": [
                    {"narration": "자료가 흩어져 있으면 찾는 데 시간이 듭니다."},
                    {"narration": "LLM 위키로 모으면 바로 다시 쓸 수 있습니다."},
                ],
            }
        )
        request = SimpleNamespace(
            headers={
                "x-task-id": "request-123",
                "authorization": "Bearer local-test-token",
            }
        )
        state = sm.MemoryState()

        with (
            patch.dict(
                os.environ,
                {video_controller._BUILDERS_LOUNGE_RENDER_TOKEN_ENV: "local-test-token"},
                clear=False,
            ),
            patch.object(video_controller.sm, "state", state),
            patch.object(
                video_controller,
                "_builders_lounge_material_names",
                return_value=["scene-a.mp4", "scene-b.mp4"],
            ),
            patch.object(video_controller.task_manager, "add_task") as add_task,
        ):
            first = video_controller.create_builders_lounge_video(request, body)
            second = video_controller.create_builders_lounge_video(request, body)

        self.assertEqual(first, second)
        self.assertEqual(first["data"]["taskId"], job_id)
        self.assertEqual(first["data"]["state"], "processing")
        self.assertIsNone(first["data"]["videoUrl"])
        add_task.assert_called_once()
        queued = add_task.call_args.kwargs["params"]
        self.assertEqual(queued.video_aspect, "9:16")
        self.assertEqual(queued.video_concat_mode, "sequential")
        self.assertEqual(queued.video_source, "local")
        self.assertEqual(queued.video_language, "ko-KR")
        self.assertEqual(
            queued.voice_name,
            video_controller._BUILDERS_LOUNGE_KOREAN_VOICE,
        )
        self.assertTrue(queued.subtitle_enabled)
        self.assertEqual(queued.bgm_type, "")
        self.assertEqual(
            [material.url for material in queued.video_materials],
            ["scene-a.mp4", "scene-b.mp4"],
        )
        self.assertIn("자료가 흩어져", queued.video_script)
        self.assertIn("LLM 위키로 모으면", queued.video_script)

    def test_builders_lounge_contract_rejects_missing_token_without_echoing_secret(self):
        """인증 실패 응답과 예외에는 실제 renderer token이 포함되면 안 된다."""
        body = BuildersLoungeVideoRequest.model_validate(
            {
                "jobId": "6c85c8cc-a77a-42b9-bc30-947815aa0558",
                "topic": "테스트",
                "scenes": [
                    {"narration": "첫 장면입니다."},
                    {"narration": "둘째 장면입니다."},
                ],
            }
        )
        request = SimpleNamespace(headers={"x-task-id": "request-123"})
        secret_token = "must-not-appear"

        with patch.dict(
            os.environ,
            {video_controller._BUILDERS_LOUNGE_RENDER_TOKEN_ENV: secret_token},
            clear=False,
        ):
            with self.assertRaises(HttpException) as raised:
                video_controller.create_builders_lounge_video(request, body)

        self.assertEqual(raised.exception.status_code, 401)
        self.assertNotIn(secret_token, raised.exception.message)

    def test_builders_lounge_status_returns_only_public_mp4_contract(self):
        """상태 조회는 내부 경로·파라미터 없이 MP4 계약 필드만 반환한다."""
        request = SimpleNamespace(
            headers={
                "x-task-id": "request-123",
                "authorization": "Bearer local-test-token",
            }
        )
        task = {
            "task_id": "6c85c8cc-a77a-42b9-bc30-947815aa0558",
            "state": const.TASK_STATE_COMPLETE,
            "progress": 100,
            "videos": ["/private/tasks/final-1.mp4"],
            "params": {"renderer_token": "do-not-return"},
        }

        with (
            patch.dict(
                os.environ,
                {video_controller._BUILDERS_LOUNGE_RENDER_TOKEN_ENV: "local-test-token"},
                clear=False,
            ),
            patch.object(video_controller.sm.state, "get_task", return_value=task),
        ):
            response = video_controller.get_builders_lounge_task(
                request, task_id=task["task_id"]
            )

        self.assertEqual(
            response["data"],
            {
                "taskId": task["task_id"],
                "state": "completed",
                "progress": 100,
                "videoUrl": (
                    "/api/v1/builders-lounge/tasks/"
                    f"{task['task_id']}/video"
                ),
                "mediaType": "video/mp4",
            },
        )

    def test_builders_lounge_video_download_is_token_protected_and_task_scoped(self):
        """완성 MP4는 공개 정적 경로가 아니라 인증된 작업 전용 경로로만 전달한다."""
        task_id = "6c85c8cc-a77a-42b9-bc30-947815aa0558"
        task_dir = utils.task_dir(task_id)
        video_path = os.path.join(task_dir, "final-1.mp4")
        Path(video_path).write_bytes(b"private-mp4")
        task = {
            "task_id": task_id,
            "state": const.TASK_STATE_COMPLETE,
            "progress": 100,
            "videos": [video_path],
        }
        authorized = SimpleNamespace(
            headers={
                "x-task-id": "request-123",
                "authorization": "Bearer local-test-token",
            }
        )
        unauthorized = SimpleNamespace(headers={"x-task-id": "request-123"})

        try:
            with (
                patch.dict(
                    os.environ,
                    {video_controller._BUILDERS_LOUNGE_RENDER_TOKEN_ENV: "local-test-token"},
                    clear=False,
                ),
                patch.object(video_controller.sm.state, "get_task", return_value=task),
            ):
                response = video_controller.get_builders_lounge_video(
                    authorized, task_id=task_id
                )
                self.assertEqual(response.media_type, "video/mp4")
                self.assertEqual(Path(response.path), Path(video_path))
                with self.assertRaises(HttpException) as raised:
                    video_controller.get_builders_lounge_video(
                        unauthorized, task_id=task_id
                    )
            self.assertEqual(raised.exception.status_code, 401)
        finally:
            shutil.rmtree(task_dir, ignore_errors=True)

    def test_create_task_removes_state_when_queue_is_full(self):
        """队列已满时必须回滚刚创建的状态，并向调用方返回 429。"""
        body = MagicMock()
        body.model_dump.return_value = {"video_subject": "Coffee"}

        with (
            patch.object(video_controller.utils, "get_uuid", return_value="task-123"),
            patch.object(video_controller.sm.state, "update_task"),
            patch.object(
                video_controller.task_manager,
                "add_task",
                side_effect=TaskQueueFullError("queue full"),
            ),
            patch.object(video_controller.sm.state, "delete_task") as delete_task,
        ):
            with self.assertRaises(HttpException) as raised:
                video_controller.create_task(
                    self._request(), body, stop_at="video"
                )

        self.assertEqual(raised.exception.status_code, 429)
        delete_task.assert_called_once_with("task-123")

    def test_create_task_removes_state_when_scheduler_fails(self):
        """调度器未能接管任务时，不能留下永远处于 processing 的状态。"""
        body = MagicMock()
        body.model_dump.return_value = {"video_subject": "Coffee"}
        scheduling_error = RuntimeError("can't start new thread")
        state = sm.MemoryState()

        with (
            patch.object(video_controller.utils, "get_uuid", return_value="task-123"),
            patch.object(video_controller.sm, "state", state),
            patch.object(
                video_controller.task_manager,
                "add_task",
                side_effect=scheduling_error,
            ),
        ):
            with self.assertRaises(RuntimeError) as raised:
                video_controller.create_task(
                    self._request(), body, stop_at="video"
                )

        self.assertIs(raised.exception, scheduling_error)
        self.assertIsNone(state.get_task("task-123"))

    def test_get_all_tasks_preserves_pagination(self):
        """任务列表响应必须包含状态层返回的总数和请求分页参数。"""
        with patch.object(
            video_controller.sm.state,
            "get_all_tasks",
            return_value=([{"id": "task-1", "cross_post_owner": "internal"}], 21),
        ) as get_all:
            response = video_controller.get_all_tasks(
                self._request(), page=2, page_size=10
            )

        self.assertEqual(
            response["data"],
            {
                "tasks": [{"id": "task-1"}],
                "total": 21,
                "page": 2,
                "page_size": 10,
            },
        )
        get_all.assert_called_once_with(2, 10)

    def test_task_query_returns_relative_url_without_mutating_state(self):
        """
        endpoint 未配置时应返回相对任务 URL，且不能把展示用 URL 回写到状态，
        否则后续请求可能基于已改写数据重复拼接路径。
        """
        task_id = "controller-task-url"
        task_dir = utils.task_dir(task_id)
        video_path = os.path.join(task_dir, "final-1.mp4")
        Path(video_path).write_bytes(b"fake-video")

        try:
            sm.state.update_task(
                task_id,
                state=const.TASK_STATE_COMPLETE,
                videos=[video_path],
                combined_videos=[video_path],
                cross_post_owner="localhost:123:internal",
            )
            with patch.dict(config.app, {"endpoint": ""}):
                response = video_controller.get_task(
                    self._request(), task_id=task_id, query=MagicMock()
                )

            self.assertEqual(
                response["data"]["videos"],
                [f"/tasks/{task_id}/final-1.mp4"],
            )
            self.assertNotIn("cross_post_owner", response["data"])
            self.assertIn("cross_post_owner", sm.state.get_task(task_id))
            self.assertEqual(sm.state.get_task(task_id)["videos"], [video_path])
        finally:
            sm.state.delete_task(task_id)
            shutil.rmtree(task_dir, ignore_errors=True)

    def test_task_query_preserves_structured_failure_details(self):
        """失败阶段和错误信息必须通过任务查询接口原样返回。"""
        failed_task = {
            "task_id": "failed-task",
            "state": const.TASK_STATE_FAILED,
            "progress": 30,
            "failed_stage": "audio",
            "error": "TTS request timed out",
        }

        with patch.object(
            video_controller.sm.state,
            "get_task",
            return_value=failed_task,
        ):
            response = video_controller.get_task(
                self._request(), task_id="failed-task", query=MagicMock()
            )

        self.assertEqual(response["data"], failed_task)

    def test_task_query_schema_documents_success_and_failure_states(self):
        """OpenAPI 模型示例必须覆盖发布成功和生成失败两种状态。"""
        examples = TaskQueryResponse.model_json_schema()["examples"]

        self.assertEqual(examples[0]["data"]["cross_post_state"], "complete")
        self.assertEqual(examples[1]["data"]["failed_stage"], "audio")
        self.assertTrue(examples[1]["data"]["error"])

        task_data_schema = TaskQueryResponse.model_json_schema()["$defs"][
            "TaskStatusData"
        ]
        self.assertIn("failed_stage", task_data_schema["properties"])
        self.assertIn("cross_post_state", task_data_schema["properties"])

        list_schema = TaskListResponse.model_json_schema()
        self.assertIn("TaskListData", list_schema["$defs"])
        self.assertIn("TaskStatusData", list_schema["$defs"])

    def test_delete_rejects_generation_and_cross_posting_tasks(self):
        """生成中和发布中的任务都在读取目录，删除接口必须返回 409。"""
        busy_tasks = (
            {
                "task_id": "generating-task",
                "state": const.TASK_STATE_PROCESSING,
                "progress": 30,
            },
            {
                "task_id": "publishing-task",
                "state": const.TASK_STATE_COMPLETE,
                "progress": 100,
                "cross_post_state": const.CROSS_POST_STATE_PROCESSING,
            },
        )

        for task in busy_tasks:
            with self.subTest(task_id=task["task_id"]), patch.object(
                video_controller.sm.state,
                "get_task",
                return_value=task,
            ), patch.object(video_controller.sm.state, "delete_task") as delete_task:
                with self.assertRaises(HttpException) as raised:
                    video_controller.delete_video(
                        self._request(), task_id=task["task_id"]
                    )

                self.assertEqual(raised.exception.status_code, 409)
                delete_task.assert_not_called()

    def test_delete_allows_completed_task(self):
        """普通已完成任务仍应保持原有删除行为。"""
        completed_task = {
            "task_id": "completed-task",
            "state": const.TASK_STATE_COMPLETE,
            "progress": 100,
            "cross_post_state": const.CROSS_POST_STATE_COMPLETE,
        }

        with patch.object(
            video_controller.sm.state,
            "get_task",
            return_value=completed_task,
        ), patch.object(
            video_controller.utils,
            "task_dir",
            return_value="/tmp/mpt-completed-task-test",
        ), patch.object(
            video_controller.os.path, "exists", return_value=False
        ), patch.object(video_controller.sm.state, "delete_task") as delete_task:
            response = video_controller.delete_video(
                self._request(), task_id="completed-task"
            )

        self.assertEqual(response["status"], 200)
        delete_task.assert_called_once_with("completed-task")

    def test_get_and_delete_missing_task_return_404(self):
        """查询或删除未知任务都应返回一致的 404，而不是空成功响应。"""
        with patch.object(video_controller.sm.state, "get_task", return_value=None):
            for operation in (
                lambda: video_controller.get_task(
                    self._request(), task_id="missing", query=MagicMock()
                ),
                lambda: video_controller.delete_video(
                    self._request(), task_id="missing"
                ),
            ):
                with self.subTest(operation=operation):
                    with self.assertRaises(HttpException) as raised:
                        operation()
                    self.assertEqual(raised.exception.status_code, 404)


class TestVideoControllerFiles(unittest.TestCase):
    @staticmethod
    def _request(range_header=None):
        headers = {"x-task-id": "request-123"}
        if range_header is not None:
            headers["Range"] = range_header
        return SimpleNamespace(headers=headers)

    def test_upload_video_material_validates_complete_extension(self):
        """大写合法扩展名应接受，无点号伪扩展名应拒绝。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            upload = SimpleNamespace(
                filename=r"C:\videos\clip.MOV",
                file=BytesIO(b"video"),
            )
            with patch.object(
                video_controller.utils,
                "storage_dir",
                return_value=temp_dir,
            ):
                response = video_controller.upload_video_material_file(
                    self._request(), upload
                )

            self.assertEqual(response["data"]["file"], "clip.MOV")
            self.assertEqual(Path(temp_dir, "clip.MOV").read_bytes(), b"video")

            invalid_upload = SimpleNamespace(
                filename="photojpg",
                file=BytesIO(b"not-an-image"),
            )
            with self.assertRaises(HttpException) as raised:
                video_controller.upload_video_material_file(
                    self._request(), invalid_upload
                )
            self.assertEqual(raised.exception.status_code, 400)

    def test_stream_video_returns_requested_bytes(self):
        """Range 响应的正文和 Content-Range 必须与计算出的区间一致。"""

        async def consume(response):
            return b"".join([chunk async for chunk in response.body_iterator])

        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, "clip.mp4").write_bytes(b"0123456789")
            with patch.object(
                video_controller.utils,
                "task_dir",
                return_value=temp_dir,
            ):
                response = asyncio.run(
                    video_controller.stream_video(
                        self._request("bytes=2-5"), "clip.mp4"
                    )
                )
                body = asyncio.run(consume(response))

        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.headers["content-range"], "bytes 2-5/10")
        self.assertEqual(response.headers["content-length"], "4")
        self.assertEqual(body, b"2345")

    def test_download_video_uses_resolved_file(self):
        """下载响应应使用白名单目录解析后的真实路径和原始文件名。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            video_path = Path(temp_dir, "final-1.mp4")
            video_path.write_bytes(b"video")
            with patch.object(
                video_controller.utils,
                "task_dir",
                return_value=temp_dir,
            ):
                response = asyncio.run(
                    video_controller.download_video(
                        self._request(), "final-1.mp4"
                    )
                )

        # macOS 的 /var 是 /private/var 符号链接，安全解析会返回真实路径。
        self.assertEqual(response.path, os.path.realpath(video_path))
        self.assertEqual(response.filename, "final-1.mp4")
        self.assertEqual(response.media_type, "video/mp4")


if __name__ == "__main__":
    unittest.main()
