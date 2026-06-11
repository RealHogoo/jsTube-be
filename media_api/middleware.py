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
        response["Referrer-Policy"] = "same-origin"
        response["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"

        if is_cors_origin:
            response["Access-Control-Allow-Origin"] = origin
            response["Access-Control-Allow-Credentials"] = "true"
            response["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
            response["Access-Control-Allow-Methods"] = "GET, POST, PATCH, OPTIONS"
            response["Vary"] = "Origin"
        return response
