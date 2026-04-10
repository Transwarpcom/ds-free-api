"""OpenAI /v1/models endpoint."""

from fastapi import APIRouter

router = APIRouter()

MODEL_INFO = {
    "id": "deepseek-web-chat",
    "object": "model",
    "created": 1700000000,
    "owned_by": "deepseek",
}

MODEL_INFO_EXPERT = {
    "id": "deepseek-web-expert",
    "object": "model",
    "created": 1700000000,
    "owned_by": "deepseek",
}

MODEL_INFO_REASONER = {
    "id": "deepseek-web-reasoner",
    "object": "model",
    "created": 1700000000,
    "owned_by": "deepseek",
}

MODEL_INFO_EXPERT_REASONER = {
    "id": "deepseek-web-expert-reasoner",
    "object": "model",
    "created": 1700000000,
    "owned_by": "deepseek",
}


@router.get("/v1/models")
async def list_models():
    """Return list of available models."""
    return {
        "object": "list",
        "data": [MODEL_INFO, MODEL_INFO_EXPERT, MODEL_INFO_REASONER, MODEL_INFO_EXPERT_REASONER],
    }
