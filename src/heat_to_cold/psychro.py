from __future__ import annotations
import math
from . import CandidateInfeasible, CommodityConfig

def saturation_vapor_pressure_Pa(T_C: float) -> float:
    """Magnus-Tetens saturation pressure with a safe range guard."""
    if not math.isfinite(T_C) or T_C <= -80.0 or T_C >= 70.0:
        raise CandidateInfeasible("PSYCHROMETRIC_T_OUT_OF_RANGE", float("nan"), T_room_C=T_C)
    return 610.94 * math.exp((17.625*T_C)/(T_C+243.04))


def humidity_ratio_from_rh(T_C: float, rh_pct: float, P_Pa: float) -> float:
    if not math.isfinite(rh_pct) or rh_pct < 0.0 or rh_pct > 100.0:
        raise CandidateInfeasible("RH_INPUT_OUT_OF_RANGE", float("nan"), T_room_C=T_C)
    if not math.isfinite(P_Pa) or P_Pa <= 0.0:
        raise CandidateInfeasible("PRESSURE_INVALID", float("nan"), T_room_C=T_C)
    p_sat = saturation_vapor_pressure_Pa(T_C)
    p_v = min((rh_pct / 100.0) * p_sat, 0.999 * P_Pa)
    denom = P_Pa - p_v
    if denom <= 0.0 or not math.isfinite(denom):
        raise CandidateInfeasible("VAPOR_PRESSURE_INVALID", float("nan"), T_room_C=T_C)
    return 0.62198 * p_v / denom


def rh_from_humidity_ratio(T_C: float, w: float, P_Pa: float) -> float:
    if not math.isfinite(w) or w < 0.0:
        raise CandidateInfeasible("HUMIDITY_RATIO_INVALID", float("nan"), T_room_C=T_C)
    p_v = w * P_Pa / (0.62198 + w)
    p_sat = saturation_vapor_pressure_Pa(T_C)
    rh = 100.0 * p_v / p_sat
    if not math.isfinite(rh):
        raise CandidateInfeasible("RH_NONFINITE", float("nan"), T_room_C=T_C)
    return max(0.0, min(100.0, rh))


def dew_point_C(w: float, P_Pa: float) -> float:
    p_v = max(1e-6, w * P_Pa / (0.62198 + w))
    ln_ratio = math.log(p_v / 610.94)
    return (243.04 * ln_ratio) / (17.625 - ln_ratio)


def moist_air_enthalpy_J_kg(T_C: float, w: float) -> float:
    return 1006.0 * T_C + w * (2501000.0 + 1860.0 * T_C)


def moist_air_density_kg_m3(T_C: float, w: float, P_Pa: float) -> float:
    if not math.isfinite(T_C) or not math.isfinite(w) or not math.isfinite(P_Pa) or w < 0.0 or P_Pa <= 0.0:
        raise CandidateInfeasible("AIR_STATE_INVALID", float("nan"), T_room_C=T_C)
    T_K = T_C + 273.15
    if T_K <= 180.0:
        raise CandidateInfeasible("AIR_T_OUT_OF_RANGE", float("nan"), T_room_C=T_C)
    R_da = 287.055
    v = R_da * T_K / P_Pa * (1.0 + 1.6078 * w) / (1.0 + w)
    if not math.isfinite(v) or v <= 0.0:
        raise CandidateInfeasible("AIR_DENSITY_INVALID", float("nan"), T_room_C=T_C)
    rho = 1.0 / v
    if not math.isfinite(rho) or rho <= 0.0:
        raise CandidateInfeasible("AIR_DENSITY_INVALID", float("nan"), T_room_C=T_C)
    return rho


def respiration_heat_W(commodity: CommodityConfig, mass_kg: float, T_C: float) -> float:
    """Respiration heat evaluated at mean product temperature."""
    if not math.isfinite(T_C) or T_C < -5.0 or T_C > 20.0:
        raise CandidateInfeasible("RESPIRATION_T_OUT_OF_RANGE", float("nan"), T_room_C=T_C)
    tonnes=max(0.0,mass_kg)/1000.0
    return commodity.respiration_ref_W_per_tonne*tonnes*commodity.respiration_q10**((T_C-commodity.respiration_ref_temp_C)/10.0)


def transpiration_kg_s(commodity: CommodityConfig, mass_kg: float, T_room_C: float, rh_pct: float, P_Pa: float) -> float:
    p_sat = saturation_vapor_pressure_Pa(T_room_C)
    p_v = (rh_pct / 100.0) * p_sat
    vpd_kPa = max(0.0, (p_sat - p_v) / 1000.0)
    ratio = vpd_kPa / commodity.transpiration_ref_vpd_kPa if commodity.transpiration_ref_vpd_kPa > 0 else 1.0
    multiplier = min(commodity.transpiration_multiplier_cap, max(0.1, ratio))
    rate_kg_kg_day = commodity.transpiration_baseline_kg_kg_day * multiplier
    return mass_kg * rate_kg_kg_day / 86400.0


def infiltration_moisture_kg_s(v_dot_m3_s: float, rho_air: float, w_out: float, w_in: float) -> float:
    m_da_dot = v_dot_m3_s * rho_air
    return m_da_dot * (w_out - w_in)


# =====================================================================
# CHUNK 06 -- Vapor-Compression Refrigeration
# =====================================================================
