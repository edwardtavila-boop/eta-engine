"""Regime Detector — scheduled task runner.
Called by ETA-RegimeDetector scheduled task every 10 minutes.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from eta_engine.feeds.regime_detector.detector import CrossAssetRegimeDetector
from eta_engine.scripts import workspace_roots

BAR_DIR = workspace_roots.WORKSPACE_ROOT / "data"
OUTPUT = workspace_roots.ETA_REGIME_STATE_PATH

detector = CrossAssetRegimeDetector(bar_dir=BAR_DIR, output_path=OUTPUT)
state = detector.run()
print(f"Regime: {state.primary_regime} conf={state.confidence}")
