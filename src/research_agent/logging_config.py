import logging
import os
from logging.handlers import RotatingFileHandler

from .settings import Settings


class RedactFilter(logging.Filter):
    def filter(self, record):
        message = record.getMessage()
        for name in ("DEEPSEEK_API_KEY", "TAVILY_API_KEY", "GITHUB_TOKEN", "OPENALEX_API_KEY", "HF_TOKEN"):
            secret = os.environ.get(name)
            if secret:
                message = message.replace(secret, "[REDACTED]")
        record.msg, record.args = message, ()
        return True


def configure_logging(settings: Settings):
    directory = settings.path(settings.data_dir) / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(directory / "service.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    handler.addFilter(RedactFilter())
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    for name in ("httpx", "httpcore", "huggingface_hub", "uvicorn.access"):
        logging.getLogger(name).setLevel(logging.WARNING)
