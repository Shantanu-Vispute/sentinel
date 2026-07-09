from digest.translator import needs_translation


def test_chinese_needs_translation():
    text = "PrismML把Qwen3.6压到4GB，27B模型塞进iPhone 加州理工学院数学家 Babak Hassibi 联合创立的 AI 实验室"
    assert needs_translation(text) is True


def test_russian_needs_translation():
    text = "Андрей Карпаты высказал про дизайн ИИ-моделей мысль, которую большинство упускает из виду."
    assert needs_translation(text) is True


def test_english_does_not_need_translation():
    text = "OpenAI released a new model today called GPT-5.6 with major improvements in reasoning."
    assert needs_translation(text) is False


def test_latin_heavy_text_with_foreign_brand_names_does_not_trigger():
    text = "Meta released Muse Spark 1.1 via the Meta Model API, opening it to developers on GitHub."
    assert needs_translation(text) is False


def test_mostly_non_latin_with_latin_brand_names_still_triggers():
    text = "Meta 发布最新多模态推理模型 Muse Spark 1.1，并开放 Meta Model API 公测。开发者现在可以直接调用新模型"
    assert needs_translation(text) is True


def test_empty_text_does_not_need_translation():
    assert needs_translation("") is False
    assert needs_translation("   ") is False


def test_url_only_text_does_not_need_translation():
    assert needs_translation("https://example.com/foo-bar-baz") is False


def test_punctuation_and_digits_only_does_not_need_translation():
    assert needs_translation("123 456 !!! ???") is False


def test_threshold_is_configurable():
    # ~10% non-Latin: default threshold (0.15) should not trigger, but a lower one should.
    text = "This is an English sentence with one Russian word: привет at the end of it."
    assert needs_translation(text) is False
    assert needs_translation(text, non_latin_ratio_threshold=0.5) is False
    assert needs_translation(text, non_latin_ratio_threshold=0.01) is True
