from __future__ import annotations
import json
import math
import os
import time
from dataclasses import dataclass
from typing import Optional

from . import CandidateInfeasible, MasterConfig, SiteConfig

try:
    import requests
    HAVE_REQUESTS = True
except ImportError:
    requests = None
    HAVE_REQUESTS = False

@dataclass
class WeatherRecord:
    hour_index: int
    utc_hour: float
    poa_global_W_m2: float
    poa_direct_W_m2: float
    poa_diffuse_W_m2: float
    poa_reflected_W_m2: float
    sun_height_deg: float
    T_amb_C: float
    wind_speed_m_s: float


def _synthetic_weather_year(site: SiteConfig) -> list[WeatherRecord]:
    """Deterministic synthetic weather generator used only when PVGIS is
    unreachable. Not real PVGIS data -- flagged via `weather_source_status`."""
    records = []
    n_hours = 8760
    for h in range(n_hours):
        day_of_year = h // 24
        hour_of_day = h % 24
        seasonal = 0.5 * (1.0 - math.cos(2 * math.pi * (day_of_year - 15) / 365.0))
        T_amb = 18.0 + 12.0 * seasonal + 6.0 * math.sin(2 * math.pi * (hour_of_day - 9) / 24.0)
        solar_angle = math.sin(math.pi * (hour_of_day - 6) / 12.0)
        sun_height = max(0.0, 60.0 * solar_angle)
        clearness = 0.75
        poa_global = max(0.0, 950.0 * solar_angle * clearness) if 6 <= hour_of_day <= 18 else 0.0
        poa_direct = 0.75 * poa_global
        poa_diffuse = 0.20 * poa_global
        poa_reflected = 0.05 * poa_global
        wind = 1.5 + 1.0 * math.sin(2 * math.pi * hour_of_day / 24.0 + 1.0)
        records.append(WeatherRecord(h, float(h), poa_global, poa_direct, poa_diffuse,
                                      poa_reflected, sun_height, T_amb, max(0.2, wind)))
    return records


def _http_get_json(url: str, timeout: float = 60.0) -> dict:
    """GET JSON using `requests` if available, otherwise stdlib urllib so the
    PVGIS integration works with no third-party dependency installed."""
    if HAVE_REQUESTS:
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    import urllib.request
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


def _parse_pvgis(raw: dict) -> list[WeatherRecord]:
    """Normalize a PVGIS seriescalc JSON response into WeatherRecords.

    PVGIS returns the plane-of-array components Gb(i) (beam), Gd(i) (diffuse)
    and Gr(i) (ground-reflected); the global POA irradiance is their sum
    (there is no single G(i) field). Timestamps are 'YYYYMMDD:HHMM' UTC.
    """
    hourly = raw["outputs"]["hourly"]
    records = []
    for i, row in enumerate(hourly):
        gb = float(row.get("Gb(i)", 0.0))
        gd = float(row.get("Gd(i)", 0.0))
        gr = float(row.get("Gr(i)", 0.0))
        records.append(WeatherRecord(
            hour_index=i,
            utc_hour=float(i),
            poa_global_W_m2=gb + gd + gr,
            poa_direct_W_m2=gb,
            poa_diffuse_W_m2=gd,
            poa_reflected_W_m2=gr,
            sun_height_deg=float(row.get("H_sun", 0.0)),
            T_amb_C=float(row.get("T2m", 20.0)),
            wind_speed_m_s=float(row.get("WS10m", 1.0)),
        ))
    return records


def fetch_pvgis_weather(cfg: MasterConfig, cache_path: str = "pvgis_2023_raw.json",
                         allow_network: bool = True) -> tuple[list[WeatherRecord], str]:
    """Retrieve PVGIS ERA5 hourly weather (Chunk 02).

    Order of precedence:
      1. A cached raw PVGIS JSON response on disk (deterministic replay).
      2. A live PVGIS 5.3 seriescalc request.

    This production entry point does NOT synthesize weather. If no valid PVGIS
    cache exists and the live PVGIS request fails, solving is aborted rather
    than silently optimizing against fabricated weather.

    PV-system conversion losses are NOT requested here (pvcalculation=0); PV
    electrical losses belong to Chunk 08 (critical rule / provenance).
    """
    site = cfg.site
    # 1. Deterministic replay from cache.
    if os.path.exists(cache_path):
        try:
            with open(cache_path) as f:
                raw = json.load(f)
            records = _parse_pvgis(raw)
            if len(records) == site.expected_hourly_records:
                return records, "pvgis_cached"
        except Exception:
            pass

    # 2. Live retrieval.
    if allow_network:
        try:
            import urllib.parse
            params = {
                "lat": site.latitude_deg,
                "lon": site.longitude_deg,
                "startyear": site.weather_year,
                "endyear": site.weather_year,
                "pvcalculation": 0,   # critical: no PV system losses in the weather layer
                "components": 1,
                "usehorizon": 1,
                "outputformat": "json",
            }
            url = "https://re.jrc.ec.europa.eu/api/v5_3/seriescalc?" + urllib.parse.urlencode(params)
            raw = _http_get_json(url, timeout=60.0)
            records = _parse_pvgis(raw)
            if len(records) == site.expected_hourly_records:
                with open(cache_path, "w") as f:
                    json.dump(raw, f)
                return records, "pvgis_live"
        except Exception:
            pass

    raise RuntimeError(
        "PVGIS weather is required for optimization, but no valid cached PVGIS "
        "dataset was found and the live PVGIS request failed. Refusing to use "
        "synthetic weather for engineering optimization."
    )


def interpolate_weather(records: list[WeatherRecord], t_hours: float, utc_offset_h: float = 0.0) -> WeatherRecord:
    """Interpolate the annual PVGIS series on the plant's LOCAL clock.

    PVGIS seriescalc timestamps are UTC. The cold-store schedule, loading
    window, and controller operate in local civil time. Therefore a local
    simulation time t maps to UTC weather time t - utc_offset_h. The annual
    dataset is treated as a periodic year, so the small year-boundary gap from
    the timezone shift wraps to the end of the same 8760-hour dataset.
    """
    n = len(records)
    if n <= 0:
        raise ValueError("weather records must be non-empty")
    t_local = t_hours % n
    t_utc = (t_local - float(utc_offset_h)) % n
    i0 = int(math.floor(t_utc)) % n
    i1 = (i0 + 1) % n
    frac = t_utc - math.floor(t_utc)
    a, b = records[i0], records[i1]

    def lerp(x, y):
        return x + frac * (y - x)

    return WeatherRecord(
        hour_index=i0,
        utc_hour=t_utc,
        poa_global_W_m2=lerp(a.poa_global_W_m2, b.poa_global_W_m2),
        poa_direct_W_m2=lerp(a.poa_direct_W_m2, b.poa_direct_W_m2),
        poa_diffuse_W_m2=lerp(a.poa_diffuse_W_m2, b.poa_diffuse_W_m2),
        poa_reflected_W_m2=lerp(a.poa_reflected_W_m2, b.poa_reflected_W_m2),
        sun_height_deg=lerp(a.sun_height_deg, b.sun_height_deg),
        T_amb_C=lerp(a.T_amb_C, b.T_amb_C),
        wind_speed_m_s=lerp(a.wind_speed_m_s, b.wind_speed_m_s),
    )


# =====================================================================
# CHUNK 03 -- Building Envelope, Ground Path, Infiltration, Internal Loads
# =====================================================================

