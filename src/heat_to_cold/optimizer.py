from __future__ import annotations
import copy
import json
import math
import os
import traceback
import warnings
from dataclasses import asdict
from typing import Optional
from concurrent.futures import ProcessPoolExecutor

from . import *
from .envelope import RHO_AIR_NOM, CP_AIR, envelope_UA, infiltration_flow_m3_s, internal_sensible_loads_W, room_thermal_coefficients
from .weather import WeatherRecord, interpolate_weather
from .product import ProductState
from .psychro import *
from .refrigeration import *
from .pcm import PCMState
from .pv_battery import *
from .control import *
from .control import (_battery_available_bus_power_W, _compressor_result_at_fraction, _fraction_for_condenser_capacity, _fraction_for_power, _fraction_for_evap_demand, _coherent_evaporator_airside)

DEBUG_MODE = False
EVAL_ERROR_COUNT = 0
LAST_NSGA2_HISTORY_ALL = []

try:
    import numpy as np
    HAVE_NUMPY = True
except ImportError:
    np = None
    HAVE_NUMPY = False

try:
    from pymoo.core.problem import Problem
    from pymoo.core.sampling import Sampling
    from pymoo.core.callback import Callback
    from pymoo.algorithms.moo.nsga2 import NSGA2
    from pymoo.optimize import minimize as pymoo_minimize
    from pymoo.operators.sampling.rnd import FloatRandomSampling
    HAVE_PYMOO = True
except ImportError:
    Problem = Sampling = Callback = NSGA2 = pymoo_minimize = FloatRandomSampling = None
    HAVE_PYMOO = False

try:
    from sklearn.gaussian_process import GaussianProcessRegressor
    from sklearn.gaussian_process.kernels import Matern, ConstantKernel, WhiteKernel
    from sklearn.preprocessing import StandardScaler
    HAVE_SKLEARN = True
except ImportError:
    GaussianProcessRegressor = None
    Matern = ConstantKernel = WhiteKernel = StandardScaler = None
    HAVE_SKLEARN = False

try:
    from scipy.stats import qmc
    HAVE_SCIPY_QMC = True
except ImportError:
    qmc = None
    HAVE_SCIPY_QMC = False

