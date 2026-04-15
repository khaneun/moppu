from moppu.config import AppConfig, LLMConfig, load_app_config


def test_default_app_config_builds():
    cfg = AppConfig()
    assert cfg.llm.provider in {"openai", "anthropic", "google"}
    assert cfg.embeddings.chunk_size > cfg.embeddings.chunk_overlap


def test_llm_resolve_override():
    cfg = LLMConfig(provider="anthropic", model="claude-sonnet-4-6")
    name, params = cfg.resolved("openai")
    assert name == "openai"
    assert params["model"] == "claude-sonnet-4-6"  # falls back to top-level when no override


def test_load_app_config_missing_file(tmp_path):
    # Non-existent path should fall back to defaults instead of blowing up.
    cfg = load_app_config(tmp_path / "missing.yaml")
    assert isinstance(cfg, AppConfig)
