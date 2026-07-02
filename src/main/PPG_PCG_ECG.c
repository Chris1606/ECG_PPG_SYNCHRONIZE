#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <ctype.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include "esp_log.h"
#include "esp_err.h"
#include "sensor_init.h"

/***Task handle global variables */
TaskHandle_t readMAXTask_handle = NULL;
TaskHandle_t readADTask_handle = NULL;
TaskHandle_t printData_handle = NULL;
TaskHandle_t commandTask_handle = NULL;

static void command_task(void *pvParameter){
  char cmd[64] = {0};
  int index = 0;

  sensor_set_mode(SENSOR_MODE_BOTH);
  printf("READY\n");

  while(1){
    int c = getchar();
    if(c == EOF){
      vTaskDelay(pdMS_TO_TICKS(20));
      continue;
    }

    if(c == '\r' || c == '\n'){
      cmd[index] = '\0';
      for(int i = 0; cmd[i] != '\0'; i++){
        cmd[i] = (char)toupper((unsigned char)cmd[i]);
      }

      if(strncmp(cmd, "START", 5) == 0){
        uint32_t duration_s = MEASUREMENT_DEFAULT_SECONDS;
        char *arg = cmd + 5;
        while(*arg == ' ' || *arg == '\t'){
          arg++;
        }
        if(*arg != '\0'){
          duration_s = (uint32_t)strtoul(arg, NULL, 10);
        }
        duration_s = sensor_normalize_duration_s(duration_s);
        esp_err_t err = sensor_start_measurement(sensor_get_selected_mode(), duration_s);
        if(err == ESP_OK){
          printf("ACK,START,%lu\n", (unsigned long)duration_s);
        }
        else{
          printf("ERR,START,%s\n", esp_err_to_name(err));
        }
      }
      else if(strcmp(cmd, "EEG") == 0 || strcmp(cmd, "ECG") == 0){
        sensor_set_mode(SENSOR_MODE_ECG);
        printf("ACK,MODE,ECG\n");
      }
      else if(strcmp(cmd, "PPG") == 0){
        sensor_set_mode(SENSOR_MODE_PPG);
        printf("ACK,MODE,PPG\n");
      }
      else if(strcmp(cmd, "BOTH") == 0 || strcmp(cmd, "ALL") == 0){
        sensor_set_mode(SENSOR_MODE_BOTH);
        printf("ACK,MODE,BOTH\n");
      }
      else if(strcmp(cmd, "IDLE") == 0 || strcmp(cmd, "STOP") == 0){
        sensor_stop_measurement(true);
        printf("ACK,STOP\n");
      }
      else if(strcmp(cmd, "STATUS") == 0){
        printf("STATUS,ECG_READY,%d,PPG_READY,%d,MODE,%d,RUNNING,%d\n",
               sensor_is_ecg_ready() ? 1 : 0,
               sensor_is_ppg_ready() ? 1 : 0,
               (int)sensor_get_selected_mode(),
               sensor_is_measurement_enabled() ? 1 : 0);
      }
      else if(cmd[0] != '\0'){
        printf("ERR,UNKNOWN_CMD\n");
      }

      index = 0;
      cmd[0] = '\0';
      continue;
    }

    if(index < (int)sizeof(cmd) - 1){
      cmd[index++] = (char)c;
    }
  }
}

void app_main(void){
  max30102_configure();
  ad8232_configure();
  // inmp441_configure();
  mutex_init();

  xTaskCreatePinnedToCore(readMAX30102_task, "readmax30102", 1024 * 5,NULL, 5, &readMAXTask_handle, 1);
  xTaskCreatePinnedToCore(readAD8232_task, "readAD8232", 1024 * 4, NULL, 5, &readADTask_handle, 1);
  xTaskCreatePinnedToCore(printData_task, "printData", 2048, NULL, 6, &printData_handle, 1);
  xTaskCreatePinnedToCore(command_task, "command", 2048, NULL, 7, &commandTask_handle, 0);
}
