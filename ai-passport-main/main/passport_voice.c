#include <stdint.h>
#include <string.h>
#include <stdio.h>
#include "bsp_display.h"
#include "bsp_button.h"
#include "bsp_audio.h"
#include "bsp_battery.h"
#include "ui_pixel.h"
#include "passport_wifi.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"

// One task owns USB writes, audio and UI updates. Button callbacks only enqueue.
static QueueHandle_t keys;
static uint32_t sequence;
static lv_obj_t *status_label, *level_label, *connection_label;
static void text_at(lv_obj_t *obj, int x, int y, int width) {
    lv_obj_set_pos(obj, x, y);
    lv_obj_set_width(obj, width);
    lv_obj_set_style_text_align(obj, LV_TEXT_ALIGN_CENTER, 0);
}
static void line_at(lv_obj_t *parent, int x, int y, int w, int h, uint32_t color) {
    lv_obj_t *obj = lv_obj_create(parent);
    lv_obj_remove_style_all(obj);
    lv_obj_set_pos(obj, x, y); lv_obj_set_size(obj, w, h);
    lv_obj_set_style_bg_color(obj, lv_color_hex(color), 0);
    lv_obj_set_style_bg_opa(obj, LV_OPA_COVER, 0);
}

static void packet(uint8_t type, const void *payload, uint16_t size) {
    uint8_t frame[656] = {'A','P','V','1',type,0,size & 255,size >> 8};
    uint32_t seq = sequence++, sum = type;
    memcpy(frame + 8, &seq, 4);
    for (int i = 0; i < size; ++i) sum += ((const uint8_t *)payload)[i];
    memcpy(frame + 12, &sum, 4);
    memcpy(frame + 16, payload, size);
    // A disconnected host cannot block microphone/UI indefinitely.
    passport_wifi_write(frame, size + 16);
}

static void key_event(bsp_btn_t key, bsp_btn_ev_t event, void *user) {
    (void)user;
    if (event == BSP_BTN_PRESS || event == BSP_BTN_RELEASE) {
        uint8_t k = (uint8_t)key | (event == BSP_BTN_RELEASE ? 0x80 : 0);
        xQueueSend(keys, &k, 0);
    }
}

