#include "passport_wifi.h"
#include "passport_wifi_config.h"
#include "demo_radio.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "lwip/sockets.h"
#include "mbedtls/md.h"
#include <errno.h>
#include <string.h>
#include <stdio.h>

static volatile bool has_ip;
static int connection = -1;
static int64_t retry_at;

static void wifi_event(void *arg, esp_event_base_t base, int32_t id, void *data) {
    (void)arg; (void)data;
    if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        has_ip = true;
        ip_event_got_ip_t *event = data;
        printf("WiFi address: " IPSTR "\n", IP2STR(&event->ip_info.ip));
    }
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        has_ip = false;
        printf("WiFi disconnected, reason %u\n", ((wifi_event_sta_disconnected_t *)data)->reason);
        esp_wifi_connect();
    }
}

void passport_wifi_init(void) {
    ESP_ERROR_CHECK(demo_radio_nvs_prepare());
    ESP_ERROR_CHECK(demo_radio_network_prepare());
    esp_netif_create_default_wifi_sta();
    wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, wifi_event, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, wifi_event, NULL));
    wifi_config_t config = {0};
    memcpy(config.sta.ssid, PASSPORT_SSID, sizeof(PASSPORT_SSID) - 1);
    memcpy(config.sta.password, PASSPORT_PASSWORD, sizeof(PASSPORT_PASSWORD) - 1);
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &config));
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
    ESP_ERROR_CHECK(esp_wifi_connect());
}

void passport_wifi_close(void) {
    if (connection >= 0) { shutdown(connection, SHUT_RDWR); close(connection); }
    connection = -1;
    retry_at = esp_timer_get_time() + 2000000;
}

bool passport_wifi_write(const void *data, size_t size) {
    const char *p = data;
    while (size && connection >= 0) {
        int n = send(connection, p, size, 0);
        if (n <= 0) { passport_wifi_close(); return false; }
        p += n; size -= n;
    }
    return size == 0;
}

static bool read_exact(void *data, size_t size) {
    char *p = data;
    while (size) {
        int n = recv(connection, p, size, 0);
        if (n <= 0) return false;
        p += n; size -= n;
    }
    return true;
}

bool passport_wifi_connect(void) {
    if (!has_ip) { if (connection >= 0) passport_wifi_close(); return false; }
    if (connection >= 0) return true;
    if (esp_timer_get_time() < retry_at) return false;
    connection = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (connection < 0) { passport_wifi_close(); return false; }
    struct timeval timeout = {.tv_sec = 1};
    setsockopt(connection, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
    setsockopt(connection, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout));
    struct sockaddr_in host = {.sin_family = AF_INET, .sin_port = htons(8765)};
    inet_pton(AF_INET, PASSPORT_HOST, &host.sin_addr);
    if (connect(connection, (struct sockaddr *)&host, sizeof(host)) != 0) goto fail;
    // Fresh challenge authenticates both peers without sending the pairing key.
    unsigned char challenge[33], response[32], expected[32];
    const mbedtls_md_info_t *md = mbedtls_md_info_from_type(MBEDTLS_MD_SHA256);
    if (!read_exact(challenge, 32)) goto fail;
    challenge[32] = 'D';
    if (mbedtls_md_hmac(md, (const unsigned char *)PASSPORT_KEY, strlen(PASSPORT_KEY), challenge, 33, response)) goto fail;
    if (!passport_wifi_write(response, 32)) goto fail;
    challenge[32] = 'P';
    if (mbedtls_md_hmac(md, (const unsigned char *)PASSPORT_KEY, strlen(PASSPORT_KEY), challenge, 33, expected)) goto fail;
    if (!read_exact(response, 32)) goto fail;
    unsigned diff = 0;
    for (int i = 0; i < 32; i++) diff |= response[i] ^ expected[i];
    if (diff) goto fail;
    return true;
fail:
    passport_wifi_close();
    return false;
}

int passport_wifi_read(void *data, size_t size) {
    if (connection < 0) return 0;
    int n = recv(connection, data, size, MSG_DONTWAIT);
    if (n == 0 || (n < 0 && errno != EAGAIN && errno != EWOULDBLOCK)) passport_wifi_close();
    return n > 0 ? n : 0;
}
