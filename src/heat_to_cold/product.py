from __future__ import annotations
import math
from dataclasses import dataclass
from typing import Optional
from . import MasterConfig

@dataclass
class ProductCohort:
    cohort_id: int
    mass_kg: float
    packaging_mass_kg: float
    creation_time_h: float
    commodity: str
    status: str  # "incoming" or "stored"
    T_surface_C: float
    T_core_C: float


class ProductState:
    def __init__(self, cfg: MasterConfig, initial_temperature_C: Optional[float] = None):
        self.cfg = cfg
        self.cohorts: list[ProductCohort] = []
        self._next_id = 0
        self.total_removed_kg = 0.0
        self.total_dispatch_violation_kg = 0.0

        # Start from a fully stocked cold store. This makes the annual model
        # represent a real operating store rather than an initially empty room.
        resident_mass = cfg.product.max_inventory_kg
        self.cohorts.append(ProductCohort(
            cohort_id=self._next_id,
            mass_kg=resident_mass,
            packaging_mass_kg=resident_mass * cfg.product.packaging_mass_fraction,
            creation_time_h=-cfg.product.pull_down_time_h,
            commodity=cfg.commodity.name,
            status="stored",
            T_surface_C=cfg.control.room_setpoint_C,
            T_core_C=cfg.control.room_setpoint_C,
        ))
        self._next_id += 1
        if initial_temperature_C is not None:
            self.cohorts[0].T_surface_C = initial_temperature_C
            self.cohorts[0].T_core_C = initial_temperature_C

    @property
    def total_mass_kg(self) -> float:
        return sum(c.mass_kg for c in self.cohorts)

    def _daily_window_overlap_h(self, t_h: float, dt_h: float, start_h: float, duration_h: float) -> float:
        """Overlap of [t_h, t_h+dt_h] with a recurring daily window."""
        if dt_h <= 0.0 or duration_h <= 0.0:
            return 0.0
        end_h = t_h + dt_h
        first_day = math.floor(t_h / 24.0) - 1
        last_day = math.floor(end_h / 24.0) + 1
        overlap = 0.0
        for day in range(first_day, last_day + 1):
            ws = day * 24.0 + start_h
            we = ws + duration_h
            overlap += max(0.0, min(end_h, we) - max(t_h, ws))
        return overlap

    def hottest_core_C(self) -> float:
        if not self.cohorts:
            return self.cfg.control.room_setpoint_C
        return max(c.T_core_C for c in self.cohorts)

    def mean_core_C(self) -> float:
        total=self.total_mass_kg
        if total<=0.0: return self.cfg.control.room_setpoint_C
        return sum(c.mass_kg*c.T_core_C for c in self.cohorts)/total

    def hottest_core_aged_C(self, t_h: float) -> float:
        """Hottest core among cohorts that have been in the room longer than the
        pull-down time (i.e., product that SHOULD have cooled to the limit).
        Used for the pull-down feasibility check so freshly loaded warm product
        is not counted as a violation."""
        pd = self.cfg.product.pull_down_time_h
        aged = [c.T_core_C for c in self.cohorts if (t_h - c.creation_time_h) >= pd]
        return max(aged) if aged else self.cfg.control.room_setpoint_C

    def add_loading(self, t_h: float, dt_h: float) -> Optional[str]:
        prod = self.cfg.product
        overlap_h = self._daily_window_overlap_h(
            t_h, dt_h, prod.loading_start_h, prod.loading_duration_h
        )
        if overlap_h <= 0.0:
            return None
        mass_in = prod.loading_rate_kg_h * overlap_h
        violation = None
        if self.total_mass_kg + mass_in > prod.max_inventory_kg + 1e-9:
            violation = "INVENTORY_CAP_VIOLATION"
            mass_in = max(0.0, prod.max_inventory_kg - self.total_mass_kg)
        if mass_in <= 0.0:
            return violation
        packaging = mass_in * prod.packaging_mass_fraction
        self.cohorts.append(ProductCohort(
            cohort_id=self._next_id, mass_kg=mass_in, packaging_mass_kg=packaging,
            creation_time_h=t_h, commodity=self.cfg.commodity.name, status="incoming",
            T_surface_C=prod.incoming_temperature_C, T_core_C=prod.incoming_temperature_C,
        ))
        self._next_id += 1
        return violation

    def remove_dispatch(self, t_h: float, dt_h: float) -> Optional[str]:
        """FIFO dispatch of daily outgoing produce before the daily loading window.

        The outgoing mass equals the nominal daily incoming mass, so the store
        operates around a constant 5-tonne inventory rather than accumulating
        produce indefinitely.
        """
        prod = self.cfg.product
        overlap_h = self._daily_window_overlap_h(
            t_h, dt_h, prod.dispatch_start_h, prod.dispatch_duration_h
        )
        if overlap_h <= 0.0:
            return None
        requested_kg = prod.loading_rate_kg_h * overlap_h
        available_kg = self.total_mass_kg
        to_remove = min(requested_kg, available_kg)
        remaining = to_remove

        # FIFO: oldest eligible cohorts leave first. A warm/young cohort is
        # not dispatched before its configured pull-down requirement is met.
        for cohort in list(self.cohorts):
            if remaining <= 1e-12:
                break
            cohort_age_h = (t_h + dt_h) - cohort.creation_time_h
            if cohort_age_h < prod.pull_down_time_h - 1e-9:
                continue
            if cohort.T_core_C > self.cfg.control.product_upper_C + 1e-9:
                continue
            take = min(cohort.mass_kg, remaining)
            if take <= 0.0:
                continue
            frac = take / cohort.mass_kg
            cohort.mass_kg -= take
            cohort.packaging_mass_kg *= max(0.0, 1.0 - frac)
            remaining -= take
            self.total_removed_kg += take
            if cohort.mass_kg <= 1e-10:
                self.cohorts.remove(cohort)

        if to_remove + 1e-9 < requested_kg:
            deficit = requested_kg - to_remove
            self.total_dispatch_violation_kg += deficit
            return "DISPATCH_INVENTORY_SHORTFALL"
        return None

    def step_thermal(self, dt_s: float, T_room_C: float, airflow_m3_h: float) -> tuple:
        """Advance two-node cohort thermal states with a backward-Euler
        (implicit) update. Returns (heat_to_room_W, G_prod_W_K, src_prod_W):

          heat_to_room_W : total heat delivered product->room air this step (W)
          G_prod_W_K     : sum of air-surface conductances (for implicit room solve)
          src_prod_W     : sum ua_as_i * Ts_i (updated) (for implicit room solve)

        Implicit integration is used because the surface node capacitance is
        small relative to the surface-core and air-surface conductances; an
        explicit update would be numerically unstable at the 15-min plant
        timestep (dt would exceed 2*C/UA). Backward Euler is unconditionally
        stable and conserves energy between the two nodes.
        """
        prod = self.cfg.commodity
        pcfg = self.cfg.product
        eta_dist = pcfg.airflow_distribution_efficiency
        v_eff = eta_dist * airflow_m3_h
        v_ref = pcfg.reference_airflow_m3_h
        ua_scale = (v_eff / v_ref) ** pcfg.airflow_ua_exponent if v_ref > 0 else 1.0
        ua_air_surface_per_kg = (
            pcfg.air_surface_UA_reference_for_incoming_cohort_W_K
            / max(pcfg.incoming_cohort_reference_mass_kg, 1e-12)
        ) * ua_scale
        ua_surface_core_per_kg = (
            pcfg.surface_core_UA_W_K
            / max(pcfg.incoming_cohort_reference_mass_kg, 1e-12)
        )

        total_heat_to_room_W = 0.0
        G_prod = 0.0
        src_prod = 0.0
        for cohort in self.cohorts:
            m_s = cohort.mass_kg * pcfg.surface_mass_fraction
            m_c = cohort.mass_kg * pcfg.core_mass_fraction
            c_pack = 2300.0
            C_s = m_s * prod.cp_J_kgK + cohort.packaging_mass_kg * c_pack
            C_c = m_c * prod.cp_J_kgK
            # Each cohort receives UA in proportion to its own mass. The UA
            # is no longer divided by total warehouse inventory, so a 765 kg
            # incoming cohort retains the reference 600/2400 W/K conductances
            # even when the room also contains older resident produce.
            ua_as = ua_air_surface_per_kg * cohort.mass_kg
            ua_sc = ua_surface_core_per_kg * cohort.mass_kg

            Ts, Tc = cohort.T_surface_C, cohort.T_core_C
            if C_s <= 0 or C_c <= 0:
                continue
            a = C_s / dt_s
            b = C_c / dt_s
            # Backward Euler 2x2 linear system for (Ts', Tc'):
            #   (a + ua_as + ua_sc) Ts' - ua_sc Tc' = a*Ts + ua_as*T_room
            #   -ua_sc Ts' + (b + ua_sc) Tc'        = b*Tc
            A11 = a + ua_as + ua_sc
            A12 = -ua_sc
            A21 = -ua_sc
            A22 = b + ua_sc
            R1 = a * Ts + ua_as * T_room_C
            R2 = b * Tc
            det = A11 * A22 - A12 * A21
            if abs(det) < 1e-12:
                continue
            Ts_new = (R1 * A22 - A12 * R2) / det
            Tc_new = (A11 * R2 - A21 * R1) / det

            cohort.T_surface_C = Ts_new
            cohort.T_core_C = Tc_new

            total_heat_to_room_W += ua_as * (Ts_new - T_room_C)
            G_prod += ua_as
            src_prod += ua_as * Ts_new

            if cohort.status == "incoming" and cohort.T_core_C <= pcfg.merge_temperature_C:
                cohort.status = "stored"

        return total_heat_to_room_W, G_prod, src_prod

    def check_inventory_cap(self) -> bool:
        return self.total_mass_kg <= self.cfg.product.max_inventory_kg + 1e-6


# =====================================================================
# CHUNK 05 -- Psychrometrics and Moisture Balance
# =====================================================================

