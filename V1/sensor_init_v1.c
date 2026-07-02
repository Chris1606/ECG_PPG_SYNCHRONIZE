/**
 * @brief Thu vien de dinh nghia cac ham thuc thi & khoi tao cua PPG - PCG - ECG
 * @author Luong Huu Phuc
 */
#include <stdio.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <freertos/semphr.h>
#include "esp_log.h"
#include "esp_err.h"
#include "sensor_init.h"

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

/** Thu vien cho ADC  */
#include "driver/adc.h"
#include "driver/ledc.h"

volatile unsigned long global_red = 0;
volatile unsigned long global_ir = 0;
volatile int global_adc_value = 0;
static volatile sensor_mode_t measurement_mode = SENSOR_MODE_IDLE;
static volatile int64_t ppg_mode_start_us = 0;

/** Global mutex variables */
SemaphoreHandle_t print_mutex = NULL; 



/**** MAX30102 ****/
max30105_t ppg_sensor;
// unsigned long red, ir;

static const char *TAG2 = "MAX30102";
static const char *TAG3 = "AD8232";

/** AD8232 configure */
void ad8232_configure(void){
  adc1_config_width(ADC_WIDTH);
  adc1_config_channel_atten(ADC_CHANNEL, ADC_ATTEN); //Suy hao
  ESP_LOGI(TAG3, "ADC Configured: Channel: %d, Attenuation: %d", ADC_CHANNEL, ADC_ATTEN);
}

/** MAX30102 configure */
void max30102_configure(void){
  uint8_t part_id = 0;

  ESP_ERROR_CHECK(max30105_init(&ppg_sensor, I2C_PORT, I2C_SDA_GPIO, I2C_SCL_GPIO, I2C_FREQ_HZ));
  if(max30105_read_part_id(&ppg_sensor, &part_id) == ESP_OK) {
    ESP_LOGI(TAG2, "Found MAX30102/MAX30105, Part ID: 0x%02x", part_id);
  }
  else {
    ESP_LOGE(TAG2, "Not found MAX30102");
  }
  ESP_ERROR_CHECK(max30105_setup(&ppg_sensor, powerLed, sampleAverage, ledMode, sampleRate, pulseWidth, adcRange));
}

/** muxtex init */
void mutex_init(void){
  //Khoi tao Semaphore
  print_mutex = xSemaphoreCreateMutex();  
  if(print_mutex == NULL){
    ESP_LOGE("MAIN", "Khong the khoi tao Mutex");
    return;
  }
}

void sensor_set_measurement_enabled(bool enabled){
  sensor_set_mode(enabled ? SENSOR_MODE_BOTH : SENSOR_MODE_IDLE);
}

bool sensor_is_measurement_enabled(void){
  return measurement_mode != SENSOR_MODE_IDLE;
}

void sensor_set_mode(sensor_mode_t mode){
  if(mode == SENSOR_MODE_PPG || mode == SENSOR_MODE_BOTH){
    max30105_clear_fifo(&ppg_sensor);
    ppg_mode_start_us = esp_timer_get_time();
  }
  measurement_mode = mode;
  if(mode == SENSOR_MODE_IDLE){
    global_red = 0;
    global_ir = 0;
    global_adc_value = 0;
    ppg_mode_start_us = 0;
  }
}

sensor_mode_t sensor_get_mode(void){
  return measurement_mode;
}

static bool ppg_uart_ready(void){
  if(ppg_mode_start_us == 0){
    return false;
  }
  int64_t elapsed_ms = (esp_timer_get_time() - ppg_mode_start_us) / 1000;
  return elapsed_ms >= PPG_STABILIZE_TIME_MS;
}

/** MAX30102 task */
void readMAX30102_task(void *pvParameter){
  ESP_LOGI(TAG2, "Bat dau doc cam bien MAX30102");

  while(1){
    uint16_t sample_count = 0;
    sensor_mode_t mode = sensor_get_mode();
    if(!(mode == SENSOR_MODE_PPG || mode == SENSOR_MODE_BOTH)){
      vTaskDelay(pdMS_TO_TICKS(20));
      continue;
    }
    vTaskDelay(1);
    if (max30105_check(&ppg_sensor, &sample_count) == ESP_OK) {
      while (max30105_available(&ppg_sensor)){
        global_red = max30105_get_fifo_red(&ppg_sensor);
        global_ir = max30105_get_fifo_ir(&ppg_sensor);
        max30105_next_sample(&ppg_sensor);
      }
    }
  }
}



/** AD8232 task */
void readAD8232_task(void *pvParameter){
  ESP_LOGI(TAG3, "Bat dau doc cam bien AD8232");

  while(true){
    sensor_mode_t mode = sensor_get_mode();
    if(!(mode == SENSOR_MODE_EEG || mode == SENSOR_MODE_BOTH)){
      vTaskDelay(pdMS_TO_TICKS(20));
      continue;
    }
    vTaskDelay(1);
    global_adc_value = (adc1_get_raw(ADC_CHANNEL));
    vTaskDelay(pdMS_TO_TICKS(1000 / ADC_SAMPLE_RATE)); //5ms - Tan so lay mau cua ADC duoc the hien qua ham nay
  }
}

/** print mutex task */
void printData_task(void *pvParameter){
  while(1){
    sensor_mode_t mode = sensor_get_mode();
    if(mode == SENSOR_MODE_IDLE){
      vTaskDelay(pdMS_TO_TICKS(20));
      continue;
    }
    if((mode == SENSOR_MODE_PPG || mode == SENSOR_MODE_BOTH) && !ppg_uart_ready()){
      vTaskDelay(pdMS_TO_TICKS(10));
      continue;
    }
    //Lay mutex truoc khi in
    if(xSemaphoreTake(print_mutex, portTICK_PERIOD_MS) == pdTRUE){
      if(mode == SENSOR_MODE_EEG){
        printf("EEG,%d\n", global_adc_value);
      }
      else if(mode == SENSOR_MODE_PPG){
        printf("PPG,%lu,%lu\n", global_red, global_ir);
      }
      else if(mode == SENSOR_MODE_BOTH){
        printf("BOTH,%lu,%lu,%d\n", global_red, global_ir, global_adc_value);
      }
      xSemaphoreGive(print_mutex);
    }
    if(mode == SENSOR_MODE_PPG || mode == SENSOR_MODE_BOTH){
      vTaskDelay(pdMS_TO_TICKS(1000 / sampleRate));
    }
    else{
      vTaskDelay(pdMS_TO_TICKS(10));
    }
  }
}
