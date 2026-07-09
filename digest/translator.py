import re
import unicodedata
from typing import Optional

import digest.llm_client as llm_client

_URL_RE = re.compile(r"https?://\S+")

_PROMPT = (
    "Translate the following text to English. Preserve the original line breaks, "
    "emoji, and any placeholders of the form [[URL1]], [[URL2]] exactly as they "
    "appear. Output only the translation.\n\nTEXT:\n{text}")

def needs_translation(
        text: str,
        non_latin_ratio_threshold: float = 0.15) -> bool:
    """True if enough alphabetic characters are outside the Latin script to warrant translation."""
    if not text or not text.strip():
        return False
    latin = 0
    non_latin = 0
    for ch in text:
        if not ch.isalpha():
            continue
        name = unicodedata.name(ch, "")
        if name.startswith("LATIN"):
            latin += 1
        else:
            non_latin += 1
    total = latin + non_latin
    if total == 0:
        return False
    return (non_latin / total) >= non_latin_ratio_threshold

def _mask_urls(text: str) -> tuple[str, list[str]]:
    urls: list[str] = []

    def _sub(match: re.Match) -> str:
        urls.append(match.group(0))
        return f"[[URL{len(urls)}]]"

    return _URL_RE.sub(_sub, text), urls

def _unmask_urls(text: str, urls: list[str]) -> str:
    for i, url in enumerate(urls, 1):
        text = text.replace(f"[[URL{i}]]", url)
    return text

def translate(text: str) -> Optional[str]:
    masked_text, urls = _mask_urls(text)
    try:
        response = llm_client.chat(
            messages=[{"role": "user", "content": _PROMPT.format(text=masked_text)}],
        )
        translated = (response.message.content or "").strip()
        if not translated:
            return None
        return _unmask_urls(translated, urls)
    except Exception as e:
        print(f"        WARN: translation failed: {e}")
        return None

if __name__ == "__main__":
    import sys

    sample = sys.stdin.read() if not sys.stdin.isatty() else (
        "Андрей Карпаты высказал про дизайн ИИ-моделей мысль, которую большинство "
        "упускает из виду. Подробнее: https://example.com/article?id=1")
    print(f"needs_translation: {needs_translation(sample)}")
    if needs_translation(sample):
        print(translate(sample))
