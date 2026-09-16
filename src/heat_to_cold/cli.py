from __future__ import annotations
import argparse
import csv
import json
import math
import os
from dataclasses import asdict

from . import *
from . import CandidateInfeasible, MasterConfig, PCM_LIBRARY
from .weather import _synthetic_weather_year, fetch_pvgis_weather
from . import optimizer as _optimizer
from .optimizer import (HAVE_COOLPROP, HAVE_PYMOO, HAVE_SKLEARN, EVAL_ERROR_COUNT,
                        _pcm_material_screen, run_diagnostic_evaluations, run_optimization, simulate_transient,
                        DESIGN_VARIABLES)
from .analysis import write_csv

def _infer_unit(pname: str) -> str:
    """Best-effort unit inference from the parameter name suffix so the audit
    registry never has a unitless active parameter (Instructions TASK A)."""
    table = [
        ("_kg_day", "kg/day"), ("_kg_m3", "kg/m3"), ("_m3_rev", "m3/rev"),
        ("_m3_h", "m3/h"), ("_W_m2K", "W/m2K"), ("_W_m2", "W/m2"),
        ("_per_hour", "1/h"), ("_per_K", "1/K"), ("_W_K", "W/K"),
        ("_m2_per_kg", "m2/kg"), ("_m2K", "m2K"), ("_J_kgK", "J/kgK"),
        ("_kg_kg_day", "kg/kg/day"), ("_kWh", "kWh"), ("_kWp", "kWp"),
        ("_rpm", "rpm"), ("_kPa", "kPa"), ("_Pa", "Pa"), ("_pct", "%"),
        ("_ach_h", "1/h"), ("_C_rate", "C"), ("_V", "V"), ("_W", "W"),
        ("_C", "degC"), ("_K", "K"), ("_h", "h"), ("_m3", "m3"),
        ("_m2", "m2"), ("_m", "m"), ("_deg", "deg"), ("_year", "year"),
        ("_records", "count"), ("efficiency", "-"), ("fraction", "-"),
        ("_share", "-"), ("_soc", "-"), ("_cop", "-"), ("_q10", "-"),
        ("_exponent", "-"), ("_factor", "-"),
    ]
    for suffix, unit in table:
        if pname.endswith(suffix) or suffix in pname:
            return unit
    return "-"


def build_parameter_registry(cfg: MasterConfig) -> list[dict]:
    """Flatten config into (name, value, unit, category, source) records for audit."""
    # Parameters that are optimization design variables (Chunk 11).
    design_var_names = {
        "pv.rated_power_kWp", "battery.nominal_energy_kWh", "pcm.pcm_mass_kg",
        "pcm.hx_area_m2", "refrig.evaporator_airflow_m3_h",
    }
    control_groups = {"control"}
    registry = []
    groups = asdict(cfg)
    for group_name, params in groups.items():
        for pname, value in params.items():
            full = f"{group_name}.{pname}"
            if full in design_var_names:
                category, source = "DESIGN_VARIABLE", "optimization bound (Chunk 11)"
            elif group_name in control_groups:
                category, source = "CONTROL_POLICY", "controller tuning (Chunk 09)"
            elif group_name in ("site", "room"):
                category, source = "ACTIVE", "design input (Chunk 01)"
            elif group_name in ("refrig", "pv", "battery", "pcm"):
                category, source = "CALIBRATION", "screening value pending OEM data"
            else:
                category, source = "ACTIVE", "screening/design input"
            registry.append({
                "name": full,
                "value": value,
                "unit": _infer_unit(pname),
                "category": category,
                "source": source,
            })
    seen = set()
    for rec in registry:
        if rec["name"] in seen:
            raise ValueError(f"Duplicate registry parameter: {rec['name']}")
        seen.add(rec["name"])
        if not rec["unit"] or not rec["category"] or not rec["source"]:
            raise ValueError(f"Registry parameter missing unit/category/source: {rec['name']}")
    return registry


def geometry_diagnostics(cfg: MasterConfig) -> dict:
    room = cfg.room
    V = room.volume_m3
    v_produce = cfg.product.max_inventory_kg / cfg.product.effective_bulk_density_kg_m3
    v_net = V - room.equipment_footprint_m3 - room.aisle_clearance_m3
    occupancy_ratio = v_produce / v_net if v_net > 0 else float("inf")
    return {
        "volume_m3": V,
        "floor_area_m2": room.floor_area_m2,
        "wall_area_gross_m2": room.wall_area_gross_m2,
        "wall_area_net_m2": room.wall_area_net_m2,
        "produce_volume_m3": v_produce,
        "net_storage_volume_m3": v_net,
        "occupancy_ratio": occupancy_ratio,
        "occupancy_warning": occupancy_ratio > 0.85,
        "door_area_fraction": room.door_area_fraction,
        "door_fraction_warning": room.door_area_fraction > 0.35,
        "turnover_fraction_per_day": cfg.product.incoming_mass_kg_day / cfg.product.max_inventory_kg,
    }


