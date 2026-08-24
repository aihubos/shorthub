import glob
import os
import pathlib
import secrets
import shutil
import threading
from typing import Union

from fastapi import BackgroundTasks, Depends, Path, Query, Request, UploadFile
from fastapi.params import File
from fastapi.responses import FileResponse, StreamingResponse
from loguru import logger

from app.config import config
from app.controllers import base
from app.controllers.manager.base_manager import TaskQueueFullError
from app.controllers.manager.memory_manager import InMemoryTaskManager
from app.controllers.manager.redis_manager import RedisTaskManager
from app.controllers.v1.base import new_router
from app.models import const
from app.models.exception import HttpException
from app.models.schema import (
    AudioRequest,
    BgmRetrieveResponse,
    BgmUploadResponse,
    BuildersLoungeTaskResponse,
    BuildersLoungeVideoRequest,
    MaterialInfo,
    SubtitleRequest,
    TaskDeletionResponse,
    TaskListResponse,
    TaskQueryRequest,
    TaskQueryResponse,
    TaskResponse,
    TaskVideoRequest,
    VideoMaterialUploadResponse,
    VideoMaterialRetrieveResponse
)
from app.services import bgm as bgm_service
from app.services import state as sm
from app.services import task as tm
from app.utils import file_security, utils

# 认证依赖项
# router = new_router(dependencies=[Depends(base.verify_token)])
router = new_router()

_enable_redis = config.app.get("enable_redis", False)
_redis_host = config.app.get("redis_host", "localhost")
_redis_port = config.app.get("redis_port", 6379)
_redis_db = config.app.get("redis_db", 0)
_redis_password = config.app.get("redis_password", None)
_max_concurrent_tasks = config.app.get("max_concurrent_tasks", 5)
_max_queued_tasks = config.app.get("max_queued_tasks", 100)

redis_url = f"redis://:{_redis_password}@{_redis_host}:{_redis_port}/{_redis_db}"
# 根据配置选择合适的任务管理器
if _enable_redis:
    task_manager = RedisTaskManager(
        max_concurrent_tasks=_max_concurrent_tasks,
        redis_url=redis_url,
        max_queued_tasks=_max_queued_tasks,
    )
else:
    task_manager = InMemoryTaskManager(
        max_concurrent_tasks=_max_concurrent_tasks,
        max_queued_tasks=_max_queued_tasks,
    )


_builders_lounge_task_lock = threading.Lock()
_BUILDERS_LOUNGE_RENDER_TOKEN_ENV = "BUILDERS_LOUNGE_RENDER_TOKEN"
_BUILDERS_LOUNGE_MATERIALS_ENV = "BUILDERS_LOUNGE_MATERIALS"
_BUILDERS_LOUNGE_KOREAN_VOICE = "ko-KR-HyunsuMultilingualNeural-Male"
_BUILDERS_LOUNGE_KOREAN_FONT = "NotoSansKR-Bold.ttf"
_BUILDERS_LOUNGE_MEDIA_TYPE = "video/mp4"
_BUILDERS_LOUNGE_MATERIAL_SUFFIXES = {
    ".avi",
    ".flv",
    ".jpeg",
    ".jpg",
    ".mkv",
    ".mov",
    ".mp4",
    ".png",
}


def _sanitize_upload_filename(filename: str, request_id: str) -> str:
    # 浏览器或客户端有时会附带目录信息，甚至可能夹带 ../ 这类穿越片段。
    # 这里只保留纯文件名，避免上传接口把文件写到目标目录之外。
    normalized_name = (filename or "").replace("\\", "/").split("/")[-1].strip()
    if not normalized_name or normalized_name in {".", ".."}:
        raise HttpException(
            task_id=request_id,
            status_code=400,
            message=f"{request_id}: invalid filename",
        )
    return normalized_name


def _resolve_path_within_directory(base_dir: str, unsafe_path: str, request_id: str) -> str:
    try:
        return file_security.resolve_path_within_directory(base_dir, unsafe_path)
    except ValueError as exc:
        logger.warning(
            f"reject unsafe file path, request_id: {request_id}, path: {unsafe_path}, "
            f"error: {str(exc)}"
        )
        raise HttpException(
            task_id=request_id,
            status_code=404 if str(exc) == "file does not exist" else 403,
            message=f"{request_id}: invalid file path",
        )


