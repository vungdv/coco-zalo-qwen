"""OrderExtractor colleague: local Qwen (Ollama) prompt, day words and validation. Runs in-process."""

from zalo_order_assistant.order_extractor.extractor import ExtractorConfig, OrderExtractor
from zalo_order_assistant.order_extractor.llm import OllamaLlm
from zalo_order_assistant.ports import Mediator


def create_extractor(mediator: Mediator, config: ExtractorConfig) -> OrderExtractor:
    llm = OllamaLlm(config.ollama_model, config.ollama_url, config.timeout_seconds)
    return OrderExtractor(mediator, llm, max_days_ahead=config.max_days_ahead)


__all__ = ["ExtractorConfig", "OrderExtractor", "OllamaLlm", "create_extractor"]
