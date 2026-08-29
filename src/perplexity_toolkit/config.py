"""Configuration management for Perplexity Toolkit."""

from dataclasses import dataclass, field


@dataclass
class Config:
    """Global configuration."""

    # Browser driver
    webbridge_url: str = "http://127.0.0.1:10086/command"
    session_prefix: str = "perplexity"

    # Perplexity
    base_url: str = "https://www.perplexity.ai"

    # Timing
    page_load_wait: float = 4.0
    search_wait: float = 15.0
    deep_research_wait: float = 90.0
    model_council_wait: float = 25.0
    step_by_step_wait: float = 20.0
    mode_switch_wait: float = 1.0
    action_wait: float = 0.5

    # Batch
    batch_delay: float = 3.0
    batch_progress_file: str = ".batch_progress"

    def make_session(self, suffix: str = "search") -> str:
        """Generate a unique session name."""
        return f"{self.session_prefix}-{suffix}"


# Singleton default config
_default: Config = Config()


def get_config() -> Config:
    return _default


def set_config(**kwargs) -> Config:
    """Update default config and return it."""
    global _default
    for k, v in kwargs.items():
        if hasattr(_default, k):
            setattr(_default, k, v)
    return _default
