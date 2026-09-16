from __future__ import annotations
import math
from . import CandidateInfeasible, MasterConfig, PCMConfig, PCMCandidate, PCM_LIBRARY

class PCMState:
    """Lumped PCM enthalpy state with explicit physical energy bounds.

    Sign convention is unchanged from the original model:
      Q_into_pcm > 0 : room -> PCM (PCM provides cooling)
      Q_into_pcm < 0 : PCM -> chiller (PCM is charged/frozen)

    The implementation keeps the existing enthalpy formulation but prevents
    the internal state from ever leaving the admissible PCM enthalpy range.
    """

    def __init__(self, cfg: PCMConfig):
        self.cfg = cfg
        if cfg.candidate not in PCM_LIBRARY:
            raise ValueError(f"Unknown PCM candidate: {cfg.candidate}")
        if cfg.pcm_mass_kg <= 0.0:
            raise ValueError("PCM mass must be positive")
        self.candidate = PCM_LIBRARY[cfg.candidate]
        self.candidate.validate_for_optimization()
        self.T_ref_C = self.candidate.T_solidus_C - 5.0

        self.E_min_J = cfg.pcm_mass_kg * self._enthalpy_specific(self.T_ref_C)
        self.E_max_J = cfg.pcm_mass_kg * self._enthalpy_specific(self.candidate.T_liquidus_C + 10.0)
        # Define initial SOC exactly in the same enthalpy space used by soc().
        # This avoids the previous mismatch where a nominal SOC of 0.5 did not
        # actually produce soc() == 0.5 because of the latent/sensible regions.
        initial_soc = max(0.0, min(1.0, float(cfg.initial_soc)))
        self.E_J = self._clamp_energy(
            self.E_max_J - initial_soc * (self.E_max_J - self.E_min_J)
        )

    def _enthalpy_specific(self, T_C: float) -> float:
        c = self.candidate
        h_solidus = c.cp_solid_J_kgK * (c.T_solidus_C - self.T_ref_C)
        if T_C <= c.T_solidus_C:
            return c.cp_solid_J_kgK * (T_C - self.T_ref_C)
        if T_C >= c.T_liquidus_C:
            return h_solidus + c.transition_enthalpy_J_kg + c.cp_liquid_J_kgK * (T_C - c.T_liquidus_C)
        frac = (T_C - c.T_solidus_C) / (c.T_liquidus_C - c.T_solidus_C)
        return h_solidus + frac * c.transition_enthalpy_J_kg

    def _clamp_energy(self, E_J: float) -> float:
        return max(self.E_min_J, min(self.E_max_J, E_J))

    def temperature_and_fraction(self) -> tuple[float, float]:
        c = self.candidate
        h = self.E_J / self.cfg.pcm_mass_kg
        h_solidus = c.cp_solid_J_kgK * (c.T_solidus_C - self.T_ref_C)
        h_liquidus = h_solidus + c.transition_enthalpy_J_kg
        if h <= h_solidus:
            T = self.T_ref_C + h / c.cp_solid_J_kgK
            return T, 0.0
        if h >= h_liquidus:
            T = c.T_liquidus_C + (h - h_liquidus) / c.cp_liquid_J_kgK
            return T, 1.0
        alpha = (h - h_solidus) / c.transition_enthalpy_J_kg
        T = c.T_solidus_C + alpha * (c.T_liquidus_C - c.T_solidus_C)
        return T, alpha

    def soc(self) -> float:
        """Cold SOC: 1 = fully charged/frozen, 0 = fully warm within model bounds."""
        span = self.E_max_J - self.E_min_J
        if span <= 0.0:
            return 0.0
        warm_fraction = (self.E_J - self.E_min_J) / span
        return max(0.0, min(1.0, 1.0 - warm_fraction))

    def max_heat_into_W(self, fluid_T_C: float) -> float:
        """Maximum physically transferable rate into PCM (positive = discharge,
        negative = charge), before power/enthalpy limits. This is used by the
        plant before compressor dispatch so the requested PCM duty is never
        larger than the actual PCM HX can accept."""
        T_pcm_C, _ = self.temperature_and_fraction()
        hx_area = max(self.cfg.hx_area_m2, 0.0)
        requested_positive = fluid_T_C >= T_pcm_C
        hx_u_base = self.cfg.hx_U_discharge_W_m2K if requested_positive else self.cfg.hx_U_charge_W_m2K
        hx_u = hx_u_base * max(0.05, float(self.candidate.hx_u_multiplier))
        ua_hx = hx_area * hx_u * self.cfg.hx_effectiveness
        q = ua_hx * (fluid_T_C - T_pcm_C)
        if requested_positive:
            return max(0.0, min(q, self.cfg.max_discharge_power_W))
        return min(0.0, max(q, -self.cfg.max_charge_power_W))

    def step(self, dt_s: float, fluid_T_C: float, requested_heat_into_pcm_W: float,
             T_ambient_C: float) -> dict:
        """Advance PCM enthalpy using the existing lumped-HX model.

        The requested rate is additionally limited by the remaining physical
        energy capacity, so charge/discharge cannot push the PCM beyond its
        represented enthalpy range.
        """
        if dt_s <= 0.0:
            raise ValueError("dt_s must be positive")

        T_pcm_C, _alpha = self.temperature_and_fraction()
        hx_area = max(self.cfg.hx_area_m2, 0.0)
        hx_u_base = (self.cfg.hx_U_discharge_W_m2K if requested_heat_into_pcm_W >= 0.0
                      else self.cfg.hx_U_charge_W_m2K)
        hx_u = hx_u_base * max(0.05, float(self.candidate.hx_u_multiplier))
        ua_hx = hx_area * hx_u * self.cfg.hx_effectiveness
        available_into_W = ua_hx * (fluid_T_C - T_pcm_C)

        if requested_heat_into_pcm_W >= 0.0:
            q_into_requested = min(
                requested_heat_into_pcm_W,
                max(0.0, available_into_W),
                self.cfg.max_discharge_power_W,
            )
        else:
            q_into_requested = max(
                requested_heat_into_pcm_W,
                min(0.0, available_into_W),
                -self.cfg.max_charge_power_W,
            )

        q_loss = self.cfg.tank_loss_UA_W_K * (T_pcm_C - T_ambient_C)

        # Bound the total energy change, including tank loss, to the physical
        # enthalpy interval. This prevents numerical overshoot at 15-min dt.
        dE_requested = (q_into_requested - q_loss) * dt_s
        E_target = self._clamp_energy(self.E_J + dE_requested)
        dE_actual = E_target - self.E_J
        self.E_J = E_target

        # Report the effective PCM heat-transfer rate that actually occurred.
        q_into_actual = dE_actual / dt_s + q_loss

        return {
            "Q_into_pcm_W": q_into_actual,
            "Q_loss_W": q_loss,
            "T_pcm_C": T_pcm_C,
            "soc": self.soc(),
            "energy_residual_W": (dE_actual / dt_s) - (q_into_actual - q_loss),
            "energy_clamped": abs(q_into_actual - q_into_requested) > 1e-9,
        }


# =====================================================================
# CHUNK 08 -- PV, DC Bus, Battery, Electrical Accounting
# =====================================================================