# =====================================================================
# CHUNK 02 -- PVGIS Weather Pipeline
# =====================================================================



def run_smoke_test(cfg: MasterConfig, duration_days: float = 1.0, weather_cache: str = "pvgis_2023_raw.json", use_pvgis: bool = False) -> None:
    print("=== Chunk 01: Foundation / Geometry ===")
    diag = geometry_diagnostics(cfg)
    for k, v in diag.items():
        print(f"  {k}: {v}")
    registry = build_parameter_registry(cfg)
    print(f"  Registered {len(registry)} active parameters, no duplicates.")

    print("=== Chunk 02: Weather ===")
    if use_pvgis:
        weather, source = fetch_pvgis_weather(cfg, cache_path=weather_cache, allow_network=True)
    else:
        weather = _synthetic_weather_year(cfg.site)
        source = "synthetic_smoke_test_only"
    print(f"  weather_source_status = {source}, records = {len(weather)}")

    print(f"=== Chunk 06: CoolProp availability = {HAVE_COOLPROP} ===")
    print(f"=== Chunk 11/12: PyMOO availability = {HAVE_PYMOO} ===")

    if not HAVE_COOLPROP:
        cfg.refrig.production_mode = False
        print("  [SMOKE] CoolProp unavailable: using test-only MockR290Backend.")

    print(f"=== Chunk 10: Transient simulation ({duration_days} day(s)) ===")
    try:
        trace = simulate_transient(cfg, weather, duration_days)
    except CandidateInfeasible as exc:
        print(f"  [SMOKE] candidate became infeasible: {exc.reason} at t={exc.t_h:.1f} h; this is a plant-feasibility result, not a code crash.")
        return
    write_csv("simulation_trace.csv", trace)
    print(f"  {len(trace)} steps written to simulation_trace.csv")

    T_room = [r["T_room_C"] for r in trace]
    RH = [r["RH_pct"] for r in trace]
    print(f"  Room T range: {min(T_room):.2f} - {max(T_room):.2f} C")
    print(f"  RH range: {min(RH):.2f} - {max(RH):.2f} %")
    print(f"  Max |refrig first-law residual| (19.1): {max(abs(r['refrig_residual_W']) for r in trace):.4f} W")
    print(f"  Max |bus residual| (19.2): {max(abs(r['bus_residual_W']) for r in trace):.4f} W")
    print(f"  Max |PCM energy residual| (19.3): {max(abs(r['pcm_energy_residual_W']) for r in trace):.6f} W")
    print(f"  Total condensate removed: {sum(r['condensate_kg_s'] for r in trace) * (15.0 * 60.0):.3f} kg")
    print(f"  Steps with inadequate condenser: {sum(1 for r in trace if not r['condenser_adequate'])}")
    print(f"  Steps refrigeration infeasible: {sum(1 for r in trace if not r['refrig_feasible'])}")
    print(f"  PCM SOC range: {min(r['pcm_soc'] for r in trace):.2f} - {max(r['pcm_soc'] for r in trace):.2f}")
    print(f"  Battery SOC range: {min(r['battery_soc'] for r in trace):.2f} - {max(r['battery_soc'] for r in trace):.2f}")
    print(f"  Inventory cap violated at any step: {any(r['inventory_violation'] for r in trace)}")
    print(f"  Final inventory: {trace[-1]['inventory_kg']:.1f} kg (cap {cfg.product.max_inventory_kg} kg)")