def _public_task_data(task: dict) -> dict:
    """复制任务状态并移除仅用于服务端进程协调的内部字段。"""
    public_task = dict(task)
    public_task.pop("cross_post_owner", None)
    return public_task


def _task_file_to_uri(file: str, endpoint: str, task_dir: str, request_id: str) -> str:
    if not isinstance(file, str):
        return file

    if file.startswith(("http://", "https://")):
        return file

    try:
        resolved_path = file_security.resolve_path_within_directory(task_dir, file)
    except ValueError as exc:
        # 任务状态理论上只应保存任务目录内的产物路径。这里不再继续拼接 URL，
        # 避免把异常路径包装成可访问链接；同时保留原值，便于排查历史脏数据。
        logger.warning(
            f"skip unsafe task output path, request_id: {request_id}, path: {file}, "
            f"error: {str(exc)}"
        )
        return file

    relative_path = os.path.relpath(resolved_path, task_dir).replace("\\", "/")
    uri_path = f"tasks/{relative_path}"
    if endpoint:
        return f"{endpoint.rstrip('/')}/{uri_path}"
    return f"/{uri_path}"


def _verify_builders_lounge_token(request: Request) -> None:
    """Authenticate the private renderer contract without echoing the token."""
    request_id = base.get_task_id(request)
    expected_token = os.getenv(_BUILDERS_LOUNGE_RENDER_TOKEN_ENV, "").strip()
    if not expected_token:
        raise HttpException(
            task_id=request_id,
            status_code=503,
            message="builders lounge renderer is not configured",
        )

    authorization = request.headers.get("authorization", "")
    scheme, separator, supplied_token = authorization.partition(" ")
    if (
        separator != " "
        or scheme.lower() != "bearer"
        or not supplied_token
        or not secrets.compare_digest(supplied_token, expected_token)
    ):
        raise HttpException(
            task_id=request_id,
            status_code=401,
            message="invalid builders lounge renderer credentials",
        )


def _builders_lounge_safe_material_names() -> list[str]:
    """Return configured server-owned material names without exposing paths."""
    local_videos_dir = utils.storage_dir("local_videos", create=True)
    configured_names = [
        item.strip()
        for item in os.getenv(_BUILDERS_LOUNGE_MATERIALS_ENV, "").split(",")
        if item.strip()
    ]
    if configured_names:
        candidate_names = configured_names
    else:
        candidate_names = sorted(
            entry.name
            for entry in pathlib.Path(local_videos_dir).iterdir()
            if entry.is_file()
            and entry.suffix.lower() in _BUILDERS_LOUNGE_MATERIAL_SUFFIXES
        )

    safe_names = []
    for name in candidate_names:
        try:
            resolved_path = file_security.resolve_path_within_directory(
                local_videos_dir, name
            )
        except ValueError:
            continue
        if pathlib.Path(resolved_path).suffix.lower() not in (
            _BUILDERS_LOUNGE_MATERIAL_SUFFIXES
        ):
            continue
        safe_names.append(os.path.basename(resolved_path))

    return list(dict.fromkeys(safe_names))


def _builders_lounge_material_names(scene_count: int, request_id: str) -> list[str]:
    """Resolve at least two server-owned local materials without exposing paths."""
    safe_names = _builders_lounge_safe_material_names()
    # A single source cannot demonstrate the requested scene change. Repeat the
    # safe list only after two distinct materials have been established.
    if len(safe_names) < 2:
        raise HttpException(
            task_id=request_id,
            status_code=503,
            message="builders lounge renderer requires two local materials",
        )

    return [safe_names[index % len(safe_names)] for index in range(scene_count)]


def builders_lounge_renderer_readiness() -> dict[str, bool]:
    """Return renderer readiness without exposing token or material details."""
    try:
        local_materials_ready = len(_builders_lounge_safe_material_names()) >= 2
    except OSError:
        local_materials_ready = False

    return {
        "renderTokenConfigured": bool(
            os.getenv(_BUILDERS_LOUNGE_RENDER_TOKEN_ENV, "").strip()
        ),
        "localMaterialsReady": local_materials_ready,
    }


