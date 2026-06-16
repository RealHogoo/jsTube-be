# jsTube Backend

Django 기반 튜브 서비스 미디어 API 서버입니다.

## 역할

- 미디어 목록/검색 API
- 웹하드 내부 API 연동
- 유튜브 다운로드 작업
- 썸네일 생성/변경
- MongoDB 기반 미디어 메타데이터 관리
- 관리자 인증/권한 검증
- `../jsTube-fe/build/web` Flutter Web 정적 파일 서빙

## 실행

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python manage.py runserver 0.0.0.0:8084
```

## FE 통합 반영

`http://localhost:8084` 루트 화면은 이 서버가 직접 만든 화면이 아니라 `../jsTube-fe/build/web` 산출물입니다. FE 수정 후 8084 화면에 반영하려면 FE 저장소에서 다음을 실행합니다.

```powershell
cd ..\jsTube-fe
.\scripts\build-web-local.ps1
```

`build/web`은 배포 산출물이며 git에 커밋하지 않습니다. 로컬에서 화면이 바뀌지 않으면 브라우저 캐시 또는 서비스워커를 지우고 새로고침합니다.

## 주요 환경변수

`.env.example`을 참고해서 운영 환경변수를 설정합니다.

## 주요 API

- `GET /api/health/`
- `GET /api/me/`
- `POST /api/sync/`
- `GET /api/media/`
- `GET /api/media/<webhard_file_id>/`
- `PATCH /api/media/<webhard_file_id>/`
- `POST /api/media/<webhard_file_id>/delete/`
- `GET /api/media/<webhard_file_id>/content-file/`
- `GET /api/media/<webhard_file_id>/thumbnail-file/`
- `POST /api/youtube/import/`

## 미디어 편집

`PATCH /api/media/<webhard_file_id>/`는 제목, 태그, 앨범, 설명, 채널명, 구독 여부를 수정합니다. 타임라인은 태그 배열에 `00:30 전주`, `01:12 후렴`처럼 시간 문자열을 포함해 저장하며, API 응답의 `time_markers`로 파싱되어 내려갑니다.

삭제는 `POST /api/media/<webhard_file_id>/delete/`를 사용합니다. 실제 파일 삭제는 웹하드 API 권한과 소유자/관리자 조건을 따릅니다.
