from llm.check import setup_problem


def test_umich_correct():
    assert setup_problem("openai_compatible", "https://api.toolkit.umgpt.umich.edu/v1") is None
    assert setup_problem("openai_compatible", "https://api.toolkit.umgpt.umich.edu/v1/") is None


def test_umich_wrong_protocol():
    assert "openai_compatible" in setup_problem("openai_responses", "https://api.toolkit.umgpt.umich.edu/v1")


def test_umich_wrong_path():
    for path in ("", "/", "/v1/chat/completions", "/v1/v1"):
        assert setup_problem("openai_compatible", "https://api.toolkit.umgpt.umich.edu" + path)


def test_other_providers_unchanged():
    assert setup_problem("openai_responses", "https://api.openai.com/v1") is None
    assert setup_problem("anthropic", "") is None
