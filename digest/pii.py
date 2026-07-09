import re

from config import SENTINEL_PII_PATTERNS

_PII_PATTERNS = [
    re.compile(pattern.strip(), re.IGNORECASE)
    for pattern in SENTINEL_PII_PATTERNS
    if pattern.strip()
]

def scrub_pii(text: str) -> str:
    for pattern in _PII_PATTERNS:
        text = pattern.sub('[REDACTED]', text)
    return text
