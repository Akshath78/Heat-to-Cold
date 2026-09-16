"""End-to-end production optimization test.

This is intentionally opt-in because a real annual NSGA-II run is expensive.
Run it with:

    RUN_FULL_OPT=1 pytest -q tests/test_full_optimization.py -s

By default the test is skipped, so ordinary unit/smoke testing stays fast.
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from heat_to_cold import MasterConfig
from heat_to_cold.weather import fetch_pvgis_weather
from heat_to_cold.optimizer import run_optimization, HAVE_PYMOO


@pytest.mark.slow
@pytest.mark.skipif(os.getenv('RUN_FULL_OPT') != '1', reason='set RUN_FULL_OPT=1 to run the full production optimization')
def test_full_biochar_nsga2_end_to_end(tmp_path, monkeypatch):
    if not HAVE_PYMOO:
        pytest.skip('pymoo is not installed in the test environment')

    cfg = MasterConfig()
    cfg.pcm.candidate = 'BIOCHAR_CS_CA_LA_OA'

    # Use the frozen production weather file committed with the repository.
    cache_candidates = [
        Path(__file__).resolve().parents[1] / 'data' / 'weather' / 'pvgis_2023_raw.json',
        Path.cwd() / 'pvgis_2023_raw.json',
        Path.home() / 'Downloads' / 'pvgis_2023_raw.json',
    ]
    cache = next((p for p in cache_candidates if p.exists()), None)
    if cache is None:
        pytest.skip('PVGIS 2023 cache not found; provide the production weather cache before running this test')

    weather, source = fetch_pvgis_weather(cfg, cache_path=str(cache), allow_network=False)
    assert len(weather) == 8760
    assert 'pvgis' in source.lower()

    monkeypatch.chdir(tmp_path)

    best, candidates, front = run_optimization(
        cfg=cfg,
        weather=weather,
        source=source,
        pop_size=80,
        n_gen=15,
        seed=1,
        duration_days=365.0,
        opt_dt_min=120.0,
        final_dt_min=60.0,
        convergence_dt_min=120.0,
        n_seeds=1,
        progress_every=100,
        optimizer_name='nsga2',
        workers=8,
    )

    assert candidates
    assert len(candidates) >= 80
    assert front
    assert best is not None
    assert best.get('feasible') is True
    assert float(best.get('unmet_kWh', 1.0)) <= 1e-8
    assert float(best.get('rh_violation_hours', 1.0)) <= 0.0 + 1e-8
    assert (tmp_path / 'optimization_results.csv').exists()
    assert (tmp_path / 'pareto_front.csv').exists()
    assert (tmp_path / 'recommended_design.json').exists()
