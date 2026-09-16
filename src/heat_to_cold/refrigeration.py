from __future__ import annotations
import math
from dataclasses import dataclass
from . import CandidateInfeasible, RefrigerationConfig
try:
    from CoolProp.CoolProp import PropsSI
    HAVE_COOLPROP = True
except ImportError:
    PropsSI = None
    HAVE_COOLPROP = False
from .psychro import humidity_ratio_from_rh, moist_air_enthalpy_J_kg
CP_AIR = 1006.0

class MockR290Backend:
    """Deterministic reduced-order propane property mock. Test-only
    (Instructions W8) -- never substituted silently for CoolProp in production."""

    R_SPECIFIC = 188.5
    T_REF_K = 231.06  # normal boiling point of propane, K
    P_REF = 101325.0
    L_REF = 425000.0
    CP_VAPOR = 1650.0
    CP_LIQUID = 2500.0
    K_ISENTROPIC = 1.14

    def psat_Pa(self, T_C: float) -> float:
        T_K = T_C + 273.15
        return self.P_REF * math.exp(-self.L_REF / self.R_SPECIFIC * (1.0 / T_K - 1.0 / self.T_REF_K))

    def tsat_C(self, P_Pa: float) -> float:
        inv_T = 1.0 / self.T_REF_K - (self.R_SPECIFIC / self.L_REF) * math.log(P_Pa / self.P_REF)
        return (1.0 / inv_T) - 273.15

    def h_liquid(self, T_C: float) -> float:
        return self.CP_LIQUID * (T_C - (self.T_REF_K - 273.15))

    def h_vapor_sat(self, T_C: float) -> float:
        return self.h_liquid(T_C) + self.L_REF

    def h_vapor_superheated(self, T_C: float, P_Pa: float) -> float:
        t_sat = self.tsat_C(P_Pa)
        return self.h_vapor_sat(t_sat) + self.CP_VAPOR * (T_C - t_sat)

    def rho_vapor(self, T_C: float, P_Pa: float) -> float:
        T_K = T_C + 273.15
        return P_Pa / (self.R_SPECIFIC * T_K)

    def isentropic_temp_rise_C(self, T1_C: float, Pe_Pa: float, Pc_Pa: float) -> float:
        T1_K = T1_C + 273.15
        k = self.K_ISENTROPIC
        T2s_K = T1_K * (Pc_Pa / Pe_Pa) ** ((k - 1.0) / k)
        return T2s_K - 273.15


@dataclass
class RefrigerationResult:
    Q_evap_W: float          # thermodynamic cycle evaporator capacity
    Q_evap_delivered_W: float  # bounded by air-side/HX capacity
    Q_cond_W: float
    W_shaft_W: float
    W_elec_W: float
    mdot_kg_s: float
    T_evap_C: float
    T_cond_C: float
    first_law_residual_W: float
    condenser_adequate: bool
    feasible: bool


