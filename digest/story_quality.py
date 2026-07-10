import re
from dataclasses import dataclass

from digest.sender_signal import sender_signal_for_story


_WORD_RE = re.compile(r"[a-z0-9][a-z0-9.+#-]*", re.I)

_AI_RELEVANCE_RE = re.compile(
    r"\b("
    r"ai|artificial intelligence|agi|agentic|agents?|llms?|large language model|"
    r"machine learning|deep learning|neural|transformer|multimodal|foundation model|"
    r"openai|anthropic|claude|chatgpt|codex|gemini|deepmind|mistral|qwen|llama|"
    r"grok|hugging ?face|nvidia|cuda|gpu|inference|fine-?tun|pre-?train|post-?train|"
    r"benchmark|evals?|rag|retrieval|embeddings?|vector|computer use|tool use|"
    r"voice|speech|asr|tts|ocr|robotics?|humanoid|world model|vision model|"
    r"data ?centers?|datacenters?|compute|model release|technical report"
    r")\b",
    re.I,
)

_STRONG_AI_RELEVANCE_RE = re.compile(
    r"\b("
    r"agentic|agents?|llms?|large language model|machine learning|deep learning|"
    r"neural|transformer|multimodal|foundation model|openai|anthropic|claude|"
    r"chatgpt|codex|gemini|deepmind|mistral|qwen|llama|grok|hugging ?face|"
    r"nvidia|cuda|gpu|inference|fine-?tun|pre-?train|post-?train|benchmark|"
    r"evals?|rag|retrieval|embeddings?|vector|computer use|tool use|voice|"
    r"speech|asr|tts|ocr|robotics?|humanoid|world model|vision model|model release|"
    r"technical report"
    r")\b",
    re.I,
)

_MATERIAL_NEWS_RE = re.compile(
    r"\b("
    r"security|vulnerabilit|cve|exploit|malware|breach|compromis|supply chain|"
    r"lawsuit|policy|regulat|executive order|government|funding|raises?|ipo|"
    r"valuation|acqui(?:res|red|sition)|shutdown|bankrupt|partnership|"
    r"technical report|benchmark|open-?source|research|paper|dataset"
    r")\b",
    re.I,
)

_DEV_LIBRARY_RE = re.compile(
    r"\b("
    r"javascript|typescript|node\.?js|npm|react(?: native)?|next\.?js|astro|"
    r"expo|tanstack|tailwind|css|html|vite|rspack|rsbuild|ember|fuse\.?js|"
    r"pglite|tinybase|eslint|oxlint|swift package manager|cocoapods|framework|"
    r"library|sdk"
    r")\b",
    re.I,
)

_RELEASE_NOISE_RE = re.compile(
    r"\b("
    r"released?|launch(?:es|ed)?|introduc(?:es|ed)|announc(?:es|ed)|adds?|"
    r"supports?|publish(?:es|ed)?|unveil(?:s|ed)?|ships?|shipped|rolls? out|"
    r"update|rfc|ported|version|v?\d+(?:\.\d+)+"
    r")\b",
    re.I,
)

_TUTORIAL_RE = re.compile(
    r"\b("
    r"tutorial|full course|course catalog|course listing|guide|how to|walkthrough|"
    r"template|prompt templates?|prompt lists?|5 prompts?|glossary|cheat sheet|"
    r"masterclass|workshop"
    r")\b",
    re.I,
)

_GENERAL_BUSINESS_RE = re.compile(
    r"\b("
    r"real estate|home prices?|housing|23andme|lithium extraction|underwater "
    r"connectivity|antenna system|gopro|sports?|celebrity|celebrities|"
    r"hollywood|hiring|job opening|freshers|internship|course catalog"
    r")\b",
    re.I,
)

_PODCAST_RE = re.compile(r"\b(podcast|episode|interview)\b", re.I)

_SHORT_SUMMARY_CHARS = 120
_VERY_SHORT_SUMMARY_CHARS = 80


@dataclass(frozen=True)
class StoryQuality:
    score: float
    reason: str
    should_skip_ingestion: bool

    @property
    def is_noise(self) -> bool:
        return bool(self.reason)


