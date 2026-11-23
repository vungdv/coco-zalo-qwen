"""OrderPublisher colleague: Supabase `orders` API client. Only this package knows about Supabase."""

from zalo_order_assistant.order_publisher.publisher import PublisherConfig, SupabaseOrderPublisher, build_payload
from zalo_order_assistant.ports import Mediator


def create_publisher(mediator: Mediator, config: PublisherConfig) -> SupabaseOrderPublisher:
    return SupabaseOrderPublisher(mediator, config)


__all__ = ["PublisherConfig", "SupabaseOrderPublisher", "build_payload", "create_publisher"]
