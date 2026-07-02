#ifndef MAX30105_H
#define MAX30105_H

#include <stdbool.h>
#include <stdint.h>
#include "driver/gpio.h"
#include "driver/i2c.h"
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

#define MAX30105_ADDRESS 0x57
#define MAX30105_STORAGE_SIZE 32

typedef struct {
    i2c_port_t i2c_port;
    uint8_t i2c_addr;
    uint8_t active_leds;
    uint32_t red[MAX30105_STORAGE_SIZE];
    uint32_t ir[MAX30105_STORAGE_SIZE];
    uint8_t head;
    uint8_t tail;
} max30105_t;

esp_err_t max30105_init(max30105_t *sensor,
                        i2c_port_t i2c_port,
                        gpio_num_t sda_pin,
                        gpio_num_t scl_pin,
                        uint32_t i2c_speed_hz);
esp_err_t max30105_setup(max30105_t *sensor,
                         uint8_t power_level,
                         uint8_t sample_average,
                         uint8_t led_mode,
                         int sample_rate,
                         int pulse_width,
                         int adc_range);
esp_err_t max30105_read_part_id(max30105_t *sensor, uint8_t *part_id);
esp_err_t max30105_clear_fifo(max30105_t *sensor);
esp_err_t max30105_check(max30105_t *sensor, uint16_t *num_samples);
uint8_t max30105_available(max30105_t *sensor);
uint32_t max30105_get_fifo_red(max30105_t *sensor);
uint32_t max30105_get_fifo_ir(max30105_t *sensor);
void max30105_next_sample(max30105_t *sensor);

#ifdef __cplusplus
}
#endif

#endif
