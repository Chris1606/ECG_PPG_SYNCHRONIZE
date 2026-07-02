/**
 * @file sensor_init.h
 * @brief Khoi tao va dong bo thu ECG AD8232 + PPG MAX30102 tren ESP32.
 */

#ifndef SENSOR_INIT_H
#define SENSOR_INIT_H

#pragma once

#include <stdbool.h>
#include <stdint.h>

#include <freertos/FreeRTOS.h>
#include <freertos/task.h>

#include "driver/i2c.h"
#include "driver/ledc.h"
#include "esp_err.h"
#include "hal/adc_types.h"
#include "max30105.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
  SENSOR_MODE_IDLE = 0,
  SENSOR_MODE_ECG,
  SENSOR_MODE_PPG,
  SENSOR_MODE_BOTH
} sensor_mode_t;

typedef struct {
  uint32_t time_ms;
  uint16_t ecg_raw;
} ecg_sample_t;

typedef struct {
  uint32_t time_ms;
  uint32_t red_raw;
  uint32_t ir_raw;
} ppg_sample_t;

// MAX30102
#define I2C_SDA_GPIO  21
#define I2C_SCL_GPIO  22
#define I2C_PORT      I2C_NUM_0
#define I2C_FREQ_HZ   400000

#define powerLed      UINT8_C(0x1F)
#define sampleAverage 4
#define ledMode       2
#define sampleRate    100
#define pulseWidth    411
#define adcRange      16384

// AD8232 ECG
#define ADC_CHANNEL   ADC_CHANNEL_6 // GPIO34
#define ADC_UNIT      ADC_UNIT_1
#define ADC_ATTEN     ADC_ATTEN_DB_12
#define ADC_SAMPLE_RATE 1000
#define ECG_CLIP_LOW_THRESHOLD 20
#define ECG_CLIP_HIGH_THRESHOLD 4075

// Measurement buffer
#define MEASUREMENT_DEFAULT_SECONDS 10
#define MEASUREMENT_MAX_SECONDS     120
#define ECG_TIMER_PERIOD_US         (1000000 / ADC_SAMPLE_RATE)
#define PPG_FIFO_READ_INTERVAL_MS   5

// Optional buzzer/R-peak config kept for later ECG processing extensions.
#define BUZZER_PIN       17
#define PWM_FREQ         1000
#define PWM_RES          LEDC_TIMER_13_BIT
#define PWM_CHANNEL      LEDC_CHANNEL_0
#define PWM_TIMER        LEDC_TIMER_0
#define R_PEAK_THREASHOLD 3000
#define NO_SIGNAL        0

void ad8232_configure(void);
void max30102_configure(void);
void mutex_init(void);

void readMAX30102_task(void *pvParameter);
void readAD8232_task(void *pvParameter);

void sensor_set_measurement_enabled(bool enabled);
bool sensor_is_measurement_enabled(void);
bool sensor_is_ecg_ready(void);
bool sensor_is_ppg_ready(void);

void sensor_set_mode(sensor_mode_t mode);
sensor_mode_t sensor_get_mode(void);
sensor_mode_t sensor_get_selected_mode(void);

uint32_t sensor_normalize_duration_s(uint32_t duration_s);
esp_err_t sensor_start_measurement(sensor_mode_t mode, uint32_t duration_s);
void sensor_stop_measurement(bool dump_after_stop);
bool sensor_is_dump_pending(void);

void printData_task(void *pvParameter);
void sensor_timer_callback(void);

#ifdef __cplusplus
}
#endif

#endif // SENSOR_INIT_H