def _builders_lounge_video_params(
    body: BuildersLoungeVideoRequest, request_id: str
) -> TaskVideoRequest:
    material_names = _builders_lounge_material_names(len(body.scenes), request_id)
    return TaskVideoRequest(
        video_subject=body.topic.strip(),
        video_script="\n\n".join(scene.narration.strip() for scene in body.scenes),
        video_aspect="9:16",
        video_concat_mode="sequential",
        video_clip_duration=4,
        video_count=1,
        video_source="local",
        video_materials=[
            MaterialInfo(provider="local", url=name, duration=4)
            for name in material_names
        ],
        video_language="ko-KR",
        voice_name=_BUILDERS_LOUNGE_KOREAN_VOICE,
        voice_volume=1.0,
        voice_rate=1.0,
        font_name=_BUILDERS_LOUNGE_KOREAN_FONT,
        bgm_type="",
        bgm_volume=0.0,
        subtitle_enabled=True,
    )


def _builders_lounge_task_response(task: dict, request_id: str) -> dict:
    state = task.get("state")
    if state == const.TASK_STATE_COMPLETE:
        public_state = "completed"
    elif state == const.TASK_STATE_FAILED:
        public_state = "failed"
    else:
        public_state = "processing"

    video_url = None
    videos = task.get("videos") or []
    if videos:
        endpoint = config.app.get("endpoint", "").rstrip("/")
        protected_path = f"/api/v1/builders-lounge/tasks/{task['task_id']}/video"
        video_url = f"{endpoint}{protected_path}" if endpoint else protected_path

    return utils.get_response(
        200,
        {
            "taskId": str(task["task_id"]),
            "state": public_state,
            "progress": max(0, min(100, int(task.get("progress", 0) or 0))),
            "videoUrl": video_url,
            "mediaType": _BUILDERS_LOUNGE_MEDIA_TYPE if video_url else None,
        },
    )


def _parse_byte_range(
    range_header: str | None, file_size: int, request_id: str
) -> tuple[int, int]:
    """解析单段 HTTP Range，并把无效或越界请求稳定转换成 416。"""
    if file_size <= 0:
        raise HttpException(
            task_id=request_id,
            status_code=416,
            message=f"{request_id}: requested range is not satisfiable",
        )

    if not range_header:
        return 0, file_size - 1

    try:
        # 视频播放器这里只需要单段 bytes range。拒绝多段请求可以避免返回体
        # 与 Content-Range 不一致，也避免异常字符串落入 int() 产生 500。
        if not range_header.startswith("bytes=") or "," in range_header:
            raise ValueError("unsupported range format")
        start_text, end_text = range_header[6:].split("-", 1)
        if not start_text and not end_text:
            raise ValueError("empty range")

        if not start_text:
            suffix_length = int(end_text)
            if suffix_length <= 0:
                raise ValueError("invalid suffix length")
            start = max(file_size - suffix_length, 0)
            end = file_size - 1
        else:
            start = int(start_text)
            end = int(end_text) if end_text else file_size - 1
            if start < 0 or start >= file_size or end < start:
                raise ValueError("range outside file")
            end = min(end, file_size - 1)
    except (TypeError, ValueError) as exc:
        logger.warning(
            f"reject invalid video range, request_id: {request_id}, "
            f"range: {range_header}, file_size: {file_size}, error: {str(exc)}"
        )
        raise HttpException(
            task_id=request_id,
            status_code=416,
            message=f"{request_id}: requested range is not satisfiable",
        ) from exc

    return start, end


@router.post("/videos", response_model=TaskResponse, summary="Generate a short video")
def create_video(
    background_tasks: BackgroundTasks, request: Request, body: TaskVideoRequest
):
    return create_task(request, body, stop_at="video")


@router.post(
    "/builders-lounge/videos",
    response_model=BuildersLoungeTaskResponse,
    summary="Create an idempotent private Builders Lounge render task",
)
def create_builders_lounge_video(
    request: Request, body: BuildersLoungeVideoRequest
):
    _verify_builders_lounge_token(request)
    request_id = base.get_task_id(request)
    task_id = str(body.job_id)

    with _builders_lounge_task_lock:
        existing_task = sm.state.get_task(task_id)
        if existing_task is not None:
            return _builders_lounge_task_response(existing_task, request_id)

        params = _builders_lounge_video_params(body, request_id)
        sm.state.update_task(task_id)
        try:
            task_manager.add_task(
                tm.start, task_id=task_id, params=params, stop_at="video"
            )
        except TaskQueueFullError as exc:
            sm.state.delete_task(task_id)
            raise HttpException(
                task_id=task_id,
                status_code=429,
                message=f"{request_id}: {str(exc)}",
            ) from exc
        except Exception:
            sm.state.delete_task(task_id)
            raise

    logger.success(
        "Builders Lounge task created: "
        f"task_id={task_id}, scene_count={len(body.scenes)}"
    )
    return _builders_lounge_task_response(sm.state.get_task(task_id), request_id)


