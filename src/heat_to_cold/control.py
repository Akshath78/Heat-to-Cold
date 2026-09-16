from __future__ import annotations
from dataclasses import dataclass
from typing import Optional
from . import CandidateInfeasible, ControlConfig, MasterConfig, RefrigerationConfig
from .pv_battery import BatteryState
from .product import ProductState
from .pcm import PCMState
from .refrigeration import RefrigerationResult, evaporator_airside_limit, solve_refrigeration_cycle, MockR290Backend
from .psychro import humidity_ratio_from_rh, rh_from_humidity_ratio, moist_air_enthalpy_J_kg, moist_air_density_kg_m3

@dataclass
class ControlState:
    compressor_running: bool = False
    minutes_since_start: float = 0.0
    minutes_since_stop: float = 1e6
    compressor_actual_fraction: float = 0.0
    dehumidifying: bool = False
    humidifying: bool = False


def is_daytime(poa_W_m2: float) -> bool:
    return poa_W_m2 > 20.0


def controller_step(cfg: ControlConfig, state: ControlState, dt_min: float, inputs: dict) -> dict:
    T_room = inputs["T_room_C"]
    T_product_max = inputs["T_product_hottest_core_C"]
    rh = inputs["rh_pct"]
    pcm_soc = inputs["pcm_soc"]
    battery_soc = inputs["battery_soc"]
    daytime = inputs["daytime"]

    emergency = (T_room >= cfg.emergency_room_C) or (T_product_max >= cfg.emergency_product_C)

    # Dehumidification demand (the cooling coil is the only dehumidifier, so a
    # rising-RH condition is a reason to run the compressor even when the PCM is
    # handling temperature; otherwise night moisture accumulates unbounded).
    # Triggering at the RH target (not the higher dehum-on threshold) keeps the
    # coil engaged to HOLD the target, avoiding a one-step overshoot to
    # saturation at the day->night handoff when the PCM takes over cooling.
    # Start dehumidification slightly before the target so the discrete controller
    # does not overshoot the 98% ceiling between 15-minute updates.  A stronger
    # demand is allowed while already in the dehumidification state.
    dehum_need = (rh >= cfg.rh_target_pct - 1.0) or (
        state.dehumidifying and rh > cfg.dehum_off_pct
    )

    # Compressor enable/fraction decision. The plant modulates delivered cooling
    # to the actual load (utilization), so the controller keeps the compressor
    # ENABLED to hold the setpoint rather than bang-banging off the instant the
    # room reaches it (which would short-cycle with large temperature swings on
    # the small-capacitance air node). A small deadband below setpoint releases
    # it when genuinely satisfied.
    cooling_needed = (T_room > cfg.room_setpoint_C) or (T_product_max > cfg.product_upper_C)
    maintain = state.compressor_running and (T_room > cfg.room_setpoint_C + 0.25)
    dehum_only = dehum_need and not cooling_needed and not emergency
    active = emergency or cooling_needed or maintain or dehum_only

    request_fraction = 0.0
    if active:
        if emergency:
            request_fraction = cfg.compressor_max_fraction
        elif cooling_needed or maintain:
            # Temperature-control demand is the primary compressor duty.
            if daytime or battery_soc > cfg.battery_reserve_SOC:
                request_fraction = cfg.compressor_max_fraction
        elif dehum_only:
            # Humidity control must not force the room below setpoint.  Ask for
            # full available compressor authority so the plant can supply the
            # latent duty actually required by the wet-coil calculation.  The
            # plant subsequently computes the minimum fraction needed from the
            # latent target and limits it by electrical availability.
            if daytime or battery_soc > cfg.battery_reserve_SOC:
                request_fraction = cfg.compressor_max_fraction

    can_start = state.minutes_since_stop >= cfg.minimum_off_time_min
    can_stop = state.minutes_since_start >= cfg.minimum_on_time_min

    enable = request_fraction > 0.0
    if enable and not state.compressor_running and not can_start and not emergency:
        enable = False
        request_fraction = 0.0
    if (not enable) and state.compressor_running and not can_stop and not emergency:
        enable = True
        request_fraction = max(request_fraction, cfg.compressor_min_fraction)

    # Charging PCM is a real refrigeration duty. During daytime, a requested
    # PCM charge therefore keeps the compressor enabled even when the room itself
    # is already at setpoint. The transient plant will still limit the resulting
    # fraction by electrical power and refrigeration capacity.
    if daytime and inputs.get("pv_surplus_W", 0.0) > 0.0 and pcm_soc < 0.98:
        if not state.compressor_running and state.minutes_since_stop < cfg.minimum_off_time_min and not emergency:
            pass  # normal minimum-off timer is respected; plant can charge later
        else:
            enable = True
            request_fraction = max(request_fraction, cfg.compressor_max_fraction)

    target_fraction = max(cfg.compressor_min_fraction, request_fraction) if enable else 0.0

    max_step = cfg.ramp_rate_fraction_per_min * dt_min
    delta = target_fraction - state.compressor_actual_fraction
    delta = max(-max_step, min(max_step, delta))
    new_fraction = max(0.0, min(1.0, state.compressor_actual_fraction + delta))

    if new_fraction > 0.0 and not state.compressor_running:
        state.minutes_since_start = 0.0
    if new_fraction <= 0.0 and state.compressor_running:
        state.minutes_since_stop = 0.0

    state.compressor_running = new_fraction > 0.0
    state.compressor_actual_fraction = new_fraction
    if state.compressor_running:
        state.minutes_since_start += dt_min
        state.minutes_since_stop = 0.0
    else:
        state.minutes_since_stop += dt_min
        state.minutes_since_start = 0.0

    # PCM charge/discharge intent (mutually exclusive)
    pcm_charge_W = 0.0
    pcm_discharge_W = 0.0
    if daytime and inputs.get("pv_surplus_W", 0.0) > 0.0 and pcm_soc < 0.98:
        # pv_surplus_W is electrical power. Convert to allowable PCM thermal
        # charging duty using the configured charging COP. Do not impose a
        # second hard-coded 3 kW limit below the PCMConfig charge-power limit;
        # that artificial cap can prevent full recharge during the solar window.
        charge_cop = max(0.1, inputs.get("pcm_charge_cop", 2.5))
        charge_cap = max(0.0, float(inputs.get("pcm_max_charge_power_W", 4000.0)))
        pcm_charge_W = min(inputs.get("pv_surplus_W", 0.0) * charge_cop, charge_cap)
    elif (not daytime) and cooling_needed and pcm_soc > 0.0:
        # Nighttime PCM priority is enforced by the plant-level dispatch, which
        # requests the full prospective sensible load and then applies the actual
        # HX/enthalpy limits. Keep this controller intent non-binding and avoid a
        # separate 2 kW hard cap that can under-utilize the PCM.
        pcm_discharge_W = None

    # RH hysteresis
    if not state.dehumidifying and rh >= cfg.dehum_on_pct:
        state.dehumidifying = True
    elif state.dehumidifying and rh <= cfg.dehum_off_pct:
        state.dehumidifying = False

    if not state.humidifying and rh <= cfg.humid_on_pct:
        state.humidifying = True
    elif state.humidifying and rh >= cfg.humid_off_pct:
        state.humidifying = False
    if rh >= cfg.rh_target_pct:
        state.humidifying = False

    return {
        "compressor_fraction_command": new_fraction,
        "pcm_charge_W": pcm_charge_W,
        "pcm_discharge_W": pcm_discharge_W,
        "dehumidify": state.dehumidifying,
        "dehum_need": dehum_need,
        "humidify": state.humidifying,
        "emergency": emergency,
    }


