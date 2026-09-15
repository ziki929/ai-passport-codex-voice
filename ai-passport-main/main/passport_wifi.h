#pragma once
#include <stdbool.h>
#include <stddef.h>
void passport_wifi_init(void);
bool passport_wifi_connect(void);
int passport_wifi_read(void *data, size_t size);
bool passport_wifi_write(const void *data, size_t size);
void passport_wifi_close(void);
