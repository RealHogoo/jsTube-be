# jsTube Backend

Django 기반 미디어 API 서버입니다.

## 역할

- 미디어 목록/검색 API
- 웹하드 내부 API 연동
- 유튜브 다운로드 작업
- 썸네일 생성/변경
- MongoDB 기반 미디어 메타데이터 관리
- 관리자 인증/권한 검증

## 실행

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python manage.py runserver 0.0.0.0:8084
```

## 주요 환경변수

`.env.example`을 참고해서 운영 환경변수를 설정합니다.

## 주요 API

- `GET /api/health/`
- `GET /api/me/`
- `POST /api/sync/`
- `GET /api/media/`
- `PATCH /api/media/<webhard_file_id>/`
- `POST /api/youtube/import/`
