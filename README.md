# Heat to Cold

## Solar-Powered Smart Mini Cold Storage System

**Heat to Cold** is a physics-based, solar-powered mini cold-storage system designed for decentralized cold-chain access near farming communities in Northeast India. The project combines solar PV, battery storage, refrigeration, PCM thermal storage, humidity-aware cooling and constrained multi-objective optimization in one engineering workflow.

## Overview

The system is designed around a 5 MT cold room with site-specific weather inputs. The engineering model evaluates coupled thermal and electrical behavior, including warm-product loading, moisture control, refrigeration, PCM storage, PV generation, battery operation and system-level optimization.

### Main features

- Solar PV and battery energy system
- Vapor-compression refrigeration model
- Biochar-enhanced organic PCM thermal storage concept
- Temperature and relative-humidity control
- Produce cooling and storage-state modeling
- Predictive / priority-based cooling control
- Offline-first monitoring with GSM alert capability
- Physics-based transient simulation
- Constrained NSGA-II optimization

## System Workflow

```text
Site-specific weather + system requirements
                    ↓
          Physics-based model
                    ↓
        NSGA-II system optimization
                    ↓
            Sensitivity analysis
                    ↓
        Validation + final design
```

## Current Design Basis

| Parameter | Model basis |
|---|---:|
| Storage inventory | 5097 kg |
| Room dimensions | ~3.05 × 3.05 × 2.44 m |
| Incoming product | 30 °C |
| Daily turnover | 15% of inventory |
| Receiving window | 3.5 h |
| Room setpoint | 4 °C |
| RH hard band | 90–99% |
| Preferred RH upper limit | 98% |
| Transmission UA | 12.58 W/K |
| Infiltration sensible UA basis | 25.97 W/K |

These values are model inputs and sizing assumptions, not certified hardware specifications.

## Weather Data

The current design study uses site-specific PVGIS ERA5 weather data for the project design point:

- Latitude: 26.144° N
- Longitude: 91.736° E
- Weather year: 2023
- Expected hourly records: 8760

The frozen weather snapshot is stored at [`data/weather/pvgis_2023_raw.json`](data/weather/pvgis_2023_raw.json).

## Model Architecture

The code is split into engineering domains under `src/heat_to_cold/`:

| Module | Purpose |
|---|---|
| `weather.py` | PVGIS weather loading, caching and weather handling |
| `envelope.py` | Room geometry, envelope UA and infiltration calculations |
| `product.py` | Produce thermal state, respiration and transpiration behavior |
| `psychro.py` | Moist-air, humidity-ratio, dew-point and enthalpy calculations |
| `refrigeration.py` | Refrigeration-cycle and evaporator-side calculations |
| `pcm.py` | PCM enthalpy state, charge/discharge and thermal storage behavior |
| `pv_battery.py` | PV generation and battery electrical-state calculations |
| `control.py` | Cooling, dehumidification, PCM and electrical dispatch logic |
| `optimizer.py` | Coupled transient simulation, feasibility evaluation and NSGA-II |
| `analysis.py` | Result tables, summaries and output/figure generation |
| `cli.py` | Command-line entry points for smoke tests, diagnostics and optimization |
| `__init__.py` | Core configuration dataclasses, PCM library and shared model definitions |

The model is a sizing-level reduced-order simulation, not a CFD/FEM model or OEM-certified equipment-selection tool.

## Optimization

Constrained NSGA-II is used to evaluate coupled system designs rather than sizing each subsystem independently.

The optimization considers system behavior and constraints involving temperature, produce cooling, relative humidity, unmet electrical energy, battery SOC, PCM state, frost/defrost behavior and thermal-energy consistency.

The stored optimization study contains:

| Quantity | Value |
|---|---:|
| Unique evaluations | 1200 |
| Feasible evaluations | 863 |
| Pareto solutions | 55 |

A representative feasible point from the stored Pareto dataset is:

| Metric | Value |
|---|---:|
| PV | 14.00 kWp |
| Battery | 29.88 kWh |
| PCM | 817.68 kg |
| Compressor electrical energy | 5844.17 kWh |

These are optimization-study outputs and should not be treated as final hardware specifications.

## Results

The repository stores the numerical outputs and figures generated from the optimization study.

```text
results/
├── optimization/
│   ├── pareto_front.csv
│   ├── optimization_results.csv
│   ├── optimization_plot_summary.json
│   └── figures/
│       └── *.png
└── simulation/
    └── simulation_trace.csv
```

## Installation

Python 3.10 or newer is required.

### Install runtime dependencies

```bash
python -m pip install -r requirements.txt
```

### Install the package

```bash
python -m pip install -e .
```

