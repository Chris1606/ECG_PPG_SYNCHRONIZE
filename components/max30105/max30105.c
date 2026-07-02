#include "max30105.h"
#include <string.h>
#include "esp_check.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#define MAX30105_INTSTAT1        0x00
#define MAX30105_FIFOWRITEPTR    0x04
#define MAX30105_FIFOOVERFLOW    0x05
#define MAX30105_FIFOREADPTR     0x06
#define MAX30105_FIFODATA        0x07
#define MAX30105_FIFOCONFIG      0x08
#define MAX30105_MODECONFIG      0x09
#define MAX30105_PARTICLECONFIG  0x0A
#define MAX30105_LED1_PULSEAMP   0x0C
#define MAX30105_LED2_PULSEAMP   0x0D
#define MAX30105_MULTILEDCONFIG1 0x11
#define MAX30105_PARTID          0xFF

static esp_err_t write_reg(max30105_t *sensor, uint8_t reg, uint8_t value)
{
    uint8_t data[2] = { reg, value };
    return i2c_master_write_to_device(sensor->i2c_port,
                                      sensor->i2c_addr,
                                      data,
                                      sizeof(data),
                                      pdMS_TO_TICKS(100));
}

static esp_err_t read_reg(max30105_t *sensor, uint8_t reg, uint8_t *value)
{
    return i2c_master_write_read_device(sensor->i2c_port,
                                        sensor->i2c_addr,
                                        &reg,
                                        1,
                                        value,
                                        1,
                                        pdMS_TO_TICKS(100));
}

static esp_err_t read_regs(max30105_t *sensor, uint8_t reg, uint8_t *data, size_t len)
{
    return i2c_master_write_read_device(sensor->i2c_port,
                                        sensor->i2c_addr,
                                        &reg,
                                        1,
                                        data,
                                        len,
                                        pdMS_TO_TICKS(100));
}

static uint8_t sample_average_bits(uint8_t sample_average)
{
    switch (sample_average) {
    case 1: return 0x00;
    case 2: return 0x20;
    case 4: return 0x40;
    case 8: return 0x60;
    case 16: return 0x80;
    case 32: return 0xA0;
    default: return 0x40;
    }
}

static uint8_t sample_rate_bits(int sample_rate)
{
    switch (sample_rate) {
    case 50: return 0x00;
    case 100: return 0x04;
    case 200: return 0x08;
    case 400: return 0x0C;
    case 800: return 0x10;
    case 1000: return 0x14;
    case 1600: return 0x18;
    case 3200: return 0x1C;
    default: return 0x14;
    }
}

static uint8_t pulse_width_bits(int pulse_width)
{
    switch (pulse_width) {
    case 69: return 0x00;
    case 118: return 0x01;
    case 215: return 0x02;
    case 411: return 0x03;
    default: return 0x03;
    }
}

static uint8_t adc_range_bits(int adc_range)
{
    switch (adc_range) {
    case 2048: return 0x00;
    case 4096: return 0x20;
    case 8192: return 0x40;
    case 16384: return 0x60;
    default: return 0x60;
    }
}