# LATENT CONTROL REVISION: sensible and latent requirements are dispatched
# independently; refrigeration remains constrained by CoolProp cycle,
# coil capacity, PV/battery electrical availability, and PCM state.
# =====================================================================
# CHUNK 10 -- Coupled Transient Plant Simulator
# =====================================================================

@dataclass
class PlantState:
    T_room_C: float
    w_room: float
    product: ProductState
    pcm: PCMState
    battery: BatteryState
    control: ControlState
    t_h: float = 0.0


def initial_plant_state(cfg: MasterConfig, initial_room_C: Optional[float] = None,
                        initial_product_C: Optional[float] = None, initial_rh_pct: Optional[float] = None) -> PlantState:
    T0 = cfg.control.room_setpoint_C if initial_room_C is None else initial_room_C
    product_T0 = T0 if initial_product_C is None else initial_product_C
    rh0 = cfg.control.rh_target_pct if initial_rh_pct is None else initial_rh_pct
    w0 = humidity_ratio_from_rh(T0, rh0, cfg.psychro.atmospheric_pressure_Pa)
    # Start the nominal plant at the requested room setpoint with the compressor
    # OFF. The controller is allowed to start it immediately when thermal or
    # humidity demand actually exists. This avoids injecting a hidden compressor
    # electrical load during the first night hours.
    control = ControlState(compressor_running=False, minutes_since_start=0.0,
                           minutes_since_stop=1e6, compressor_actual_fraction=0.0)
    return PlantState(
        T_room_C=T0, w_room=w0, product=ProductState(cfg), pcm=PCMState(cfg.pcm),
        battery=BatteryState(cfg.battery), control=control, t_h=0.0,
    )


