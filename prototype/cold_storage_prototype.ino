#include <DHT.h>

#define DHTPIN 2
#define DHTTYPE DHT22
#define RELAY_PIN 7

DHT dht(DHTPIN, DHTTYPE);

float coolingON = 30.0;
float coolingOFF = 28.0;

void setup() {
  Serial.begin(9600);

  dht.begin();

  pinMode(RELAY_PIN, OUTPUT);

  // Relay is Active LOW
  digitalWrite(RELAY_PIN, HIGH);   // Cooling OFF initially

  Serial.println("Cold Storage System Started");
}

void loop() {

  float temperature = dht.readTemperature();
  float humidity = dht.readHumidity();

  // Check sensor
  if (isnan(temperature) || isnan(humidity)) {
    Serial.println("DHT22 Sensor Error");
    delay(2000);
    return;
  }

  // COOLING CONTROL
  if (temperature >= coolingON) {
    digitalWrite(RELAY_PIN, LOW);   // Relay ON
    Serial.println("Cooling ON");
  }
  else if (temperature <= coolingOFF) {
    digitalWrite(RELAY_PIN, HIGH);  // Relay OFF
    Serial.println("Cooling OFF");
  }

  // Display readings
  Serial.print("Temperature: ");
  Serial.print(temperature);
  Serial.print(" C | Humidity: ");
  Serial.print(humidity);
  Serial.println(" %");

  delay(2000);
}