def main():
    parser=argparse.ArgumentParser(description="Hybrid PV-Battery-PCM Cold Storage -- constrained NSGA-II research plot pack")
    parser.add_argument("--days",type=float,default=365.0); parser.add_argument("--pop-size",type=int,default=64); parser.add_argument("--n-gen",type=int,default=12)
    parser.add_argument("--seed",type=int,default=1); parser.add_argument("--n-seeds",type=int,default=1); parser.add_argument("--weather-cache",default="pvgis_2023_raw.json")
    parser.add_argument("--optimizer",choices=["surrogate-nsga2","nsga2"],default="nsga2"); parser.add_argument("--initial-samples",type=int,default=8); parser.add_argument("--eval-budget",type=int,default=16); parser.add_argument("--batch-size",type=int,default=2)
    parser.add_argument("--virtual-pop",type=int,default=48); parser.add_argument("--workers",type=int,default=max(1,min(8,(os.cpu_count() or 2)-1))); parser.add_argument("--virtual-gen",type=int,default=5); parser.add_argument("--progress-every",type=int,default=25); parser.add_argument("--debug",action="store_true")
    parser.add_argument("--smoke-test",action="store_true"); parser.add_argument("--smoke-pvgis",action="store_true"); parser.add_argument("--diagnostic",action="store_true", help="run fixed real-physics feasibility diagnostics; not an optimization or smoke test"); parser.add_argument("--no-plots",action="store_true", help="skip SIH presentation plots during diagnostics"); parser.add_argument("--diagnostic-days",type=float,default=7.0); parser.add_argument("--diagnostic-dt-min",type=float,default=15.0); parser.add_argument("--opt-dt-min",type=float,default=120.0); parser.add_argument("--final-dt-min",type=float,default=60.0); parser.add_argument("--convergence-dt-min",type=float,default=120.0); parser.add_argument("--run-robustness",action="store_true")
    parser.add_argument("--pcm-candidate",default="RT3HC",choices=sorted(PCM_LIBRARY.keys()),
                        help="PCM material candidate used by the optimization")
    parser.add_argument("--list-pcm-candidates",action="store_true",
                        help="List PCM library candidates and exit")
    args=parser.parse_args(); _optimizer.DEBUG_MODE=args.debug; cfg=MasterConfig()
    if args.list_pcm_candidates:
        print("PCM candidates:")
        for name,c in PCM_LIBRARY.items():
            print(f"  {name:28s} | Ttr={c.nominal_transition_C!s:>6} C | k={c.conductivity_W_mK!s:>8} W/mK | H={c.transition_enthalpy_J_kg!s:>10} J/kg | cost={c.cost_per_kg!s:>8} INR/kg | status={c.data_status}")
        return
    cfg.pcm.candidate = args.pcm_candidate
    material_screen = _pcm_material_screen(cfg.pcm.candidate)
    if material_screen is not None:
        raise SystemExit(f"ERROR: selected PCM candidate cannot be optimized: {material_screen}")
    if args.smoke_test: run_smoke_test(cfg,duration_days=min(args.days,1.0),weather_cache=args.weather_cache,use_pvgis=args.smoke_pvgis); return
    if not HAVE_COOLPROP or not HAVE_PYMOO: raise SystemExit("ERROR: CoolProp and pymoo are required")
    if args.optimizer=="surrogate-nsga2" and not HAVE_SKLEARN: raise SystemExit("ERROR: scikit-learn is required")
    weather,source=fetch_pvgis_weather(cfg,cache_path=args.weather_cache,allow_network=True)
    print(f"PVGIS weather source: {source} ({len(weather)} hourly records)"); print("CoolProp: enabled"); print(f"Optimization engine: {args.optimizer}")
    pc=PCM_LIBRARY[cfg.pcm.candidate]
    print(f"PCM candidate: {pc.name} | Ttr={pc.nominal_transition_C:.2f} C | k={pc.conductivity_W_mK:.3f} W/mK | Htr={pc.transition_enthalpy_J_kg/1000.0:.1f} kJ/kg | cost~{pc.cost_per_kg:.1f} INR/kg [{pc.cost_status}]")
    print(f"[INFO] PVGIS UTC-to-local weather alignment: +{cfg.site.utc_offset_h:g} h (local simulation clock)")
    if args.diagnostic:
        run_diagnostic_evaluations(cfg,weather,duration_days=max(0.25,args.diagnostic_days),dt_min=max(1.0,args.diagnostic_dt_min),make_plots=not args.no_plots)
        return
    print(f"[INFO] {len(DESIGN_VARIABLES)} procurement-oriented design variables"); print("[INFO] PV upper bound = 25 kWp; battery upper bound = 35 kWh"); print("[INFO] early infeasible-candidate termination enabled"); print("[INFO] fixed compressor efficiencies retained; no OEM map"); print("[INFO] SIH optimization graphs will be generated automatically"); print("[INFO] Dense research mode: 64 population × 12 generations (up to ~768 real NSGA-II evaluations)")
    if args.optimizer=="surrogate-nsga2":
        print(f"[INFO] NSGA-II population={args.pop_size}; generations={args.n_gen}; workers={args.workers}")
    run_optimization(cfg,weather,source,args.pop_size,args.n_gen,args.seed,args.days,args.opt_dt_min,args.final_dt_min,args.convergence_dt_min,args.n_seeds,args.progress_every,args.optimizer,args.initial_samples,args.eval_budget,args.batch_size,args.virtual_pop,args.virtual_gen,args.run_robustness,args.workers)
    print(f"[DONE] candidate exceptions/rejections observed: {EVAL_ERROR_COUNT}",flush=True)


if __name__ == "__main__":

    main()