def _battery_available_bus_power_W(cfg: MasterConfig, battery: BatteryState, dt_s: float) -> float:
    """Maximum battery power that can be delivered while respecting the
    supervisory reserve SOC and the battery's own minimum SOC.

    This is only a pre-dispatch availability calculation; BatteryState.step
    remains the single state-update mechanism.
    """
    if dt_s <= 0.0:
        return 0.0
    reserve_soc = max(cfg.battery.minimum_SOC, cfg.control.battery_reserve_SOC)
    cap_J = cfg.battery.nominal_energy_kWh * 3.6e6
    available_J = max(0.0, (battery.soc - reserve_soc) * cap_J)
    by_energy = available_J * cfg.battery.discharge_converter_efficiency / dt_s
    return max(0.0, min(battery.max_discharge_W(), by_energy))


def _compressor_result_at_fraction(cfg: MasterConfig, T_evap_C: float, T_cond_C: float,
                                   fraction: float, backend: Optional[MockR290Backend],
                                   T_room_C: Optional[float] = None,
                                   T_amb_C: Optional[float] = None) -> RefrigerationResult:
    """Solve the unchanged refrigerant-cycle model at a specific compressor
    fraction. A zero fraction is treated as compressor-off rather than the
    cycle's finite minimum-speed point.
    """
    if fraction <= 0.0:
        return RefrigerationResult(0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
                                   T_evap_C, T_cond_C, 0.0, True, True)
    return solve_refrigeration_cycle(
        cfg, T_evap_C, T_cond_C, fraction, backend,
        T_room_C=T_room_C, T_amb_C=T_amb_C,
    )


def _affine_rate_interpolation(f0: float, r0: float, f1: float, r1: float, target: float) -> float:
    if abs(r1 - r0) <= 1e-12:
        return f1 if target >= r1 else f0
    f = f0 + (target - r0) * (f1 - f0) / (r1 - r0)
    return max(min(f, max(f0, f1)), min(f0, f1))


def _fraction_for_condenser_capacity(cfg: RefrigerationConfig, T_evap_C: float, T_cond_C: float,
                                      T_amb_C: Optional[float], command_fraction: float,
                                      backend: Optional[MockR290Backend],
                                      T_room_C: Optional[float] = None) -> float:
    """Maximum compressor fraction that the installed condenser can reject.

    At fixed evaporating/condensing temperatures, refrigerant properties are
    fixed and refrigerant mass flow (and therefore Q_cond) is affine with
    compressor fraction. The installed condenser UA is therefore enforced as
    a hard dispatch limit before the final cycle result is accepted.

    The model deliberately does not raise T_cond to hide an undersized
    condenser; the installed UA must support the selected operating point.
    """
    command_fraction = max(0.0, min(1.0, command_fraction))
    if command_fraction <= 0.0 or T_amb_C is None:
        return command_fraction
    q_capacity = max(0.0, cfg.condenser_UA_W_K * (T_cond_C - T_amb_C))
    if q_capacity <= 1e-12:
        return 0.0
    r_cmd = _compressor_result_at_fraction(
        cfg, T_evap_C, T_cond_C, command_fraction, backend, T_room_C, T_amb_C
    )
    q_cond_cmd = max(0.0, r_cmd.Q_cond_W)
    if q_cond_cmd <= q_capacity + 1e-9:
        return command_fraction
    return max(0.0, min(command_fraction, command_fraction * q_capacity / q_cond_cmd))