### Install test dependencies

```bash
python -m pip install -e ".[test]"
```

The editable install exposes the `heat-to-cold` command and also makes `python -m heat_to_cold` available from the project environment.

## Running the Model

### Show the CLI options

```bash
heat-to-cold --help
```

or:

```bash
python -m heat_to_cold --help
```

### List PCM candidates

```bash
heat-to-cold --list-pcm-candidates
```

### Run a short synthetic-weather smoke simulation

This is the fastest way to verify that the coupled model starts and produces finite states without requiring the stored PVGIS weather file.

```bash
heat-to-cold --smoke-test --days 1
```

By default the smoke test uses synthetic weather. To exercise the PVGIS loading path as well:

```bash
heat-to-cold --smoke-test --smoke-pvgis --weather-cache data/weather/pvgis_2023_raw.json --days 1
```

### Run fixed real-physics diagnostics

```bash
heat-to-cold --diagnostic --weather-cache data/weather/pvgis_2023_raw.json --diagnostic-days 7 --diagnostic-dt-min 15
```

Add `--no-plots` when only the numerical diagnostics are required.

### Run the optimization

A production optimization uses the committed PVGIS weather cache and can be computationally expensive.

```bash
heat-to-cold \
  --weather-cache data/weather/pvgis_2023_raw.json \
  --days 365 \
  --pop-size 64 \
  --n-gen 12 \
  --optimizer nsga2
```

The optimizer writes generated outputs to the working directory from which it is run. For a clean run, execute it from a separate output directory and move or copy the selected outputs into `results/` as required.

## Tests

The test suite is divided by purpose so routine checks stay fast while expensive end-to-end optimization remains opt-in.

| Test | What it checks | Typical use |
|---|---|---|
| `tests/test_physics.py` | Psychrometrics, envelope UA, PV response and refrigeration first-law consistency | Check core equations and component physics |
| `tests/test_pcm.py` | PCM SOC, charge/discharge direction, energy bounds and available-energy limits | Check PCM state behavior |
| `tests/test_smoke.py` | Short coupled transient simulation, finite outputs, SOC bounds and energy-accounting fields | Check that the integrated model still runs |
| `tests/test_full_optimization.py` | End-to-end annual NSGA-II run using the committed PVGIS cache | Optional production-level regression check |

### Run the normal test suite

```bash
pytest -q
```

The full optimization test is skipped by default, so the normal suite does not launch a long annual NSGA-II run.

### Run the end-to-end optimization regression test

```bash
RUN_FULL_OPT=1 pytest -q tests/test_full_optimization.py -s
```

This test is intentionally expensive and requires the production dependencies plus the committed `data/weather/pvgis_2023_raw.json` weather cache.

## Documentation

- [`docs/system_architecture.md`](docs/system_architecture.md) — system-level architecture and subsystem relationships
- [`docs/thermodynamic_model.md`](docs/thermodynamic_model.md) — modeled thermal, moisture and energy domains
- [`docs/optimization_methodology.md`](docs/optimization_methodology.md) — optimization variables, objectives, constraints and fidelity strategy

## Repository Structure

```text
Heat-to-Cold/
├── src/
│   └── heat_to_cold/
│       ├── __init__.py
│       ├── __main__.py
│       ├── cli.py
│       ├── weather.py
│       ├── envelope.py
│       ├── product.py
│       ├── psychro.py
│       ├── refrigeration.py
│       ├── pcm.py
│       ├── pv_battery.py
│       ├── control.py
│       ├── optimizer.py
│       └── analysis.py
│
├── data/
│   └── weather/
│       └── pvgis_2023_raw.json
│
├── results/
│   ├── optimization/
│   │   ├── pareto_front.csv
│   │   ├── optimization_results.csv
│   │   ├── optimization_plot_summary.json
│   │   └── figures/
│   └── simulation/
│       └── simulation_trace.csv
│
├── docs/
│   ├── system_architecture.md
│   ├── thermodynamic_model.md
│   └── optimization_methodology.md
│
├── tests/
│   ├── test_physics.py
│   ├── test_pcm.py
│   ├── test_smoke.py
│   └── test_full_optimization.py
│
├── .gitignore
├── LICENSE
├── README.md
├── pyproject.toml
└── requirements.txt
```

## Scope and Limitations

This repository represents a sizing-level reduced-order engineering model and its generated study outputs. Final prototype development requires validation against measured refrigeration performance, PCM thermophysical properties, insulation and infiltration behavior, sensor measurements and electrical system performance.

## Team

**Heat to Cold**

Solar-powered smart mini cold storage for decentralized cold-chain access.

## License

MIT License. See [`LICENSE`](LICENSE).
