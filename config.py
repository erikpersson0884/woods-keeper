import os
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv()

PORTAINER_URL = os.environ.get("PORTAINER_URL", "https://localhost:9000")
API_KEY = os.environ.get("API_KEY", "")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
SECRET_KEY = os.environ.get("SECRET_KEY")
ALLOW_INSECURE_PORTAINER_HTTP = (
    os.environ.get("ALLOW_INSECURE_PORTAINER_HTTP", "").lower() == "true"
)
PORT = int(os.environ.get("PORT", 5000))


def validate_portainer_url():
    parsed_url = urlparse(PORTAINER_URL)
    allowed_schemes = {"https"}
    if ALLOW_INSECURE_PORTAINER_HTTP:
        allowed_schemes.add("http")
    if parsed_url.scheme not in allowed_schemes or not parsed_url.netloc:
        required_scheme = "HTTP or HTTPS" if ALLOW_INSECURE_PORTAINER_HTTP else "HTTPS"
        raise RuntimeError(
            f"PORTAINER_URL must be an absolute {required_scheme} URL"
        )
