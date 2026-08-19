# DESIGN.md — 쇼츠 제작소

Gonggamtoon(공감툰)의 Apple White + Blue 작업대를 쇼츠 제작 화면에 옮긴 토큰 계약.
원본 MoneyPrinterTurbo의 4열 기술 패널 대신, 왼쪽 설정 / 오른쪽 결과의 한글 작업 화면을 쓴다.

## 1. Atmosphere

밝은 작업실. 흰 종이 위 파란 점 하나. 임원이 주제를 적고 바로 쇼츠를 만드는 화면이다.
장식 그라데이션, 보라빛 네온, 영문 대시보드 느낌은 쓰지 않는다.
정면은 한글 제목, 짧은 안내, 큰 만들기 버튼이다.

## 2. Color

- primary: #007AFF / --ggt-primary / 선택, 진행, 만들기 버튼
- primary-light: #EBF5FF / --ggt-primary-light / 선택 배경
- primary-dark: #0056CC / --ggt-primary-dark / 버튼 눌림
- accent: #5856D6 / --ggt-accent / 보조 강조
- success: #34C759 / --ggt-success / 완료
- warning: #FF9500 / --ggt-warning / 주의
- error: #FF3B30 / --ggt-error / 실패
- surface: #F5F5F7 / --ggt-surface / 페이지 배경
- card: #FFFFFF / --ggt-card / 설정 패널
- border: #D2D2D7 / --ggt-border / 구분선
- text: #1D1D1F / --ggt-text / 본문
- muted: #86868B / --ggt-muted / 보조 설명
- header: rgba(255,255,255,0.80) / --ggt-header / 상단 바

대비: 본문 #1D1D1F on #F5F5F7 / #FFFFFF 이상 4.5:1. 흰 글자 on #007AFF 이상 3:1.

## 3. Typography

- Stack: Noto Sans KR, -apple-system, BlinkMacSystemFont, system-ui, sans-serif
- Display / 브랜드: 28-32px / 700 / 1.15 / -0.4px
- Section title: 18px / 700 / 1.3
- Body: 16px / 400 / 1.55
- Caption: 13px / 500 / 1.4 / #86868B
- Button: 16px / 700

## 4. Spacing

Base 4px.

- --space-2 8px 칩 간격
- --space-3 12px 필드 간격
- --space-4 16px 카드 안쪽
- --space-5 20px 섹션 간격
- --space-6 24px 좌우 페이지 여백
- --space-8 32px 빈 화면 위아래
- page max: 1400px
- left settings: 약 420px
- phone frame: 9:16, 220x390

## 5. Components

- Header: sticky, blur, 하단 1px border, 높이 64px
- Card: 흰 배경, 1px border, radius 16px, shadow 0 2px 8px rgba(0,0,0,0.04)
- Primary button: #007AFF, 흰 글자, radius 12px, hover #0056CC, active scale 0.98
- Secondary button: surface 배경, border, muted 글자
- Input: radius 12px, inset shadow 0 1px 2px rgba(0,0,0,0.06)
- Stepper: 원형 번호, 완료 시 primary 채움
- Empty state: 중앙 정렬, 짧은 3단계 안내
- Phone preview: 9:16 프레임, 테두리 #D2D2D7, radius 24px

## 6. Motion

- fade-in 180ms ease-out, translateY(8px) to 0
- button press scale(0.98) 120ms
- stepper fill 400ms ease-out
- reduced-motion: transform 제거, opacity만

## 7. Depth

테두리 + 한 단계 소프트 그림자만 사용. 유리 재질, 보라 글로우, 큰 드롭섀도는 금지.

Do: 한글 먼저, 왼쪽 설정 / 오른쪽 결과, 파란 점 하나.
Don't: 4열 영문 패널, Inter, 보라 그라데이션, 가짜 대시보드 스크린샷.
