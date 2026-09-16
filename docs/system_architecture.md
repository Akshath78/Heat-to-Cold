# System Architecture

Heat to Cold is a decentralized mini cold-storage system combining renewable electrical generation, electrical storage, refrigeration, thermal storage, sensing and supervisory control.

## Architecture workflow

```text
PVGIS weather + storage requirements
              ↓
       Physics-based model
              ↓
 ┌────────────┼────────────┐
 ↓            ↓            ↓
PV         Battery        PCM
 └────────────┼────────────┘
              ↓
        Refrigeration
              ↓
          Cold room
              ↓
   Temperature / RH / product state
              ↓
     Predictive priority control
              ↓
     Local monitoring + GSM alerts
```

## Main subsystems

### Energy subsystem

Solar PV supplies electrical energy to the refrigeration and auxiliary loads. Battery storage provides an electrical buffer when PV generation is insufficient.

### Thermal-storage subsystem

PCM provides thermal buffering and can absorb cooling demand when the compressor is not operating. The model represents PCM behavior using an enthalpy/state formulation.

### Refrigeration subsystem

The refrigeration model accounts for cooling capacity, sensible and latent heat removal, operating conditions, condensation, frost and defrost behavior.

### Cold-room subsystem

The room model tracks air temperature, moisture conditions and resident/incoming produce states. Warm produce is introduced during the defined receiving window and its cooling load is included in the transient simulation.

### Control and monitoring

The intended control layer prioritizes critical cooling demand using room conditions, product state, PCM state and available electrical energy. Local monitoring is designed to remain functional without continuous internet connectivity, with GSM used for critical alerts.

## Design boundary

This architecture represents the system-level engineering model. Physical hardware selection, sensor calibration, refrigeration controls and final PCM implementation require prototype-level validation.
