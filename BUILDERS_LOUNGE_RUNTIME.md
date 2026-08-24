# Builders Lounge 렌더러 런타임

이 변경은 MPT main `c44859f210b516eec7c16f701bd18cf1b1d9b44d`를 부모로
한다. 이 checkout의 소스로 MoneyPrinterTurbo API만 실행하며 공개 `latest` 이미지는
사용하지 않는다. 비밀값은 checkout 밖의 운영 비밀 저장소에서 주입하고 커밋하지 않는다.

## Checkout 실행

Docker가 없는 기존 Mac에서는 고정 커밋 checkout과 잠금 파일을 그대로 사용한다.

```sh
git checkout --detach <인계된-병합-SHA>
uv sync --frozen --no-dev
test -e config.toml || cp config.example.toml config.toml
# 운영 비밀 저장소에서 아래 필수 환경변수 두 개를 현재 프로세스에 주입
exec .venv/bin/python main.py
```

`config.toml`의 API 수신값은 `listen_host = "0.0.0.0"`, `listen_port = 8080`이다.
운영자가 이미 관리 중인 `config.toml`과 `storage`가 있으면 덮어쓰지 않고 같은 경로에
연결한다. 실제 토큰과 자료 파일명은 명령 인자, Git, 채널, 로그에 남기지 않는다.

## 준비

```sh
test -e config.toml || cp config.example.toml config.toml
export BUILDERS_LOUNGE_ENV_FILE=/secure/path/builders-lounge.env
```

외부 env-file에는 다음 이름을 설정한다. 실제 값은 이 문서나 저장소에 기록하지 않는다.

- `BUILDERS_LOUNGE_RENDER_TOKEN`: Builders Lounge API 요청에 사용할 Bearer 토큰
- `BUILDERS_LOUNGE_MATERIALS`: `storage/local_videos` 안의 서버 소유 자료 파일명 목록

이미지 이름은 `BUILDERS_LOUNGE_IMAGE`로 바꿀 수 있으며, 기본값은
`moneyprinterturbo-builders-lounge:local`이다.

## 빌드와 시작

현재 디렉터리에서 다음 명령을 실행한다.

```sh
docker compose -f docker-compose.builders-lounge.yml build
docker compose -f docker-compose.builders-lounge.yml up -d
```

중지는 다음과 같다.

```sh
docker compose -f docker-compose.builders-lounge.yml down
```

## 실행 경계

- 호스트 `127.0.0.1:8080`만 컨테이너의 API 포트 `8080`에 연결한다.
- `./config.toml`을 `/MoneyPrinterTurbo/config.toml`로, `./storage`를
  `/MoneyPrinterTurbo/storage`로 연결한다.
- compose 서비스는 `Dockerfile`로 현재 checkout을 빌드하고 `python3 main.py`로
  API 모드만 시작한다. WebUI 포트는 열지 않는다.

## Health와 API 계약

`GET http://127.0.0.1:8080/healthz`는 토큰 설정과 서버 소유 로컬 자료 2개 이상
사용 가능 여부를 확인한다. 두 조건이 모두 충족되면 HTTP 200과 `ready`를, 아니면
HTTP 503과 `unavailable`을 반환한다.

```json
{"status":"ready","contractVersion":"builders-lounge-renderer-v1","checks":{"renderTokenConfigured":true,"localMaterialsReady":true}}
```

`checks`는 boolean만 포함한다. 응답에는 토큰, 환경값, 내부 주소, 파일 경로를 넣지
않으며, 실제 네트워크 TTS 성공은 이 health 계약이 판정하지 않는다. 컨테이너
healthcheck도 이 경로의 HTTP 200 여부만 확인한다.

기존 Builders Lounge 전용 API 경로는 유지한다.

- `POST /api/v1/builders-lounge/videos`
- `GET /api/v1/builders-lounge/tasks/{jobId}`
- `GET /api/v1/builders-lounge/tasks/{jobId}/video`

위 전용 API 요청에는 env-file의 `BUILDERS_LOUNGE_RENDER_TOKEN`과 같은 Bearer 토큰을
사용한다. 생성·상태 조회 응답은 기존 `taskId`, `state`, `progress`, `videoUrl`,
`mediaType` 계약을 따른다.

생성 요청은 Worker의 Lounge 작업 UUID를 그대로 사용하며 장면은 2~8개다.

