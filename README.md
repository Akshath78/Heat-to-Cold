# Heat to Cold

## Solar-Powered Smart Mini Cold Storage System

> **Physics-based cold-storage sizing, optimization and prototype development for decentralized cold-chain access in Northeast India.**

Heat to Cold is a solar-powered mini cold-storage system designed around a 5 MT cold room. The project combines solar PV, battery storage, vapor-compression refrigeration, biochar-enhanced organic PCM thermal storage, humidity-aware cooling, predictive / priority-based control and constrained multi-objective optimization with a working embedded prototype.

## Project at a Glance

| | Design basis |
|---|---:|
| **Storage capacity** | 5 MT |
| **Cold-room size** | ~3.05 × 3.05 × 2.44 m |
| **Incoming produce** | 30 °C |
| **Room setpoint** | 4 °C |
| **Target RH** | 90–98% preferred operating band |
| **Daily turnover** | 15% of inventory |
| **Receiving window** | 3.5 h |
| **Weather basis** | PVGIS ERA5, Guwahati design point, 2023 |

The repository contains the modular simulation code, site weather input, optimization outputs, engineering documentation, automated tests and the embedded firmware used for the working prototype.

## Problem

Farmers in the North Eastern Region face cold-chain gaps near production clusters alongside unreliable power, difficult terrain, monsoon disruptions and transportation constraints. Limited local cold-storage access can shorten the marketable window of perishable produce and increase pressure to sell soon after harvest.

Heat to Cold addresses this system-level problem through a decentralized cold-storage architecture that couples thermal storage, renewable energy, refrigeration and intelligent control instead of treating cooling as an isolated refrigeration problem.

## Our Idea

A **Solar-Powered Smart Mini Cold Storage System** that integrates solar PV, battery storage, refrigeration and biochar-based organic PCM thermal storage with predictive control and offline monitoring to provide reliable and energy-efficient cooling for remote farming communities.

The system is designed to maintain cold-room conditions while managing warm-product loading, humidity, variable solar generation, stored thermal energy and electrical constraints as one coupled system.

## Key Features

- **Hybrid solar–battery energy system** for cooling during variable solar availability and low-generation periods
- **Biochar–organic PCM thermal storage** for cooling-energy storage and thermal buffering during compressor-off or low-energy periods
- **Microprocessor-based control** using temperature, humidity, energy and operating-state information
- **Predictive priority cooling** that prioritizes the storage or product condition becoming critical
- **Offline-first monitoring with GSM alerts** for environments where continuous internet connectivity cannot be assumed
- **Unified multi-objective optimization** across PV, battery, PCM, refrigeration and heat-transfer design variables
- **Physics-based transient simulation** with thermal and electrical balance checks
- **Working prototype firmware** connecting a DHT22 temperature/RH sensor to relay-controlled cooling

## Innovation / Uniqueness

### Biochar-Enhanced Organic PCM

Suitable agricultural residues can be converted into biochar and integrated with an organic PCM to form a thermal-storage material intended to improve heat-transfer capability while creating a higher-value use for agricultural residues.

### Predictive Priority Cooling

The control concept moves beyond a simple thermostat by using predicted storage conditions and available energy to determine where and when cooling demand should be prioritized.

### Integrated Energy–Thermal Optimization

PV, battery, biochar-PCM and refrigeration sizing are evaluated together under multiple performance and feasibility constraints rather than sizing each subsystem independently.

### Offline-First Intelligent Architecture

The system is designed around local operation and local monitoring, with GSM-based alerts for critical conditions so that internet access is not a prerequisite for basic monitoring.

### Modular Decentralized Cold Storage

The architecture is intended to be adaptable to different storage capacities and deployment conditions, including farm-level and production-cluster installations.

## System Workflow

```text
Regional cold-chain problem
             ↓
 Site + storage requirements
             ↓
      Physics-based model
             ↓
 Coupled thermal/electrical simulation
             ↓
      NSGA-II optimization
             ↓
      Sensitivity analysis
             ↓
 Higher-resolution validation
             ↓
       Final design study
             ↓
      Physical prototype
```

## System Architecture

```text
          PVGIS weather + design inputs
                       │
                       ▼
              Physics-based model
                       │
          ┌────────────┼────────────┐
          ▼            ▼            ▼
         PV         Battery        PCM
          └────────────┼────────────┘
                       ▼
                 Refrigeration
                       ▼
                   Cold room
                       │
             ┌─────────┴─────────┐
             ▼                   ▼
       Temperature / RH      Product state
             └─────────┬─────────┘
                       ▼
              Predictive / priority control
                       ▼
              Local monitoring + GSM alerts

        Physical prototype layer
                       │
             DHT22 temperature/RH
                       ↓
             Arduino-compatible MCU
                       ↓
              Relay-controlled cooling
```

## Impact & Benefits

The project is intended to increase the practical storage window of perishable produce and improve cold-chain resilience by placing smaller cooling units closer to production areas.

