# Thermodynamic Model

The current model is a reduced-order engineering simulation of a 5 MT cold room.

## Main modeled domains

- Room air and envelope heat transfer
- Moist-air psychrometrics
- Resident produce surface/core temperatures
- Warm incoming produce during the 3.5 h receiving window
- Respiration and transpiration
- Refrigeration and evaporator behavior
- Condensation and frost formation
- Defrost
- PCM enthalpy storage
- PV generation
- Battery SOC
- Electrical availability
- Thermal and energy residuals

## Design basis represented in the current script

- Room: approximately 3.05 × 3.05 × 2.44 m
- Resident produce: 5097 kg
- Daily turnover: 15% of inventory
- Incoming product: 30 °C
- Room setpoint: 4 °C
- RH band: 90–99%
- Transmission UA: 12.58 W/K
- Infiltration sensible UA basis: 25.97 W/K

The values are model inputs and should be replaced with measured or vendor data as the prototype is refined.
