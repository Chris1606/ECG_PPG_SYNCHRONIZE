/**
 * @brief Khoi tao va dong bo thu tin hieu ECG AD8232 + PPG MAX30102.
 *
 * Luong dung:
 * - PC gui mode: ECG / PPG / BOTH.
 * - PC gui START <duration_s>.
 * - ESP32 reset timestamp, cap phat buffer, bat esp_timer 1 kHz cho ECG.
 * - PPG duoc doc tu FIFO MAX30102 va gan timestamp nguoc theo sample rate.
 * - Trong luc do khong stream mau realtime.
 * - Het thoi gian do moi gui toan bo du lieu tho CSV qua UART.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <limits.h>

#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>
#include <freertos/task.h>

#include "esp_check.h"
#include "esp_err.h"
#include "esp_log.h"
#include "esp_timer.h"

#include "driver/gpio.h"
#include "driver/adc.h"
#include "driver/i2c.h"
#include "driver/i2c_types.h"
#include "driver/ledc.h"

#include "max30105.h"
#include "sensor_init.h"

SemaphoreHandle_t print_mutex = NULL;
max30105_t ppg_sensor;

static const char *TAG_MAX = "MAX30102";
static const char *TAG_ADC = "AD8232";
static const char *TAG_SYNC = "SYNC";

static sensor_mode_t selected_mode = SENSOR_MODE_BOTH;
static volatile sensor_mode_t active_mode = SENSOR_MODE_IDLE;
static volatile bool measurement_running = false;
static volatile bool dump_pending = false;
static volatile bool ecg_overflow = false;
static volatile bool ppg_overflow = false;
static bool adc_ready = false;
static bool ppg_ready = false;

static volatile int64_t measurement_start_us = 0;
static volatile uint32_t measurement_duration_ms = 0;
static uint32_t ecg_sample_index = 0;
static uint32_t ecg_first_actual_us = 0;
static uint32_t ecg_last_actual_us = 0;
static uint32_t ecg_min_period_us = UINT_MAX;
static uint32_t ecg_max_period_us = 0;
static uint32_t ecg_clip_low_count = 0;
static uint32_t ecg_clip_high_count = 0;

static ecg_sample_t *ecg_buffer = NULL;
static ppg_sample_t *ppg_buffer = NULL;
static size_t ecg_capacity = 0;
static size_t ppg_capacity = 0;
static volatile size_t ecg_count = 0;
static volatile size_t ppg_count = 0;

static esp_timer_handle_t ecg_timer_handle = NULL;
static portMUX_TYPE data_lock = portMUX_INITIALIZER_UNLOCKED;

static void ecg_timer_callback(void *arg);
static void store_ecg_sample(uint16_t raw);

static bool mode_has_ecg(sensor_mode_t mode)
{
  return mode == SENSOR_MODE_ECG || mode == SENSOR_MODE_BOTH;
}

static bool mode_has_ppg(sensor_mode_t mode)
{
  return mode == SENSOR_MODE_PPG || mode == SENSOR_MODE_BOTH;
}

static TickType_t delay_ticks_at_least_1(uint32_t ms)
{
  TickType_t ticks = pdMS_TO_TICKS(ms);
  return (ticks == 0) ? 1 : ticks;
}

uint32_t sensor_normalize_duration_s(uint32_t duration_s)
{
  if(duration_s == 0){
    return MEASUREMENT_DEFAULT_SECONDS;
  }
  if(duration_s > MEASUREMENT_MAX_SECONDS){
    return MEASUREMENT_MAX_SECONDS;
  }
  return duration_s;
}

static uint32_t measurement_time_ms(void)
{
  int64_t start_us = measurement_start_us;
  if(start_us == 0){
    return 0;
  }
  return (uint32_t)((esp_timer_get_time() - start_us) / 1000);
}

static void free_measurement_buffers(void)
{
  free(ecg_buffer);
  free(ppg_buffer);
  ecg_buffer = NULL;
  ppg_buffer = NULL;
  ecg_capacity = 0;
  ppg_capacity = 0;
}

static void reset_measurement_state(void)
{
  ecg_count = 0;
  ppg_count = 0;
  ecg_sample_index = 0;
  ecg_first_actual_us = 0;
  ecg_last_actual_us = 0;
  ecg_min_period_us = UINT_MAX;
  ecg_max_period_us = 0;
  ecg_clip_low_count = 0;
  ecg_clip_high_count = 0;
  ecg_overflow = false;
  ppg_overflow = false;
}

static bool measurement_time_in_range(uint32_t time_ms)
{
  return time_ms < measurement_duration_ms;
}

void ad8232_configure(void)
{
  adc_ready = false;

  esp_err_t err = adc1_config_width(ADC_WIDTH_BIT_12);
  if(err != ESP_OK){
    ESP_LOGE(TAG_ADC, "adc1_config_width failed: %s", esp_err_to_name(err));
    return;
  }

  err = adc1_config_channel_atten(ADC_CHANNEL, ADC_ATTEN);
  if(err != ESP_OK){
    ESP_LOGE(TAG_ADC, "adc1_config_channel_atten failed: %s", esp_err_to_name(err));
    return;
  }

  if(ecg_timer_handle == NULL){
    const esp_timer_create_args_t timer_args = {
      .callback = ecg_timer_callback,
      .arg = NULL,
      .dispatch_method = ESP_TIMER_TASK,
      .name = "ecg_1khz",
      .skip_unhandled_events = true,
    };
    err = esp_timer_create(&timer_args, &ecg_timer_handle);
    if(err != ESP_OK){
      ESP_LOGE(TAG_ADC, "esp_timer_create failed: %s", esp_err_to_name(err));
      return;
    }
  }

  adc_ready = true;
  ESP_LOGI(TAG_ADC, "ADC timer configured: channel=%d ecg_rate=%dHz",
           ADC_CHANNEL, ADC_SAMPLE_RATE);
}

void max30102_configure(void)
{
  ppg_ready = false;
  uint8_t part_id = 0;

  esp_err_t err = max30105_init(&ppg_sensor, I2C_PORT, I2C_SDA_GPIO, I2C_SCL_GPIO, I2C_FREQ_HZ);
  if(err != ESP_OK){
    ESP_LOGE(TAG_MAX, "I2C init failed: %s", esp_err_to_name(err));
    return;
  }

  err = max30105_read_part_id(&ppg_sensor, &part_id);
  if(err == ESP_OK){
    ESP_LOGI(TAG_MAX, "Found MAX30102/MAX30105, Part ID: 0x%02x", part_id);
  }
  else{
    ESP_LOGE(TAG_MAX, "read part id failed: %s", esp_err_to_name(err));
    return;
  }

  err = max30105_setup(&ppg_sensor, powerLed, sampleAverage, ledMode,
                       sampleRate, pulseWidth, adcRange);
  if(err != ESP_OK){
    ESP_LOGE(TAG_MAX, "MAX30102 setup failed: %s", esp_err_to_name(err));
    return;
  }

  ppg_ready = true;
}

void mutex_init(void)
{
  print_mutex = xSemaphoreCreateMutex();
  if(print_mutex == NULL){
    ESP_LOGE(TAG_SYNC, "Khong the khoi tao print mutex");
  }
}

void sensor_set_measurement_enabled(bool enabled)
{
  if(enabled){
    (void)sensor_start_measurement(selected_mode, MEASUREMENT_DEFAULT_SECONDS);
  }
  else{
    sensor_stop_measurement(true);
  }
}

bool sensor_is_measurement_enabled(void)
{
  return measurement_running;
}

bool sensor_is_ecg_ready(void)
{
  return adc_ready;
}

bool sensor_is_ppg_ready(void)
{
  return ppg_ready;
}

void sensor_set_mode(sensor_mode_t mode)
{
  if(measurement_running){
    ESP_LOGW(TAG_SYNC, "Dang do, khong doi mode");
    return;
  }

  selected_mode = (mode == SENSOR_MODE_IDLE) ? SENSOR_MODE_BOTH : mode;
}

sensor_mode_t sensor_get_mode(void)
{
  return active_mode;
}

sensor_mode_t sensor_get_selected_mode(void)
{
  return selected_mode;
}

esp_err_t sensor_start_measurement(sensor_mode_t mode, uint32_t duration_s)
{
  esp_err_t ret = ESP_OK;

  if(measurement_running){
    return ESP_ERR_INVALID_STATE;
  }

  if(mode == SENSOR_MODE_IDLE){
    mode = selected_mode;
  }
  if(mode == SENSOR_MODE_IDLE){
    mode = SENSOR_MODE_BOTH;
  }
  duration_s = sensor_normalize_duration_s(duration_s);

  free_measurement_buffers();
  reset_measurement_state();

  if(mode_has_ecg(mode) && !adc_ready){
    return ESP_ERR_INVALID_STATE;
  }
  if(mode_has_ppg(mode) && !ppg_ready){
    return ESP_ERR_INVALID_STATE;
  }

  ecg_capacity = mode_has_ecg(mode) ? ((size_t)duration_s * ADC_SAMPLE_RATE + ADC_SAMPLE_RATE) : 0;
  ppg_capacity = mode_has_ppg(mode) ? ((size_t)duration_s * sampleRate + MAX30105_STORAGE_SIZE * 2) : 0;

  if(ecg_capacity > 0){
    ecg_buffer = (ecg_sample_t *)calloc(ecg_capacity, sizeof(ecg_sample_t));
    ESP_GOTO_ON_FALSE(ecg_buffer != NULL, ESP_ERR_NO_MEM, fail, TAG_SYNC, "No memory for ECG buffer");
  }

  if(ppg_capacity > 0){
    ppg_buffer = (ppg_sample_t *)calloc(ppg_capacity, sizeof(ppg_sample_t));
    ESP_GOTO_ON_FALSE(ppg_buffer != NULL, ESP_ERR_NO_MEM, fail, TAG_SYNC, "No memory for PPG buffer");
  }

  if(mode_has_ppg(mode)){
    ESP_GOTO_ON_ERROR(max30105_clear_fifo(&ppg_sensor), fail, TAG_MAX, "clear FIFO failed");
  }

  if(mode_has_ecg(mode)){
    ESP_GOTO_ON_FALSE(ecg_timer_handle != NULL, ESP_ERR_INVALID_STATE, fail, TAG_ADC, "ECG timer not configured");
  }

  portENTER_CRITICAL(&data_lock);
  selected_mode = mode;
  active_mode = mode;
  measurement_start_us = esp_timer_get_time();
  measurement_duration_ms = duration_s * 1000U;
  measurement_running = true;
  dump_pending = false;
  portEXIT_CRITICAL(&data_lock);

  if(mode_has_ecg(mode)){
    int first_raw = adc1_get_raw(ADC_CHANNEL);
    if(first_raw >= 0){
      store_ecg_sample((uint16_t)first_raw);
    }
    ESP_GOTO_ON_ERROR(esp_timer_start_periodic(ecg_timer_handle, ECG_TIMER_PERIOD_US),
                      fail_after_start, TAG_ADC, "start ECG timer failed");
  }

  ESP_LOGI(TAG_SYNC, "START mode=%d duration=%lu s", mode, (unsigned long)duration_s);
  return ESP_OK;

fail_after_start:
  portENTER_CRITICAL(&data_lock);
  active_mode = SENSOR_MODE_IDLE;
  measurement_start_us = 0;
  measurement_duration_ms = 0;
  measurement_running = false;
  dump_pending = false;
  portEXIT_CRITICAL(&data_lock);
  free_measurement_buffers();
  reset_measurement_state();
  return ret;

fail:
  portENTER_CRITICAL(&data_lock);
  active_mode = SENSOR_MODE_IDLE;
  measurement_start_us = 0;
  measurement_duration_ms = 0;
  measurement_running = false;
  dump_pending = false;
  portEXIT_CRITICAL(&data_lock);
  free_measurement_buffers();
  reset_measurement_state();
  return ret;
}

void sensor_stop_measurement(bool dump_after_stop)
{
  sensor_mode_t mode_to_stop;

  portENTER_CRITICAL(&data_lock);
  mode_to_stop = active_mode;
  if(measurement_running && dump_after_stop && active_mode != SENSOR_MODE_IDLE){
    dump_pending = true;
  }
  measurement_running = false;
  active_mode = SENSOR_MODE_IDLE;
  portEXIT_CRITICAL(&data_lock);

  if(mode_has_ecg(mode_to_stop) && ecg_timer_handle != NULL){
    (void)esp_timer_stop(ecg_timer_handle);
  }
}

bool sensor_is_dump_pending(void)
{
  return dump_pending;
}

static void store_ecg_sample(uint16_t raw)
{
  uint32_t actual_us = 0;
  int64_t start_us = measurement_start_us;
  if(start_us != 0){
    actual_us = (uint32_t)(esp_timer_get_time() - start_us);
  }

  portENTER_CRITICAL(&data_lock);
  if(!measurement_running || !mode_has_ecg(active_mode)){
    portEXIT_CRITICAL(&data_lock);
    return;
  }

  uint32_t time_ms = (ecg_sample_index * 1000U) / ADC_SAMPLE_RATE;
  ecg_sample_index++;

  if(measurement_time_in_range(time_ms) && ecg_buffer != NULL && ecg_count < ecg_capacity){
    if(ecg_count == 0){
      ecg_first_actual_us = actual_us;
    }
    else{
      uint32_t period_us = actual_us - ecg_last_actual_us;
      if(period_us < ecg_min_period_us){
        ecg_min_period_us = period_us;
      }
      if(period_us > ecg_max_period_us){
        ecg_max_period_us = period_us;
      }
    }
    ecg_last_actual_us = actual_us;
    if(raw <= ECG_CLIP_LOW_THRESHOLD){
      ecg_clip_low_count++;
    }
    if(raw >= ECG_CLIP_HIGH_THRESHOLD){
      ecg_clip_high_count++;
    }
    ecg_buffer[ecg_count].time_ms = time_ms;
    ecg_buffer[ecg_count].ecg_raw = raw;
    ecg_count++;
  }
  else if(measurement_time_in_range(time_ms)){
    ecg_overflow = true;
  }
  portEXIT_CRITICAL(&data_lock);
}

static void ecg_timer_callback(void *arg)
{
  (void)arg;

  sensor_mode_t mode = sensor_get_mode();
  if(!measurement_running || !mode_has_ecg(mode)){
    return;
  }

  int raw = adc1_get_raw(ADC_CHANNEL);
  if(raw >= 0){
    store_ecg_sample((uint16_t)raw);
  }
}

static void store_ppg_sample(uint32_t time_ms, uint32_t red, uint32_t ir)
{
  portENTER_CRITICAL(&data_lock);
  if(!measurement_running || !mode_has_ppg(active_mode)){
    portEXIT_CRITICAL(&data_lock);
    return;
  }

  if(measurement_time_in_range(time_ms) && ppg_buffer != NULL && ppg_count < ppg_capacity){
    ppg_buffer[ppg_count].time_ms = time_ms;
    ppg_buffer[ppg_count].red_raw = red;
    ppg_buffer[ppg_count].ir_raw = ir;
    ppg_count++;
  }
  else if(measurement_time_in_range(time_ms)){
    ppg_overflow = true;
  }
  portEXIT_CRITICAL(&data_lock);
}

static void dump_measurement_csv(void)
{
  size_t local_ecg_count;
  size_t local_ppg_count;
  bool local_ecg_overflow;
  bool local_ppg_overflow;
  uint32_t expected_ecg_count;
  uint32_t expected_ppg_count;
  uint32_t local_ecg_first_actual_us;
  uint32_t local_ecg_last_actual_us;
  uint32_t local_ecg_min_period_us;
  uint32_t local_ecg_max_period_us;
  uint32_t local_ecg_clip_low_count;
  uint32_t local_ecg_clip_high_count;

  portENTER_CRITICAL(&data_lock);
  local_ecg_count = ecg_count;
  local_ppg_count = ppg_count;
  local_ecg_overflow = ecg_overflow;
  local_ppg_overflow = ppg_overflow;
  expected_ecg_count = (ecg_capacity > 0) ? ((measurement_duration_ms * ADC_SAMPLE_RATE) / 1000U) : 0;
  expected_ppg_count = (ppg_capacity > 0) ? ((measurement_duration_ms * sampleRate) / 1000U) : 0;
  local_ecg_first_actual_us = ecg_first_actual_us;
  local_ecg_last_actual_us = ecg_last_actual_us;
  local_ecg_min_period_us = (ecg_min_period_us == UINT_MAX) ? 0 : ecg_min_period_us;
  local_ecg_max_period_us = ecg_max_period_us;
  local_ecg_clip_low_count = ecg_clip_low_count;
  local_ecg_clip_high_count = ecg_clip_high_count;
  portEXIT_CRITICAL(&data_lock);

  printf("BEGIN_SYNC_CSV,%u,%u\n", (unsigned)local_ecg_count, (unsigned)local_ppg_count);
  printf("time_ms,ecg_raw,ppg_red_raw,ppg_ir_raw\n");

  size_t i = 0;
  size_t j = 0;
  while(i < local_ecg_count || j < local_ppg_count){
    if(i < local_ecg_count && j < local_ppg_count && ecg_buffer[i].time_ms == ppg_buffer[j].time_ms){
      printf("%lu,%u,%lu,%lu\n",
             (unsigned long)ecg_buffer[i].time_ms,
             (unsigned)ecg_buffer[i].ecg_raw,
             (unsigned long)ppg_buffer[j].red_raw,
             (unsigned long)ppg_buffer[j].ir_raw);
      i++;
      j++;
    }
    else if(j >= local_ppg_count || (i < local_ecg_count && ecg_buffer[i].time_ms < ppg_buffer[j].time_ms)){
      printf("%lu,%u,,\n",
             (unsigned long)ecg_buffer[i].time_ms,
             (unsigned)ecg_buffer[i].ecg_raw);
      i++;
    }
    else{
      printf("%lu,,%lu,%lu\n",
             (unsigned long)ppg_buffer[j].time_ms,
             (unsigned long)ppg_buffer[j].red_raw,
             (unsigned long)ppg_buffer[j].ir_raw);
      j++;
    }
  }

  printf("STATS,ECG_EXPECTED,%lu,ECG_ACTUAL,%u,PPG_EXPECTED,%lu,PPG_ACTUAL,%u\n",
         (unsigned long)expected_ecg_count,
         (unsigned)local_ecg_count,
         (unsigned long)expected_ppg_count,
         (unsigned)local_ppg_count);
  printf("STATS,ECG_FIRST_ACTUAL_US,%lu,ECG_LAST_ACTUAL_US,%lu,ECG_MIN_PERIOD_US,%lu,ECG_MAX_PERIOD_US,%lu\n",
         (unsigned long)local_ecg_first_actual_us,
         (unsigned long)local_ecg_last_actual_us,
         (unsigned long)local_ecg_min_period_us,
         (unsigned long)local_ecg_max_period_us);
  printf("STATS,ECG_CLIP_LOW,%lu,ECG_CLIP_HIGH,%lu,ECG_OVERFLOW,%d,PPG_OVERFLOW,%d\n",
         (unsigned long)local_ecg_clip_low_count,
         (unsigned long)local_ecg_clip_high_count,
         local_ecg_overflow ? 1 : 0,
         local_ppg_overflow ? 1 : 0);
  printf("END_SYNC_CSV,%u,%u,%d,%d\n",
         (unsigned)local_ecg_count,
         (unsigned)local_ppg_count,
         local_ecg_overflow ? 1 : 0,
         local_ppg_overflow ? 1 : 0);

  portENTER_CRITICAL(&data_lock);
  dump_pending = false;
  measurement_start_us = 0;
  measurement_duration_ms = 0;
  portEXIT_CRITICAL(&data_lock);

  free_measurement_buffers();
  reset_measurement_state();
}

void readMAX30102_task(void *pvParameter)
{
  ESP_LOGI(TAG_MAX, "Bat dau doc cam bien MAX30102");

  while(1){
    sensor_mode_t mode = sensor_get_mode();
    if(!measurement_running || !mode_has_ppg(mode)){
      vTaskDelay(delay_ticks_at_least_1(5));
      continue;
    }

    uint16_t sample_count = 0;
    uint32_t batch_time_ms = measurement_time_ms();
    if(max30105_check(&ppg_sensor, &sample_count) == ESP_OK && sample_count > 0){
      uint8_t available = max30105_available(&ppg_sensor);
      uint32_t ppg_period_ms = 1000U / sampleRate;

      for(uint8_t i = 0; i < available; i++){
        uint32_t age_ms = ((uint32_t)available - 1U - i) * ppg_period_ms;
        uint32_t time_ms = (batch_time_ms > age_ms) ? (batch_time_ms - age_ms) : 0;
        uint32_t red = max30105_get_fifo_red(&ppg_sensor);
        uint32_t ir = max30105_get_fifo_ir(&ppg_sensor);
        store_ppg_sample(time_ms, red, ir);
        max30105_next_sample(&ppg_sensor);
      }
    }

    vTaskDelay(delay_ticks_at_least_1(PPG_FIFO_READ_INTERVAL_MS));
  }
}

void readAD8232_task(void *pvParameter)
{
  ESP_LOGI(TAG_ADC, "AD8232 ECG sampled by esp_timer");
  while(true){
    vTaskDelay(delay_ticks_at_least_1(1000));
  }
}

void printData_task(void *pvParameter)
{
  while(1){
    if(measurement_running && measurement_time_ms() >= measurement_duration_ms){
      sensor_stop_measurement(true);
    }

    if(sensor_is_dump_pending()){
      if(print_mutex == NULL || xSemaphoreTake(print_mutex, portMAX_DELAY) == pdTRUE){
        dump_measurement_csv();
        if(print_mutex != NULL){
          xSemaphoreGive(print_mutex);
        }
      }
    }

    vTaskDelay(delay_ticks_at_least_1(10));
  }
}

void sensor_timer_callback(void)
{
  /* ECG da duoc lay mau boi esp_timer 1 kHz trong file nay. */
}