### Storage-Life Improvement

The following values are **optimum-storage references** carried from the project presentation. They illustrate how controlled storage conditions can change the potential marketable window of different vegetables; they are **not guaranteed performance values for the Heat to Cold prototype**.

| Crop | Typical life without optimum cold storage | With optimum cold storage |
|---|---:|---:|
| Spinach / leafy vegetables | 1–2 days | 1–3 weeks |
| Green chilli | 3–5 days | 2–3 weeks |
| Ripe tomato | 2–5 days | 2–14 days |
| Cabbage | 1–2 weeks | 3–6 months |

**Key message:** days → weeks → months of additional storage opportunity, depending on crop and maintained storage conditions.

### Regional Post-Harvest Impact Context

The project presentation uses the following **regional value-chain benchmark**:

```text
428,000 MT/year
regional value-chain volume
        ↓
     −10% loss
        ↓
42,800 MT/year
potential reduction in losses
        ↓
₹42.8 crore/year
estimated annual value saved
```

This benchmark is attributed in the project material to the NEDFi/NEC North Eastern Region action-plan context. It is **not a measured saving from one Heat to Cold unit**.

### Farmer & Social Impact

**Reduced distress selling**

Additional storage time can reduce the need to sell immediately after harvest when market conditions or transportation are unfavourable.

**Improved market flexibility**

A longer storage window can give farmers, FPOs and aggregators more flexibility in selecting buyers, markets and transportation opportunities.

**Decentralized cold access**

Mini cold-storage units can be positioned closer to production clusters, addressing the infrastructure gap targeted by the project.

**Resilience to regional constraints**

Local storage provides a buffer against monsoon disruptions, difficult terrain, poor connectivity and transportation delays.

### Economic Impact

**Preserve more → sell better → retain more value**

Reduced spoilage increases the portion of harvested produce that remains usable or marketable. A longer storage window also provides more flexibility in deciding when and where produce is sold.

The concept also supports local cold-chain services through deployment, operation, maintenance, aggregation and monitoring activities.

### Environmental Impact

**Agricultural residues → biochar → organic PCM**

The PCM concept provides a route for converting suitable agricultural residues into biochar and incorporating that material into thermal storage.

**Thermal buffering**

Stored thermal energy can help bridge periods when active refrigeration is reduced or solar availability is low.

**Reduced food waste**

Longer preservation can reduce avoidable losses of harvested food before it reaches consumers.

**Renewable-energy utilization**

Solar generation can reduce dependence on conventional electricity for decentralized refrigeration.

### Biochar–Organic PCM: Reported Research Performance

The following values are presented as **literature performance of the cited material study, not experimental performance of the complete Heat to Cold system**.

| Property | Reported value |
|---|---:|
| Transition temperature | 3.1 °C |
| Latent heat | 104.9 kJ/kg |
| Thermal conductivity | 1.853 W/m·K |
| Latent-heat retention after 500 cycles | >97.7% |
| 2–8 °C maintenance in reported refrigerator simulation | 560 min |

### SDG Alignment

- **SDG 2 – Zero Hunger:** reducing post-harvest losses helps preserve agricultural output and food availability
- **SDG 7 – Affordable and Clean Energy:** solar-powered refrigeration supports decentralized cooling with renewable-energy utilization
- **SDG 9 – Industry, Innovation and Infrastructure:** modular cold-storage infrastructure supports remote agricultural value chains
- **SDG 12 – Responsible Consumption and Production:** food-loss reduction and agricultural-residue valorization
- **SDG 13 – Climate Action:** renewable cooling, thermal storage and reduced food waste

## Working Prototype

The repository also contains the firmware used for the working cold-storage prototype under [`prototype/`](prototype/).

The prototype reads temperature and relative humidity from a DHT22 sensor and uses hysteresis-based relay control:

```text
Temperature ≥ 30 °C  →  Cooling ON
Temperature ≤ 28 °C  →  Cooling OFF
```

The relay is configured as **active LOW**, and sensor read failures are reported through the serial interface. The firmware is a working prototype controller and is separate from the higher-fidelity research simulation.

See [`prototype/cold_storage_prototype.ino`](prototype/cold_storage_prototype.ino) and [`prototype/README.md`](prototype/README.md) for the implementation and hardware interface details.

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
- [`prototype/README.md`](prototype/README.md) — working prototype firmware and hardware interface

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
├── prototype/
│   ├── cold_storage_prototype.ino
│   └── README.md
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

This repository represents a sizing-level reduced-order engineering model, generated optimization outputs and a working embedded prototype. Final prototype development requires validation against measured refrigeration performance, PCM thermophysical properties, insulation and infiltration behavior, sensor measurements and electrical system performance.

## Team

**Heat to Cold**

Solar-powered smart mini cold storage for decentralized cold-chain access.

## License

MIT License. See [`LICENSE`](LICENSE).
