idf_component_register(SRCS "main/PPG_PCG_ECG.c" "Sensor_init/sensor_init.c"
                       INCLUDE_DIRS "main" "Sensor_init"
                       REQUIRES driver esp_timer max30105)
