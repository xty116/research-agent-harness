import os
import re


def safe_error(exc: Exception) -> str:
    text = str(exc)
    for key in ("DEEPSEEK_API_KEY", "TAVILY_API_KEY", "GITHUB_TOKEN", "OPENALEX_API_KEY", "HF_TOKEN"):
        value = os.environ.get(key)
        if value and len(value) > 5:
            text = text.replace(value, "[REDACTED]")
    text = re.sub(r"(?i)(api_key|token|authorization|password)=([^&\s]+)", r"\1=[REDACTED]", text)
    return f"{type(exc).__name__}: {text[:240]}"