def _fraction_for_power(cfg: RefrigerationConfig, T_evap_C: float, T_cond_C: float,
                        power_budget_W: float, command_fraction: float,
                        backend: Optional[MockR290Backend],
                        T_room_C: Optional[float] = None, T_amb_C: Optional[float] = None) -> float:
    """Fast exact solve for the current fixed-efficiency compressor model.

    For fixed T_evap/T_cond, refrigerant properties are fixed and mass flow,
    evaporator capacity and electrical power are affine in compressor fraction.
    This replaces the old 32-iteration bisection with two cycle evaluations.
    """
    command_fraction = max(0.0, min(1.0, command_fraction))
    if command_fraction <= 0.0 or power_budget_W <= 0.0:
        return 0.0
    eps = 1e-9
    r0 = _compressor_result_at_fraction(cfg, T_evap_C, T_cond_C, eps, backend, T_room_C, T_amb_C)
    r1 = _compressor_result_at_fraction(cfg, T_evap_C, T_cond_C, command_fraction, backend, T_room_C, T_amb_C)
    if r1.W_elec_W <= power_budget_W + 1e-9:
        return command_fraction
    if r0.W_elec_W > power_budget_W + 1e-9:
        return 0.0
    return _affine_rate_interpolation(eps, r0.W_elec_W, command_fraction, r1.W_elec_W, power_budget_W)


def _fraction_for_evap_demand(cfg: RefrigerationConfig, T_evap_C: float, T_cond_C: float,
                              required_evap_W: float, airside_room_limit_W: float,
                              command_fraction: float, backend: Optional[MockR290Backend],
                              T_room_C: Optional[float] = None, T_amb_C: Optional[float] = None) -> float:
    """Fast exact solve for required evaporator duty under the current model."""
    command_fraction = max(0.0, min(1.0, command_fraction))
    required_evap_W = max(0.0, required_evap_W)
    airside_room_limit_W = max(0.0, airside_room_limit_W)
    if required_evap_W <= 1e-9 or command_fraction <= 0.0:
        return 0.0
    if required_evap_W > airside_room_limit_W + 1e-9:
        return command_fraction
    eps = 1e-9
    r0 = _compressor_result_at_fraction(cfg, T_evap_C, T_cond_C, eps, backend, T_room_C, T_amb_C)
    r1 = _compressor_result_at_fraction(cfg, T_evap_C, T_cond_C, command_fraction, backend, T_room_C, T_amb_C)
    if r0.Q_evap_W >= required_evap_W - 1e-9:
        return eps
    if r1.Q_evap_W < required_evap_W - 1e-9:
        return command_fraction
    return _affine_rate_interpolation(eps, r0.Q_evap_W, command_fraction, r1.Q_evap_W, required_evap_W)


def _coherent_evaporator_airside(cfg: RefrigerationConfig, rho_air: float, T_room_C: float,
                                 w_room: float, P_Pa: float, T_evap_C: float) -> dict:
    air_mdot = rho_air * cfg.evaporator_airflow_m3_h / 3600.0
    if air_mdot <= 0.0:
        return {
            "Q_total_limit_W": 0.0, "Q_sens_limit_W": 0.0,
            "Q_lat_limit_W": 0.0, "condensate_limit_kg_s": 0.0,
            "coil": {},
        }
    return evaporator_airside_limit(
        cfg, air_mdot, T_room_C, w_room, P_Pa, T_evap_C
    )


