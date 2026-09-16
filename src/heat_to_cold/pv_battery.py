from __future__ import annotations
import math
from . import CandidateInfeasible, BatteryConfig, PVConfig

def pv_dc_power_W(cfg: PVConfig, G_poa_W_m2: float, T_amb_C: float) -> dict:
    T_cell = T_amb_C + cfg.cell_temp_rise_coeff_K_m2_W * G_poa_W_m2
    P_rated_W = cfg.rated_power_kWp * 1000.0
    if cfg.G_STC_W_m2 > 0:
        P_dc_raw = P_rated_W * (G_poa_W_m2 / cfg.G_STC_W_m2) * (
            1.0 + cfg.temperature_coefficient_per_K * (T_cell - cfg.reference_cell_temperature_C))
    else:
        P_dc_raw = 0.0
    P_dc_raw = max(0.0, P_dc_raw)
    P_bus = P_dc_raw * (1.0 - cfg.dc_wiring_loss_fraction) * cfg.mppt_efficiency
    return {"T_cell_C": T_cell, "P_dc_raw_W": P_dc_raw, "P_pv_bus_W": P_bus}


class BatteryState:
    def __init__(self, cfg: BatteryConfig):
        self.cfg = cfg
        self.soc = cfg.initial_SOC

    @property
    def energy_J(self) -> float:
        return self.soc * self.cfg.nominal_energy_kWh * 3.6e6

    def max_charge_W(self) -> float:
        return self.cfg.maximum_charge_C_rate * self.cfg.nominal_energy_kWh * 1000.0

    def max_discharge_W(self) -> float:
        return self.cfg.maximum_discharge_C_rate * self.cfg.nominal_energy_kWh * 1000.0

    def step(self, dt_s: float, P_request_W: float, floor_soc: Optional[float] = None) -> dict:
        """
        Advance the battery by one timestep.

        P_request_W > 0 -> charge the battery;
        P_request_W < 0 -> discharge and deliver power to the DC bus.

        ``floor_soc`` is an optional operational reserve. When supplied, the
        battery is never intentionally discharged or self-discharged below that
        floor. This keeps the physical state consistent with the supervisory
        reserve used by the plant controller.
        """
        if dt_s <= 0.0:
            return {"soc": self.soc, "P_bus_effect_W": 0.0, "standby_loss_W": 0.0}

        cap_J = self.cfg.nominal_energy_kWh * 3.6e6
        if cap_J <= 0.0:
            self.soc = 0.0
            return {"soc": self.soc, "P_bus_effect_W": 0.0, "standby_loss_W": 0.0}

        lower_soc = self.cfg.minimum_SOC if floor_soc is None else max(self.cfg.minimum_SOC, float(floor_soc))
        lower_soc = min(lower_soc, self.cfg.maximum_SOC)

        standby_loss_W = self.cfg.standby_loss_fraction_per_hour * self.energy_J / 3600.0

        if P_request_W >= 0.0:
            p_actual = min(P_request_W, self.max_charge_W())
            headroom_J = max(0.0, (self.cfg.maximum_SOC - self.soc) * cap_J)
            p_actual = min(p_actual, headroom_J / dt_s)
            dE = p_actual * self.cfg.charge_converter_efficiency * dt_s
            p_bus_effect = -p_actual
        else:
            p_actual = min(-P_request_W, self.max_discharge_W())
            available_J = max(0.0, (self.soc - lower_soc) * cap_J)
            p_actual = min(p_actual, available_J * self.cfg.discharge_converter_efficiency / dt_s)
            dE = -p_actual / self.cfg.discharge_converter_efficiency * dt_s
            p_bus_effect = p_actual

        # Apply standby/self-discharge only to energy above the operating floor.
        # The previous implementation subtracted standby loss after the discharge
        # clamp, allowing SOC to drift below the configured reserve.
        energy_before_J = self.energy_J
        max_standby_loss_J = max(0.0, (self.soc - lower_soc) * cap_J)
        standby_loss_J = min(standby_loss_W * dt_s, max_standby_loss_J)
        dE -= standby_loss_J

        new_energy_J = energy_before_J + dE
        floor_energy_J = lower_soc * cap_J
        max_energy_J = self.cfg.maximum_SOC * cap_J
        new_energy_J = min(max(new_energy_J, floor_energy_J), max_energy_J)
        self.soc = new_energy_J / cap_J

        actual_standby_loss_W = standby_loss_J / dt_s
        return {
            "soc": self.soc,
            "P_bus_effect_W": p_bus_effect,
            "standby_loss_W": actual_standby_loss_W,
            "operating_floor_SOC": lower_soc,
        }


def electrical_bus_balance(P_pv_W: float, P_load_W: float, P_battery_bus_effect_W: float) -> dict:
    """Canonical balance: P_PV - P_load - P_battery - P_curtail + P_unmet = 0.
    P_battery_bus_effect_W > 0 means battery is delivering power to the bus."""
    net = P_pv_W - P_load_W + P_battery_bus_effect_W
    if net >= 0:
        curtail = net
        unmet = 0.0
    else:
        curtail = 0.0
        unmet = -net
    residual = P_pv_W - P_load_W - (-P_battery_bus_effect_W) - curtail + unmet
    return {"P_curtailment_W": curtail, "P_unmet_W": unmet, "bus_residual_W": residual}


# =====================================================================
# CHUNK 09 -- Supervisory Control
# =====================================================================