def simulate_transient(cfg: MasterConfig, weather: list[WeatherRecord], duration_days: float,
                        dt_min: float = 15.0, backend: Optional[MockR290Backend] = None,
                        initial_room_C: Optional[float] = None, initial_product_C: Optional[float] = None,
                        initial_rh_pct: Optional[float] = None) -> list[dict]:
    """Run the coupled reduced-order plant simulation.

    The existing room, product, PV, battery, controller, refrigerant and
    psychrometric equations are retained. The plant-level coupling is corrected
    so that:
      1. available electrical power limits compressor operation before cooling
         is delivered;
      2. PCM charging consumes the same evaporator capacity as any other load;
      3. the wet-coil sensible/latent split comes from one coherent air-side
         process rather than a separate moisture-only cooling calculation; and
      4. PCM state energy is bounded by the physical enthalpy range.
    """
    if duration_days <= 0.0:
        return []
    if dt_min <= 0.0:
        raise ValueError("dt_min must be positive")
    if not weather:
        raise ValueError("weather must contain at least one hourly record")

    state = initial_plant_state(cfg, initial_room_C, initial_product_C, initial_rh_pct)
    dt_s = dt_min * 60.0
    n_steps = int(duration_days * 24 * 60 / dt_min)
    results = []

    m_air = cfg.room.volume_m3 * RHO_AIR_NOM
    C_air = m_air * CP_AIR

    progress_stride = max(1, n_steps // 10)
    for step in range(n_steps):
        t_h = step * dt_min / 60.0
        if DEBUG_MODE and (step % progress_stride == 0 or step == n_steps - 1):
            print(f"[SIM] {100.0*(step+1)/max(n_steps,1):5.1f}% | t={t_h:8.1f} h", flush=True)
        w = interpolate_weather(weather, t_h, cfg.site.utc_offset_h)
        hour_of_day = t_h % 24.0
        is_loading = cfg.product.loading_start_h <= hour_of_day < (
            cfg.product.loading_start_h + cfg.product.loading_duration_h
        )
        daytime = is_daytime(w.poa_global_W_m2)

        if not math.isfinite(state.T_room_C):
            raise CandidateInfeasible("ROOM_T_NONFINITE", t_h, state.T_room_C, state.product.hottest_core_aged_C(t_h), state.battery.soc)
        if state.T_room_C > cfg.control.room_upper_C + 0.25:
            raise CandidateInfeasible("ROOM_T_LIMIT", t_h, state.T_room_C, state.product.hottest_core_aged_C(t_h), state.battery.soc, {
                "stage": "pre_step", "T_amb_C": w.T_amb_C if 'w' in locals() else float("nan"),
                "RH_pct": rh_from_humidity_ratio(state.T_room_C, state.w_room, cfg.psychro.atmospheric_pressure_Pa) if 'state' in locals() else float("nan"),
                "pcm_soc": state.pcm.soc(), "compressor_fraction": state.control.compressor_actual_fraction,
                "reason_detail": "room already above early-termination threshold at step start"})
        if state.product.hottest_core_aged_C(t_h) > cfg.control.product_upper_C + 0.50:
            raise CandidateInfeasible("AGED_PRODUCT_T_LIMIT", t_h, state.T_room_C, state.product.hottest_core_aged_C(t_h), state.battery.soc, {
                "stage": "pre_step",
                "RH_pct": rh_from_humidity_ratio(state.T_room_C, state.w_room, cfg.psychro.atmospheric_pressure_Pa),
                "pcm_soc": state.pcm.soc(),
                "compressor_commanded_fraction": state.control.compressor_actual_fraction,
                "compressor_running": state.control.compressor_running,
                "product_hottest_core_C": state.product.hottest_core_C(),
                "product_airflow_active": (state.product.hottest_core_C() > cfg.control.product_upper_C + 0.05),
                "reason_detail": "aged product has not met pull-down target within configured pull-down window"})

        dt_h_step = dt_min / 60.0
        dispatch_violation = state.product.remove_dispatch(t_h, dt_h_step)
        cap_violation = state.product.add_loading(t_h, dt_h_step)
        inventory_violation = bool(dispatch_violation or cap_violation)

        pv = pv_dc_power_W(cfg.pv, w.poa_global_W_m2, w.T_amb_C)
        P_pv_bus = pv["P_pv_bus_W"]

        rho_air = moist_air_density_kg_m3(
            state.T_room_C, state.w_room, cfg.psychro.atmospheric_pressure_Pa
        )
        rh_room = rh_from_humidity_ratio(
            state.T_room_C, state.w_room, cfg.psychro.atmospheric_pressure_Pa
        )
        m_da = rho_air * cfg.room.volume_m3 / (1.0 + state.w_room)

        # Product pull-down and humidity control both require air circulation
        # independently of the *previous* compressor state.  A dehumidification
        # request may start a stopped compressor in this same timestep; the
        # evaporator fan must therefore be on before the refrigeration solve so
        # that the wet-coil model has nonzero air flow and can remove moisture.
        product_pull_down_active = (
            state.product.hottest_core_C() > cfg.control.product_upper_C + 0.05
        )
        humidity_control_active = (
            rh_room >= cfg.control.rh_target_pct - 1.0
            or state.control.dehumidifying
            or rh_room >= cfg.control.dehum_on_pct
        )
        # ------------------------------------------------------------------
        # LOW-POWER FAN-ONLY MODE
        # ------------------------------------------------------------------
        # Fan-only is allowed only when the compressor cannot be powered after
        # preserving the battery reserve. It provides circulation/mixing only;
        # it is never counted as refrigeration or moisture removal.
        T_evap_C = min(
            state.T_room_C - cfg.refrig.minimum_evaporator_approach_K, -2.0
        )
        T_cond_C = max(
            w.T_amb_C + cfg.refrig.minimum_condenser_approach_K, 30.0
        )
        battery_available_pre_W = _battery_available_bus_power_W(cfg, state.battery, dt_s)
        nonfan_aux_W = internal_sensible_loads_W(cfg, is_loading, False) + (
            150.0 if state.control.humidifying else 0.0
        )
        min_comp_power_W = float('inf')
        compressor_power_possible = False
        try:
            r_min_comp = _compressor_result_at_fraction(
                cfg.refrig, T_evap_C, T_cond_C,
                max(cfg.control.compressor_min_fraction, 1e-9), backend,
                state.T_room_C, w.T_amb_C
            )
            if r_min_comp.W_elec_W > 0.0 and math.isfinite(r_min_comp.W_elec_W):
                min_comp_power_W = r_min_comp.W_elec_W
                compressor_power_possible = bool(r_min_comp.feasible)
        except Exception:
            # The normal dispatch solver below remains authoritative. The
            # pre-check must never convert a model calculation exception into
            # an undefined-variable failure.
            compressor_power_possible = False

        full_fan_budget_W = max(
            0.0, P_pv_bus + battery_available_pre_W - nonfan_aux_W
            - cfg.refrig.evaporator_fan_power_W
        )
        compressor_power_possible = bool(
            compressor_power_possible and
            full_fan_budget_W + 1e-9 >= min_comp_power_W
        )

        fan_requested = (
            daytime
            or state.control.compressor_running
            or product_pull_down_active
            or humidity_control_active
        )
        fan_only_mode = bool(
            fan_requested and (not daytime) and (not compressor_power_possible) and
            (product_pull_down_active or humidity_control_active or state.control.compressor_running)
        )
        fan_on = fan_requested
        fan_airflow = (
            cfg.refrig.evaporator_airflow_m3_h
            if fan_on and not fan_only_mode else
            (cfg.refrig.evaporator_airflow_m3_h *
             max(0.0, min(1.0, cfg.refrig.evaporator_fan_only_airflow_fraction))
             if fan_only_mode else 0.0)
        )
        product_heat_W, G_prod, src_prod = state.product.step_thermal(
            dt_s, state.T_room_C, fan_airflow
        )
        resp_W = respiration_heat_W(
            cfg.commodity, state.product.total_mass_kg, state.product.mean_core_C()
        )

        coeff = room_thermal_coefficients(
            cfg, w.T_amb_C, rho_air, is_loading, fan_on, resp_W,
            fan_only=fan_only_mode
        )
        G_total = coeff["G"] + G_prod
        Q_src_total = coeff["Q_src"] + src_prod
        cap_over_dt = C_air / dt_s
        denom = cap_over_dt + G_total
        gains_total_W = Q_src_total - G_total * state.T_room_C

        v_dot = coeff["infiltration_flow_m3_s"]
        w_out = humidity_ratio_from_rh(
            w.T_amb_C, cfg.infiltration.ambient_rh_for_infiltration_pct,
            cfg.psychro.atmospheric_pressure_Pa
        )
        rho_out = moist_air_density_kg_m3(
            w.T_amb_C, w_out, cfg.psychro.atmospheric_pressure_Pa
        )
        # Moisture infiltration is driven by the incoming outdoor dry-air mass flow.
        # Using room density here introduced a small but systematic psychrometric
        # inconsistency during hot/humid conditions.
        m_da_in = v_dot * rho_out / (1.0 + w_out)
        m_inf_moisture = m_da_in * (w_out - state.w_room)
        m_transp = transpiration_kg_s(
            cfg.commodity, state.product.total_mass_kg, state.T_room_C, rh_room,
            cfg.psychro.atmospheric_pressure_Pa
        )

        # Predict the auxiliary electrical load first. This load always has
        # priority over refrigeration because the plant cannot run the coil
        # without its fans/controllers.
        P_aux_W = internal_sensible_loads_W(cfg, is_loading, fan_on, fan_only=fan_only_mode) + (
            150.0 if state.control.humidifying else 0.0
        )

        controller_inputs = {
            "T_room_C": state.T_room_C,
            "T_product_hottest_core_C": state.product.hottest_core_C(),
            "rh_pct": rh_room,
            "pcm_soc": state.pcm.soc(),
            "battery_soc": state.battery.soc,
            "daytime": daytime,
            "pv_surplus_W": max(0.0, P_pv_bus - P_aux_W),
            "pcm_charge_cop": cfg.pcm.charge_cop,
            "pcm_max_charge_power_W": cfg.pcm.max_charge_power_W,
        }
        cmd = controller_step(cfg.control, state.control, dt_min, controller_inputs)

        # The original controller only decided compressor operation from room
        # cooling need. PCM charging is itself a legitimate refrigeration load,
        # so ensure a requested daytime charge can start the compressor.
        if cmd["pcm_charge_W"] > 0.0 and daytime and not cmd["emergency"]:
            cmd["compressor_fraction_command"] = max(
                cmd["compressor_fraction_command"], 1.0
            )

        airside = _coherent_evaporator_airside(
            cfg.refrig, rho_air, state.T_room_C, state.w_room,
            cfg.psychro.atmospheric_pressure_Pa, T_evap_C
        )
        q_room_total_limit_W = airside["Q_total_limit_W"]
        q_room_sens_limit_W = airside["Q_sens_limit_W"]

        # Required sensible cooling is only what is needed to hold the room at
        # the setpoint. High RH must NOT be implemented by deliberately cooling
        # the room toward the 2 C lower bound; humidity control is handled through
        # the wet-coil condensate demand calculated below.
        Q_cool_setpoint_W = max(
            0.0, cap_over_dt * state.T_room_C + Q_src_total
            - denom * cfg.control.room_setpoint_C
        )
        Q_room_sensible_target_W = min(q_room_sens_limit_W, Q_cool_setpoint_W)

        # Estimate moisture entering the room during this step and determine the
        # evaporator duty needed to bring RH back toward the control target. The
        # coil model maps total evaporator duty to condensate capacity, while the
        # room thermal equation applies only the associated sensible component.
        w_after_sources_target = state.w_room + (m_inf_moisture + m_transp) * dt_s / max(m_da, 1e-12)
        w_floor_target = humidity_ratio_from_rh(
            state.T_room_C, cfg.control.rh_target_pct, cfg.psychro.atmospheric_pressure_Pa
        )
        desired_condensate_kg_s = max(
            0.0, (w_after_sources_target - w_floor_target) * m_da / dt_s
        ) if dt_s > 0.0 else 0.0
        dehum_total_target_W = 0.0
        if cmd["dehum_need"] and airside["condensate_limit_kg_s"] > 1e-12:
            dehum_fraction = min(1.0, desired_condensate_kg_s / airside["condensate_limit_kg_s"])
            dehum_total_target_W = q_room_total_limit_W * dehum_fraction

        if q_room_total_limit_W > 1e-9 and q_room_sens_limit_W > 1e-9:
            sensible_fraction = q_room_sens_limit_W / q_room_total_limit_W
        else:
            sensible_fraction = 1.0

        # PCM is the designated nighttime sensible-cooling resource. Dispatch
        # it from the prospective room load itself, not from whether the room has
        # already drifted above setpoint. This lets PCM absorb ongoing night gains
        # while the room remains at 4 C.
        Q_pcm_cooling_W = 0.0
        pcm_result = None
        if (not daytime) and Q_room_sensible_target_W > 1e-9 and state.pcm.soc() > 0.0:
            q_pcm_request = min(
                Q_room_sensible_target_W,
                state.pcm.max_heat_into_W(state.T_room_C)
            )
            pcm_result = state.pcm.step(
                dt_s, state.T_room_C, q_pcm_request, w.T_amb_C
            )
            Q_pcm_cooling_W = max(0.0, pcm_result["Q_into_pcm_W"])
            Q_pcm_cooling_W = min(Q_pcm_cooling_W, Q_room_sensible_target_W)

        remaining_sensible_target_W = max(
            0.0, Q_room_sensible_target_W - Q_pcm_cooling_W
        )

        # PCM charging is an additional evaporator-side load. It can only use
        # refrigeration capacity that is left after the room coil's required
        # duty, and it is never allowed to occur while the PCM is discharging.
        pcm_charge_target_W = max(0.0, cmd["pcm_charge_W"]) if Q_pcm_cooling_W <= 1e-9 else 0.0
        if pcm_charge_target_W > 0.0:
            q_pcm_charge_cap = max(0.0, -state.pcm.max_heat_into_W(T_evap_C))
            pcm_charge_target_W = min(pcm_charge_target_W, q_pcm_charge_cap)

        if q_room_total_limit_W > 1e-9 and sensible_fraction > 1e-9:
            sensible_total_target_W = remaining_sensible_target_W / sensible_fraction
        else:
            sensible_total_target_W = 0.0

        # At night the compressor supplies only the residual sensible/latent
        # duty that PCM cannot cover. PCM is dispatched first; compressor cooling
        # is explicitly sized only for what remains.
        if (not daytime) and remaining_sensible_target_W > 1e-9:
            residual_evap_target_W = remaining_sensible_target_W / max(sensible_fraction, 1e-9)
            residual_fraction = _fraction_for_evap_demand(
                cfg.refrig, T_evap_C, T_cond_C, residual_evap_target_W,
                q_room_total_limit_W, 1.0, backend,
                state.T_room_C, w.T_amb_C
            )
            cmd["compressor_fraction_command"] = max(
                cmd["compressor_fraction_command"], residual_fraction
            )
        # Sensible and latent cooling are simultaneous evaporator duties.  The
        # previous implementation used max(sensible, latent), which allowed a
        # larger sensible requirement to silently replace the latent duty.  In
        # this hybrid system PCM may supply the sensible duty while the
        # refrigeration coil still has to supply the latent/dehumidification
        # duty, so the targets must be combined before dispatch.
        q_room_total_target_W = min(
            q_room_total_limit_W,
            sensible_total_target_W + dehum_total_target_W
        )

        command_fraction = max(
            0.0, min(1.0, cmd["compressor_fraction_command"])
        )
        # The controller command is allowed to START a stopped compressor.
        # The previous extra state.compressor_running gate made every legitimate
        # start request collapse to zero, so warm incoming produce could heat
        # indefinitely while the room controller still reported 4 C.
        compressor_requested = command_fraction > 0.0

        # A dehumidification request can require the compressor even when the
        # room's sensible target is already zero. In that case the existing
        # controller's minimum running command is retained, subject to electrical
        # availability, but the room sensible transfer remains capped above.
        required_total_evap_W = q_room_total_target_W + pcm_charge_target_W
        if compressor_requested and required_total_evap_W > 1e-9:
            f_thermal = _fraction_for_evap_demand(
                cfg.refrig, T_evap_C, T_cond_C, required_total_evap_W,
                q_room_total_limit_W + pcm_charge_target_W, command_fraction, backend,
                state.T_room_C, w.T_amb_C
            )
        elif compressor_requested and cmd["dehum_need"]:
            # Humidity-only operation requests the minimum compressor fraction;
            # the wet-coil latent target above may request more when necessary.
            f_thermal = max(0.0, cfg.control.compressor_min_fraction)
        else:
            f_thermal = 0.0

        # Electrical availability is a hard physical constraint. Auxiliary loads
        # are served first; only the remaining PV + battery reserve power can be
        # used by the compressor.
        battery_available_W = _battery_available_bus_power_W(cfg, state.battery, dt_s)
        thermal_power_budget_W = max(0.0, P_pv_bus + battery_available_W - P_aux_W)
        f_power = _fraction_for_power(
            cfg.refrig, T_evap_C, T_cond_C, thermal_power_budget_W,
            command_fraction, backend, state.T_room_C, w.T_amb_C
        )
        # Hard condenser capacity limit: back off compressor speed to the
        # highest physically rejectable fraction at the current T_cond/T_amb.
        f_condenser = _fraction_for_condenser_capacity(
            cfg.refrig, T_evap_C, T_cond_C, w.T_amb_C, command_fraction,
            backend, state.T_room_C
        )

        # ------------------------------------------------------------------
        # EMERGENCY PCM POWER-SHEDDING / DISPATCH
        # ------------------------------------------------------------------
        # If the compressor is power-starved during the daytime, do not let the
        # room simply drift above the 6 C limit while the PCM still contains
        # usable cooling energy.  The normal architecture remains PCM-first at
        # night; this is an emergency daytime-only backup path.
        #
        # We solve the amount of sensible duty that must be transferred to PCM
        # with a small bisection loop.  The goal is not to make the compressor
        # magically stronger: PCM removes part of the room sensible load, which
        # legitimately reduces the refrigeration/electrical requirement.
        emergency_pcm_used = False
        if (
            daytime
            and state.pcm.soc() > 1e-9
            and Q_room_sensible_target_W > 1e-9
            and f_power + 1e-9 < f_thermal
            and state.T_room_C >= cfg.control.room_setpoint_C
        ):
            pcm_power_cap = max(0.0, state.pcm.max_heat_into_W(state.T_room_C))
            q_pcm_emergency_max = min(Q_room_sensible_target_W, pcm_power_cap)

            if q_pcm_emergency_max > 1e-6:
                q_lo = 0.0
                q_hi = q_pcm_emergency_max

                # Re-evaluate the electrical limit as the PCM removes more
                # sensible room load.  A few iterations are sufficient because
                # the mapping is monotonic for this supervisory calculation.
                for _ in range(14):
                    q_try = 0.5 * (q_lo + q_hi)
                    remaining_try = max(0.0, Q_room_sensible_target_W - q_try)
                    if q_room_total_limit_W > 1e-9 and sensible_fraction > 1e-9:
                        sensible_total_try = remaining_try / sensible_fraction
                    else:
                        sensible_total_try = 0.0
                    total_try = min(
                        q_room_total_limit_W,
                        sensible_total_try + dehum_total_target_W
                    )
                    f_try_thermal = _fraction_for_evap_demand(
                        cfg.refrig, T_evap_C, T_cond_C, total_try,
                        q_room_total_limit_W + pcm_charge_target_W,
                        command_fraction, backend, state.T_room_C, w.T_amb_C
                    ) if total_try > 1e-9 else 0.0
                    f_try_power = _fraction_for_power(
                        cfg.refrig, T_evap_C, T_cond_C, thermal_power_budget_W,
                        command_fraction, backend, state.T_room_C, w.T_amb_C
                    )
                    if f_try_thermal <= f_try_power + 1e-9:
                        q_hi = q_try
                    else:
                        q_lo = q_try

                q_emergency = q_hi
                if q_emergency > 1e-6:
                    pcm_result = state.pcm.step(
                        dt_s, state.T_room_C, q_emergency, w.T_amb_C
                    )
                    Q_pcm_cooling_W = max(0.0, pcm_result["Q_into_pcm_W"])
                    Q_pcm_cooling_W = min(Q_pcm_cooling_W, Q_room_sensible_target_W)
                    remaining_sensible_target_W = max(
                        0.0, Q_room_sensible_target_W - Q_pcm_cooling_W
                    )
                    if q_room_total_limit_W > 1e-9 and sensible_fraction > 1e-9:
                        sensible_total_target_W = remaining_sensible_target_W / sensible_fraction
                    else:
                        sensible_total_target_W = 0.0
                    q_room_total_target_W = min(
                        q_room_total_limit_W,
                        sensible_total_target_W + dehum_total_target_W
                    )
                    required_total_evap_W = q_room_total_target_W + pcm_charge_target_W
                    f_thermal = _fraction_for_evap_demand(
                        cfg.refrig, T_evap_C, T_cond_C, required_total_evap_W,
                        q_room_total_limit_W + pcm_charge_target_W,
                        command_fraction, backend, state.T_room_C, w.T_amb_C
                    ) if required_total_evap_W > 1e-9 else 0.0
                    f_power = _fraction_for_power(
                        cfg.refrig, T_evap_C, T_cond_C, thermal_power_budget_W,
                        command_fraction, backend, state.T_room_C, w.T_amb_C
                    )
                    f_condenser = _fraction_for_condenser_capacity(
                        cfg.refrig, T_evap_C, T_cond_C, w.T_amb_C, command_fraction,
                        backend, state.T_room_C
                    )
                    emergency_pcm_used = Q_pcm_cooling_W > 1e-9

        actual_fraction = min(f_thermal, f_power, f_condenser, command_fraction)
        actual_fraction = max(0.0, actual_fraction)
        commanded_fraction = command_fraction

        refrig = _compressor_result_at_fraction(
            cfg.refrig, T_evap_C, T_cond_C, actual_fraction, backend,
            state.T_room_C, w.T_amb_C
        )
        condenser_capacity_W = max(0.0, cfg.refrig.condenser_UA_W_K * (T_cond_C - w.T_amb_C))
        condenser_margin_W = condenser_capacity_W - max(0.0, refrig.Q_cond_W)
        condenser_limited = bool(
            f_condenser + 1e-9 < min(f_thermal, f_power, command_fraction)
        )
        compressor_on = actual_fraction > 1e-9

        # Synchronize the controller state with the physically achievable
        # operating point after the PV/battery power limit is applied.
        state.control.compressor_actual_fraction = actual_fraction
        if compressor_on:
            state.control.compressor_running = True
            state.control.minutes_since_start = max(state.control.minutes_since_start, dt_min)
            state.control.minutes_since_stop = 0.0
        else:
            state.control.compressor_running = False
            state.control.minutes_since_stop = max(state.control.minutes_since_stop, dt_min)
            state.control.minutes_since_start = 0.0

        q_cycle_available_W = max(0.0, refrig.Q_evap_W)
        q_room_total_actual_W = min(
            q_room_total_target_W, q_room_total_limit_W, q_cycle_available_W
        )
        # Allocate actual coil duty conservatively to the requested sensible
        # target first, while preserving any remaining duty for latent removal.
        # This prevents PCM-supplied sensible cooling from suppressing the
        # compressor's independent dehumidification requirement.
        q_room_sensible_actual_W = min(
            remaining_sensible_target_W,
            max(0.0, q_room_total_actual_W * sensible_fraction)
        )
        if remaining_sensible_target_W > 1e-9 and sensible_fraction > 1e-9:
            q_room_total_for_sensible_W = q_room_sensible_actual_W / sensible_fraction
        else:
            q_room_total_for_sensible_W = 0.0
        q_room_latent_actual_W = max(
            0.0, q_room_total_actual_W - q_room_total_for_sensible_W
        )
        q_room_total_actual_W = min(
            q_room_total_actual_W,
            q_room_total_limit_W
        )

        # Any evaporator capacity left after the room coil can be used to charge
        # the PCM. This creates a true refrigeration-capacity competition between
        # room cooling and storage charging.
        q_pcm_charge_W = min(
            pcm_charge_target_W,
            max(0.0, q_cycle_available_W - q_room_total_actual_W),
            cfg.pcm.max_charge_power_W,
        )

        q_evap_used_W = q_room_total_actual_W + q_pcm_charge_W
        p_compressor_total_W = refrig.W_elec_W if compressor_on else 0.0
        if q_evap_used_W > 1e-9 and p_compressor_total_W > 0.0:
            p_pcm_charge_elec_W = (
                p_compressor_total_W * q_pcm_charge_W / q_evap_used_W
            )
            p_compressor_elec_W = max(
                0.0, p_compressor_total_W - p_pcm_charge_elec_W
            )
        else:
            p_pcm_charge_elec_W = 0.0
            p_compressor_elec_W = p_compressor_total_W

        Q_evap_delivered_W = q_room_total_actual_W

        # Apply PCM charging only after the compressor allocation has been solved.
        # Charging and discharging are mutually exclusive in this timestep.
        if q_pcm_charge_W > 0.0:
            pcm_result = state.pcm.step(
                dt_s, T_evap_C, -q_pcm_charge_W, w.T_amb_C
            )
            q_pcm_charge_W = max(0.0, -pcm_result["Q_into_pcm_W"])
        elif pcm_result is None:
            pcm_result = state.pcm.step(
                dt_s, state.T_room_C, 0.0, w.T_amb_C
            )

        # The final room sensible load is the sum of the two physical sensible
        # paths and is guaranteed not to exceed the setpoint/lower-bound target.
        Q_cooling_actual_W = min(
            Q_room_sensible_target_W,
            q_room_sensible_actual_W + Q_pcm_cooling_W
        )

        # Latent control was already included in q_room_total_target_W before
        # compressor/electrical dispatch. Do not recalculate a larger thermal
        # fraction after dispatch because that would not propagate through the
        # power limit. Actual moisture removal must be based on the actual coil
        # duty delivered this timestep.

        coil_scale = 0.0
        if q_room_total_limit_W > 1e-9:
            # Only cooling that was physically delivered can remove moisture.
            # Using the requested target here overestimated condensate whenever
            # PV/battery power limited the compressor.
            coil_scale = min(
                1.0,
                q_room_total_actual_W / q_room_total_limit_W
            )
        coil_cond_capacity_kg_s = max(
            0.0, airside["condensate_limit_kg_s"] * coil_scale
        )

        # Electrical bus solve uses the actual compressor demand. Keep two separate
        # shortage metrics: (1) critical room refrigeration service that the thermal
        # plant failed to deliver, and (2) total electrical bus shortage. Constraint
        # 3 uses only critical room refrigeration unmet energy; PCM charging is an
        # optional storage-building load and must not masquerade as unmet cooling.
        refrigeration_required_W = max(0.0, q_room_total_target_W)
        refrigeration_delivered_W = max(0.0, q_room_total_actual_W)
        refrigeration_unmet_W = max(0.0, refrigeration_required_W - refrigeration_delivered_W)
        P_load_W = P_aux_W + p_compressor_elec_W + p_pcm_charge_elec_W
        batt_request_W = P_pv_bus - P_load_W
        # Enforce the same supervisory reserve used during pre-dispatch inside
        # the battery state update. This prevents standby loss or finite-step
        # discharge from silently pushing SOC below the operational reserve.
        battery_floor_soc = max(cfg.battery.minimum_SOC, cfg.control.battery_reserve_SOC)
        batt_result = state.battery.step(dt_s, batt_request_W, floor_soc=battery_floor_soc)

        # Final plant-level reserve invariant.  The battery must never appear
        # below the supervisory operating floor in the plant trajectory, even if
        # a future change to BatteryState introduces a numerical drift.  We also
        # record whether this guard had to intervene so that such a condition is
        # visible during diagnostics rather than silently hidden.
        pre_guard_soc = float(state.battery.soc)
        if state.battery.soc < battery_floor_soc:
            state.battery.soc = battery_floor_soc
        batt_result["soc"] = float(state.battery.soc)
        batt_result["soc_was_hard_clamped"] = bool(
            pre_guard_soc < battery_floor_soc - 1e-12
        )

        bus = electrical_bus_balance(
            P_pv_bus, P_load_W, batt_result["P_bus_effect_W"]
        )

        # Room thermal update remains backward-Euler and uses ONLY sensible coil
        # cooling. Latent cooling is accounted through the humidity balance below.
        state.T_room_C = (
            cap_over_dt * state.T_room_C + Q_src_total - Q_cooling_actual_W
        ) / denom

        if not math.isfinite(state.T_room_C):
            raise CandidateInfeasible("ROOM_T_NONFINITE", t_h + dt_h_step, state.T_room_C, state.product.hottest_core_aged_C(t_h + dt_h_step), state.battery.soc)
        if state.T_room_C > cfg.control.room_upper_C + 0.25:
            details = {
                "stage": "post_step",
                "T_amb_C": w.T_amb_C,
                "RH_pct": rh_from_humidity_ratio(state.T_room_C, state.w_room, cfg.psychro.atmospheric_pressure_Pa),
                "T_evap_C": T_evap_C if 'T_evap_C' in locals() else float("nan"),
                "compressor_commanded_fraction": commanded_fraction if 'commanded_fraction' in locals() else float("nan"),
                "compressor_actual_fraction": actual_fraction if 'actual_fraction' in locals() else float("nan"),
                "compressor_fraction": actual_fraction if 'actual_fraction' in locals() else float("nan"),
                "compressor_rpm": (
                    0.0 if not ('actual_fraction' in locals()) or actual_fraction <= 1e-9 else
                    cfg.refrig.min_speed_rpm + actual_fraction *
                    (cfg.refrig.max_speed_rpm - cfg.refrig.min_speed_rpm)
                ),
                "Q_evap_delivered_W": Q_evap_delivered_W if 'Q_evap_delivered_W' in locals() else float("nan"),
                "Q_evap_cycle_W": q_cycle_available_W if 'q_cycle_available_W' in locals() else float("nan"),
                "Q_evap_airside_limit_W": q_room_total_limit_W if 'q_room_total_limit_W' in locals() else float("nan"),
                "Q_evap_sensible_W": q_room_sensible_actual_W if 'q_room_sensible_actual_W' in locals() else float("nan"),
                "Q_pcm_cooling_W": Q_pcm_cooling_W if 'Q_pcm_cooling_W' in locals() else float("nan"),
                "Q_pcm_charge_W": q_pcm_charge_W if 'q_pcm_charge_W' in locals() else float("nan"),
                "room_gains_W": gains_total_W if 'gains_total_W' in locals() else float("nan"),
                "product_heat_W": product_heat_W if 'product_heat_W' in locals() else float("nan"),
                "P_pv_bus_W": P_pv_bus if 'P_pv_bus' in locals() else float("nan"),
                "P_compressor_elec_W": p_compressor_elec_W if 'p_compressor_elec_W' in locals() else float("nan"),
                "P_pcm_charge_elec_W": p_pcm_charge_elec_W if 'p_pcm_charge_elec_W' in locals() else float("nan"),
                "battery_soc_after": batt_result["soc"] if 'batt_result' in locals() else state.battery.soc,
                "battery_floor_soc": battery_floor_soc if 'battery_floor_soc' in locals() else float("nan"),
                "battery_soc_hard_clamped": bool(batt_result.get("soc_was_hard_clamped", False)) if 'batt_result' in locals() else False,
                "pcm_soc_after": pcm_result["soc"] if isinstance(pcm_result, dict) and "soc" in pcm_result else state.pcm.soc(),
                "unmet_W": bus["P_unmet_W"] if 'bus' in locals() else float("nan"),
                "refrig_residual_W": refrig.first_law_residual_W if 'refrig' in locals() else float("nan"),
                "bus_residual_W": bus["bus_residual_W"] if 'bus' in locals() else float("nan"),
                "P_aux_W": P_aux_W if 'P_aux_W' in locals() else float("nan"),
                "thermal_power_budget_W": thermal_power_budget_W if 'thermal_power_budget_W' in locals() else float("nan"),
                "battery_available_above_reserve_W": battery_available_W if 'battery_available_W' in locals() else float("nan"),
                "dehum_total_target_W": dehum_total_target_W if 'dehum_total_target_W' in locals() else float("nan"),
                "latent_cooling_actual_W": q_room_latent_actual_W if 'q_room_latent_actual_W' in locals() else float("nan"),
                "desired_condensate_kg_s": desired_condensate_kg_s if 'desired_condensate_kg_s' in locals() else float("nan"),
            }
            raise CandidateInfeasible("ROOM_T_LIMIT", t_h + dt_h_step, state.T_room_C,
                                      state.product.hottest_core_aged_C(t_h + dt_h_step),
                                      state.battery.soc, details)

        # Moisture balance: infiltration + transpiration + humidifier, followed by
        # condensate removal from that same wet-coil operating point. Predict source
        # RH first so the moisture controller reacts within the same timestep.
        m_humidifier = 0.0
        w_source_only = state.w_room + (m_inf_moisture + m_transp) * dt_s / max(m_da, 1e-12)
        rh_source_only = rh_from_humidity_ratio(
            state.T_room_C, w_source_only, cfg.psychro.atmospheric_pressure_Pa
        )
        if cmd["humidify"] or rh_source_only < cfg.control.rh_lower_pct:
            w_target = humidity_ratio_from_rh(
                state.T_room_C, min(cfg.control.rh_target_pct, cfg.control.humid_off_pct),
                cfg.psychro.atmospheric_pressure_Pa
            )
            deficit_kg = max(0.0, (w_target - state.w_room) * m_da)
            humidifier_capacity = float(getattr(cfg.control, "humidifier_max_kg_h", 2.0)) / 3600.0
            m_humidifier = min(deficit_kg / dt_s, humidifier_capacity)

        m_sources = m_inf_moisture + m_transp + m_humidifier
        w_after_sources = (
            state.w_room + m_sources * dt_s / m_da if m_da > 0.0 else state.w_room
        )

        condensate_kg_s = 0.0
        if coil_cond_capacity_kg_s > 0.0:
            w_floor = humidity_ratio_from_rh(
                state.T_room_C, cfg.control.rh_target_pct,
                cfg.psychro.atmospheric_pressure_Pa
            )
            desired_removal = max(
                0.0, (w_after_sources - w_floor) * m_da / dt_s
            )
            condensate_kg_s = min(coil_cond_capacity_kg_s, desired_removal)

        w_new = (
            w_after_sources - condensate_kg_s * dt_s / m_da
            if m_da > 0.0 else w_after_sources
        )
        state.w_room = max(0.0, w_new)
        rh_check = rh_from_humidity_ratio(
            state.T_room_C, state.w_room, cfg.psychro.atmospheric_pressure_Pa
        )
        if rh_check > 100.0:
            w_sat = humidity_ratio_from_rh(
                state.T_room_C, 100.0, cfg.psychro.atmospheric_pressure_Pa
            )
            condensate_kg_s += max(
                0.0, (state.w_room - w_sat) * m_da / dt_s
            )
            state.w_room = w_sat

        state.t_h = t_h + dt_min / 60.0

        results.append({
            "t_h": state.t_h,
            "T_room_C": state.T_room_C,
            "RH_pct": rh_from_humidity_ratio(
                state.T_room_C, state.w_room, cfg.psychro.atmospheric_pressure_Pa
            ),
            "T_product_max_C": state.product.hottest_core_C(),
            "T_product_aged_max_C": state.product.hottest_core_aged_C(state.t_h),
            "inventory_kg": state.product.total_mass_kg,
            "P_pv_bus_W": P_pv_bus,
            "P_load_W": P_load_W,
            "P_compressor_elec_W": p_compressor_elec_W,
            "P_pcm_charge_elec_W": p_pcm_charge_elec_W,
            "P_compressor_total_W": p_compressor_total_W,
            "Q_evap_delivered_W": Q_evap_delivered_W,
            "Q_evap_used_W": q_evap_used_W,
            "Q_pcm_cooling_W": Q_pcm_cooling_W,
            "Q_pcm_charge_W": q_pcm_charge_W,
            "Q_evap_cycle_W": q_cycle_available_W,
            "Q_evap_airside_limit_W": q_room_total_limit_W,
            "Q_evap_sensible_W": q_room_sensible_actual_W,
            "Q_evap_latent_W": max(0.0, q_room_total_actual_W - q_room_sensible_actual_W),
            "room_gains_W": gains_total_W,
            "product_heat_W": product_heat_W,
            "condensate_kg_s": condensate_kg_s,
            "battery_soc": batt_result["soc"],
            "battery_floor_soc": battery_floor_soc,
            "battery_soc_hard_clamped": bool(batt_result.get("soc_was_hard_clamped", False)),
            "pcm_soc": pcm_result["soc"],
            "pcm_energy_residual_W": pcm_result["energy_residual_W"],
            "pcm_energy_clamped": pcm_result.get("energy_clamped", False),
            "unmet_W": bus["P_unmet_W"],
            "refrigeration_unmet_W": refrigeration_unmet_W,
            "electrical_unmet_W": bus["P_unmet_W"],
            "refrigeration_required_W": refrigeration_required_W,
            "refrigeration_delivered_W": refrigeration_delivered_W,
            "curtailment_W": bus["P_curtailment_W"],
            "refrig_residual_W": refrig.first_law_residual_W,
            "bus_residual_W": bus["bus_residual_W"],
            "condenser_adequate": refrig.condenser_adequate,
            "condenser_capacity_W": condenser_capacity_W,
            "condenser_margin_W": condenser_margin_W,
            "condenser_limited": condenser_limited,
            "inventory_violation": bool(cap_violation),
            "refrig_feasible": refrig.feasible,
            "refrig_cycle_hard_failure": bool(
                (not refrig.feasible) and actual_fraction > 1e-9
            ),
            "compressor_fraction_actual": actual_fraction,
            "thermal_power_budget_W": thermal_power_budget_W,
            "battery_available_above_reserve_W": battery_available_W,
            "fan_only_mode": bool(fan_only_mode),
            "fan_power_W": (cfg.refrig.evaporator_fan_only_power_W if fan_only_mode else cfg.refrig.evaporator_fan_power_W) if fan_on else 0.0,
            "fan_only_airflow_m3_h": fan_airflow if fan_only_mode else 0.0,
            "compressor_power_possible": bool(compressor_power_possible),
            "min_compressor_power_W": min_comp_power_W,
            "fan_energy_savings_W": (cfg.refrig.evaporator_fan_power_W - cfg.refrig.evaporator_fan_only_power_W) if fan_only_mode else 0.0,
            # Internal dispatch fractions retained for post-run bottleneck attribution.
            "_f_thermal": float(f_thermal),
            "_f_power": float(f_power),
            "_f_condenser": float(f_condenser),
            "compressor_commanded_fraction": float(commanded_fraction),
            "dispatch_limiting_cause": "UNCLASSIFIED",
            "daytime": bool(daytime),
            "night_room_sensible_demand_W": Q_room_sensible_target_W if not daytime else 0.0,
            "night_pcm_cooling_W": Q_pcm_cooling_W if not daytime else 0.0,
            "emergency_pcm_used": bool(emergency_pcm_used),
            "dispatch_violation": bool(dispatch_violation),
            "dispatch_removed_kg": state.product.total_removed_kg,
            "dispatch_shortfall_kg": state.product.total_dispatch_violation_kg,
        })
        results[-1]["dispatch_limiting_cause"] = _dispatch_limiting_cause(results[-1])

    return results


# =====================================================================
# CHUNK 11 -- Optimization Bridge
# =====================================================================

# ---------------------------------------------------------------------
# Purchasable / supplier-facing optimization variables
# ---------------------------------------------------------------------
# The optimizer no longer exposes internal compressor displacement or
# heat-exchanger UA as design variables. Those are internal physics/model
# parameters derived from the purchasable ratings below.
#
# These are intentionally bounded to a practical commercial 5-MT cold-store
# screening envelope rather than broad mathematical ranges.
# Final proper-NSGA-II study: cap battery at 35 kWh and expand PV to 25 kWp.
# The diagnostic matrix holds the refrigeration/PCM architecture fixed at the
# current best reference plant so the PV-vs-battery tradeoff is isolated.
# Battery reserve remains 20%.
DESIGN_VARIABLES = [
    ("pv_kWp", 4.0, 25.0),
    ("battery_kWh", 4.0, 35.0),
    ("pcm_kg", 300.0, 1000.0),
    ("compressor_capacity_kW", 3.0, 10.0),
    ("evaporator_capacity_kW", 4.0, 12.0),
    ("condenser_capacity_kW", 6.0, 20.0),
    ("evaporator_airflow_m3_h", 3000.0, 12000.0),
    ("pcm_hx_area_m2", 10.0, 40.0),
]

COMPRESSOR_REFERENCE_CAPACITY_KW=5.0
COMPRESSOR_REFERENCE_DISPLACEMENT_M3_REV=0.000025
COMPRESSOR_REFERENCE_MAX_SPEED_RPM=4500.0
COMPRESSOR_REFERENCE_EVAP_C=-10.0
COMPRESSOR_REFERENCE_COND_C=45.0
EVAPORATOR_RATING_DELTA_T_K=5.0
CONDENSER_RATING_DELTA_T_K=8.0
AIRFLOW_PER_KW_MIN_M3_H=400.0
AIRFLOW_PER_KW_MAX_M3_H=1200.0
_COMPRESSOR_CAP_PER_DISP_CACHE={}

def _reference_compressor_capacity_per_displacement(base_cfg):
    """Calibrate the internal displacement-to-capacity conversion once using
    the same CoolProp/fixed-efficiency cycle used by the plant. This does not
    create an OEM compressor map; it only makes the user-facing kW rating map
    exactly onto the existing thermodynamic model at the declared reference
    condition."""
    key=(base_cfg.refrig.refrigerant, base_cfg.refrig.suction_superheat_K,
         base_cfg.refrig.liquid_subcooling_K, base_cfg.refrig.volumetric_efficiency,
         base_cfg.refrig.isentropic_efficiency, base_cfg.refrig.motor_efficiency,
         base_cfg.refrig.drive_efficiency, COMPRESSOR_REFERENCE_MAX_SPEED_RPM,
         COMPRESSOR_REFERENCE_EVAP_C, COMPRESSOR_REFERENCE_COND_C)
    if key in _COMPRESSOR_CAP_PER_DISP_CACHE:
        return _COMPRESSOR_CAP_PER_DISP_CACHE[key]
    if HAVE_COOLPROP:
        import copy
        rcfg=copy.deepcopy(base_cfg.refrig)
        rcfg.compressor_displacement_m3_rev=COMPRESSOR_REFERENCE_DISPLACEMENT_M3_REV
        rcfg.min_speed_rpm=1500.0
        rcfg.max_speed_rpm=COMPRESSOR_REFERENCE_MAX_SPEED_RPM
        rcfg.production_mode=True
        r=solve_refrigeration_cycle(rcfg, COMPRESSOR_REFERENCE_EVAP_C, COMPRESSOR_REFERENCE_COND_C, 1.0)
        q=max(1e-9, r.Q_evap_W)
        cap_per_disp=q/COMPRESSOR_REFERENCE_DISPLACEMENT_M3_REV
    else:
        # Test-only fallback retains the original nominal scale. Production
        # optimization requires CoolProp and therefore uses the calibrated path.
        cap_per_disp=COMPRESSOR_REFERENCE_CAPACITY_KW*1000.0/COMPRESSOR_REFERENCE_DISPLACEMENT_M3_REV
    _COMPRESSOR_CAP_PER_DISP_CACHE[key]=cap_per_disp
    return cap_per_disp

def apply_design_vector(base_cfg, x):
    import copy
    cfg=copy.deepcopy(base_cfg)
    if len(x)!=len(DESIGN_VARIABLES):
        raise ValueError(f"Expected {len(DESIGN_VARIABLES)} design variables, got {len(x)}")
    cfg.pv.rated_power_kWp=float(x[0])
    cfg.battery.nominal_energy_kWh=float(x[1])
    cfg.pcm.pcm_mass_kg=float(x[2])
    comp=max(float(x[3]),0.001)
    evap=max(float(x[4]),0.001)
    cond=max(float(x[5]),0.001)
    cfg.refrig.evaporator_airflow_m3_h=max(float(x[6]),0.0)
    cfg.pcm.hx_area_m2=max(float(x[7]),0.1)
    cap_per_disp=_reference_compressor_capacity_per_displacement(base_cfg)
    cfg.refrig.compressor_displacement_m3_rev=comp*1000.0/cap_per_disp
    cfg.refrig.min_speed_rpm=1500.0
    cfg.refrig.max_speed_rpm=COMPRESSOR_REFERENCE_MAX_SPEED_RPM
    cfg.refrig.evaporator_UA_W_K=evap*1000.0/EVAPORATOR_RATING_DELTA_T_K
    cfg.refrig.condenser_UA_W_K=cond*1000.0/CONDENSER_RATING_DELTA_T_K
    return cfg


def _nightly_pcm_metrics(trace: list[dict], dt_h: float, min_night_demand_kWh: float = 0.05) -> dict:
    """Compute per-night PCM contribution using the actual nighttime cooling demand."""
    nights: dict[int, dict[str, float]] = {}
    for r in trace:
        if r.get("daytime", False):
            continue
        night_id = int(math.floor((r["t_h"] - 1e-9) / 24.0))
        rec = nights.setdefault(night_id, {"pcm_kWh": 0.0, "demand_kWh": 0.0})
        rec["pcm_kWh"] += max(0.0, r.get("night_pcm_cooling_W", 0.0)) * dt_h / 1000.0
        rec["demand_kWh"] += max(0.0, r.get("night_room_sensible_demand_W", 0.0)) * dt_h / 1000.0

    shares = []
    # Do not judge the initial partial night against the steady-state PCM-share
    # requirement because the initial PCM SOC is a boundary condition, not a
    # nightly recharge/discharge cycle. Operational nights begin after t=24 h.
    for night_id, rec in nights.items():
        if night_id <= 0:
            continue
        if rec["demand_kWh"] >= min_night_demand_kWh:
            shares.append(min(1.0, rec["pcm_kWh"] / max(rec["demand_kWh"], 1e-12)))
    return {
        "night_count": len(shares),
        "min_night_pcm_share": min(shares) if shares else 1.0,
        "mean_night_pcm_share": (sum(shares) / len(shares)) if shares else 1.0,
        "night_shares": shares,
    }


CONSTRAINT_SCALES=[1.0,1.0,1.0,1.0,1.0,1.0,1.0,0.01,1.0,100.0]
def normalize_constraints(raw): return [float(g)/sc for g,sc in zip(raw,CONSTRAINT_SCALES)]
def _raw_constraint_values(base_cfg,x,trace):
    dt_h=float(trace[1]["t_h"]-trace[0]["t_h"]) if len(trace)>1 else 1.0
    Tr=[r["T_room_C"] for r in trace]; Tp=[r["T_product_max_C"] for r in trace]; Ta=[r["T_product_aged_max_C"] for r in trace]; RH=[r["RH_pct"] for r in trace]
    # Constraint 3 = unserved critical room refrigeration thermal duty, not generic
    # electrical bus deficit and not optional PCM charging demand. The latter is an
    # optimization/storage action, not a service-failure metric.
    unmet=sum(max(0.0, r.get("refrigeration_unmet_W", 0.0)) for r in trace)*dt_h/1000.0
    comp=sum(r["P_compressor_elec_W"]+r["P_pcm_charge_elec_W"] for r in trace)*dt_h/1000.0
    inv=max(r["inventory_kg"] for r in trace); bad=sum(1 for r in trace if r.get("refrig_cycle_hard_failure", False)); night=_nightly_pcm_metrics(trace,dt_h)
    gT=max(0.0,max(Tr)-base_cfg.control.room_upper_C); gp=max(0.0,max(Ta)-base_cfg.control.product_upper_C)
    # RH constraint is explicitly a violation-HOURS metric.  The earlier
    # implementation accumulated RH degree-hours but named the result
    # rh_violation_hours, making the magnitude dependent on how far outside
    # the band the humidity drifted.  Count each timestep once when RH is outside
    # the specified storage band.
    rh_low_hours = sum(
        dt_h for r in trace
        if r["RH_pct"] < base_cfg.control.rh_lower_pct
    )
    rh_high_hours = sum(
        dt_h for r in trace
        if r["RH_pct"] > base_cfg.control.rh_upper_pct
    )
    grh = rh_low_hours + rh_high_hours
    gm=max(0.0,inv-base_cfg.product.max_inventory_kg)
    if any(r.get("inventory_violation",False) for r in trace): gm=max(gm,1.0)
    gpcm=max(0.0,base_cfg.control.pcm_minimum_share-night["min_night_pcm_share"])
    gsoc=max(0.0,base_cfg.control.battery_reserve_SOC-min(r["battery_soc"] for r in trace))
    gcap=max(0.0,float(x[3])-float(x[4])); ar=float(x[6])/float(x[4]) if float(x[4])>0 else 0.0
    gair=max(0.0,AIRFLOW_PER_KW_MIN_M3_H-ar,ar-AIRFLOW_PER_KW_MAX_M3_H)
    raw=[gT,gp,grh,unmet,gm,float(bad),gpcm,gsoc,gcap,gair]
    return raw,(Tr,Tp,Ta,RH,unmet,comp,inv,bad,night,grh,gsoc,dt_h)

def _pcm_material_screen(candidate_name: str) -> Optional[str]:
    # Material-level qualification before expensive annual simulation.
    c = PCM_LIBRARY.get(candidate_name)
    if c is None:
        return "UNKNOWN_PCM_CANDIDATE"
    try:
        c.validate_for_optimization()
    except Exception as exc:
        return f"PCM_PROPERTY_DATA_INCOMPLETE: {exc}"
    if not (2.0 <= c.nominal_transition_C <= 4.0):
        return "PCM_TRANSITION_OUTSIDE_TARGET"
    if math.isfinite(c.leakage_pct) and c.leakage_pct > 10.0:
        return "PCM_LEAKAGE_ABOVE_SCREENING_LIMIT"
    if math.isfinite(c.cycle_retention_500) and c.cycle_retention_500 < 0.95:
        return "PCM_CYCLE_RETENTION_BELOW_SCREENING_LIMIT"
    return None


def _cheap_design_screen(x):
    """Reject impossible procurement combinations before an annual simulation."""
    comp, evap, cond, airflow = float(x[3]), float(x[4]), float(x[5]), float(x[6])
    if comp <= 0.0 or evap <= 0.0 or cond <= 0.0 or airflow <= 0.0:
        return "NONPOSITIVE_EQUIPMENT_RATING"
    if comp > evap + 1e-9:
        return "COMPRESSOR_GT_EVAPORATOR_CAPACITY"
    ratio = airflow / evap
    if ratio < AIRFLOW_PER_KW_MIN_M3_H - 1e-9 or ratio > AIRFLOW_PER_KW_MAX_M3_H + 1e-9:
        return "EVAPORATOR_AIRFLOW_RATIO_OUT_OF_RANGE"
    return None

def _rh_diagnostics_from_trace(trace, cfg, total_duration_h=None):
    """Return robust RH summary fields for completed traces."""
    if not trace:
        return {
            "rh_min": float("nan"), "rh_max": float("nan"),
            "rh_low_hours": float("nan"), "rh_high_hours": float("nan"),
            "rh_in_band_hours": float("nan"), "rh_violation_hours": float("nan"),
        }
    if len(trace) > 1:
        dt_h = float(trace[1]["t_h"] - trace[0]["t_h"])
    else:
        dt_h = float(total_duration_h) if total_duration_h is not None else 0.0
    dt_h = max(0.0, dt_h)
    rh_vals = [float(r.get("RH_pct", float("nan"))) for r in trace]
    finite = [v for v in rh_vals if math.isfinite(v)]
    if not finite:
        return {
            "rh_min": float("nan"), "rh_max": float("nan"),
            "rh_low_hours": float("nan"), "rh_high_hours": float("nan"),
            "rh_in_band_hours": float("nan"), "rh_violation_hours": float("nan"),
        }
    low_h = sum(dt_h for v in rh_vals if math.isfinite(v) and v < cfg.control.rh_lower_pct)
    high_h = sum(dt_h for v in rh_vals if math.isfinite(v) and v > cfg.control.rh_upper_pct)
    observed_h = len(trace) * dt_h
    if total_duration_h is not None and total_duration_h > 0.0:
        observed_h = min(float(total_duration_h), observed_h)
    return {
        "rh_min": min(finite),
        "rh_max": max(finite),
        "rh_low_hours": low_h,
        "rh_high_hours": high_h,
        "rh_in_band_hours": max(0.0, observed_h - low_h - high_h),
        "rh_violation_hours": low_h + high_h,
    }

def _dispatch_limiting_cause(row: dict) -> str:
    """Attribute positive room-refrigeration unmet duty to the first physical bottleneck.

    This is a diagnostic classification only; it never changes the plant state.
    Priority follows the dispatch hierarchy: compressor disabled, electrical
    starvation, condenser limit, compressor/thermal capacity, then other.
    """
    unmet = max(0.0, float(row.get("refrigeration_unmet_W", 0.0)))
    if unmet <= 1e-9:
        return "NONE"
    command = max(0.0, float(row.get("compressor_commanded_fraction", 0.0)))
    actual = max(0.0, float(row.get("compressor_fraction_actual", 0.0)))
    if command <= 1e-9 or actual <= 1e-9:
        if float(row.get("thermal_power_budget_W", 0.0)) <= 1e-9:
            return "ENERGY_STARVED"
        return "COMPRESSOR_DISABLED"
    f_thermal = float(row.get("_f_thermal", command))
    f_power = float(row.get("_f_power", command))
    f_cond = float(row.get("_f_condenser", command))
    lower = min(f_thermal, f_power, f_cond, command)
    tol = 2e-7
    if f_power <= lower + tol and f_power + tol < command:
        return "ELECTRICAL_LIMITED"
    if f_cond <= lower + tol and f_cond + tol < command:
        return "CONDENSER_LIMITED"
    if f_thermal <= lower + tol and f_thermal + tol < command:
        return "THERMAL_CAPACITY_LIMITED"
    return "OTHER_LIMIT"


def evaluate_design(base_cfg,x,weather,duration_days=365.0,dt_min=60.0,initial_room_C=None,initial_product_C=None,initial_rh_pct=None):
    global EVAL_ERROR_COUNT
    trace = []
    material_reason = _pcm_material_screen(base_cfg.pcm.candidate)
    if material_reason is not None:
        EVAL_ERROR_COUNT += 1
        raw = [10.0] * len(CONSTRAINT_SCALES)
        return {
            "objectives":[float(x[0]),float(x[1]),float(x[2]),1e4],
            "constraints":normalize_constraints(raw),
            "raw_constraints":raw,
            "feasible":False,
            "failure_reason":material_reason,
            "failure_details":{"pcm_candidate":base_cfg.pcm.candidate},
            "pcm_candidate":base_cfg.pcm.candidate,
        }
    screen_reason=_cheap_design_screen(x)
    if screen_reason is not None:
        EVAL_ERROR_COUNT+=1
        raw=[0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,max(0.0,float(x[3])-float(x[4])),
             max(0.0,AIRFLOW_PER_KW_MIN_M3_H-float(x[6])/max(float(x[4]),1e-9),
                 float(x[6])/max(float(x[4]),1e-9)-AIRFLOW_PER_KW_MAX_M3_H)]
        if screen_reason=="EVAPORATOR_AIRFLOW_RATIO_OUT_OF_RANGE": raw[9]=max(raw[9],1.0)
        if screen_reason=="COMPRESSOR_GT_EVAPORATOR_CAPACITY": raw[8]=max(raw[8],float(x[3])-float(x[4]))
        return {"objectives":[float(x[0]),float(x[1]),float(x[2]),1e4],"constraints":normalize_constraints(raw),"raw_constraints":raw,"feasible":False,"failure_reason":screen_reason}
    try:
        trace=simulate_transient(apply_design_vector(base_cfg,x),weather,duration_days,dt_min=dt_min,initial_room_C=initial_room_C,initial_product_C=initial_product_C,initial_rh_pct=initial_rh_pct)
        raw,m=_raw_constraint_values(base_cfg,x,trace); cons=normalize_constraints(raw)
        rhdiag = _rh_diagnostics_from_trace(trace, base_cfg, total_duration_h=float(duration_days)*24.0)
        battery_soc_series=[float(r["battery_soc"]) for r in trace]
        dt_h = (float(trace[1]["t_h"] - trace[0]["t_h"]) if len(trace) > 1 else
                float(dt_min) / 60.0)
        dt_h = max(0.0, dt_h)
        violated=[(i,float(raw[i]),float(cons[i])) for i in range(len(cons)) if cons[i]>1e-8]
        failure_reason = "" if not violated else "POST_SIM_CONSTRAINT"
        failure_details = {} if not violated else {
            "violated_constraints": violated,
            "constraint_names":["room_temperature","aged_product_temperature","RH_hours","unmet_energy","inventory","refrigeration_bad_steps","PCM_night_share","battery_reserve","compressor_vs_evaporator","airflow_ratio"],
        }
        pcm_c = PCM_LIBRARY[base_cfg.pcm.candidate]
        return {"objectives":[float(x[0]),float(x[1]),float(x[2]),m[5]],"constraints":cons,"raw_constraints":raw,"feasible":all(g<=1e-8 for g in cons),
                "failure_reason":failure_reason,"failure_time_h":float("nan"),"failure_details":failure_details,
                "pcm_candidate":base_cfg.pcm.candidate,
                "pcm_transition_C":pcm_c.nominal_transition_C,
                "pcm_conductivity_W_mK":pcm_c.conductivity_W_mK,
                "pcm_usable_enthalpy_J_kg":pcm_c.usable_enthalpy_J_kg,
                "pcm_leakage_pct":pcm_c.leakage_pct,
                "pcm_cycle_retention_500":pcm_c.cycle_retention_500,
                "pcm_cost_INR_kg":pcm_c.cost_per_kg,
                "pcm_data_status":pcm_c.data_status,
                "peak_room_C":max(m[0]),"min_room_C":min(m[0]),"peak_product_C":max(m[1]),"peak_product_aged_C":max(m[2]),"rh_min":rhdiag["rh_min"],"rh_max":rhdiag["rh_max"],"rh_violation_hours":rhdiag["rh_violation_hours"],
                "rh_low_hours":rhdiag["rh_low_hours"],
                "rh_high_hours":rhdiag["rh_high_hours"],
                "rh_in_band_hours":rhdiag["rh_in_band_hours"],
                "unmet_kWh":m[4],"compressor_kWh":m[5],
                "fan_only_steps":sum(1 for r in trace if r.get("fan_only_mode", False)),
                "fan_only_hours":sum(dt_h for r in trace if r.get("fan_only_mode", False)),
                "fan_energy_savings_kWh":sum(max(0.0, r.get("fan_energy_savings_W", 0.0)) for r in trace)*dt_h/1000.0,
                "dispatch_cause_hours": {
                    cause: sum(dt_h for r in trace if r.get("dispatch_limiting_cause") == cause)
                    for cause in ("ENERGY_STARVED","ELECTRICAL_LIMITED","CONDENSER_LIMITED","THERMAL_CAPACITY_LIMITED","COMPRESSOR_DISABLED","OTHER_LIMIT","NONE")
                },
                "dispatch_cause_unmet_kWh": {
                    cause: sum(max(0.0, r.get("refrigeration_unmet_W", 0.0)) * dt_h for r in trace if r.get("dispatch_limiting_cause") == cause) / 1000.0
                    for cause in ("ENERGY_STARVED","ELECTRICAL_LIMITED","CONDENSER_LIMITED","THERMAL_CAPACITY_LIMITED","COMPRESSOR_DISABLED","OTHER_LIMIT","NONE")
                },
                "pv_generation_kWh":sum(r["P_pv_bus_W"] for r in trace)*m[11]/1000.0,"battery_soc_min":min(battery_soc_series),"battery_soc_max":max(battery_soc_series),"battery_floor_soc":max(base_cfg.battery.minimum_SOC, base_cfg.control.battery_reserve_SOC),"pcm_soc_min":min(r["pcm_soc"] for r in trace),"pcm_soc_max":max(r["pcm_soc"] for r in trace),
                "condenser_limited_steps":sum(1 for r in trace if r.get("condenser_limited",False)),"condenser_inadequate_steps":sum(1 for r in trace if not r.get("condenser_adequate",True)),"worst_condenser_margin_W":min(r.get("condenser_margin_W",float("inf")) for r in trace),
                "battery_soc_hard_clamp_count":sum(1 for r in trace if r.get("battery_soc_hard_clamped",False)),"condensate_kg":sum(r["condensate_kg_s"] for r in trace)*dt_s_from_min(dt_min),"refrig_infeasible_steps":m[7],"max_refrig_residual_W":max(abs(r["refrig_residual_W"]) for r in trace),"max_bus_residual_W":max(abs(r["bus_residual_W"]) for r in trace),"night_count":m[8]["night_count"],"min_night_pcm_share":m[8]["min_night_pcm_share"],"mean_night_pcm_share":m[8]["mean_night_pcm_share"],"dispatch_removed_kg":trace[-1].get("dispatch_removed_kg",0.0),"dispatch_inventory_shortfall_kg":trace[-1].get("dispatch_shortfall_kg",0.0),"optimization_duration_days":duration_days,"optimization_dt_min":dt_min}
    except CandidateInfeasible as exc:
        EVAL_ERROR_COUNT+=1
        if DEBUG_MODE: print(f"[REJECT] #{EVAL_ERROR_COUNT} {exc.reason} at t={exc.t_h:.1f} h",flush=True)
        gT=max(0.0,exc.T_room_C-base_cfg.control.room_upper_C) if math.isfinite(exc.T_room_C) else 0.0
        gp=max(0.0,exc.T_product_aged_C-base_cfg.control.product_upper_C) if math.isfinite(exc.T_product_aged_C) else 0.0
        grh=1.0 if "RH" in exc.reason or "PSYCHROMETRIC" in exc.reason else 0.0

        # CandidateInfeasible may be raised before the current timestep is appended
        # to `trace`. The previous implementation therefore printed RH=nan for
        # ordinary thermal early-termination cases even when valid RH data existed
        # in the completed trajectory (and/or in exc.details for the failing step).
        details = exc.details if isinstance(exc.details, dict) else {}
        rh_completed = []
        for row in trace:
            try:
                v = float(row.get("RH_pct", float("nan")))
            except (TypeError, ValueError):
                v = float("nan")
            if math.isfinite(v):
                rh_completed.append(v)

        try:
            failing_rh = float(details.get("RH_pct", float("nan")))
        except (TypeError, ValueError):
            failing_rh = float("nan")

        # The failing endpoint is used for min/max visibility, but NOT counted as
        # a completed hour in the duration metrics. This avoids inventing an extra
        # violation interval merely because the simulation stopped at that state.
        rh_all = rh_completed + ([failing_rh] if math.isfinite(failing_rh) else [])
        if rh_all:
            rh_min = min(rh_all)
            rh_max = max(rh_all)
            dt_h_rh = max(0.0, float(dt_min) / 60.0)
            rh_low_hours = sum(dt_h_rh for v in rh_completed if v < base_cfg.control.rh_lower_pct)
            rh_high_hours = sum(dt_h_rh for v in rh_completed if v > base_cfg.control.rh_upper_pct)
            observed_h = len(rh_completed) * dt_h_rh
            rh_in_band_hours = max(0.0, observed_h - rh_low_hours - rh_high_hours)
            rh_violation_hours = rh_low_hours + rh_high_hours
        else:
            rh_min = rh_max = float("nan")
            rh_low_hours = rh_high_hours = rh_in_band_hours = rh_violation_hours = float("nan")

        # A terminated thermal trajectory is a hard infeasibility observation.
        # Keep its penalty conservative without inventing a fake detailed load trace.
        raw=[gT,gp,grh,10.0,0.0,1.0,0.0,
             max(0.0,base_cfg.control.battery_reserve_SOC-exc.battery_soc) if math.isfinite(exc.battery_soc) else 0.0,
             max(0.0,float(x[3])-float(x[4])),0.0]
        return {
            "objectives":[float(x[0]),float(x[1]),float(x[2]),1e4],
            "constraints":normalize_constraints(raw),
            "raw_constraints":raw,
            "feasible":False,
            "failure_reason":exc.reason,
            "failure_time_h":exc.t_h,
            "peak_room_C":exc.T_room_C,
            "peak_product_aged_C":exc.T_product_aged_C,
            "battery_soc_min":exc.battery_soc,
            "battery_floor_soc":max(base_cfg.battery.minimum_SOC, base_cfg.control.battery_reserve_SOC),
            "rh_min":rh_min,
            "rh_max":rh_max,
            "rh_low_hours":rh_low_hours,
            "rh_high_hours":rh_high_hours,
            "rh_in_band_hours":rh_in_band_hours,
            "rh_violation_hours":rh_violation_hours,
            "min_night_pcm_share":0.0,
            "pcm_soc_min":float(details.get("pcm_soc", float("nan"))),
            "pcm_soc_max":float(details.get("pcm_soc", float("nan"))),
            "failure_details":details
        }
    except Exception as exc:
        EVAL_ERROR_COUNT+=1
        tb = traceback.format_exc()
        if DEBUG_MODE:
            print(f"[ERROR] candidate evaluation failed (#{EVAL_ERROR_COUNT}): {exc}",flush=True)
            print(f"        x={x}",flush=True)
            print(tb,flush=True)
        return {"objectives":[float(x[0]),float(x[1]),float(x[2]),1e4],
                "constraints":[1e4]*len(CONSTRAINT_SCALES),
                "raw_constraints":[1e4]*len(CONSTRAINT_SCALES),
                "feasible":False,
                "failure_reason":f"{type(exc).__name__}: {exc}",
                "failure_details":{"exception_type":type(exc).__name__,"exception_message":str(exc),"traceback":tb},
                "validation_error":True,
                "rh_min":float("nan"),"rh_max":float("nan"),
                "rh_low_hours":float("nan"),"rh_high_hours":float("nan"),
                "rh_in_band_hours":float("nan"),"rh_violation_hours":float("nan") }


def dt_s_from_min(dt_min: float) -> float:
    return dt_min * 60.0


if HAVE_PYMOO:
    class InitialSeedSampling(Sampling):
        """Seed the first population with high-PV/high-battery designs, then fill randomly."""
        def __init__(self, seed: int = 1):
            super().__init__()
            self.seed = int(seed)

        def _do(self, problem, n_samples, **kwargs):
            rng = np.random.default_rng(self.seed)
            xl = np.asarray(problem.xl, dtype=float)
            xu = np.asarray(problem.xu, dtype=float)
            anchors = [
                [12.0, 30.0, 1000.0, 10.0, 12.0, 18.0, 12000.0, 40.0],
                [14.0, 30.0, 1000.0, 10.0, 12.0, 18.0, 12000.0, 40.0],
                [16.0, 30.0, 1000.0, 10.0, 12.0, 18.0, 12000.0, 40.0],
                [18.0, 30.0, 1000.0, 10.0, 12.0, 18.0, 12000.0, 40.0],
                [20.0, 30.0, 1000.0, 10.0, 12.0, 18.0, 12000.0, 40.0],
                [22.0, 30.0, 1000.0, 10.0, 12.0, 20.0, 12000.0, 40.0],
                [25.0, 35.0, 1000.0, 10.0, 12.0, 20.0, 12000.0, 40.0],
                [18.0, 35.0, 1000.0, 10.0, 12.0, 20.0, 12000.0, 40.0],
            ]
            pop=[]
            for a in anchors:
                x=np.asarray(a,dtype=float)
                if len(pop) < n_samples and np.all(x>=xl) and np.all(x<=xu) and _cheap_design_screen(x) is None:
                    pop.append(x)
            while len(pop) < n_samples:
                x=xl+rng.random(len(xl))*(xu-xl)
                if _cheap_design_screen(x) is None:
                    pop.append(x)
            return np.asarray(pop[:n_samples],dtype=float)

    def _nsga2_eval_worker(args):
        base_cfg, x, weather, duration_days, dt_min = args
        r = evaluate_design(base_cfg, list(map(float, x)), weather, duration_days, dt_min=dt_min)
        return r

    class ResearchHistoryCallback(Callback):
        """Collect lightweight generation statistics without pymoo deepcopy.

        pymoo's ``save_history=True`` deep-copies the whole Problem object at
        every generation.  Our Problem intentionally owns a ProcessPoolExecutor
        for parallel annual simulations, and that object contains thread locks
        which cannot be pickled/deep-copied.  This callback records only the
        numeric generation statistics needed for research-grade convergence
        plots, so the optimizer itself remains untouched and no executor or
        physics-state objects are copied.
        """
        def __init__(self):
            super().__init__()
            self.records = []

        def notify(self, algorithm):
            try:
                pop = algorithm.pop
                F = np.asarray(pop.get("F"), dtype=float)
                Graw = pop.get("G")
                G = np.asarray(Graw, dtype=float) if Graw is not None else np.zeros((len(F), len(CONSTRAINT_SCALES)))
                cv = np.sum(np.maximum(0.0, G), axis=1)
                feasible = cv <= 1e-8

                if np.any(feasible):
                    ff = F[feasible]
                    fronts = _nondominated_sort(ff.tolist())
                    pareto_n = len(fronts[0]) if fronts else 0
                    fmin = np.min(ff, axis=0)
                    hv_value = float("nan")
                    try:
                        from pymoo.indicators.hv import HV
                        ref = np.max(ff, axis=0) * 1.05 + 1e-12
                        scale = np.maximum(ref, 1e-12)
                        hv_value = float(HV(ref_point=np.ones(4)).do(
                            np.clip(ff / scale, 0.0, 1.0)))
                    except Exception:
                        pass
                else:
                    pareto_n = 0
                    fmin = np.full(4, np.nan)
                    hv_value = float("nan")

                self.records.append({
                    "generation": int(getattr(algorithm, "n_gen", len(self.records) + 1)),
                    "n_population": int(len(F)),
                    "n_feasible": int(np.sum(feasible)),
                    "n_pareto": int(pareto_n),
                    "cv_min": float(np.min(cv)) if len(cv) else float("nan"),
                    "cv_mean": float(np.mean(cv)) if len(cv) else float("nan"),
                    "pv_min": float(fmin[0]),
                    "battery_min": float(fmin[1]),
                    "pcm_min": float(fmin[2]),
                    "compressor_energy_min": float(fmin[3]),
                    "hypervolume_normalized": hv_value,
                })
            except Exception:
                # Plotting/history telemetry must never interrupt optimization.
                return


    class ColdStorageProblem(Problem):
        def __init__(self, base_cfg: MasterConfig, weather: list[WeatherRecord],
                     duration_days: float = 365.0, dt_min: float = 60.0, progress_every: int = 25,
                     executor=None):
            xl = np.array([v[1] for v in DESIGN_VARIABLES])
            xu = np.array([v[2] for v in DESIGN_VARIABLES])
            super().__init__(n_var=len(DESIGN_VARIABLES), n_obj=4, n_constr=10, xl=xl, xu=xu)
            self.base_cfg = base_cfg
            self.weather = weather
            self.duration_days = duration_days
            self.dt_min = dt_min
            self.progress_every = max(0, int(progress_every))
            self.executor = executor
            self._eval_count = 0
            self.result_cache = {}

        def _evaluate(self, X, out, *args, **kwargs):
            xs=[np.asarray(x,dtype=float) for x in X]
            missing=[]; missing_keys=[]
            for x in xs:
                key=_design_key(x)
                if key not in self.result_cache:
                    missing.append(x.tolist()); missing_keys.append(key)
            if missing:
                jobs=[(self.base_cfg,x,self.weather,self.duration_days,self.dt_min) for x in missing]
                if self.executor is not None:
                    results=list(self.executor.map(_nsga2_eval_worker,jobs))
                else:
                    results=[_nsga2_eval_worker(j) for j in jobs]
                for key,r in zip(missing_keys,results):
                    self.result_cache[key]=r

            F=np.zeros((len(xs),4)); G=np.zeros((len(xs),10))
            for i,x in enumerate(xs):
                r=self.result_cache[_design_key(x)]
                F[i,:]=r["objectives"]; G[i,:]=r["constraints"]
                self._eval_count += 1
                if self.progress_every and self._eval_count % self.progress_every == 0:
                    print(f"[OPT] evaluated {self._eval_count} candidate positions; feasible={bool(r.get('feasible',False))}",flush=True)
            out["F"]=F; out["G"]=G


# =====================================================================
# CHUNK 12 -- Optimization Execution, Pareto, Robustness, Reporting
# =====================================================================

def dominates(a: list[float], b: list[float]) -> bool:
    return all(x <= y for x, y in zip(a, b)) and any(x < y for x, y in zip(a, b))


def pareto_front(candidates: list[dict]) -> list[dict]:
    feasible = [c for c in candidates if c["feasible"]]
    front = []
    for c in feasible:
        if not any(dominates(o["objectives"], c["objectives"]) for o in feasible if o is not c):
            front.append(c)
    return front


def compromise_select(front: list[dict], weights=(0.20, 0.20, 0.20, 0.40)) -> Optional[dict]:
    if not front:
        return None
    n_obj = len(front[0]["objectives"])
    mins = [min(c["objectives"][k] for c in front) for k in range(n_obj)]
    maxs = [max(c["objectives"][k] for c in front) for k in range(n_obj)]
    best, best_score = None, float("inf")
    for c in front:
        score = 0.0
        for k in range(n_obj):
            span = maxs[k] - mins[k]
            norm = (c["objectives"][k] - mins[k]) / span if span > 0 else 0.0
            score += weights[k] * norm
        if score < best_score:
            best_score, best = score, c
    return best


def robustness_scenarios() -> list[dict]:
    """Scenario matrix (Instructions 12-E / 21.5). Each entry is a named set of
    multiplicative/additive perturbations applied to a base config + weather."""
    return [
        {"name": "nominal", "amb_delta_C": 0.0, "poa_scale": 1.0, "ach_scale": 1.0,
         "incoming_scale": 1.0, "isentropic_scale": 1.0, "pcm_capacity_scale": 1.0,
         "respiration_scale": 1.0},
        {"name": "high_ambient", "amb_delta_C": 6.0, "poa_scale": 1.0, "ach_scale": 1.0,
         "incoming_scale": 1.0, "isentropic_scale": 1.0, "pcm_capacity_scale": 1.0,
         "respiration_scale": 1.0},
        {"name": "high_humidity", "amb_delta_C": 2.0, "poa_scale": 1.0, "ach_scale": 1.0,
         "incoming_scale": 1.0, "isentropic_scale": 1.0, "pcm_capacity_scale": 1.0,
         "respiration_scale": 1.0, "ambient_rh_pct": 95.0},
        {"name": "severe_loading", "amb_delta_C": 2.0, "poa_scale": 1.0, "ach_scale": 1.5,
         "incoming_scale": 1.5, "isentropic_scale": 1.0, "pcm_capacity_scale": 1.0,
         "respiration_scale": 1.0},
        {"name": "low_pv_yield", "amb_delta_C": 0.0, "poa_scale": 0.6, "ach_scale": 1.0,
         "incoming_scale": 1.0, "isentropic_scale": 1.0, "pcm_capacity_scale": 1.0,
         "respiration_scale": 1.0},
        {"name": "high_infiltration", "amb_delta_C": 2.0, "poa_scale": 1.0, "ach_scale": 2.0,
         "incoming_scale": 1.0, "isentropic_scale": 1.0, "pcm_capacity_scale": 1.0,
         "respiration_scale": 1.0},
        {"name": "low_compressor_eff", "amb_delta_C": 2.0, "poa_scale": 1.0, "ach_scale": 1.0,
         "incoming_scale": 1.0, "isentropic_scale": 0.85, "pcm_capacity_scale": 1.0,
         "respiration_scale": 1.0},
        {"name": "reduced_pcm", "amb_delta_C": 2.0, "poa_scale": 1.0, "ach_scale": 1.0,
         "incoming_scale": 1.0, "isentropic_scale": 1.0, "pcm_capacity_scale": 0.7,
         "respiration_scale": 1.0},
        {"name": "high_respiration", "amb_delta_C": 2.0, "poa_scale": 1.0, "ach_scale": 1.0,
         "incoming_scale": 1.0, "isentropic_scale": 1.0, "pcm_capacity_scale": 1.0,
         "respiration_scale": 2.0},
        {"name": "hot_start_recovery", "amb_delta_C": 2.0, "poa_scale": 1.0, "ach_scale": 1.0,
         "incoming_scale": 1.0, "isentropic_scale": 1.0, "pcm_capacity_scale": 1.0,
         "respiration_scale": 1.0, "hot_start": True},
    ]


def _apply_scenario(base_cfg: MasterConfig, weather: list[WeatherRecord], scn: dict) -> tuple[MasterConfig, list[WeatherRecord]]:
    import copy
    cfg = copy.deepcopy(base_cfg)
    cfg.infiltration.background_ach_h *= scn["ach_scale"]
    cfg.infiltration.loading_ach_h *= scn["ach_scale"]
    cfg.product.incoming_mass_kg_day *= scn["incoming_scale"]
    cfg.refrig.isentropic_efficiency *= scn["isentropic_scale"]
    cfg.commodity.respiration_ref_W_per_tonne *= scn["respiration_scale"]
    if "ambient_rh_pct" in scn:
        cfg.infiltration.ambient_rh_for_infiltration_pct = scn["ambient_rh_pct"]
    # Reduced effective PCM capacity via a smaller transition enthalpy candidate.
    if scn["pcm_capacity_scale"] != 1.0:
        c = PCM_LIBRARY[cfg.pcm.candidate]
        scaled = PCMCandidate(
            name=c.name,
            T_solidus_C=c.T_solidus_C,
            T_liquidus_C=c.T_liquidus_C,
            transition_enthalpy_J_kg=c.transition_enthalpy_J_kg * scn["pcm_capacity_scale"],
            cp_solid_J_kgK=c.cp_solid_J_kgK,
            cp_liquid_J_kgK=c.cp_liquid_J_kgK,
            density_kg_m3=c.density_kg_m3,
            conductivity_W_mK=c.conductivity_W_mK,
            cost_per_kg=c.cost_per_kg,
            nominal_transition_C=c.nominal_transition_C,
            biochar_fraction_wt_pct=c.biochar_fraction_wt_pct,
            usable_enthalpy_J_kg=(c.usable_enthalpy_J_kg * scn["pcm_capacity_scale"]
                                  if math.isfinite(c.usable_enthalpy_J_kg) else c.usable_enthalpy_J_kg),
            leakage_pct=c.leakage_pct,
            cycle_retention_500=c.cycle_retention_500,
            hx_u_multiplier=c.hx_u_multiplier,
            data_status=c.data_status,
            cost_status=c.cost_status,
        )
        cfg.pcm = copy.deepcopy(cfg.pcm)
        # Register a scenario-specific candidate name so PCMState can find it.
        scn_key = f"{c.name}__{scn['name']}"
        PCM_LIBRARY[scn_key] = scaled
        cfg.pcm.candidate = scn_key
    scn_weather = [
        WeatherRecord(r.hour_index, r.utc_hour,
                      r.poa_global_W_m2 * scn["poa_scale"],
                      r.poa_direct_W_m2 * scn["poa_scale"],
                      r.poa_diffuse_W_m2 * scn["poa_scale"],
                      r.poa_reflected_W_m2 * scn["poa_scale"],
                      r.sun_height_deg,
                      r.T_amb_C + scn["amb_delta_C"],
                      r.wind_speed_m_s)
        for r in weather
    ]
    return cfg, scn_weather


def run_robustness(base_cfg: MasterConfig, design_x: list[float], weather: list[WeatherRecord],
                   duration_days: float = 365.0, dt_min: float = 60.0) -> list[dict]:
    """Run the selected design across the scenario matrix (Instructions 21.5:
    a design is not robust merely because it is feasible under one nominal day)."""
    rows = []
    for scn in robustness_scenarios():
        cfg, scn_weather = _apply_scenario(base_cfg, weather, scn)
        if scn.get("hot_start"):
            r = evaluate_design(cfg, design_x, scn_weather, duration_days, dt_min=dt_min,
                                initial_room_C=min(7.0, cfg.control.emergency_room_C),
                                initial_product_C=7.0,
                                initial_rh_pct=cfg.control.rh_target_pct)
        else:
            r = evaluate_design(cfg, design_x, scn_weather, duration_days, dt_min=dt_min)
        rows.append({
            "scenario": scn["name"], "feasible": r["feasible"],
            "peak_room_C": r.get("peak_room_C"), "peak_product_C": r.get("peak_product_C"),
            "rh_min": r.get("rh_min"), "rh_max": r.get("rh_max"),
            "rh_violation_hours": r.get("rh_violation_hours"),
            "unmet_kWh": r.get("unmet_kWh"), "compressor_kWh": r.get("compressor_kWh"),
            "refrig_infeasible_steps": r.get("refrig_infeasible_steps"),
        })
    return rows


def _nondominated_sort(objs: list[list[float]]) -> list[list[int]]:
    """Fast non-dominated sort -> list of fronts (each a list of indices)."""
    n = len(objs)
    S = [[] for _ in range(n)]
    ndom = [0] * n
    fronts = [[]]
    for p in range(n):
        for q in range(n):
            if p == q:
                continue
            if dominates(objs[p], objs[q]):
                S[p].append(q)
            elif dominates(objs[q], objs[p]):
                ndom[p] += 1
        if ndom[p] == 0:
            fronts[0].append(p)
    i = 0
    while fronts[i]:
        nxt = []
        for p in fronts[i]:
            for q in S[p]:
                ndom[q] -= 1
                if ndom[q] == 0:
                    nxt.append(q)
        i += 1
        fronts.append(nxt)
    return fronts[:-1]


def _crowding_distance(front: list[int], objs: list[list[float]]) -> dict:
    dist = {i: 0.0 for i in front}
    if not front:
        return dist
    n_obj = len(objs[front[0]])
    for m in range(n_obj):
        order = sorted(front, key=lambda i: objs[i][m])
        dist[order[0]] = dist[order[-1]] = float("inf")
        lo, hi = objs[order[0]][m], objs[order[-1]][m]
        span = hi - lo
        if span <= 0:
            continue
        for k in range(1, len(order) - 1):
            dist[order[k]] += (objs[order[k + 1]][m] - objs[order[k - 1]][m]) / span
    return dist


def _constraint_violation(constraints: list[float]) -> float:
    return sum(max(0.0, g) for g in constraints)


def _builtin_nsga2(base_cfg: MasterConfig, weather: list[WeatherRecord], pop_size: int,
                   n_gen: int, seed: int, duration_days: float, dt_min: float = 60.0) -> list[dict]:
    """Pure-Python NSGA-II (stdlib only) used when pymoo is not installed.

    Implements the core of Deb's NSGA-II: constrained binary-tournament
    selection (feasibility + rank + crowding), SBX crossover, polynomial
    mutation, and (mu+lambda) elitist replacement by non-dominated rank and
    crowding distance. Deterministic given the seed.
    """
    import random
    rng = random.Random(seed)
    xl = [v[1] for v in DESIGN_VARIABLES]
    xu = [v[2] for v in DESIGN_VARIABLES]
    nv = len(DESIGN_VARIABLES)
    eta_c, eta_m = 15.0, 20.0
    p_mut = 1.0 / nv

    def clamp(x, lo, hi):
        return lo if x < lo else hi if x > hi else x

    def evaluate(x):
        r = evaluate_design(base_cfg, x, weather, duration_days, dt_min=dt_min)
        r["x"] = list(x)
        r["_cv"] = _constraint_violation(r["constraints"])
        return r

    def tournament(pop, ranks, crowd):
        a, b = rng.randrange(len(pop)), rng.randrange(len(pop))
        ca, cb = pop[a]["_cv"], pop[b]["_cv"]
        # Feasibility first, then rank, then crowding (constrained-domination).
        if (ca <= 1e-9) != (cb <= 1e-9):
            return a if ca <= 1e-9 else b
        if ca > 1e-9 and cb > 1e-9:
            return a if ca < cb else b
        if ranks[a] != ranks[b]:
            return a if ranks[a] < ranks[b] else b
        return a if crowd[a] >= crowd[b] else b

    def sbx(p1, p2):
        c1, c2 = list(p1), list(p2)
        for i in range(nv):
            if rng.random() > 0.5:
                continue
            if abs(p1[i] - p2[i]) < 1e-14:
                continue
            x1, x2 = min(p1[i], p2[i]), max(p1[i], p2[i])
            u = rng.random()
            beta = 1.0 + 2.0 * (x1 - xl[i]) / (x2 - x1)
            alpha = 2.0 - beta ** (-(eta_c + 1))
            bq = (u * alpha) ** (1.0 / (eta_c + 1)) if u <= 1.0 / alpha else (1.0 / (2.0 - u * alpha)) ** (1.0 / (eta_c + 1))
            c1[i] = clamp(0.5 * ((x1 + x2) - bq * (x2 - x1)), xl[i], xu[i])
            beta = 1.0 + 2.0 * (xu[i] - x2) / (x2 - x1)
            alpha = 2.0 - beta ** (-(eta_c + 1))
            bq = (u * alpha) ** (1.0 / (eta_c + 1)) if u <= 1.0 / alpha else (1.0 / (2.0 - u * alpha)) ** (1.0 / (eta_c + 1))
            c2[i] = clamp(0.5 * ((x1 + x2) + bq * (x2 - x1)), xl[i], xu[i])
        return c1, c2

    def mutate(x):
        y = list(x)
        for i in range(nv):
            if rng.random() > p_mut:
                continue
            span = xu[i] - xl[i]
            if span <= 0:
                continue
            delta1 = (y[i] - xl[i]) / span
            delta2 = (xu[i] - y[i]) / span
            u = rng.random()
            mut_pow = 1.0 / (eta_m + 1.0)
            if u < 0.5:
                xy = 1.0 - delta1
                val = 2.0 * u + (1.0 - 2.0 * u) * xy ** (eta_m + 1.0)
                dq = val ** mut_pow - 1.0
            else:
                xy = 1.0 - delta2
                val = 2.0 * (1.0 - u) + 2.0 * (u - 0.5) * xy ** (eta_m + 1.0)
                dq = 1.0 - val ** mut_pow
            y[i] = clamp(y[i] + dq * span, xl[i], xu[i])
        return y

    def rank_and_crowd(pop):
        objs = [p["objectives"] for p in pop]
        # Constrained: infeasible sorted after feasible by violation via penalty
        # ordering is handled in tournament/selection; here rank feasible set.
        fronts = _nondominated_sort(objs)
        ranks = [0] * len(pop)
        crowd = [0.0] * len(pop)
        for r, fr in enumerate(fronts):
            cd = _crowding_distance(fr, objs)
            for i in fr:
                ranks[i] = r
                crowd[i] = cd[i]
        return ranks, crowd, fronts

    # Initial population (Latin-ish uniform random).
    pop = [evaluate([rng.uniform(xl[i], xu[i]) for i in range(nv)]) for _ in range(pop_size)]

    for _ in range(n_gen):
        ranks, crowd, _ = rank_and_crowd(pop)
        # Offspring.
        offspring = []
        while len(offspring) < pop_size:
            i1 = tournament(pop, ranks, crowd)
            i2 = tournament(pop, ranks, crowd)
            c1, c2 = sbx(pop[i1]["x"], pop[i2]["x"])
            offspring.append(evaluate(mutate(c1)))
            if len(offspring) < pop_size:
                offspring.append(evaluate(mutate(c2)))
        # (mu+lambda) elitist survival.
        combined = pop + offspring
        objs = [p["objectives"] for p in combined]
        # Partition feasible / infeasible.
        feas = [i for i, p in enumerate(combined) if p["_cv"] <= 1e-9]
        infeas = [i for i, p in enumerate(combined) if p["_cv"] > 1e-9]
        chosen = []
        # Rank feasible by non-dominated fronts + crowding.
        feas_objs = {i: objs[i] for i in feas}
        if feas:
            sub = [feas_objs[i] for i in feas]
            fronts = _nondominated_sort(sub)
            for fr in fronts:
                idxs = [feas[k] for k in fr]
                if len(chosen) + len(idxs) <= pop_size:
                    chosen.extend(idxs)
                else:
                    cd = _crowding_distance(fr, sub)
                    ordered = sorted(fr, key=lambda k: cd[k], reverse=True)
                    for k in ordered:
                        if len(chosen) < pop_size:
                            chosen.append(feas[k])
                    break
        # Fill remainder with least-violating infeasible.
        if len(chosen) < pop_size:
            infeas.sort(key=lambda i: combined[i]["_cv"])
            for i in infeas:
                if len(chosen) < pop_size:
                    chosen.append(i)
        pop = [combined[i] for i in chosen]

    return pop


def run_nsga2(base_cfg, weather, pop_size=64, n_gen=12, seed=1, duration_days=365.0,
              dt_min=120.0, progress_every=12, workers=8):
    """Proper pymoo NSGA-II with process-parallel annual evaluations.

    Every candidate remains a real CoolProp/PVGIS transient simulation. Pymoo
    handles constrained selection, SBX crossover, polynomial mutation, non-
    dominated sorting, and crowding distance; a process pool evaluates each
    population in parallel.
    """
    if not HAVE_PYMOO or not HAVE_NUMPY:
        raise RuntimeError("proper nsga2 requires numpy and pymoo")
    workers=max(1,int(workers))
    pool=ProcessPoolExecutor(max_workers=workers) if workers>1 else None
    try:
        problem=ColdStorageProblem(base_cfg,weather,duration_days,dt_min=dt_min,
                                   progress_every=progress_every,executor=pool)
        sampling=InitialSeedSampling(seed=seed)
        algorithm=NSGA2(pop_size=int(pop_size), sampling=sampling, eliminate_duplicates=True)
        print(f"[NSGA2] pop={pop_size} | generations={n_gen} | workers={workers} | opt_dt={dt_min:g} min",flush=True)
        # IMPORTANT: do not use save_history=True here.  pymoo deep-copies the
        # entire Problem at each generation when history saving is enabled.
        # ColdStorageProblem contains a ProcessPoolExecutor, whose internal
        # synchronization primitives include thread locks and cannot be
        # deep-copied/pickled.  A lightweight callback gives us the same
        # research-relevant convergence statistics without copying the executor.
        history_cb = ResearchHistoryCallback()
        res=pymoo_minimize(problem,algorithm,("n_gen",int(n_gen)),seed=seed,verbose=True,
                           callback=history_cb,save_history=False)
        LAST_NSGA2_HISTORY_ALL.append({"seed":int(seed),"history":list(history_cb.records)})

        # IMPORTANT: return *all* unique real evaluations, not only the final
        # population. This is what gives the research plots hundreds of actual
        # simulation points instead of 1 population-worth of dots.
        all_evals=[]
        for key,r in problem.result_cache.items():
            all_evals.append({**r,"x":list(map(float,key))})
        return all_evals
    finally:
        if pool is not None:
            pool.shutdown(wait=True, cancel_futures=False)

SURROGATE_TARGET_CLIP = 1.0e4

class GPSurrogateBank:
    def __init__(self):
        if not HAVE_SKLEARN: raise RuntimeError("scikit-learn required for surrogate-nsga2")
        self.xsc=StandardScaler(); self.models=[]
    def fit(self,X,Y):
        X=np.asarray(X,float); Y=np.asarray(Y,float)
        if not np.all(np.isfinite(Y)):
            Y=np.nan_to_num(Y,nan=SURROGATE_TARGET_CLIP,posinf=SURROGATE_TARGET_CLIP,neginf=-SURROGATE_TARGET_CLIP)
        Y=np.clip(Y,-SURROGATE_TARGET_CLIP,SURROGATE_TARGET_CLIP)
        self.y_std=np.maximum(np.std(Y,axis=0,ddof=1) if len(Y)>1 else np.ones(Y.shape[1]),1e-12)
        Xs=self.xsc.fit_transform(X); self.models=[]
        for j in range(Y.shape[1]):
            sc=StandardScaler(); ys=sc.fit_transform(Y[:,j:j+1]).ravel()
            # Small-data GP: bounded kernel hyperparameters, no restarts.
            # Convergence warnings are suppressed only for this fit because the
            # surrogate is a search guide, while true simulation failures are
            # never hidden.
            kernel=(ConstantKernel(1.0,(1e-2,1e2))*
                    Matern(length_scale=np.ones(Xs.shape[1]),length_scale_bounds=(1e-2,1e2),nu=2.5)+
                    WhiteKernel(noise_level=1e-6,noise_level_bounds=(1e-9,1e-2)))
            gp=GaussianProcessRegressor(kernel=kernel,alpha=1e-6,normalize_y=False,
                                        n_restarts_optimizer=0,random_state=0)
            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", category=Warning)
                gp.fit(Xs,ys)
            self.models.append((gp,sc))
    def predict(self,X):
        Xs=self.xsc.transform(np.asarray(X,float)); mus=[]; sds=[]
        for gp,sc in self.models:
            mu,sd=gp.predict(Xs,return_std=True); mus.append(sc.inverse_transform(mu.reshape(-1,1)).ravel()); sds.append(sd*sc.scale_[0])
        return np.column_stack(mus),np.column_stack(sds)

def _sample_lhs(n,seed):
    xl=np.asarray([v[1] for v in DESIGN_VARIABLES],float); xu=np.asarray([v[2] for v in DESIGN_VARIABLES],float)
    if HAVE_SCIPY_QMC: return qmc.scale(qmc.LatinHypercube(d=len(DESIGN_VARIABLES),seed=seed).random(n),xl,xu)
    return xl+np.random.default_rng(seed).random((n,len(DESIGN_VARIABLES)))*(xu-xl)

def _sample_screened_lhs(n,seed,max_draws=5000):
    """Space-filling initial samples restricted to procurement-valid designs."""
    n=max(0,int(n))
    if n==0: return np.empty((0,len(DESIGN_VARIABLES)),float)
    rng=np.random.default_rng(seed)
    xl=np.asarray([v[1] for v in DESIGN_VARIABLES],float); xu=np.asarray([v[2] for v in DESIGN_VARIABLES],float)
    accepted=[]
    # Rejection sampling is cheap because the screen is algebraic and prevents
    # the DOE from wasting expensive annual simulations on impossible combinations.
    for _ in range(max(1,int(max_draws))):
        if HAVE_SCIPY_QMC and len(accepted)==0:
            batch=qmc.scale(qmc.LatinHypercube(d=len(DESIGN_VARIABLES),seed=seed).random(min(max_draws,n*20)),xl,xu)
        else:
            batch=xl+rng.random((min(128,n*8),len(DESIGN_VARIABLES)))*(xu-xl)
        for x in batch:
            if _cheap_design_screen(x) is None:
                accepted.append(x)
                if len(accepted)>=n:
                    return np.asarray(accepted[:n],float)
    # Deterministic fallback around the middle of the procurement envelope.
    mid=0.5*(xl+xu)
    out=[]
    for k in range(max_draws):
        x=mid.copy()
        jitter=0.30*(xu-xl)*rng.normal(size=len(x))
        x=np.clip(x+jitter,xl,xu)
        # Enforce the two algebraic procurement relationships for the fallback.
        x[4]=max(x[4],x[3])
        x[6]=np.clip(x[6],AIRFLOW_PER_KW_MIN_M3_H*x[4],AIRFLOW_PER_KW_MAX_M3_H*x[4])
        x=np.clip(x,xl,xu)
        if _cheap_design_screen(x) is None:
            out.append(x)
            if len(out)>=n: break
    return np.asarray(out,float)

def _feasibility_anchor_designs():
    """High-electrical anchors spanning the revised 20% reserve and 30 kWh battery envelope."""
    return [
        np.array([12.0,30.0,1000.0,10.0,12.0,16.0,12000.0,40.0],float),
        np.array([12.0,30.0,800.0,10.0,12.0,16.0,12000.0,20.0],float),
        np.array([12.0,30.0,1000.0,8.0,12.0,16.0,12000.0,40.0],float),
        np.array([10.0,20.0,800.0,8.0,10.0,14.0,8000.0,24.0],float),
        np.array([11.0,25.0,900.0,9.0,11.0,15.0,10500.0,32.0],float),
        np.array([12.0,25.0,900.0,9.0,12.0,16.0,11500.0,30.0],float),
    ]

def _design_key(x): return tuple(round(float(v),8) for v in x)

def run_surrogate_nsga2(base_cfg, weather, seed=1, duration_days=365.0,
                         dt_min=60.0, initial_samples=16, eval_budget=40,
                         batch_size=4, virtual_pop=96, virtual_gen=8):
    """Budgeted surrogate-assisted NSGA-II.

    A small space-filling DOE is evaluated with the real annual simulator.
    Gaussian-process surrogates are then fit to the four objectives and the
    normalized constraints. A virtual NSGA-II run explores the cheap surrogate,
    and only a small batch of promising/uncertain designs is sent to the real
    CoolProp/PVGIS simulator. This preserves the real simulator as the source
    of truth while sharply reducing expensive evaluations.
    """
    if not HAVE_NUMPY or not HAVE_PYMOO or not HAVE_SKLEARN:
        raise RuntimeError("surrogate-nsga2 requires numpy, pymoo and scikit-learn")
    eval_budget=max(1,int(eval_budget))
    initial_samples=max(1,min(int(initial_samples),eval_budget))
    batch_size=max(1,int(batch_size))
    virtual_pop=max(20,int(virtual_pop)); virtual_gen=max(1,int(virtual_gen))

    cache={}; Xobs=[]; obs=[]; count=0

    def true_eval(x,label):
        nonlocal count
        x=[float(v) for v in x]
        key=_design_key(x)
        if key in cache:
            return cache[key]
        count+=1
        t0=time.perf_counter()
        r=evaluate_design(base_cfg,x,weather,duration_days,dt_min=dt_min)
        r.update({"x":x,"evaluation_id":count,"evaluation_label":label,
                  "wall_time_s":time.perf_counter()-t0})
        cache[key]=r; Xobs.append(x); obs.append(r)
        print(f"[SURR] real {count}/{eval_budget} | feasible={bool(r.get('feasible',False))} | "
              f"reason={r.get('failure_reason','-')} | time={r['wall_time_s']:.1f}s",flush=True)
        return r

    print(f"[SURR] initial DOE: {initial_samples} real evaluations",flush=True)
    # Seed the surrogate with several designs already demonstrated feasible in
    # the real 7-day physics diagnostic, then fill the remainder with screened
    # space-filling points. This avoids the pathological all-infeasible DOE that
    # leaves a constrained GP without any positive feasibility examples.
    baseline=np.array([8.0,10.0,800.0,5.0,8.0,10.0,8000.0,16.0],dtype=float)
    seen_initial=[]
    if initial_samples>=1:
        seen_initial.append(baseline)
    for anchor in _feasibility_anchor_designs():
        if len(seen_initial)>=initial_samples: break
        if _cheap_design_screen(anchor) is None and _design_key(anchor) not in {_design_key(v) for v in seen_initial}:
            seen_initial.append(anchor)
    remaining=initial_samples-len(seen_initial)
    if remaining>0:
        lhs=_sample_screened_lhs(remaining,seed)
        seen_initial.extend(lhs)
    for x in seen_initial[:initial_samples]:
        true_eval(np.asarray(x).tolist(),"initial")

    it=0
    while count < eval_budget:
        it+=1
        Y=np.asarray([r["objectives"]+r["constraints"] for r in obs],dtype=float)
        bank=GPSurrogateBank(); bank.fit(Xobs,Y)

        class SurrogateProblem(Problem):
            def __init__(self):
                xl=np.asarray([v[1] for v in DESIGN_VARIABLES],dtype=float)
                xu=np.asarray([v[2] for v in DESIGN_VARIABLES],dtype=float)
                super().__init__(n_var=len(DESIGN_VARIABLES),n_obj=4,
                                 n_constr=len(CONSTRAINT_SCALES),xl=xl,xu=xu)
            def _evaluate(self,X,out,*args,**kwargs):
                mu,sd=bank.predict(X)
                # Conservative feasibility: use posterior mean + 1.5 sigma.
                out["F"]=mu[:,:4]
                out["G"]=mu[:,4:]+1.5*sd[:,4:]

        virtual_algorithm=NSGA2(pop_size=virtual_pop,sampling=FloatRandomSampling())
        vr=pymoo_minimize(SurrogateProblem(),virtual_algorithm,
                          ("n_gen",virtual_gen),seed=seed+it,verbose=False)
        VX=vr.pop.get("X") if vr.pop is not None else vr.X
        if VX is None or len(VX)==0:
            fallback=_sample_lhs(min(batch_size,eval_budget-count),seed+1000+it)
            VX=np.asarray(fallback)

        mu,sd=bank.predict(VX)
        gu=mu[:,4:]+1.5*sd[:,4:]
        # Until the first real feasible design has been observed, keep the
        # acquisition feasibility-seeking rather than trusting a surrogate that
        # has only seen failures.
        have_feasible=any(bool(r.get("feasible",False)) for r in obs)
        cv=np.sum(np.maximum(0.0,gu),axis=1)
        f=mu[:,:4]

        # Normalize objective predictions using the already-observed true data,
        # avoiding a moving target caused by virtual predictions.
        obsF=Y[:,:4]
        lo=np.min(obsF,axis=0); hi=np.max(obsF,axis=0); span=np.maximum(hi-lo,1e-12)
        fn=np.clip((f-lo)/span,0.0,1.0)
        q=0.20*fn[:,0]+0.20*fn[:,1]+0.20*fn[:,2]+0.40*fn[:,3]
        obj_sigma=np.maximum(bank.y_std[:4],1e-12)
        unc_obj=sd[:,:4]/obj_sigma
        unc_con=sd[:,4:]
        unc=np.mean(np.column_stack([unc_obj,unc_con]),axis=1)
        unc_norm=unc/(np.max(unc)+1e-12)
        feasible_mask=cv<=1e-8
        # Exploit among predicted-feasible designs; use exploration there.
        # For predicted-infeasible designs, uncertainty is not allowed to erase
        # the feasibility penalty and is instead a small additional cost.
        if have_feasible:
            score=np.where(feasible_mask, q-0.10*unc_norm, cv+10.0+q+0.05*unc_norm)
        else:
            # No real feasible observation yet: explicitly minimize predicted
            # constraint violation while retaining a mild uncertainty bonus.
            score=cv+0.10*q-0.05*unc_norm

        order=np.argsort(score)
        selected=[]
        for idx in order:
            if _design_key(VX[idx].tolist()) not in cache:
                selected.append(VX[idx])
            if len(selected)>=batch_size:
                break
        if len(selected)<min(batch_size,eval_budget-count):
            for idx in np.argsort(-unc):
                if _design_key(VX[idx].tolist()) not in cache and all(_design_key(v.tolist())!=_design_key(VX[idx].tolist()) for v in selected):
                    selected.append(VX[idx])
                if len(selected)>=min(batch_size,eval_budget-count):
                    break
        if not selected:
            # Never loop forever because a virtual front may be entirely cached.
            for trial in range(100):
                candidate=_sample_lhs(1,seed+1000*it+trial)[0]
                if _design_key(candidate.tolist()) not in cache:
                    selected=[candidate]; break
        if not selected:
            print("[SURR] no new candidate available; stopping safely",flush=True)
            break

        print(f"[SURR] iteration={it} | observed={len(obs)} | true batch={len(selected)} | virtual={len(VX)}",flush=True)
        for x in selected[:min(batch_size,eval_budget-count)]:
            true_eval(x.tolist(),f"surrogate_{it}")

    try:
        write_csv("surrogate_observations.csv",[
            {**{f"x{i}":r["x"][i] for i in range(len(r["x"]))},
             "feasible":r.get("feasible",False),"failure_reason":r.get("failure_reason"),
             "compressor_kWh":r["objectives"][3],"run_time_s":r.get("wall_time_s","")}
            for r in obs
        ])
    except Exception:
        pass
    return obs


def run_diagnostic_evaluations(base_cfg, weather, duration_days=7.0, dt_min=15.0, make_plots=True):
    """PV-vs-battery tradeoff diagnostic with a fixed reference plant."""
    fixed = (1000.0, 10.0, 12.0, 18.0, 12000.0, 40.0)
    pairs = [
        (12.0,20.0),(12.0,25.0),(12.0,30.0),(12.0,35.0),
        (14.0,25.0),(14.0,30.0),(14.0,35.0),
        (16.0,20.0),(16.0,25.0),(16.0,30.0),(16.0,35.0),
        (18.0,20.0),(18.0,25.0),(18.0,30.0),(18.0,35.0),
    ]
    tests = {}
    pcm, comp, evap, cond, airflow, hx = fixed
    for pv, batt in pairs:
        tests[f"pv{pv:g}_battery{batt:g}"] = [pv,batt,pcm,comp,evap,cond,airflow,hx]
    print(f"[DIAG] PV-vs-battery tradeoff | days={duration_days:g} | dt={dt_min:g} min", flush=True)
    print("[DIAG] Fixed plant: PCM=1000 kg, compressor=10 kW, evaporator=12 kW, condenser=18 kW, airflow=12000 m3/h, PCM HX=40 m2", flush=True)
    rows=[]
    for name,x in tests.items():
        t0=time.perf_counter()
        r=evaluate_design(base_cfg,x,weather,duration_days=duration_days,dt_min=dt_min)
        elapsed=time.perf_counter()-t0
        print(f"\n[DIAG] {name}", flush=True)
        print(f"        x={dict(zip([v[0] for v in DESIGN_VARIABLES], x))}", flush=True)
        print(f"        feasible={r.get('feasible')} | reason={r.get('failure_reason','-')} | time={elapsed:.2f}s", flush=True)
        print(f"        peak_room={r.get('peak_room_C',float('nan')):.3f} C | peak_aged_product={r.get('peak_product_aged_C',float('nan')):.3f} C", flush=True)
        print(f"        RH={r.get('rh_min',float('nan')):.2f}..{r.get('rh_max',float('nan')):.2f}% | RH_high={r.get('rh_high_hours',float('nan')):.2f} h", flush=True)
        print(f"        battery_SOC_min={r.get('battery_soc_min',float('nan')):.3f} | PCM_SOC={r.get('pcm_soc_min',float('nan')):.3f}..{r.get('pcm_soc_max',float('nan')):.3f}", flush=True)
        print(f"        compressor={r.get('compressor_kWh',float('nan')):.2f} kWh | refrig_unmet={r.get('unmet_kWh',float('nan')):.3f} kWh | refrig_bad_steps={r.get('refrig_infeasible_steps',-1)}", flush=True)
        print(f"        dispatch_cause_unmet_kWh={r.get('dispatch_cause_unmet_kWh',{})}", flush=True)
        print(f"        fan_only_hours={r.get('fan_only_hours',float('nan'))} | fan_energy_savings_kWh={r.get('fan_energy_savings_kWh',float('nan'))}", flush=True)
        rows.append({
            "case":name,"pv_kWp":x[0],"battery_kWh":x[1],"feasible":r.get("feasible",False),
            "failure_reason":r.get("failure_reason",""),"peak_room_C":r.get("peak_room_C",""),
            "peak_product_aged_C":r.get("peak_product_aged_C",""),"rh_high_hours":r.get("rh_high_hours",""),
            "unmet_kWh":r.get("unmet_kWh",""),"refrig_bad_steps":r.get("refrig_infeasible_steps",""),
            "battery_soc_min":r.get("battery_soc_min",""),"pcm_soc_min":r.get("pcm_soc_min",""),
            "pcm_soc_max":r.get("pcm_soc_max",""),"compressor_kWh":r.get("compressor_kWh",""),
            "fan_only_hours":r.get("fan_only_hours",""),"fan_energy_savings_kWh":r.get("fan_energy_savings_kWh",""),
            "dispatch_cause_unmet_kWh":json.dumps(r.get("dispatch_cause_unmet_kWh",{}),sort_keys=True),
        })
    write_csv("pv_battery_tradeoff_summary.csv",rows)
    print("\n[DIAG] wrote pv_battery_tradeoff_summary.csv",flush=True)
    if make_plots:
        generate_sih_visuals(rows, output_dir=".")
    return rows




def run_optimization(cfg,weather,source,pop_size,n_gen,seed,duration_days,opt_dt_min,final_dt_min,convergence_dt_min,n_seeds=1,progress_every=25,optimizer_name="nsga2",initial_samples=8,eval_budget=16,batch_size=2,virtual_pop=48,virtual_gen=5,run_robustness_flag=False,workers=8):
    from .analysis import generate_optimization_visuals, write_csv
    global LAST_NSGA2_HISTORY_ALL
    LAST_NSGA2_HISTORY_ALL=[]
    all_candidates=[]
    if optimizer_name=="nsga2":
        print(f"[INFO] Proper constrained NSGA-II: {pop_size} population × {n_gen} generations; parallel real evaluations at {opt_dt_min:g}-min timestep",flush=True)
    else:
        print(f"[INFO] Surrogate annual optimization: {eval_budget} real evaluations at {opt_dt_min:g}-min timestep",flush=True)
    for k in range(max(1,n_seeds)):
        rs=seed+k; print(f"[RUN] seed={rs} | engine={optimizer_name}",flush=True)
        r=run_surrogate_nsga2(cfg,weather,rs,duration_days,opt_dt_min,initial_samples,eval_budget,batch_size,virtual_pop,virtual_gen) if optimizer_name=="surrogate-nsga2" else run_nsga2(cfg,weather,pop_size,n_gen,rs,duration_days,opt_dt_min,progress_every,workers)
        for c in r: c["run_seed"]=rs
        all_candidates.extend(r)
    unique={_design_key(c["x"]):c for c in all_candidates}; candidates=list(unique.values()); front=pareto_front(candidates)
    feasible=[c for c in candidates if c.get("feasible",False)]
    print(f"[RESULT] unique real evaluations={len(candidates)} | feasible={len(feasible)} | Pareto={len(front)}",flush=True)
    write_csv("optimization_results.csv",[{**{f"x{i}":c["x"][i] for i in range(len(c["x"]))},
             "pv_kWp":c["objectives"][0],"battery_kWh":c["objectives"][1],"pcm_kg":c["objectives"][2],"compressor_kWh":c["objectives"][3],
             "feasible":c["feasible"],"failure_reason":c.get("failure_reason"),"unmet_kWh":c.get("unmet_kWh",""),
             "rh_violation_hours":c.get("rh_violation_hours",""),"battery_soc_min":c.get("battery_soc_min",""),"pcm_soc_min":c.get("pcm_soc_min",""),
             "pcm_candidate":c.get("pcm_candidate",cfg.pcm.candidate),"pcm_transition_C":c.get("pcm_transition_C",""),
             "pcm_conductivity_W_mK":c.get("pcm_conductivity_W_mK",""),"pcm_usable_enthalpy_J_kg":c.get("pcm_usable_enthalpy_J_kg",""),
             "pcm_leakage_pct":c.get("pcm_leakage_pct",""),"pcm_cycle_retention_500":c.get("pcm_cycle_retention_500",""),
             "pcm_cost_INR_kg":c.get("pcm_cost_INR_kg",""),"pcm_data_status":c.get("pcm_data_status",PCM_LIBRARY[cfg.pcm.candidate].data_status)}
             for c in candidates])
    with open("optimization_results.json","w") as f: json.dump(candidates,f,indent=2,default=str)
    write_csv("pareto_front.csv",[{"pv_kWp":c["objectives"][0],"battery_kWh":c["objectives"][1],"pcm_kg":c["objectives"][2],"compressor_kWh":c["objectives"][3],"feasible":c.get("feasible",False)} for c in front])

    best=compromise_select(front) if front else None
    if best is None and candidates:
        def rank_key(c):
            cv=sum(max(0.0,float(g)) for g in c.get("constraints",[]))
            o=c.get("objectives",[1e9]*4)
            return (cv,float(o[0]),float(o[1]),float(o[2]),float(o[3]))
        best=min(candidates,key=rank_key)
        print("[RESULT] No feasible Pareto point yet; reporting least-violating observed design for diagnostic guidance.",flush=True)

    if best is not None:
        x=best["x"]
        print("\n[OPTIMAL / RECOMMENDED DESIGN]",flush=True)
        names=[v[0] for v in DESIGN_VARIABLES]
        for name,val in zip(names,x): print(f"  {name:32s}= {float(val):.6g}",flush=True)
        print(f"  feasible                         = {bool(best.get('feasible',False))}",flush=True)
        print(f"  annual unmet refrigeration       = {float(best.get('unmet_kWh',float('nan'))):.6g} kWh",flush=True)
        print(f"  RH violation                     = {float(best.get('rh_violation_hours',float('nan'))):.6g} h",flush=True)

        print(f"[VALIDATION] final dt={final_dt_min:g} min",flush=True)
        final_eval=evaluate_design(cfg,x,weather,duration_days,dt_min=final_dt_min)
        print(f"[VALIDATION] feasible={final_eval.get('feasible')} | unmet={final_eval.get('unmet_kWh',float('nan'))} kWh | RH violation={final_eval.get('rh_violation_hours',float('nan'))} h",flush=True)
        if final_eval.get("failure_reason"):
            print(f"[VALIDATION] failure_reason={final_eval.get('failure_reason')}",flush=True)
        fd=final_eval.get("failure_details")
        if isinstance(fd,dict) and fd.get("exception_type"):
            print(f"[VALIDATION] exception={fd.get('exception_type')}: {fd.get('exception_message')}",flush=True)
            if DEBUG_MODE and fd.get("traceback"):
                print(fd["traceback"],flush=True)
        print(f"[VALIDATION] convergence dt={convergence_dt_min:g} min",flush=True)
        conv=evaluate_design(cfg,x,weather,duration_days,dt_min=convergence_dt_min)
        print(f"[VALIDATION] convergence feasible={conv.get('feasible')} | unmet={conv.get('unmet_kWh',float('nan'))} kWh | RH violation={conv.get('rh_violation_hours',float('nan'))} h",flush=True)
        if conv.get("failure_reason"):
            print(f"[VALIDATION] convergence failure_reason={conv.get('failure_reason')}",flush=True)
        fd=conv.get("failure_details")
        if isinstance(fd,dict) and fd.get("exception_type"):
            print(f"[VALIDATION] convergence exception={fd.get('exception_type')}: {fd.get('exception_message')}",flush=True)
            if DEBUG_MODE and fd.get("traceback"):
                print(fd["traceback"],flush=True)
        rec={"pcm_candidate":cfg.pcm.candidate,
             "pcm_properties":asdict(PCM_LIBRARY[cfg.pcm.candidate]),
             "design_variables":dict(zip(names,x)),"optimization_engine":optimizer_name,"optimization_objectives":best["objectives"],
             "optimization_constraints_raw":best.get("raw_constraints"),"optimization_feasible":best.get("feasible",False),
             "final_validation":final_eval,"convergence_validation":conv,
             "search_bounds":{row[0]:[row[1],row[2]] for row in DESIGN_VARIABLES}}
        with open("recommended_design.json","w") as f: json.dump(rec,f,indent=2,default=str)
        write_csv("simulation_trace.csv",simulate_transient(apply_design_vector(cfg,x),weather,duration_days,dt_min=final_dt_min))
        if run_robustness_flag:
            rr=run_robustness(cfg,x,weather,duration_days,dt_min=opt_dt_min); write_csv("robustness_results.csv",rr)
    else:
        print("[RESULT] No valid candidate was produced.",flush=True)

    plot_paths = generate_optimization_visuals(candidates,front,best,output_dir="optimization_figures",history_records=LAST_NSGA2_HISTORY_ALL)
    print("[OUTPUT] optimization artifacts written:", flush=True)
    for path in ["optimization_results.csv","optimization_results.json","pareto_front.csv","recommended_design.json","simulation_trace.csv"]:
        print(f"        {os.path.abspath(path)}", flush=True)
    for key, path in plot_paths.items():
        print(f"        plot[{key}]={os.path.abspath(path)}", flush=True)
    return best,candidates,front


