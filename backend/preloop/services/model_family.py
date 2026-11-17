"""Model family identity independent of aggregator routing prefixes."""


def is_deepseek_family(model: str) -> bool:
    """Recognize the DeepSeek namespace anywhere before the model name."""
    return "deepseek" in model.strip().lower().split("/")[:-1]
