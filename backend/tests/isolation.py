from beework.config import settings


def disable_external_integrations() -> None:
    """Unit tests must never reach a real gateway, knowledge engine or tracer."""
    settings.litellm_base_url = ""
    settings.litellm_master_key = ""
    settings.ragflow_base_url = ""
    settings.ragflow_api_key = ""
    settings.langfuse_host = ""
    settings.langfuse_public_key = ""
    settings.langfuse_secret_key = ""