void app_main(void) {
    ESP_ERROR_CHECK(bsp_display_init());
    if (!bsp_lvgl_init()) return;
    bsp_display_backlight(70);
    keys = xQueueCreate(8, sizeof(uint8_t));
    if (!keys) return;
    ESP_ERROR_CHECK(bsp_button_init(key_event, NULL));
    ESP_ERROR_CHECK(bsp_audio_init());
    ESP_ERROR_CHECK(bsp_audio_set_format(16000, 16, 1));
    bsp_audio_set_volume(0);
    bool battery_ok = bsp_battery_init() == ESP_OK;
    if (bsp_lvgl_lock(1000)) {
        lv_obj_t *screen = lv_obj_create(NULL);
        lv_obj_remove_flag(screen, LV_OBJ_FLAG_SCROLLABLE);
        lv_obj_set_style_bg_color(screen, lv_color_hex(0xFAFAFA), 0);
        lv_obj_set_style_pad_all(screen, 0, 0);
        lv_obj_t *title = ui_pixel_label(screen, "Codex Voice", &lv_font_montserrat_14, 0x202020);
        lv_obj_set_pos(title, 17, 18);
        connection_label = ui_pixel_label(screen, "AI PASSPORT / CONNECTING", &lv_font_montserrat_10, 0x777777);
        lv_obj_set_pos(connection_label, 17, 42);
        for (int i=0; i<3; ++i) line_at(screen, 210+i*5, 30-i*4, 3, 4+i*4, 0x303030);
        // Draw the terminal cursor as geometry, independent of font glyphs.
        static lv_point_precise_t chevron[] = {{0,0},{17,17},{0,34}};
        lv_obj_t *arrow = lv_line_create(screen);
        lv_line_set_points(arrow, chevron, 3);
        lv_obj_set_pos(arrow, 91, 92);
        lv_obj_set_style_line_width(arrow, 3, 0);
        lv_obj_set_style_line_color(arrow, lv_color_hex(0x303030), 0);
        line_at(screen, 113, 130, 29, 3, 0x303030);
        status_label = ui_pixel_label(screen, "Connecting...", &lv_font_montserrat_24, 0x202020);
        text_at(status_label, 10, 176, 220);
        level_label = ui_pixel_label(screen, "Waiting for your computer.", &lv_font_montserrat_10, 0x777777);
        text_at(level_label, 12, 216, 216);
        line_at(screen, 14, 267, 212, 1, 0xDEDEDE);
        const char *hints[] = {"Tap", "Double tap", "Hold", "Send", "Interrupt", "Speak"};
        for (int i=0; i<6; ++i) {
            lv_obj_t *label = ui_pixel_label(screen, hints[i], &lv_font_montserrat_10, 0x777777);
            text_at(label, 14+(i%3)*71, i<3 ? 280 : 297, 70);
        }
        lv_screen_load(screen);
        bsp_lvgl_unlock();
    }
    // No text logs after binary transport starts. Boot logs remain available.
    esp_log_level_set("*", ESP_LOG_NONE);
    passport_wifi_init();
    bool active = false;
    bool processing = false;
    int64_t heartbeat = 0, last_ui = 0, notice_until = 0;
    bool interrupted = false;
    int16_t pcm[320];
    for (;;) {
        uint8_t commands[32], key;
        bool linked = passport_wifi_connect();
        int n = passport_wifi_read(commands, sizeof(commands));
        int64_t now = esp_timer_get_time();
        for (int i = 0; i < n; ++i) {
            if (commands[i] == 'H' || commands[i] == 'R' || commands[i] == 'S') heartbeat = now;
            if (commands[i] == 'R') { active = true; processing = false; interrupted = false; }
            if (commands[i] == 'S') {
                if (active) packet(4, "", 0); // ordered end-of-recording barrier
                active = false;
            }
            if (commands[i] == 'B') { processing = true; interrupted = false; notice_until = now + 3000000; }
            if (commands[i] == 'X') { interrupted = true; processing = false; notice_until = now + 1500000; }
            if (commands[i] == 'I') processing = false;
        }
        if (now >= notice_until) { processing = false; interrupted = false; }
        bool connected = linked && heartbeat && now - heartbeat < 3000000;
        if (!connected) { active = false; processing = false; }
        while (xQueueReceive(keys, &key, 0) == pdTRUE) {
            if (connected) packet(2, &key, 1);
        }
        // Drain the ADC even while muted so starting never transmits stale audio.
        esp_err_t result = bsp_audio_read(pcm, sizeof(pcm));
        unsigned peak = 0;
        if (result == ESP_OK && active) {
            for (unsigned i = 0; i < 320; ++i) {
                unsigned v = pcm[i] < 0 ? -(int)pcm[i] : pcm[i];
                if (v > peak) peak = v;
            }
            packet(1, pcm, sizeof(pcm));
        } else if (result != ESP_OK) {
            active = false;
            vTaskDelay(pdMS_TO_TICKS(20));
        }
        if (now - last_ui > 500000) {
            last_ui = now;
            int soc = battery_ok ? bsp_battery_soc() : -1;
            char state[100];
            snprintf(state, sizeof(state), "{\"mic\":%d,\"peak\":%u,\"battery\":%d,\"audio_ok\":%d}", active, peak, soc, result == ESP_OK);
            if (connected) packet(3, state, strlen(state));
            if (bsp_lvgl_lock(50)) {
                lv_label_set_text(connection_label, connected ? "AI PASSPORT / CONNECTED" : "AI PASSPORT / CONNECTING");
                lv_label_set_text(status_label, !connected ? "Connecting..." : active ? "Listening..." : interrupted ? "Paused." : processing ? "One moment." : "Let's talk.");
                lv_label_set_text(level_label, !connected ? "Waiting for your computer." : active ? "Release OK when you're done." : interrupted ? "Interrupt requested." : processing ? "Audio sent to Codex." : "Hold OK to speak.");
                bsp_lvgl_unlock();
            }
        }
    }
}