def _normalized_text(title: str, summary: str) -> str:
    return f"{title or ''} {summary or ''}".strip()


def _same_title_summary(title: str, summary: str) -> bool:
    title_words = " ".join(_WORD_RE.findall(title or "")).lower()
    summary_words = " ".join(_WORD_RE.findall(summary or "")).lower()
    return bool(title_words and title_words == summary_words)


def assess_story_quality(
    title: str,
    summary: str,
    *,
    source_type: str = "email",
    sender: str = "",
) -> StoryQuality:
    """Classify non-external digest stories for ingestion and ranking."""
    source_type = (source_type or "email").strip().lower()
    if source_type not in {"email", "telegram"}:
        return StoryQuality(score=0.0, reason="", should_skip_ingestion=False)

    text = _normalized_text(title, summary)
    text_l = text.lower()
    summary_l = (summary or "").strip().lower()
    summary_len = len((summary or "").strip())

    has_ai = bool(_AI_RELEVANCE_RE.search(text_l))
    has_strong_ai = bool(_STRONG_AI_RELEVANCE_RE.search(text_l))
    has_material_news = bool(_MATERIAL_NEWS_RE.search(text_l))
    has_dev_library = bool(_DEV_LIBRARY_RE.search(text_l))
    has_release_noise = bool(_RELEASE_NOISE_RE.search(text_l))
    sender_signal = sender_signal_for_story([sender], source_type=source_type)
    trusted_early_sender = sender_signal.has_trusted_ai_signal

    if not (title or "").strip() or not (summary or "").strip():
        return StoryQuality(score=-8.0, reason="empty_story", should_skip_ingestion=True)

    if _same_title_summary(title, summary):
        return StoryQuality(score=-6.0, reason="headline_only", should_skip_ingestion=True)

    if _TUTORIAL_RE.search(text_l) and not has_material_news:
        return StoryQuality(score=-4.0, reason="tutorial_or_course", should_skip_ingestion=True)

    if (
        has_dev_library
        and has_release_noise
        and not has_material_news
        and not (trusted_early_sender and has_strong_ai)
    ):
        return StoryQuality(score=-4.0, reason="library_release", should_skip_ingestion=True)

    if sender_signal.is_dev_lane_only and not has_material_news and not has_strong_ai:
        return StoryQuality(score=-4.0, reason="dev_lane_sender", should_skip_ingestion=True)

    if _PODCAST_RE.search(text_l) and not _AI_RELEVANCE_RE.search((title or "").lower()):
        return StoryQuality(score=-3.0, reason="podcast_or_interview", should_skip_ingestion=True)

    if _GENERAL_BUSINESS_RE.search(text_l) and not has_strong_ai:
        return StoryQuality(score=-3.5, reason="peripheral_or_non_ai", should_skip_ingestion=True)

    if summary_len < _VERY_SHORT_SUMMARY_CHARS:
        if trusted_early_sender and has_strong_ai and (has_material_news or has_release_noise):
            return StoryQuality(score=0.4 + sender_signal.score, reason="", should_skip_ingestion=False)
        return StoryQuality(score=-4.0, reason="very_short", should_skip_ingestion=True)

    if summary_len < _SHORT_SUMMARY_CHARS and not (has_strong_ai and has_material_news):
        if trusted_early_sender and has_strong_ai:
            return StoryQuality(score=0.2 + sender_signal.score, reason="", should_skip_ingestion=False)
        return StoryQuality(score=-2.5, reason="short_low_context", should_skip_ingestion=True)

    if not has_ai and not has_material_news:
        return StoryQuality(score=-3.0, reason="low_ai_relevance", should_skip_ingestion=True)

    score = 0.0
    if has_strong_ai:
        score += 1.4
    elif has_ai:
        score += 0.5
    if has_material_news:
        score += 1.0
    if summary_len >= 240:
        score += 0.4
    if has_dev_library and not has_material_news:
        score -= 1.2
    if summary_len < _SHORT_SUMMARY_CHARS:
        score -= 1.0
    if sender_signal.is_noisy_only and not has_material_news:
        score -= 0.8

    return StoryQuality(score=score, reason="", should_skip_ingestion=False)
