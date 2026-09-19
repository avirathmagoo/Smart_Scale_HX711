#include <Arduino.h>
#include "HX711.h"

#define SHARED_SCK_PIN 15
#define DOUT_PIN_1 2
#define DOUT_PIN_2 14
#define DOUT_PIN_3 13
#define DOUT_PIN_4 4

#define SCALE_FACTOR 40.0f
#define NOISE_FLOOR_G 120.0f

HX711 scale1;
HX711 scale2;
HX711 scale3;
HX711 scale4;

float weight1 = 0.0f;
float weight2 = 0.0f;
float weight3 = 0.0f;
float weight4 = 0.0f;

void initScales()
{
    scale1.begin(DOUT_PIN_1, SHARED_SCK_PIN);
    scale2.begin(DOUT_PIN_2, SHARED_SCK_PIN);
    scale3.begin(DOUT_PIN_3, SHARED_SCK_PIN);
    scale4.begin(DOUT_PIN_4, SHARED_SCK_PIN);

    scale1.set_scale(SCALE_FACTOR);
    scale2.set_scale(SCALE_FACTOR);
    scale3.set_scale(SCALE_FACTOR);
    scale4.set_scale(SCALE_FACTOR);

    scale1.tare();
    scale2.tare();
    scale3.tare();
    scale4.tare();
}

float readScale(HX711& scale)
{
    if (!scale.is_ready()) {
        return 0.0f;
    }

    float value = fabsf(scale.get_units(1));

    if (value < NOISE_FLOOR_G) {
        value = 0.0f;
    }

    return value;
}

void sendValues()
{
    Serial.printf(
        "%.1f,%.1f,%.1f,%.1f\n",
        weight1,
        weight2,
        weight3,
        weight4
    );
}

void setup()
{
    Serial.begin(115200);
    Serial.setDebugOutput(false);
    initScales();
}

void loop()
{
    static uint32_t lastRead = 0;
    static uint32_t lastSend = 0;

    uint32_t now = millis();

    if (now - lastRead >= 10) {
        lastRead = now;

        float v1 = readScale(scale1);
        float v2 = readScale(scale2);
        float v3 = readScale(scale3);
        float v4 = readScale(scale4);

        if (v1 != 0.0f || scale1.is_ready()) weight1 = v1;
        if (v2 != 0.0f || scale2.is_ready()) weight2 = v2;
        if (v3 != 0.0f || scale3.is_ready()) weight3 = v3;
        if (v4 != 0.0f || scale4.is_ready()) weight4 = v4;
    }

    if (now - lastSend >= 250) {
        lastSend = now;
        sendValues();
    }

    delay(1);
}
