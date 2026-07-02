#include <stdio.h>
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
  char cmd[16] = {0};
  int index = 0;

  sensor_set_measurement_enabled(false);
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

      if(strcmp(cmd, "EEG") == 0 || strcmp(cmd, "ECG") == 0){
        sensor_set_mode(SENSOR_MODE_EEG);
        printf("ACK,EEG\n");
      }
      else if(strcmp(cmd, "PPG") == 0){
        sensor_set_mode(SENSOR_MODE_PPG);
        printf("ACK,PPG\n");
      }
      else if(strcmp(cmd, "BOTH") == 0 || strcmp(cmd, "ALL") == 0){
        sensor_set_mode(SENSOR_MODE_BOTH);
        printf("ACK,BOTH\n");
      }
      else if(strcmp(cmd, "IDLE") == 0 || strcmp(cmd, "STOP") == 0){
        sensor_set_mode(SENSOR_MODE_IDLE);
        printf("ACK,IDLE\n");
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
