import sys
from pathlib import Path
import math

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from heat_to_cold import MasterConfig, PCMConfig
from heat_to_cold.pcm import PCMState


def make_pcm_state(initial_soc=0.5):
    cfg = MasterConfig()
    cfg.pcm = PCMConfig(candidate='BIOCHAR_CS_CA_LA_OA', pcm_mass_kg=800.0, hx_area_m2=25.0, initial_soc=initial_soc)
    return PCMState(cfg.pcm)


def test_pcm_soc_bounds():
    pcm = make_pcm_state(0.5)
    assert 0.0 <= pcm.soc() <= 1.0


def test_pcm_initial_soc_is_consistent():
    for initial in (0.0, 0.25, 0.5, 0.75, 1.0):
        pcm = make_pcm_state(initial)
        assert math.isclose(pcm.soc(), initial, rel_tol=0.0, abs_tol=1e-10)


def test_pcm_discharge_reduces_cold_soc():
    pcm = make_pcm_state(1.0)
    soc_before = pcm.soc()
    out = pcm.step(
        dt_s=900.0,
        fluid_T_C=8.0,
        requested_heat_into_pcm_W=1500.0,
        T_ambient_C=30.0,
    )
    assert out['Q_into_pcm_W'] >= 0.0
    assert pcm.soc() < soc_before
    assert 0.0 <= pcm.soc() <= 1.0


def test_pcm_charge_increases_cold_soc():
    pcm = make_pcm_state(0.0)
    soc_before = pcm.soc()
    out = pcm.step(
        dt_s=900.0,
        fluid_T_C=0.0,
        requested_heat_into_pcm_W=-1500.0,
        T_ambient_C=0.0,
    )
    assert out['Q_into_pcm_W'] <= 0.0
    assert pcm.soc() > soc_before
    assert 0.0 <= pcm.soc() <= 1.0


def test_pcm_energy_is_bounded():
    pcm = make_pcm_state(0.5)
    pcm.step(900.0, 20.0, 1e9, 20.0)
    assert pcm.E_min_J <= pcm.E_J <= pcm.E_max_J
    pcm.step(900.0, -20.0, -1e9, -20.0)
    assert pcm.E_min_J <= pcm.E_J <= pcm.E_max_J


def test_pcm_discharge_cannot_exceed_available_energy():
    pcm = make_pcm_state(0.0)
    E_before = pcm.E_J
    pcm.step(900.0, 8.0, 1e9, 8.0)
    assert pcm.E_J >= pcm.E_min_J
    assert pcm.E_J <= E_before
    assert 0.0 <= pcm.soc() <= 1.0