```json
{
  "jobId": "<lounge-job-uuid>",
  "topic": "<topic>",
  "detailedPrompt": "<server-authored-plan>",
  "scenes": [
    {"narration": "<korean narration>", "visualPrompt": "<visual>"},
    {"narration": "<korean narration>", "visualPrompt": "<visual>"}
  ]
}
```

생성·상태 응답은 같은 envelope를 사용한다. 같은 `jobId`의 생성 요청을 반복해도 새
작업을 만들지 않는다.

```json
{
  "status": 200,
  "message": "success",
  "data": {
    "taskId": "<same-lounge-job-uuid>",
    "state": "processing",
    "progress": 5,
    "videoUrl": null,
    "mediaType": null
  }
}
```

완료 시 `state`는 `completed`, `progress`는 `100`, `mediaType`은 `video/mp4`,
`videoUrl`은 해당 작업 전용 `/video` 경로다. MP4 경로도 같은 Bearer 인증을 요구하고
`Content-Type: video/mp4`를 반환한다. 인증 누락·불일치는 HTTP 401이다.

## HTTPS ingress 조건

- 공개 HTTPS origin은 경로 없는 단일 origin이어야 하며 Worker의
  `SHORTS_RENDERER_URL`에는 그 origin만 설정한다.
- ingress는 로컬 `127.0.0.1:8080`으로 프록시하고 `Authorization` 헤더를 보존한다.
- 외부 허용 경로는 `GET /healthz`, `POST /api/v1/builders-lounge/videos`,
  `GET /api/v1/builders-lounge/tasks/{jobId}`와 그 `/video` 하위 경로로 제한한다.
- `/tasks`, `/docs`, WebUI와 그 밖의 일반 MPT API는 외부에 공개하지 않는다.
- redirect를 만들지 않는다. 완료 응답의 `videoUrl`은 같은 HTTPS origin의 정확한
  `/api/v1/builders-lounge/tasks/{jobId}/video` 경로여야 한다.
- MPT CORS는 서버 간 인증을 대신하지 않는다. 브라우저는 MPT origin을 직접 호출하지
  않고 Worker만 동일 Bearer 토큰으로 호출한다.

## 디스크 용량과 정리 정책

2026-08-24 기존 Mac 실측 기준으로 전체 설치본은 약 1.7 GiB였다. 주요 구성은 Git
이력 529 MiB, Python 가상환경 714 MiB, 번들 리소스 207 MiB, 현재 작업·자료 저장소
273 MiB다. Git 이력을 제외한 고정 checkout·가상환경·최소 자료 2개는 약 1 GiB를
예상한다. 기존 설치를 제자리 갱신해 중복 checkout과 가상환경을 만들지 않는 것이
원칙이다.

8.5초 Lounge 대표 렌더의 작업 폴더는 4.8 MiB였고, 기존 로컬 작업 5건은 작업당
5.9~82 MiB였다. 작업 중 `combined-1.mp4`와 `final-1.mp4`가 함께 존재하므로 완성본
크기의 약 2배 이상이 필요하다. Worker는 완성 MP4를 최대 25 MiB로 제한하지만 FFmpeg
임시파일과 로그 여유까지 포함해 동시 작업 1건당 150 MiB를 계획한다.

- 최소: 설치용 약 1 GiB와 동시 작업 1건용 여유 1 GiB.
- 권장: 설치 후 빈 공간 5 GiB 이상, 디스크 압박이 해소될 때까지 동시 렌더 1건.
- `storage/local_videos`, `config.toml`, 가상환경과 현재 실행 중인 작업 폴더는 자동
  정리 대상이 아니다.
- 완료 작업은 Worker가 MP4를 받아 R2 저장·구조 검증을 끝낼 때까지 보존한다. 재조회와
  장애 분석을 위해 완료 시점부터 최소 24시간 유지한 뒤 정리 후보로만 분류한다.
- 실패·취소 작업도 Worker의 Build 예약 해제 확인과 마지막 수정 후 24시간이 모두 지난
  뒤 정리 후보가 된다. 경로·용량·작업 ID를 먼저 보고하며 자동 삭제하지 않는다.
- 잔여파일 회수는 새 작업 수신 중지 → Worker의 `confirmed` 또는 `released` 확인 → 해당
  `storage/tasks/{jobId}`가 비활성인지 확인 → 정확한 한 폴더만 별도 승인 후 정리 순서다.
  `storage` 전체, 자료 폴더, R2 객체와 Build 원장은 일괄 삭제하지 않는다.
