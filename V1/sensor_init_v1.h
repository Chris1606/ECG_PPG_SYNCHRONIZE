/**
 * @file sensor_init.h 
 * @brief Ham khoi tao & thuc thi dong bo cam bien PPG - PCG - ECG
 * @author Luong Huu Phuc
 */

#ifndef SENSOR_INIT_H
#define SENSOR_INIT_H

#pragma once

#include <stdbool.h>
#include <stdio.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include "esp_log.h"
#include "esp_err.h"

/****Thu vien cho I2S****/
#if 0
#include "driver/i2s_std.h"
#include "driver/i2c_master.h"
#include "driver/i2s_types.h"
#include "driver/i2s_common.h"
#include "driver/i2s_types_legacy.h"
#endif

/** Thu vien cho I2C */
#include <string.h>
#include <esp_timer.h>
#include "driver/i2c.h"
#include "driver/i2c_types.h"
#include "driver/gpio.h"
#include "max30105.h"

typedef enum {
  SENSOR_MODE_IDLE = 0,
  SENSOR_MODE_EEG,
  SENSOR_MODE_PPG,
  SENSOR_MODE_BOTH
} sensor_mode_t;

//Cau hinh chan cho INMP441 
#if 0
#define I2S_PORT      I2S_NUM_0
#define DIN_PIN       33
#define BCLK_PIN      32
#define LRCL_PIN      25
#define SAMPLE_RATE   1000
#define dmaDesc       6 //6 bo dac ta dma
#define dmaLength     64//So bytes moi buffer, cang lon thi buffer_durations cang lau
#define DMA_BUFFER_SIZE  (dmaDesc * dmaLength) //6 * 128 = 768 bytes, buffer_duration = 6ms
#endif

//Cau hinh chan cho MAX30102
#define I2C_SDA_GPIO  21
#define I2C_SCL_GPIO  22
#define I2C_PORT      I2C_NUM_0
#define I2C_FREQ_HZ   400000
#define powerLed      UINT8_C(0x1F) //Cuong do led, tieu thu 6.4mA
#define sampleAverage 4
#define ledMode       2
#define sampleRate    100 //Tan so lay mau MAX30102
#define PPG_STABILIZE_TIME_MS 15000 //Cho PPG on dinh truoc khi gui UART
#define pulseWidth    411 //Xung cang rong, dai thu duoc cang nhieu (18 bit)
#define adcRange      16384 //14 bit ADC tieu thu 65.2pA moi LSB

//Cau hinh chan cho AD8232
#define ADC_CHANNEL      ADC_CHANNEL_6 //GPIO34
#define ADC_UNIT         ADC_UNIT_1
#define ADC_ATTEN        ADC_ATTEN_DB_12 //Tang pham vi do 
#define ADC_WIDTH        ADC_WIDTH_BIT_12
#define ADC_SAMPLE_RATE  1000
#define BUZZER_PIN       17

//Buzzer configure
#define PWM_FREQ          1000
#define PWM_RES           LEDC_TIMER_13_BIT
#define PWM_CHANNEL       LEDC_CHANNEL_0
#define PWM_TIMER         LEDC_TIMER_0
#define R_PEAK_THREASHOLD 3000 //Nguong toi thieu de phat hien dinh
#define NO_SIGNAL         0



/**
 * @note Ham cau hinh ADC cho ECG
 */
void ad8232_configure(void);

/**
 * @note Ham cau hinh I2C cho PPG 
 */
void max30102_configure(void);

/**
 * @note Hma thuc thi doc du lieu PPG
 */
void readMAX30102_task(void *pvParameter);


/**
 * @brief Ham thuc thi doc du lieu ECG
 */
void readAD8232_task(void *pvParameter);

/**
 * @brief Ham khoi tao mutex cho semaphore
 */
void mutex_init(void);

void sensor_set_measurement_enabled(bool enabled);
bool sensor_is_measurement_enabled(void);
void sensor_set_mode(sensor_mode_t mode);
sensor_mode_t sensor_get_mode(void);

/**
 * @brief Ham in dong bo ket qua 3 cam bien ra man hinh 
 */
void printData_task(void *pvParameter);

/**
 * @brief Ham su dung Timer de dong bo cam bien
 */
void sensor_timer_callback(void);

#endif //SENSOR_INIT_H
