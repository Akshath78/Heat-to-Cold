# Optimization Methodology

The integrated model uses constrained NSGA-II to size the major energy and thermal subsystems together. The optimization evaluates candidate designs through the coupled thermal, refrigeration, PCM, PV and battery model rather than sizing each subsystem independently.

## Decision variables

The current optimizer varies the main system sizing variables for PV capacity, battery capacity, PCM mass and refrigeration-side sizing. The exact design-variable definitions and bounds are implemented in [`../src/heat_to_cold/optimizer.py`](../src/heat_to_cold/optimizer.py).

## Constraints

Candidate designs are screened against room and produce temperature limits, incoming-product cooling, relative humidity, unmet electrical energy, battery SOC and cycling, PCM state and cycling, frost/defrost behavior, thermal-energy residuals and condensation/dew-point margins.

## Fidelity strategy

The optimization stage uses a reduced-resolution transient simulation for candidate screening. Feasible candidates are then re-evaluated at higher resolution using the validation settings implemented by the optimizer.

## Outputs

The generated optimization outputs are stored under [`../results/optimization/`](../results/optimization/), including Pareto data, evaluation tables, summary metadata and generated figures.
