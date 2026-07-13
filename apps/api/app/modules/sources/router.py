from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.providers.registry import ProviderRegistry

router = APIRouter(prefix="/sources", tags=["sources"])


class SourceRead(BaseModel):
    name: str
    display_name: str
    capabilities: list[str]


@router.get("", response_model=list[SourceRead])
def list_sources(request: Request) -> list[SourceRead]:
    registry: ProviderRegistry = request.app.state.providers
    return [
        SourceRead(
            name=provider.name,
            display_name=provider.display_name,
            capabilities=sorted(capability.value for capability in provider.capabilities),
        )
        for provider in registry.all()
    ]
