/* SPDX-License-Identifier: MIT
 * First-party notification mode pinned to one accepted Rofi binary/Mode ABI 7.
 * Delegate presentation and action policy to the fixed packaged script helper.
 * Feed metadata wakes callback 28 only; it chooses no command or session action.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <rofi/mode-private.h>
#include <signal.h>
#include <stdint.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#if ABI_VERSION != 7
#error "This integration requires the accepted Rofi Mode ABI 7 layout."
#endif

typedef struct {
    Mode *script;
    guint timer;
    int root;
    uint64_t sequence, expired, last_callback;
    gboolean observed;
} NotifyState;

typedef struct { uint64_t sequence, received, expiry, pid, ready; } Record;

static Mode *(*script_setup)(const char *);
static void *(*active_view)(void);
static Mode *(*view_mode)(void *);
static int (*completed_view)(void *);
static void (*update_view)(void *);
static void (*trigger_action)(void *, int, unsigned int);
static unsigned int (*action_from_name)(const char *);

static uint64_t now_ms(void) {
    struct timespec value;
    if (clock_gettime(CLOCK_BOOTTIME, &value) != 0) { return 0; }
    return (uint64_t)value.tv_sec * 1000 + (uint64_t)value.tv_nsec / 1000000;
}

static gboolean number(const char **cursor, uint64_t *result, uint64_t maximum, char delimiter) {
    const char *p = *cursor;
    uint64_t value = 0;
    unsigned int count = 0;
    unsigned int width = maximum == 1 ? 1 : maximum == INT32_MAX ? 10 : 19;
    if (!g_ascii_isdigit(*p)) { return FALSE; }
    while (g_ascii_isdigit(*p)) {
        unsigned int digit = (unsigned int)(*p - '0');
        if (++count > width || value > maximum / 10 ||
            (value == maximum / 10 && digit > maximum % 10)) { return FALSE; }
        value = value * 10 + digit;
        p++;
    }
    if (*p != delimiter) { return FALSE; }
    *cursor = p + 1;
    *result = value;
    return TRUE;
}

static gboolean read_record(NotifyState *data, Record *record) {
    int fd = openat(data->root, "notify", O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC);
    if (fd < 0) { return FALSE; }
    struct stat info;
    char raw[194];
    ssize_t size = -1;
    if (fstat(fd, &info) == 0 && S_ISREG(info.st_mode) && info.st_uid == getuid() &&
        (info.st_mode & 0777) == 0600 && info.st_size <= 192) {
        size = read(fd, raw, sizeof(raw) - 1);
    }
    close(fd);
    if (size < 5 || size > 192 || memchr(raw, '\0', (size_t)size) || memcmp(raw, "TP1 ", 4)) {
        return FALSE;
    }
    raw[size] = '\0';
    const char *p = raw + 4;
    return number(&p, &record->sequence, INT64_MAX, ' ') &&
        number(&p, &record->received, INT64_MAX, ' ') &&
        number(&p, &record->expiry, INT64_MAX, ' ') &&
        number(&p, &record->pid, INT32_MAX, ' ') &&
        number(&p, &record->ready, 1, '\n') && *p == '\0' &&
        (!record->ready || record->pid > 0);
}

static gboolean notify_tick(gpointer context) {
    Mode *sw = context;
    void *view = active_view();
    g_debug("Tmux Plus notify: active=%d selected-mode=%d", view != NULL, view && view_mode(view) == sw);
    if (!view || view_mode(view) != sw) { return G_SOURCE_CONTINUE; }
    if (completed_view(view)) { update_view(view); }
    /* A synchronous delegate update can destroy/switch the mode. Reacquire all
     * borrowed state and check the active pointer before dereferencing sw. */
    view = active_view();
    if (!view || view_mode(view) != sw) { return G_SOURCE_CONTINUE; }
    NotifyState *data = mode_get_private_data(sw);
    if (!data) { return G_SOURCE_REMOVE; }
    Record record;
    uint64_t now = now_ms();
    gboolean valid = read_record(data, &record);
    g_debug("Tmux Plus notify: record=%d observed=%d", valid, data->observed);
    gboolean unavailable = !valid || !record.ready || record.received > now ||
        now - record.received >= 10000 || kill((pid_t)record.pid, 0) != 0;
    gboolean due = unavailable;
    if (valid) {
        due = due || !data->observed || record.sequence != data->sequence ||
            (record.expiry > 0 && now >= record.expiry && data->expired != record.expiry);
        if (data->observed && record.sequence < data->sequence) { due = TRUE; }
    }
    /* Coalesce bursts; persistent failure has a bounded visible retry cadence. */
    uint64_t interval = unavailable ? 1000 : 100;
    if (due && now >= data->last_callback + interval) {
        data->last_callback = now;
        if (valid) {
            data->sequence = record.sequence;
            data->observed = TRUE;
            if (record.expiry > 0 && now >= record.expiry) { data->expired = record.expiry; }
        }
        trigger_action(view, 0, action_from_name("kb-custom-19"));
        update_view(view);
        /* Do not access data/view after this call: delegate lifetime may end. */
    }
    return G_SOURCE_CONTINUE;
}