@router.get(
    "/builders-lounge/tasks/{task_id}",
    response_model=BuildersLoungeTaskResponse,
    summary="Query a private Builders Lounge render task",
)
def get_builders_lounge_task(
    request: Request, task_id: str = Path(..., description="Lounge job UUID")
):
    _verify_builders_lounge_token(request)
    request_id = base.get_task_id(request)
    task = sm.state.get_task(task_id)
    if task is None:
        raise HttpException(
            task_id=task_id,
            status_code=404,
            message=f"{request_id}: task not found",
        )
    return _builders_lounge_task_response(task, request_id)


@router.get(
    "/builders-lounge/tasks/{task_id}/video",
    summary="Download a private Builders Lounge MP4",
)
def get_builders_lounge_video(
    request: Request, task_id: str = Path(..., description="Lounge job UUID")
):
    _verify_builders_lounge_token(request)
    request_id = base.get_task_id(request)
    task = sm.state.get_task(task_id)
    if task is None:
        raise HttpException(
            task_id=task_id,
            status_code=404,
            message=f"{request_id}: task not found",
        )
    videos = task.get("videos") or []
    if task.get("state") != const.TASK_STATE_COMPLETE or not videos:
        raise HttpException(
            task_id=task_id,
            status_code=409,
            message=f"{request_id}: task video is not ready",
        )

    task_directory = utils.task_dir(task_id)
    video_path = _resolve_path_within_directory(
        task_directory, videos[0], request_id
    )
    if pathlib.Path(video_path).suffix.lower() != ".mp4":
        raise HttpException(
            task_id=task_id,
            status_code=415,
            message=f"{request_id}: task video is not an MP4",
        )
    return FileResponse(
        path=video_path,
        media_type=_BUILDERS_LOUNGE_MEDIA_TYPE,
        filename=f"builders-lounge-{task_id}.mp4",
    )


@router.post("/subtitle", response_model=TaskResponse, summary="Generate subtitle only")
def create_subtitle(
    background_tasks: BackgroundTasks, request: Request, body: SubtitleRequest
):
    return create_task(request, body, stop_at="subtitle")


@router.post("/audio", response_model=TaskResponse, summary="Generate audio only")
def create_audio(
    background_tasks: BackgroundTasks, request: Request, body: AudioRequest
):
    return create_task(request, body, stop_at="audio")


def create_task(
    request: Request,
    body: Union[TaskVideoRequest, SubtitleRequest, AudioRequest],
    stop_at: str,
):
    task_id = utils.get_uuid()
    request_id = base.get_task_id(request)
    try:
        task = {
            "task_id": task_id,
            "request_id": request_id,
            "params": body.model_dump(),
        }
        sm.state.update_task(task_id)
        try:
            task_manager.add_task(
                tm.start, task_id=task_id, params=body, stop_at=stop_at
            )
        except Exception:
            # 状态记录在调度前创建，默认标记为 processing。如果调度器没能
            # 接管任务（例如线程启动失败或 Redis 队列不可用），必须回滚该
            # 记录，否则 API 和 WebUI 会永久展示一个实际从未运行的任务。
            sm.state.delete_task(task_id)
            raise
        logger.success(f"Task created: {utils.to_json(task)}")
        return utils.get_response(200, task)
    except TaskQueueFullError as e:
        logger.warning(
            f"reject task because queue is full, request_id: {request_id}, task_id: {task_id}"
        )
        raise HttpException(
            task_id=task_id, status_code=429, message=f"{request_id}: {str(e)}"
        )
    except ValueError as e:
        raise HttpException(
            task_id=task_id, status_code=400, message=f"{request_id}: {str(e)}"
        )

@router.get("/tasks", response_model=TaskListResponse, summary="Get all tasks")
def get_all_tasks(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1),
):
    tasks, total = sm.state.get_all_tasks(page, page_size)

    response = {
        "tasks": [_public_task_data(task) for task in tasks],
        "total": total,
        "page": page,
        "page_size": page_size,
    }
    return utils.get_response(200, response)



