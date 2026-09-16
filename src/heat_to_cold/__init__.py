"""Heat to Cold modular cold-storage optimization package.

Core configuration and PCM candidate definitions live here; computational
modules are split across weather/envelope/product/psychro/refrigeration/pcm,
pv_battery/control/optimizer/analysis/cli.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional
import math

# Core exception used by all physics modules.
class CandidateInfeasible(RuntimeError):
    """Intentional early termination of a provably infeasible candidate."""
    def __init__(self, reason, t_h, T_room_C=float("nan"), T_product_aged_C=float("nan"),
                 battery_soc=float("nan"), details=None):
        super().__init__(reason)
        self.reason = reason
        self.t_h = t_h
        self.T_room_C = T_room_C
        self.T_product_aged_C = T_product_aged_C
        self.battery_soc = battery_soc
        self.details = details or {}

@dataclass
class SiteConfig:
    latitude_deg: float = 26.144
    longitude_deg: float = 91.736
    timezone: str = "Asia/Kolkata"
    utc_offset_h: float = 5.5
    standard_meridian_deg: float = 82.5
    weather_year: int = 2023
    weather_database: str = "PVGIS-ERA5"
    expected_hourly_records: int = 8760


@dataclass
class RoomConfig:
    length_m: float = 3.05
    width_m: float = 3.05
    height_m: float = 2.44
    dimension_basis: str = "internal_clear"
    wall_panel_thickness_m: float = 0.10
    ceiling_panel_thickness_m: float = 0.12
    equipment_footprint_m3: float = 2.50
    aisle_clearance_m3: float = 4.00
    door_area_m2: float = 4.0

    @property
    def volume_m3(self) -> float:
        return self.length_m * self.width_m * self.height_m

    @property
    def floor_area_m2(self) -> float:
        return self.length_m * self.width_m

    @property
    def wall_area_gross_m2(self) -> float:
        return 2.0 * (self.length_m + self.width_m) * self.height_m

    @property
    def wall_area_net_m2(self) -> float:
        return self.wall_area_gross_m2 - self.door_area_m2

    @property
    def largest_wall_area_m2(self) -> float:
        return self.length_m * self.height_m if self.length_m >= self.width_m else self.width_m * self.height_m

    @property
    def door_area_fraction(self) -> float:
        return self.door_area_m2 / self.largest_wall_area_m2


@dataclass
class EnvelopeConfig:
    wall_u_W_m2K: float = 0.25
    ceiling_u_W_m2K: float = 0.18
    floor_u_W_m2K: float = 0.18
    door_u_W_m2K: float = 0.93
    ground_temperature_C: float = 24.0


@dataclass
class InfiltrationConfig:
    background_ach_h: float = 0.10
    loading_ach_h: float = 0.25
    ambient_rh_for_infiltration_pct: float = 80.0  # explicit screening assumption, Instructions 18.9


@dataclass
class ProductConfig:
    max_inventory_kg: float = 5000.0
    incoming_mass_kg_day: float = 765.0
    loading_start_h: float = 10.0
    loading_duration_h: float = 3.5
    incoming_temperature_C: float = 30.0
    merge_temperature_C: float = 7.0
    # Two-node product conductances are defined on a per-incoming-cohort basis.
    # The 600/2400 W/K reference pair is taken to represent the nominal
    # 765 kg/day incoming cohort at the reference airflow, rather than the full
    # 5-tonne inventory. This avoids diluting the newest warm cohort's UA by
    # unrelated resident product mass. Final values should be calibrated against
    # measured crate/product pull-down data.
    surface_core_UA_W_K: float = 2400.0
    air_surface_UA_reference_for_incoming_cohort_W_K: float = 600.0
    incoming_cohort_reference_mass_kg: float = 765.0
    surface_mass_fraction: float = 0.35
    core_mass_fraction: float = 0.65
    packaging_mass_fraction: float = 0.05
    effective_bulk_density_kg_m3: float = 350.0
    reference_airflow_m3_h: float = 8000.0
    reference_air_surface_UA_W_K: float = 600.0
    airflow_distribution_efficiency: float = 0.70
    airflow_ua_exponent: float = 0.8
    # Pull-down target time: freshly loaded warm product must reach the product
    # temperature limit within this many hours (Instructions Chunk 04 D9 /
    # Section 20). The feasibility check only judges product older than this, so
    # the instantaneous 30 C incoming temperature is not treated as a violation.
    pull_down_time_h: float = 24.0
    # Steady-state store turnover: the same daily mass entering is dispatched
    # before loading, keeping the resident inventory near the 5-tonne design
    # level instead of accumulating product indefinitely.
    dispatch_start_h: float = 6.5
    dispatch_duration_h: float = 3.5

    @property
    def loading_rate_kg_h(self) -> float:
        return self.incoming_mass_kg_day / self.loading_duration_h


@dataclass
class CommodityConfig:
    name: str = "generic_vegetable"
    cp_J_kgK: float = 3900.0
    storage_min_C: float = 2.0
    storage_max_C: float = 7.0
    freezing_point_C: float = -0.5
    rh_min_pct: float = 90.0
    rh_max_pct: float = 98.0
    characteristic_length_m: float = 0.05
    respiration_ref_W_per_tonne: float = 40.0
    respiration_q10: float = 2.0
    respiration_ref_temp_C: float = 5.0
    transpiration_baseline_kg_kg_day: float = 0.002
    transpiration_ref_vpd_kPa: float = 0.3
    transpiration_multiplier_cap: float = 4.0


@dataclass
class PsychroConfig:
    atmospheric_pressure_Pa: float = 101325.0


@dataclass
class RefrigerationConfig:
    refrigerant: str = "R290"
    suction_superheat_K: float = 7.0
    liquid_subcooling_K: float = 3.0
    compressor_displacement_m3_rev: float = 0.000025
    min_speed_rpm: float = 1500.0
    max_speed_rpm: float = 4500.0
    volumetric_efficiency: float = 0.80
    isentropic_efficiency: float = 0.65
    motor_efficiency: float = 0.90
    drive_efficiency: float = 0.97
    minimum_turndown: float = 0.25
    evaporator_UA_W_K: float = 600.0
    condenser_UA_W_K: float = 900.0
    evaporator_airflow_m3_h: float = 8000.0
    condenser_fan_power_W: float = 150.0
    evaporator_fan_power_W: float = 250.0
    # Battery-saving circulation mode: fan-only operation consumes less power
    # but provides no refrigeration/dehumidification by itself.
    evaporator_fan_only_power_W: float = 75.0
    evaporator_fan_only_airflow_fraction: float = 0.35
    minimum_evaporator_approach_K: float = 5.0
    minimum_condenser_approach_K: float = 8.0
    bypass_factor: float = 0.15
    # W8: in production the mock R290 backend must NOT silently stand in for
    # CoolProp. When True and CoolProp is unavailable, solving raises.
    production_mode: bool = True


@dataclass
class PCMCandidate:
    name: str
    T_solidus_C: float
    T_liquidus_C: float
    transition_enthalpy_J_kg: float
    cp_solid_J_kgK: float
    cp_liquid_J_kgK: float
    density_kg_m3: float
    conductivity_W_mK: float
    cost_per_kg: float
    nominal_transition_C: float
    biochar_fraction_wt_pct: float = 0.0
    usable_enthalpy_J_kg: float = float("nan")
    leakage_pct: float = float("nan")
    cycle_retention_500: float = float("nan")
    hx_u_multiplier: float = 1.0
    data_status: str = "screening"
    cost_status: str = "screening_assumption"

    def validate_for_optimization(self) -> None:
        vals = (
            self.T_solidus_C, self.T_liquidus_C, self.transition_enthalpy_J_kg,
            self.cp_solid_J_kgK, self.cp_liquid_J_kgK, self.density_kg_m3,
            self.conductivity_W_mK, self.cost_per_kg, self.nominal_transition_C,
        )
        if not all(math.isfinite(float(v)) for v in vals):
            raise ValueError(f"PCM candidate {self.name!r} has incomplete/non-finite required properties.")
        if self.transition_enthalpy_J_kg <= 0 or self.density_kg_m3 <= 0:
            raise ValueError(f"PCM candidate {self.name!r} has invalid enthalpy/density.")
        if not (self.T_solidus_C < self.T_liquidus_C):
            raise ValueError(f"PCM candidate {self.name!r} has invalid solidus/liquidus.")
        if not (self.T_solidus_C <= self.nominal_transition_C <= self.T_liquidus_C):
            raise ValueError(f"PCM candidate {self.name!r} nominal transition is outside its transition interval.")


PCM_LIBRARY = {
    "RT2HC": PCMCandidate(
        "RT2HC", 1.0, 3.0, 200000.0, 2000.0, 2000.0, 880.0, 0.20, 300.0,
        nominal_transition_C=2.0, data_status="screening_supplier_family"
    ),
    "RT3HC": PCMCandidate(
        "RT3HC", 2.0, 4.0, 190000.0, 2000.0, 2000.0, 880.0, 0.20, 290.0,
        nominal_transition_C=3.0, data_status="screening_supplier_family"
    ),
    "RT4": PCMCandidate(
        "RT4", 3.0, 5.0, 180000.0, 2000.0, 2200.0, 880.0, 0.20, 260.0,
        nominal_transition_C=4.0, data_status="screening_supplier_family"
    ),
    "BIOCHAR_CS_CA_LA_OA": PCMCandidate(
        name="BIOCHAR_CS_CA_LA_OA",
        T_solidus_C=2.5,
        T_liquidus_C=3.7,
        transition_enthalpy_J_kg=104900.0,
        cp_solid_J_kgK=2000.0,
        cp_liquid_J_kgK=2000.0,
        density_kg_m3=950.0,
        conductivity_W_mK=1.853,
        cost_per_kg=180.0,
        nominal_transition_C=3.1,
        biochar_fraction_wt_pct=float("nan"),
        usable_enthalpy_J_kg=104900.0,
        leakage_pct=6.49,
        cycle_retention_500=0.977,
        hx_u_multiplier=1.0,
        data_status="literature_core_properties_plus_screening_assumptions",
        cost_status="editable_bulk_cost_screening_assumption",
    ),
    "BIOCHAR_RICE_HUSK_3C": PCMCandidate(
        "BIOCHAR_RICE_HUSK_3C", float("nan"), float("nan"), float("nan"),
        float("nan"), float("nan"), float("nan"), float("nan"), float("nan"),
        nominal_transition_C=float("nan"), data_status="TBD_EXPERIMENTAL"
    ),
    "BIOCHAR_BAMBOO_3C": PCMCandidate(
        "BIOCHAR_BAMBOO_3C", float("nan"), float("nan"), float("nan"),
        float("nan"), float("nan"), float("nan"), float("nan"), float("nan"),
        nominal_transition_C=float("nan"), data_status="TBD_EXPERIMENTAL"
    ),
    "BIOCHAR_MAIZE_3C": PCMCandidate(
        "BIOCHAR_MAIZE_3C", float("nan"), float("nan"), float("nan"),
        float("nan"), float("nan"), float("nan"), float("nan"), float("nan"),
        nominal_transition_C=float("nan"), data_status="TBD_EXPERIMENTAL"
    ),
}


@dataclass
class PCMConfig:
    candidate: str = "RT3HC"
    pcm_mass_kg: float = 800.0
    hx_area_m2: float = 16.0
    # Separate charge/discharge coefficients; equal defaults preserve the prior model
    # until measured HX data are available.
    hx_U_charge_W_m2K: float = 60.0
    hx_U_discharge_W_m2K: float = 60.0
    max_charge_power_W: float = 4000.0
    max_discharge_power_W: float = 4000.0
    hx_effectiveness: float = 0.75
    tank_loss_UA_W_K: float = 5.0
    initial_soc: float = 0.5
    # Charging the PCM (freezing it against the chiller) draws compressor
    # electrical energy; without this penalty the optimizer could charge PCM
    # for free (a W10-style hidden loophole).
    charge_cop: float = 2.5


@dataclass
class PVConfig:
    rated_power_kWp: float = 8.0
    module_efficiency_stc: float = 0.20
    temperature_coefficient_per_K: float = -0.0035
    cell_temp_rise_coeff_K_m2_W: float = 0.029
    dc_wiring_loss_fraction: float = 0.02
    mppt_efficiency: float = 0.98
    reference_cell_temperature_C: float = 25.0
    G_STC_W_m2: float = 1000.0


@dataclass
class BatteryConfig:
    nominal_energy_kWh: float = 10.0
    minimum_SOC: float = 0.20
    maximum_SOC: float = 0.95
    initial_SOC: float = 0.80
    maximum_charge_C_rate: float = 0.5
    maximum_discharge_C_rate: float = 1.0
    charge_converter_efficiency: float = 0.97
    discharge_converter_efficiency: float = 0.97
    standby_loss_fraction_per_hour: float = 0.001
    bus_voltage_V: float = 48.0


@dataclass
class ControlConfig:
    room_setpoint_C: float = 4.0
    room_lower_C: float = 2.0
    room_upper_C: float = 6.0
    product_upper_C: float = 7.0
    product_lower_C: float = 2.0
    rh_lower_pct: float = 90.0
    rh_target_pct: float = 95.0
    rh_upper_pct: float = 98.0
    dehum_on_pct: float = 97.0
    dehum_off_pct: float = 94.0
    humid_on_pct: float = 91.0
    humid_off_pct: float = 94.0
    humidifier_max_kg_h: float = 2.0
    pcm_night_target_share: float = 0.95
    pcm_minimum_share: float = 0.90
    battery_reserve_SOC: float = 0.20
    minimum_on_time_min: float = 10.0
    minimum_off_time_min: float = 10.0
    compressor_min_fraction: float = 0.25
    compressor_max_fraction: float = 1.0
    ramp_rate_fraction_per_min: float = 0.10
    emergency_room_C: float = 7.0
    emergency_product_C: float = 8.0


@dataclass
class MasterConfig:
    site: SiteConfig = field(default_factory=SiteConfig)
    room: RoomConfig = field(default_factory=RoomConfig)
    envelope: EnvelopeConfig = field(default_factory=EnvelopeConfig)
    infiltration: InfiltrationConfig = field(default_factory=InfiltrationConfig)
    product: ProductConfig = field(default_factory=ProductConfig)
    commodity: CommodityConfig = field(default_factory=CommodityConfig)
    psychro: PsychroConfig = field(default_factory=PsychroConfig)
    refrig: RefrigerationConfig = field(default_factory=RefrigerationConfig)
    pcm: PCMConfig = field(default_factory=PCMConfig)
    pv: PVConfig = field(default_factory=PVConfig)
    battery: BatteryConfig = field(default_factory=BatteryConfig)
    control: ControlConfig = field(default_factory=ControlConfig)




__version__ = "1.0.0-modular"

# Lightweight public re-exports are populated lazily by users importing the
# individual modules. This avoids import cycles at package import time.
__all__ = [name for name in globals() if not name.startswith("_")]
