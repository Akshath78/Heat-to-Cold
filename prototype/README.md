# Working Prototype

This directory contains the control firmware used for the working cold-storage prototype.

## Prototype hardware interface

- Arduino-compatible microcontroller
- DHT22 temperature/humidity sensor
- Relay module
- Cooling load controlled through the relay

## Control logic

The prototype reads temperature and relative humidity from the DHT22 every 2 seconds and uses hysteresis-based temperature control:

- Cooling turns **ON** at or above **30 °C**.
- Cooling turns **OFF** at or below **28 °C**.
- The relay is configured as **active LOW**.
- A sensor read failure is reported through the serial interface.

The prototype firmware is intentionally separate from the research simulation in `src/heat_to_cold/`. The simulation represents the full physics-based system, while this Arduino program is the embedded control implementation used in the physical prototype.

## Arduino library

Install the **DHT sensor library** compatible with the Arduino environment before compiling the sketch.

## Firmware

[`cold_storage_prototype.ino`](cold_storage_prototype.ino)
