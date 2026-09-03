from sigsummerrise.config import Settings
from sigsummerrise.db import Database
from sigsummerrise.runtime import resolve_llm_config, resolve_prompts, save_runtime_config


def test_runtime_model_override(tmp_db: Database):
    settings = Settings(openrouter_model="env-model", db_key="k")
    save_runtime_config(tmp_db, {"openrouter_model": "db-model"})
    assert resolve_llm_config(settings, tmp_db).openrouter_model == "db-model"


def test_api_key_blank_save_keeps_existing(tmp_db: Database):
    settings = Settings(openrouter_api_key="env-key-1234", db_key="k")
    save_runtime_config(tmp_db, {"openrouter_api_key": "db-key-5678"})
    save_runtime_config(tmp_db, {})
    cfg = resolve_llm_config(settings, tmp_db)
    assert cfg.openrouter_api_key == "db-key-5678"
    assert cfg.api_key_suffix == "5678"


def test_prompt_db_override(tmp_db: Database, settings):
    save_runtime_config(
        tmp_db,
        {
            "summarize_system": "custom summarize {bot_name} {current_time} {group_name}",
            "followup_system": "custom followup {bot_name} {current_time} {group_name}",
            "ask_system": "custom ask {bot_name} {current_time} {group_name}",
        },
    )
    prompts = resolve_prompts(settings, tmp_db)
    assert prompts.summarize_system.startswith("custom summarize")


def test_clear_prompts_resets_to_file(tmp_db: Database, settings):
    save_runtime_config(tmp_db, {"summarize_system": "only this"})
    save_runtime_config(tmp_db, {}, clear_prompts=True)
    prompts = resolve_prompts(settings, tmp_db)
    assert "Signal group" in prompts.summarize_system or "{group_name}" in prompts.summarize_system
