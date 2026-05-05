"""Quick validation — Claude CLI via npx (non-interactive)."""
import sys
sys.path.insert(0, r"C:\EvolutionaryTradingAlgo")
from eta_engine.brain.cli_provider import call_claude, check_claude_available

print(f"Claude available: {check_claude_available()}")

print("Calling Claude (sonnet) — short test...")
resp = call_claude(
    system_prompt="Reply with exactly one sentence. Be concise.",
    user_message="What is 2+2? Answer in one sentence.",
    model="sonnet",
    max_tokens=100,
    timeout=60,
)

print(f"  exit_code: {resp.exit_code}")
print(f"  elapsed: {resp.elapsed_ms:.0f}ms")
print(f"  text: {resp.text[:300]}")
