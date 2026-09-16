import sys
from pathlib import Path
import math

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from heat_to_cold import MasterConfig
from heat_to_cold.weather import _synthetic_weather_year
from heat_to_cold.optimizer import simulate_transient
from heat_to_cold.refrigeration import HAVE_COOLPROP


def test_short_simulation_runs_and_returns_finite_trace():
    cfg = MasterConfig()
    cfg.pcm.candidate = 'BIOCHAR_CS_CA_LA_OA'
    # Keep smoke test independent of the optional production CoolProp install.
    if not HAVE_COOLPROP:
        cfg.refrig.production_mode = False
    weather = _synthetic_weather_year(cfg.site)

    trace = simulate_transient(
        cfg,
        weather,
        duration_days=0.5,
        dt_min=60.0,
        initial_room_C=cfg.control.room_setpoint_C,
        initial_product_C=cfg.control.room_setpoint_C,
        initial_rh_pct=cfg.control.rh_target_pct,
    )

    assert trace
    assert len(trace) == 12
    required = ('T_room_C', 'RH_pct', 'battery_soc', 'pcm_soc', 'bus_residual_W')
    for row in trace:
        for key in required:
            assert key in row
            assert math.isfinite(float(row[key]))
        assert 0.0 <= float(row['battery_soc']) <= 1.0
        assert 0.0 <= float(row['pcm_soc']) <= 1.0


def test_short_simulation_does_not_generate_nan_energy_accounting():
    cfg = MasterConfig()
    cfg.pcm.candidate = 'BIOCHAR_CS_CA_LA_OA'
    if not HAVE_COOLPROP:
        cfg.refrig.production_mode = False
    weather = _synthetic_weather_year(cfg.site)
    trace = simulate_transient(cfg, weather, duration_days=0.25, dt_min=60.0)
    assert trace
    for row in trace:
        for key in ('refrig_residual_W', 'pcm_energy_residual_W', 'bus_residual_W'):
            assert math.isfinite(float(row[key]))