static int initialize(Mode *sw) {
    const char *root = g_getenv("ROFI_TMUX_PLUS_RUNTIME");
    if (!root || strlen(root) > 4096) { return FALSE; }
    int fd = open(root, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    struct stat info;
    if (fd < 0) { return FALSE; }
    if (fstat(fd, &info) || info.st_uid != getuid() || (info.st_mode & 0777) != 0700) {
        close(fd); return FALSE;
    }
    script_setup = dlsym(RTLD_DEFAULT, "script_mode_parse_setup");
    active_view = dlsym(RTLD_DEFAULT, "rofi_view_get_active");
    view_mode = dlsym(RTLD_DEFAULT, "rofi_view_get_mode");
    completed_view = dlsym(RTLD_DEFAULT, "rofi_view_get_completed");
    update_view = dlsym(RTLD_DEFAULT, "rofi_view_maybe_update");
    trigger_action = dlsym(RTLD_DEFAULT, "rofi_view_trigger_action");
    action_from_name = dlsym(RTLD_DEFAULT, "key_binding_get_action_from_name");
    if (!script_setup || !active_view || !view_mode || !completed_view ||
        !update_view || !trigger_action || !action_from_name) {
        g_warning("Tmux Plus: accepted private Rofi symbols are unavailable");
        close(fd); return FALSE;
    }
    Dl_info location;
    if (!dladdr((void *)&initialize, &location) || !location.dli_fname) { close(fd); return FALSE; }
    char *directory = g_path_get_dirname(location.dli_fname);
    char *helper = g_build_filename(directory, "rofi-tmux-plus-mode", NULL);
    char *quoted = g_shell_quote(helper);
    char *specification = g_strconcat("tmux-plus-renderer:", quoted, NULL);
    g_free(directory); g_free(helper); g_free(quoted);
    NotifyState *data = g_new0(NotifyState, 1);
    data->root = fd;
    data->script = script_setup(specification);
    g_free(specification);
    if (!data->script || !mode_init(data->script)) {
        if (data->script) { mode_destroy(data->script); mode_free(&data->script); }
        close(fd); g_free(data); return FALSE;
    }
    mode_set_private_data(sw, data);
    sw->display_name = data->script->display_name;
    data->timer = g_timeout_add(50, notify_tick, sw);
    return TRUE;
}

static void destroy(Mode *sw) {
    NotifyState *data = mode_get_private_data(sw);
    if (data) {
        g_source_remove(data->timer);
        sw->display_name = NULL;
        mode_set_private_data(sw, NULL);
        mode_destroy(data->script);
        mode_free(&data->script);
        close(data->root);
        g_free(data);
    }
}

static unsigned int count(const Mode *sw) {
    NotifyState *data = mode_get_private_data(sw);
    return mode_get_num_entries(data->script);
}
static char *display(const Mode *sw, unsigned int line, int *state, GList **attributes, int get_entry) {
    NotifyState *data = mode_get_private_data(sw);
    return mode_get_display_value(data->script, line, state, attributes, get_entry);
}
static int match(const Mode *sw, rofi_int_matcher **tokens, unsigned int line) {
    NotifyState *data = mode_get_private_data(sw);
    return mode_token_match(data->script, tokens, line);
}
static char *message(const Mode *sw) {
    NotifyState *data = mode_get_private_data(sw);
    return mode_get_message(data->script);
}
static cairo_surface_t *icon(const Mode *sw, unsigned int line, unsigned int height) {
    NotifyState *data = mode_get_private_data(sw);
    return mode_get_icon(data->script, line, height);
}
static ModeMode result(Mode *sw, int action, char **input, unsigned int selected) {
    NotifyState *data = mode_get_private_data(sw);
    g_debug("Tmux Plus result: action=%d selected=%u", action, selected);
    ModeMode next = mode_result(data->script, action, input, selected);
    sw->display_name = data->script->display_name;
    return next;
}

G_MODULE_EXPORT Mode mode = {
    .abi_version = ABI_VERSION, .name = "tmux-plus",
    ._init = initialize, ._destroy = destroy, ._get_num_entries = count,
    ._get_display_value = display, ._token_match = match, ._get_message = message,
    ._get_icon = icon, ._result = result, .type = MODE_TYPE_SWITCHER,
};
