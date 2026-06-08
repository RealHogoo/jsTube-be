# 미디어 백엔드 설계 메모

## 목표

웹하드에 저장된 이미지/영상 파일을 미디어 서비스에서 조회, 검색, 재생할 수 있도록 MongoDB 메타데이터와 웹하드 내부 API를 연동한다.

## 주요 구성

- Django JSON API
- MongoDB `media_items` 컬렉션
- 웹하드 내부 API 연동
- 관리자 서비스 JWT 인증/권한 확인
- 유튜브 다운로드와 웹하드 저장 연동
- 썸네일 생성/변경

## 동기화

`POST /api/sync/`는 웹하드의 활성 이미지/영상 파일을 읽어 미디어 MongoDB 문서로 upsert한다.

동기화 대상:

- `content_kind IN ('IMAGE', 'VIDEO')`
- 웹하드에서 삭제되지 않은 파일
- 일반 사용자는 본인 파일
- 관리자는 접근 가능한 파일 전체

주요 필드:

- `webhard_file_id`
- `owner_user_id`
- `owner_is_admin`
- `file_name`
- `display_name`
- `file_size`
- `content_type`
- `content_kind`
- `thumbnail_url`
- `content_url`
- `download_url`
- `tags`
- `webhard_tags`
- `webhard_memo`
- `album`
- `synced_at`

## 권한

- 조회는 관리자 또는 읽기 가능한 미디어만 허용한다.
- 수정은 등록자 본인 또는 관리자만 허용한다.
- 웹하드 파일 접근은 웹하드 내부 API 권한 검증을 통과해야 한다.

## 운영 주의사항

- 기존 웹하드 태그를 검색에 반영하려면 미디어 동기화를 실행해야 한다.
- 원본 파일은 웹하드가 소유하고, 미디어 백엔드는 메타데이터와 프록시 URL만 관리한다.
- 유튜브 다운로드에는 `yt-dlp`와 `ffmpeg`가 필요하며, 백엔드에서 설치/상태 확인을 수행한다.
