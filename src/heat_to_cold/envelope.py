from __future__ import annotations
import math
from . import CandidateInfeasible, MasterConfig
RHO_AIR_NOM = 1.2
CP_AIR = 1006.0

RHO_AIR_NOM = 1.2
CP_AIR = 1006.0


def envelope_UA(cfg: MasterConfig) -> dict:
    room, env = cfg.room, cfg.envelope
    ua_wall = env.wall_u_W_m2K * room.wall_area_net_m2
    ua_ceiling = env.ceiling_u_W_m2K * room.floor_area_m2
    ua_floor = env.floor_u_W_m2K * room.floor_area_m2
    ua_door = env.door_u_W_m2K * room.door_area_m2
    return {
        "wall": ua_wall, "ceiling": ua_ceiling, "floor": ua_floor, "door": ua_door,
        "above_grade_total": ua_wall + ua_ceiling + ua_door,
    }


def infiltration_flow_m3_s(cfg: MasterConfig, is_loading: bool) -> float:
    ach = cfg.infiltration.loading_ach_h if is_loading else cfg.infiltration.background_ach_h
    return ach * cfg.room.volume_m3 / 3600.0


def internal_sensible_loads_W(cfg: MasterConfig, is_loading: bool, fan_on: bool,
                              fan_only: bool = False) -> float:
    lighting_W = 60.0
    controller_W = 15.0
    people_W = 200.0 if is_loading else 0.0
    if fan_on:
        fan_W = (cfg.refrig.evaporator_fan_only_power_W if fan_only
                 else cfg.refrig.evaporator_fan_power_W)
    else:
        fan_W = 0.0
    return lighting_W + controller_W + people_W + fan_W


def room_thermal_coefficients(cfg: MasterConfig, T_amb_C: float, rho_air: float,
                               is_loading: bool, fan_on: bool, resp_W: float,
                               fan_only: bool = False) -> dict:
    """Return the linear conductance G [W/K] and source Q_src [W] of the room
    air node EXCLUDING product (handled separately) and excluding cooling, so
    the room can be integrated implicitly:  C dT/dt = Q_src - G*T - Q_cool.
    Envelope, ground and infiltration are linear in room temperature and so are
    treated implicitly for unconditional stability at the 15-min timestep."""
    ua = envelope_UA(cfg)
    G_env = ua["wall"] + ua["ceiling"] + ua["door"]      # coupled to ambient
    G_floor = ua["floor"]                                 # coupled to ground
    v_dot = infiltration_flow_m3_s(cfg, is_loading)
    G_inf = rho_air * v_dot * CP_AIR                      # coupled to ambient
    q_internal = internal_sensible_loads_W(cfg, is_loading, fan_on, fan_only=fan_only)
    G = G_env + G_floor + G_inf
    Q_src = (G_env * T_amb_C + G_floor * cfg.envelope.ground_temperature_C
             + G_inf * T_amb_C + q_internal + resp_W)
    return {"G": G, "Q_src": Q_src, "infiltration_flow_m3_s": v_dot,
            "q_internal": q_internal, "G_env": G_env, "G_floor": G_floor, "G_inf": G_inf}