@router.get(
    "/tasks/{task_id}", response_model=TaskQueryResponse, summary="Query task status"
)
def get_task(
    request: Request,
    task_id: str = Path(..., description="Task ID"),
    query: TaskQueryRequest = Depends(),
):
    request_id = base.get_task_id(request)
    endpoint = config.app.get("endpoint", "").rstrip("/")
    task = sm.state.get_task(task_id)
    if task:
        task_dir = utils.task_dir()
        response_task = _public_task_data(task)

        if "videos" in task:
            response_task["videos"] = [
                _task_file_to_uri(v, endpoint, task_dir, request_id)
                for v in task["videos"]
            ]
        if "combined_videos" in task:
            response_task["combined_videos"] = [
                _task_file_to_uri(v, endpoint, task_dir, request_id)
                for v in task["combined_videos"]
            ]
        return utils.get_response(200, response_task)

    raise HttpException(
        task_id=task_id, status_code=404, message=f"{request_id}: task not found"
    )


@router.delete(
    "/tasks/{task_id}",
    response_model=TaskDeletionResponse,
    summary="Delete a generated short video task",
)
def delete_video(request: Request, task_id: str = Path(..., description="Task ID")):
    request_id = base.get_task_id(request)
    task = sm.state.get_task(task_id)
    if task:
        if tm.is_task_busy(task):
            logger.warning(
                f"refuse to delete busy task, request_id: {request_id}, "
                f"task_id: {task_id}, state: {task.get('state')}, "
                f"cross_post_state: {task.get('cross_post_state')}"
            )
            raise HttpException(
                task_id=task_id,
                status_code=409,
                message=f"{request_id}: task is still running",
            )

        tasks_dir = utils.task_dir()
        current_task_dir = os.path.join(tasks_dir, task_id)
        if os.path.exists(current_task_dir):
            shutil.rmtree(current_task_dir)

        sm.state.delete_task(task_id)
        logger.success(f"video deleted: {utils.to_json(task)}")
        return utils.get_response(200)

    raise HttpException(
        task_id=task_id, status_code=404, message=f"{request_id}: task not found"
    )


@router.get(
    "/musics", response_model=BgmRetrieveResponse, summary="Retrieve local BGM files"
)
def get_bgm_list(request: Request):
    bgm_list = []
    for file in bgm_service.list_bgm_files():
        filename = os.path.basename(file)
        bgm_list.append(
            {
                "name": filename,
                "size": os.path.getsize(file),
                # 只返回文件名，避免把服务器绝对路径暴露给调用方。服务端会
                # 在 storage/bgm 和 resource/songs 两个白名单目录中重新解析。
                "file": filename,
            }
        )
    response = {"files": bgm_list}
    return utils.get_response(200, response)


@router.post(
    "/musics",
    response_model=BgmUploadResponse,
    summary="Upload a background music file",
    description=(
        "Validate an MP3, M4A, AAC, WAV, FLAC, OGG, OPUS, or WMA file up to "
        "30 MB and store it under an immutable UUID filename in storage/bgm."
    ),
    responses={
        400: {"description": "The filename, format, size, or audio stream is invalid"},
        500: {"description": "FFmpeg validation or persistent storage is unavailable"},
    },
)
def upload_bgm_file(request: Request, file: UploadFile = File(...)):
    request_id = base.get_task_id(request)
    try:
        safe_filename = bgm_service.save_bgm_upload(file.filename, file.file)
    except bgm_service.BgmUploadError as exc:
        # 上传失败通常可以由用户更换文件后恢复，因此记录 request_id 和明确原因，
        # 但不输出文件内容或绝对路径，避免日志泄露用户数据。
        logger.warning(
            f"background music upload rejected: request_id={request_id}, error={str(exc)}"
        )
        raise HttpException(
            task_id=request_id,
            status_code=400,
            message=f"{request_id}: {str(exc)}",
        )
    except bgm_service.BgmServiceError as exc:
        # 工具链或存储故障属于服务端问题，不能伪装成用户文件错误。日志保留
        # request_id 和内部原因，HTTP 响应只返回稳定文案，避免暴露服务器路径。
        logger.error(
            f"background music upload failed: request_id={request_id}, error={str(exc)}"
        )
        raise HttpException(
            task_id=request_id,
            status_code=500,
            message=f"{request_id}: background music validation is unavailable",
        )

    response = {"file": safe_filename}
    return utils.get_response(200, response)

