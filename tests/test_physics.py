import sys
from pathlib import Path
import math

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from heat_to_cold import MasterConfig, RefrigerationConfig
from heat_to_cold.psychro import (
    saturation_vapor_pressure_Pa,
    humidity_ratio_from_rh,
    rh_from_humidity_ratio,
    dew_point_C,
    moist_air_enthalpy_J_kg,
    moist_air_density_kg_m3,
)
from heat_to_cold.envelope import envelope_UA
from heat_to_cold.pv_battery import pv_dc_power_W
from heat_to_cold.refrigeration import solve_refrigeration_cycle, MockR290Backend, HAVE_COOLPROP


def test_saturation_pressure_increases_with_temperature():
    p4 = saturation_vapor_pressure_Pa(4.0)
    p10 = saturation_vapor_pressure_Pa(10.0)
    assert p4 > 0.0
    assert p10 > p4


def test_humidity_ratio_increases_with_rh():
    p = 101325.0
    w_low = humidity_ratio_from_rh(4.0, 90.0, p)
    w_high = humidity_ratio_from_rh(4.0, 98.0, p)
    assert w_high > w_low


def test_rh_round_trip():
    p = 101325.0
    w = humidity_ratio_from_rh(4.0, 95.0, p)
    rh = rh_from_humidity_ratio(4.0, w, p)
    assert math.isclose(rh, 95.0, rel_tol=0.0, abs_tol=1e-8)


def test_dew_point_is_below_air_temperature_for_subsaturated_air():
    p = 101325.0
    w = humidity_ratio_from_rh(4.0, 90.0, p)
    td = dew_point_C(w, p)
    assert td < 4.0


def test_moist_air_enthalpy_and_density_are_finite_positive():
    p = 101325.0
    w = humidity_ratio_from_rh(4.0, 95.0, p)
    h = moist_air_enthalpy_J_kg(4.0, w)
    rho = moist_air_density_kg_m3(4.0, w, p)
    assert math.isfinite(h)
    assert math.isfinite(rho)
    assert rho > 0.0


def test_envelope_ua_is_positive():
    cfg = MasterConfig()
    ua = envelope_UA(cfg)
    assert ua['above_grade_total'] > 0.0
    assert ua['floor'] > 0.0


def test_pv_power_is_zero_without_irradiance():
    cfg = MasterConfig()
    out = pv_dc_power_W(cfg.pv, 0.0, 25.0)
    assert out['P_pv_bus_W'] == 0.0


def test_pv_power_increases_with_irradiance():
    cfg = MasterConfig()
    low = pv_dc_power_W(cfg.pv, 200.0, 25.0)['P_pv_bus_W']
    high = pv_dc_power_W(cfg.pv, 800.0, 25.0)['P_pv_bus_W']
    assert high > low >= 0.0


def test_refrigeration_first_law_and_positive_cop():
    cfg = RefrigerationConfig(production_mode=False)
    if HAVE_COOLPROP:
        # Production path is preferred whenever CoolProp is installed.
        result = solve_refrigeration_cycle(cfg, 0.0, 40.0, 0.75, T_room_C=4.0, T_amb_C=30.0)
    else:
        result = solve_refrigeration_cycle(
            cfg, 0.0, 40.0, 0.75, backend=MockR290Backend(), T_room_C=4.0, T_amb_C=30.0
        )
    assert result.feasible
    assert result.Q_evap_W > 0.0
    assert result.W_elec_W > 0.0
    assert abs(result.first_law_residual_W) < 1e-6 * max(1.0, result.Q_cond_W)
    cop = result.Q_evap_delivered_W / result.W_elec_W if result.W_elec_W > 0 else 0.0
    assert cop > 0.0