def solve_refrigeration_cycle(cfg: RefrigerationConfig, T_evap_C: float, T_cond_C: float,
                               speed_fraction: float, backend: Optional[MockR290Backend] = None,
                               T_room_C: Optional[float] = None,
                               T_amb_C: Optional[float] = None) -> RefrigerationResult:
    """Solve the vapor-compression cycle for given saturation temperatures
    and compressor speed fraction. Uses CoolProp in production; MockR290Backend
    only if CoolProp is unavailable (flagged upstream)."""
    if cfg.production_mode and not HAVE_COOLPROP:
        # W8: refuse to silently substitute the test-only mock in production.
        raise RuntimeError(
            "production_mode is set but CoolProp is unavailable; refusing to "
            "use the mock R290 backend for production refrigerant properties.")
    speed_fraction = max(0.0, min(1.0, speed_fraction))
    rpm = cfg.min_speed_rpm + speed_fraction * (cfg.max_speed_rpm - cfg.min_speed_rpm)

    T1_C = T_evap_C + cfg.suction_superheat_K
    T3_C = T_cond_C - cfg.liquid_subcooling_K

    if HAVE_COOLPROP:
        fluid = "Propane"
        Te_K, Tc_K, T1_K, T3_K = (T_evap_C + 273.15, T_cond_C + 273.15, T1_C + 273.15, T3_C + 273.15)
        Pe = PropsSI("P", "T", Te_K, "Q", 1, fluid)
        Pc = PropsSI("P", "T", Tc_K, "Q", 1, fluid)
        h1 = PropsSI("H", "T", T1_K, "P", Pe, fluid)
        s1 = PropsSI("S", "T", T1_K, "P", Pe, fluid)
        h2s = PropsSI("H", "P", Pc, "S", s1, fluid)
        h2 = h1 + (h2s - h1) / cfg.isentropic_efficiency
        h3 = PropsSI("H", "T", T3_K, "P", Pc, fluid)
        h4 = h3
        rho_suction = PropsSI("D", "T", T1_K, "P", Pe, fluid)
    else:
        be = backend or MockR290Backend()
        Pe = be.psat_Pa(T_evap_C)
        Pc = be.psat_Pa(T_cond_C)
        h1 = be.h_vapor_superheated(T1_C, Pe)
        T2s_C = be.isentropic_temp_rise_C(T1_C, Pe, Pc)
        h2s = be.h_vapor_superheated(T2s_C, Pc)
        h2 = h1 + (h2s - h1) / cfg.isentropic_efficiency
        h3 = be.h_liquid(T3_C)
        h4 = h3
        rho_suction = be.rho_vapor(T1_C, Pe)

    mdot = rho_suction * cfg.compressor_displacement_m3_rev * (rpm / 60.0) * cfg.volumetric_efficiency

    Q_evap = mdot * (h1 - h4)
    Q_cond = mdot * (h2 - h3)
    W_shaft = mdot * (h2 - h1)
    W_elec = W_shaft / (cfg.motor_efficiency * cfg.drive_efficiency)
    residual = Q_cond - Q_evap - W_shaft

    # Air-side / heat-exchanger bound on evaporator capacity. Actual cooling
    # delivered is the min of the thermodynamic cycle capacity and what the
    # evaporator UA can transfer at the current room/evap temperature split
    # (Instructions Chunk 06: "Actual evaporator capacity is bounded by the
    # minimum of thermodynamic cycle capacity, heat-exchanger/air-side capacity").
    if T_room_C is not None:
        q_evap_airside = max(0.0, cfg.evaporator_UA_W_K * (T_room_C - T_evap_C))
        Q_evap_delivered = min(Q_evap, q_evap_airside)
    else:
        Q_evap_delivered = Q_evap

    # Condenser adequacy: can the condenser reject the required heat within
    # its UA and approach at the ambient temperature?
    if T_amb_C is not None:
        q_cond_capacity = max(0.0, cfg.condenser_UA_W_K * (T_cond_C - T_amb_C))
        condenser_adequate = q_cond_capacity >= Q_cond - 1e-6
    else:
        condenser_adequate = True

    feasible = ((Q_evap > 0.0) and (W_elec > 0.0) and math.isfinite(W_elec)
                and math.isfinite(Q_evap) and condenser_adequate)

    return RefrigerationResult(Q_evap, Q_evap_delivered, Q_cond, W_shaft, W_elec, mdot,
                               T_evap_C, T_cond_C, residual, condenser_adequate, feasible)


def evaporator_wet_coil(cfg: RefrigerationConfig, air_mass_flow_kg_s: float, T_air_in_C: float,
                         w_air_in: float, P_Pa: float, T_evap_C: float) -> dict:
    """Bypass-factor wet-coil approximation: leaving conditions blend
    entering air with saturated-at-coil-surface air."""
    bf = cfg.bypass_factor
    T_adp = T_evap_C  # apparatus dew point approximated at evaporating temperature
    w_adp = humidity_ratio_from_rh(T_adp, 100.0, P_Pa)
    T_out = T_adp + bf * (T_air_in_C - T_adp)
    w_out = w_adp + bf * (w_air_in - w_adp)
    w_out = min(w_out, w_air_in)

    h_in = moist_air_enthalpy_J_kg(T_air_in_C, w_air_in)
    h_out = moist_air_enthalpy_J_kg(T_out, w_out)
    q_total = air_mass_flow_kg_s * (h_in - h_out)
    q_sens = air_mass_flow_kg_s * CP_AIR * (T_air_in_C - T_out)
    q_lat = max(0.0, q_total - q_sens)
    condensate_kg_s = max(0.0, air_mass_flow_kg_s * (w_air_in - w_out))

    return {"T_out_C": T_out, "w_out": w_out, "Q_sens_W": q_sens, "Q_lat_W": q_lat,
            "Q_total_W": q_total, "condensate_kg_s": condensate_kg_s}


def evaporator_airside_limit(cfg: RefrigerationConfig, air_mass_flow_kg_s: float,
                              T_air_in_C: float, w_air_in: float, P_Pa: float,
                              T_evap_C: float) -> dict:
    """Return a single coherent reduced-order evaporator air-side limit.

    The refrigerant side supplies an upper bound; this routine supplies the
    air-side total heat-transfer limit and its sensible/latent split.
    """
    coil = evaporator_wet_coil(
        cfg, air_mass_flow_kg_s, T_air_in_C, w_air_in, P_Pa, T_evap_C
    )
    q_ua = max(0.0, cfg.evaporator_UA_W_K * (T_air_in_C - T_evap_C))
    q_total_limit = min(max(0.0, coil["Q_total_W"]), q_ua)
    if coil["Q_total_W"] > 1e-9:
        scale = q_total_limit / coil["Q_total_W"]
    else:
        scale = 0.0
    return {
        "Q_total_limit_W": q_total_limit,
        "Q_sens_limit_W": max(0.0, coil["Q_sens_W"] * scale),
        "Q_lat_limit_W": max(0.0, coil["Q_lat_W"] * scale),
        "condensate_limit_kg_s": max(0.0, coil["condensate_kg_s"] * scale),
        "coil": coil,
    }


# =====================================================================
# CHUNK 07 -- PCM Thermal Storage
# =====================================================================

