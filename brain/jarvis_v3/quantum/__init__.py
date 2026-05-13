"""Quantum-hybrid layer for JARVIS (Wave-9 → Wave-10 supercharged, 2026-04-30).

Public surface:

  * QuboProblem, simulated_annealing_solve  -- QUBO + SA solver
  * portfolio_allocation_qubo               -- Markowitz -> QUBO encoder
  * sizing_basket_qubo                      -- discrete sizing combo encoder
  * risk_parity_qubo                        -- equalize risk contribution
  * regime_aware_qubo                       -- regime-warped allocation
  * multi_horizon_qubo                      -- multi-timeframe optimization
  * hedging_basket_qubo                     -- optimal hedge selection
  * parallel_tempering_solve                -- replica-exchange MC solver
  * adaptive_solve                          -- auto-select best solver
  * select_top_signal_combination           -- tensor-network signal picker
  * QuantumCloudAdapter                     -- optional Qiskit/PennyLane bridge
  * QuantumOptimizerAgent                   -- firm-board pluggable agent

All modules are pure-stdlib by default. Cloud quantum capabilities
auto-activate ONLY when qiskit / pennylane / dwave-ocean-sdk are
importable; otherwise the adapter falls back transparently to the
classical QUBO solver.
"""

from eta_engine.brain.jarvis_v3.quantum.cloud_adapter import (
    QuantumBackend,
    QuantumCloudAdapter,
)
from eta_engine.brain.jarvis_v3.quantum.quantum_agent import QuantumOptimizerAgent
from eta_engine.brain.jarvis_v3.quantum.qubo_solver import (
    QuboProblem,
    portfolio_allocation_qubo,
    simulated_annealing_solve,
    sizing_basket_qubo,
)
from eta_engine.brain.jarvis_v3.quantum.qubo_supercharged import (
    adaptive_solve,
    hedging_basket_qubo,
    multi_horizon_qubo,
    parallel_tempering_solve,
    regime_aware_qubo,
    risk_parity_qubo,
)
from eta_engine.brain.jarvis_v3.quantum.tensor_network import (
    SignalScore,
    select_top_signal_combination,
)

__all__ = [
    "QuantumBackend",
    "QuantumCloudAdapter",
    "QuantumOptimizerAgent",
    "QuboProblem",
    "SignalScore",
    "adaptive_solve",
    "hedging_basket_qubo",
    "multi_horizon_qubo",
    "parallel_tempering_solve",
    "portfolio_allocation_qubo",
    "regime_aware_qubo",
    "risk_parity_qubo",
    "select_top_signal_combination",
    "simulated_annealing_solve",
    "sizing_basket_qubo",
]