esp_err_t max30105_init(max30105_t *sensor,
                        i2c_port_t i2c_port,
                        gpio_num_t sda_pin,
                        gpio_num_t scl_pin,
                        uint32_t i2c_speed_hz)
{
    if (sensor == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    memset(sensor, 0, sizeof(*sensor));
    sensor->i2c_port = i2c_port;
    sensor->i2c_addr = MAX30105_ADDRESS;

    i2c_config_t conf = {
        .mode = I2C_MODE_MASTER,
        .sda_io_num = sda_pin,
        .scl_io_num = scl_pin,
        .sda_pullup_en = GPIO_PULLUP_ENABLE,
        .scl_pullup_en = GPIO_PULLUP_ENABLE,
        .master.clk_speed = i2c_speed_hz,
        .clk_flags = 0,
    };

    esp_err_t err = i2c_param_config(i2c_port, &conf);
    if (err != ESP_OK) {
        return err;
    }

    err = i2c_driver_install(i2c_port, conf.mode, 0, 0, 0);
    return (err == ESP_ERR_INVALID_STATE) ? ESP_OK : err;
}

esp_err_t max30105_setup(max30105_t *sensor,
                         uint8_t power_level,
                         uint8_t sample_average,
                         uint8_t led_mode,
                         int sample_rate,
                         int pulse_width,
                         int adc_range)
{
    if (sensor == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    sensor->active_leds = (led_mode == 2) ? 2 : 1;

    ESP_RETURN_ON_ERROR(write_reg(sensor, MAX30105_MODECONFIG, 0x40), "max30105", "reset failed");
    vTaskDelay(pdMS_TO_TICKS(100));
    ESP_RETURN_ON_ERROR(write_reg(sensor, MAX30105_FIFOCONFIG, sample_average_bits(sample_average) | 0x10), "max30105", "fifo config failed");
    ESP_RETURN_ON_ERROR(write_reg(sensor, MAX30105_PARTICLECONFIG, adc_range_bits(adc_range) | sample_rate_bits(sample_rate) | pulse_width_bits(pulse_width)), "max30105", "spo2 config failed");
    ESP_RETURN_ON_ERROR(write_reg(sensor, MAX30105_LED1_PULSEAMP, power_level), "max30105", "red led config failed");
    ESP_RETURN_ON_ERROR(write_reg(sensor, MAX30105_LED2_PULSEAMP, power_level), "max30105", "ir led config failed");
    ESP_RETURN_ON_ERROR(write_reg(sensor, MAX30105_MULTILEDCONFIG1, 0x21), "max30105", "slot config failed");
    ESP_RETURN_ON_ERROR(write_reg(sensor, MAX30105_MODECONFIG, (led_mode == 2) ? 0x03 : 0x02), "max30105", "mode config failed");

    return max30105_clear_fifo(sensor);
}

esp_err_t max30105_read_part_id(max30105_t *sensor, uint8_t *part_id)
{
    if (sensor == NULL || part_id == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    return read_reg(sensor, MAX30105_PARTID, part_id);
}

esp_err_t max30105_clear_fifo(max30105_t *sensor)
{
    if (sensor == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    ESP_RETURN_ON_ERROR(write_reg(sensor, MAX30105_FIFOWRITEPTR, 0), "max30105", "clear write ptr failed");
    ESP_RETURN_ON_ERROR(write_reg(sensor, MAX30105_FIFOOVERFLOW, 0), "max30105", "clear overflow failed");
    ESP_RETURN_ON_ERROR(write_reg(sensor, MAX30105_FIFOREADPTR, 0), "max30105", "clear read ptr failed");
    sensor->head = 0;
    sensor->tail = 0;

    return ESP_OK;
}

esp_err_t max30105_check(max30105_t *sensor, uint16_t *num_samples)
{
    if (sensor == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    uint8_t write_ptr = 0;
    uint8_t read_ptr = 0;
    ESP_RETURN_ON_ERROR(read_reg(sensor, MAX30105_INTSTAT1, &write_ptr), "max30105", "clear irq failed");
    ESP_RETURN_ON_ERROR(read_reg(sensor, MAX30105_FIFOWRITEPTR, &write_ptr), "max30105", "read write ptr failed");
    ESP_RETURN_ON_ERROR(read_reg(sensor, MAX30105_FIFOREADPTR, &read_ptr), "max30105", "read read ptr failed");

    int samples = write_ptr - read_ptr;
    if (samples < 0) {
        samples += 32;
    }
    if (samples > MAX30105_STORAGE_SIZE) {
        samples = MAX30105_STORAGE_SIZE;
    }

    for (int i = 0; i < samples; i++) {
        uint8_t data[6] = {0};
        ESP_RETURN_ON_ERROR(read_regs(sensor, MAX30105_FIFODATA, data, sensor->active_leds * 3), "max30105", "read fifo failed");

        uint8_t next = (sensor->head + 1) % MAX30105_STORAGE_SIZE;
        sensor->red[sensor->head] = (((uint32_t)data[0] << 16) | ((uint32_t)data[1] << 8) | data[2]) & 0x3FFFF;
        if (sensor->active_leds > 1) {
            sensor->ir[sensor->head] = (((uint32_t)data[3] << 16) | ((uint32_t)data[4] << 8) | data[5]) & 0x3FFFF;
        } else {
            sensor->ir[sensor->head] = 0;
        }
        sensor->head = next;
        if (sensor->head == sensor->tail) {
            sensor->tail = (sensor->tail + 1) % MAX30105_STORAGE_SIZE;
        }
    }

    if (num_samples != NULL) {
        *num_samples = (uint16_t)samples;
    }

    return ESP_OK;
}

uint8_t max30105_available(max30105_t *sensor)
{
    if (sensor == NULL) {
        return 0;
    }

    int available = sensor->head - sensor->tail;
    if (available < 0) {
        available += MAX30105_STORAGE_SIZE;
    }

    return (uint8_t)available;
}

uint32_t max30105_get_fifo_red(max30105_t *sensor)
{
    return (sensor == NULL) ? 0 : sensor->red[sensor->tail];
}

uint32_t max30105_get_fifo_ir(max30105_t *sensor)
{
    return (sensor == NULL) ? 0 : sensor->ir[sensor->tail];
}

void max30105_next_sample(max30105_t *sensor)
{
    if (max30105_available(sensor) > 0) {
        sensor->tail = (sensor->tail + 1) % MAX30105_STORAGE_SIZE;
    }
}
