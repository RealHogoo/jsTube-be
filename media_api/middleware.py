from django.conf import settings
from django.http import HttpResponse


class SecurityHeaderMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        origin = request.headers.get("Origin", "")
        is_cors_origin = bool(origin and origin in settings.CORS_ORIGINS)
        if request.method == "OPTIONS" and is_cors_origin:
            response = HttpResponse(status=204)
        else:
            response = self.get_response(request)

        response["X-Content-Type-Options"] = "nosniff"
        response["X-Frame-Options"] = "DENY"
        response["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'wasm-unsafe-eval'; "
            "connect-src 'self' https://fonts.gstatic.com; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; "
            "media-src 'self' blob:; "
            "font-src 'self' data: https://fonts.gstatic.com; "
            "worker-src 'self' blob:; "
            "object-src 'none'; "
            "base-uri 'self'; "
            "form-action 'self'; "
            "frame-ancestors 'none'"
        )
        response["Referrer-Policy"] = "same-origin"
        response["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"

        if is_cors_origin:
            response["Access-Control-Allow-Origin"] = origin
            response["Access-Control-Allow-Credentials"] = "true"
            response["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
            response["Access-Control-Allow-Methods"] = "GET, POST, PATCH, OPTIONS"
            response["Vary"] = "Origin"
        return response
