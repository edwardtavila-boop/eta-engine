"""Regime Detector — scheduled task runner.
Called by ETA-RegimeDetector scheduled task every 10 minutes.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from eta_engine.feeds.regime_detector.detector import CrossAssetRegimeDetector

BAR_DIR = Path("C:/EvolutionaryTradingAlgo/data")
OUTPUT = Path("C:/EvolutionaryTradingAlgo/var/eta_engine/state/jarvis_intel/regime_state.json")

detector = CrossAssetRegimeDetector(bar_dir=BAR_DIR, output_path=OUTPUT)
state = detector.run()
print(f"Regime: {state.primary_regime} conf={state.confidence}")
