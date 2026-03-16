# SQL Agent package
# Load .env from project root so all code sees the same env (OPENROUTER_*, etc.)
from pathlib import Path

from dotenv import load_dotenv

_env_path = Path(__file__).resolve().parents[1] / ".env"
load_dotenv(dotenv_path=_env_path)

from src.pipeline import AnalyticsPipeline, ConversationPipeline
from src.types import ConversationContext, ConversationTurn, PipelineOutput

__all__ = [
    "AnalyticsPipeline",
    "ConversationContext",
    "ConversationPipeline",
    "ConversationTurn",
    "PipelineOutput",
]
