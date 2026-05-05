"""Live integration test — DeepSeek API via Force Multiplier orchestrator."""
import sys, logging
sys.path.insert(0, r"C:\EvolutionaryTradingAlgo")
logging.basicConfig(level=logging.DEBUG, format="%(name)s %(levelname)s: %(message)s")

from eta_engine.brain.multi_model import route_and_execute
from eta_engine.brain.model_policy import TaskCategory

print("Testing DeepSeek route (BOILERPLATE -> deepseek)...")
resp = route_and_execute(
    category=TaskCategory.BOILERPLATE,
    system_prompt="Reply concisely in one sentence.",
    user_message="What is 2+2?",
    max_tokens=100,
)

print(f"  provider: {resp.provider.value}")
print(f"  model: {resp.model}")
print(f"  text: {resp.text[:200]}")
print(f"  cost: USD {resp.cost_usd:.6f}")
print(f"  elapsed: {resp.elapsed_ms:.0f}ms")
print(f"  fallback: {resp.fallback_used}")
print()

print("Testing Claude route (ARCHITECTURE_DECISION -> claude)...")
resp2 = route_and_execute(
    category=TaskCategory.ARCHITECTURE_DECISION,
    system_prompt="You are an architect. Reply in one sentence.",
    user_message="Should we use Redis or PostgreSQL for caching?",
    max_tokens=200,
)

print(f"  provider: {resp2.provider.value}")
print(f"  model: {resp2.model}")
print(f"  text: {resp2.text[:300]}")
print(f"  elapsed: {resp2.elapsed_ms:.0f}ms")
print(f"  fallback: {resp2.fallback_used}")
print(f"  fallback_reason: {resp2.fallback_reason}")