@router.get(
    "/video_materials", response_model=VideoMaterialRetrieveResponse, summary="Retrieve local video materials"
)
def get_video_materials_list(request: Request):
    allowed_suffixes = ("mp4", "mov", "avi", "flv", "mkv", "jpg", "jpeg", "png")
    local_videos_dir = utils.storage_dir("local_videos", create=True)
    files = []
    for suffix in allowed_suffixes:
        files.extend(glob.glob(os.path.join(local_videos_dir, f"*.{suffix}")))
    # 文件系统枚举顺序不稳定，直接返回会导致“顺序拼接”在不同机器或不同
    # 时刻表现不一致。这里统一按文件名排序，至少保证服务端返回顺序可预测。
    files.sort(key=lambda file_path: os.path.basename(file_path).lower())
    video_materials_list = []
    for file in files:
        filename = os.path.basename(file)
        video_materials_list.append(
            {
                "name": filename,
                "size": os.path.getsize(file),
                # 与 BGM 一样，只返回文件名；创建任务时再在 local_videos
                # 白名单目录内解析，避免 API 泄露宿主机绝对路径。
                "file": filename,
            }
        )
    response = {"files": video_materials_list}
    return utils.get_response(200, response)


@router.post(
    "/video_materials",
    response_model=VideoMaterialUploadResponse,
    summary="Upload the video material file to the local videos directory",
)
def upload_video_material_file(request: Request, file: UploadFile = File(...)):
    request_id = base.get_task_id(request)
    safe_filename = _sanitize_upload_filename(file.filename, request_id)
    # check file ext
    allowed_suffixes = ("mp4", "mov", "avi", "flv", "mkv", "jpg", "jpeg", "png")
    suffix = pathlib.Path(safe_filename).suffix.lower().lstrip(".")
    # 按完整扩展名校验，既兼容 .MOV 这类大写后缀，也避免 photojpg 这种没有
    # 点号的文件名因为 endswith("jpg") 被误当成合法图片。
    if suffix in allowed_suffixes:
        local_videos_dir = utils.storage_dir("local_videos", create=True)
        save_path = os.path.join(local_videos_dir, safe_filename)
        # save file
        with open(save_path, "wb+") as buffer:
            # If the file already exists, it will be overwritten
            file.file.seek(0)
            buffer.write(file.file.read())
        response = {"file": safe_filename}
        return utils.get_response(200, response)

    raise HttpException(
        "", status_code=400, message=f"{request_id}: Only files with extensions {', '.join(allowed_suffixes)} can be uploaded"
    )

@router.get("/stream/{file_path:path}")
async def stream_video(request: Request, file_path: str):
    request_id = base.get_task_id(request)
    tasks_dir = utils.task_dir()
    video_path = _resolve_path_within_directory(tasks_dir, file_path, request_id)
    range_header = request.headers.get("Range")
    video_size = os.path.getsize(video_path)
    start, end = _parse_byte_range(range_header, video_size, request_id)
    length = end - start + 1

    def file_iterator(file_path, offset=0, bytes_to_read=None):
        with open(file_path, "rb") as f:
            f.seek(offset, os.SEEK_SET)
            remaining = bytes_to_read or video_size
            while remaining > 0:
                bytes_to_read = min(4096, remaining)
                data = f.read(bytes_to_read)
                if not data:
                    break
                remaining -= len(data)
                yield data

    response = StreamingResponse(
        file_iterator(video_path, start, length), media_type="video/mp4"
    )
    response.headers["Content-Range"] = f"bytes {start}-{end}/{video_size}"
    response.headers["Accept-Ranges"] = "bytes"
    response.headers["Content-Length"] = str(length)
    response.status_code = 206  # Partial Content

    return response


@router.get("/download/{file_path:path}")
async def download_video(request: Request, file_path: str):
    """
    download video
    :param request: Request request
    :param file_path: video file path, eg: /cd1727ed-3473-42a2-a7da-4faafafec72b/final-1.mp4
    :return: video file
    """
    request_id = base.get_task_id(request)
    tasks_dir = utils.task_dir()
    video_path = _resolve_path_within_directory(tasks_dir, file_path, request_id)
    file_path = pathlib.Path(video_path)
    filename = file_path.stem
    extension = file_path.suffix
    headers = {"Content-Disposition": f"attachment; filename={filename}{extension}"}
    return FileResponse(
        path=video_path,
        headers=headers,
        filename=f"{filename}{extension}",
        media_type=f"video/{extension[1:]}",
    )
