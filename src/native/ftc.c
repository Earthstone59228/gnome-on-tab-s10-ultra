/* ftc.c — frame-timing client in C (2026-09-27). Same measurement as frametime.py (intervals between the
 * GTK4 frame clock's frames = what the compositor actually delivered), but in C: the Python client
 * segfaults on the libhybris GL path (_PyThreadState_Attach: non-NULL old thread state), C GTK apps do not.
 * GTK4 is loaded with dlopen — the chroot has the libraries but no development headers.
 * Usage: ftc SECONDS LABEL [WIDTH HEIGHT]   (run inside a live GNOME session)
 * Output: one line: label frames fps mean median p95 p99 max late(>1.5x median). Read-only w.r.t. the system. */
#include <dlfcn.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

typedef void *P;
typedef int gboolean;
typedef void (*draw_fn)(P area, P cr, int w, int h, P data);
typedef gboolean (*tick_fn)(P widget, P clock, P data);

static void (*p_gtk_init)(void);
static P (*p_gtk_window_new)(void);
static void (*p_gtk_window_set_default_size)(P, int, int);
static void (*p_gtk_window_set_title)(P, const char *);
static void (*p_gtk_window_set_child)(P, P);
static void (*p_gtk_window_present)(P);
static P (*p_gtk_drawing_area_new)(void);
static void (*p_gtk_drawing_area_set_draw_func)(P, draw_fn, P, P);
static unsigned (*p_gtk_widget_add_tick_callback)(P, tick_fn, P, P);
static void (*p_gtk_widget_queue_draw)(P);
static int64_t (*p_gdk_frame_clock_get_frame_time)(P);
static gboolean (*p_g_main_context_iteration)(P, gboolean);
static int64_t (*p_g_get_monotonic_time)(void);
static void (*p_cairo_set_source_rgb)(P, double, double, double);
static void (*p_cairo_paint)(P);
static void (*p_cairo_rectangle)(P, double, double, double, double);
static void (*p_cairo_fill)(P);

#define MAXF 20000
static int64_t stamps[MAXF];
static int nst;

static void draw(P area, P cr, int w, int h, P data) {
	(void)area; (void)data;
	double t = (double)p_g_get_monotonic_time() / 1e6;
	p_cairo_set_source_rgb(cr, 0.1, 0.1, 0.1);
	p_cairo_paint(cr);
	p_cairo_set_source_rgb(cr, 0.9, 0.6, 0.1);
	double span = w > 40 ? w - 40 : 1;
	double x = t * 150.0;
	x -= span * (double)(long)(x / span);
	p_cairo_rectangle(cr, x, h / 3.0, 40, h / 3.0);
	p_cairo_fill(cr);
}

static gboolean tick(P widget, P clock, P data) {
	(void)data;
	if (nst < MAXF) {
		stamps[nst++] = p_gdk_frame_clock_get_frame_time(clock);
	}
	p_gtk_widget_queue_draw(widget);
	return 1; /* G_SOURCE_CONTINUE */
}

static int cmp(const void *a, const void *b) {
	double x = *(const double *)a, y = *(const double *)b;
	return (x > y) - (x < y);
}

#define SYM(h, name) do { *(void **)&p_##name = dlsym(h, #name); \
	if (!p_##name) { fprintf(stderr, "missing symbol %s\n", #name); return 2; } } while (0)

int main(int argc, char **argv) {
	double secs = argc > 1 ? atof(argv[1]) : 10.0;
	const char *label = argc > 2 ? argv[2] : "";
	int w = argc > 3 ? atoi(argv[3]) : 1400, h = argc > 4 ? atoi(argv[4]) : 800;
	void *gtk = dlopen("libgtk-4.so.1", RTLD_NOW | RTLD_GLOBAL);
	void *cairo = dlopen("libcairo.so.2", RTLD_NOW | RTLD_GLOBAL);
	if (!gtk || !cairo) {
		fprintf(stderr, "dlopen failed: %s\n", dlerror());
		return 2;
	}
	SYM(gtk, gtk_init); SYM(gtk, gtk_window_new); SYM(gtk, gtk_window_set_default_size);
	SYM(gtk, gtk_window_set_title); SYM(gtk, gtk_window_set_child); SYM(gtk, gtk_window_present);
	SYM(gtk, gtk_drawing_area_new); SYM(gtk, gtk_drawing_area_set_draw_func);
	SYM(gtk, gtk_widget_add_tick_callback); SYM(gtk, gtk_widget_queue_draw);
	SYM(gtk, gdk_frame_clock_get_frame_time);
	SYM(RTLD_DEFAULT, g_main_context_iteration); SYM(RTLD_DEFAULT, g_get_monotonic_time);
	SYM(cairo, cairo_set_source_rgb); SYM(cairo, cairo_paint); SYM(cairo, cairo_rectangle); SYM(cairo, cairo_fill);

	p_gtk_init();
	P win = p_gtk_window_new();
	p_gtk_window_set_title(win, "ftc");
	p_gtk_window_set_default_size(win, w, h);
	P area = p_gtk_drawing_area_new();
	p_gtk_drawing_area_set_draw_func(area, draw, NULL, NULL);
	p_gtk_window_set_child(win, area);
	p_gtk_widget_add_tick_callback(area, tick, NULL, NULL);
	p_gtk_window_present(win);

	int64_t end = p_g_get_monotonic_time() + (int64_t)(secs * 1e6);
	while (p_g_get_monotonic_time() < end) {
		p_g_main_context_iteration(NULL, 1);
	}
	if (nst < 6) {
		printf("%s too few frames (%d)\n", label, nst);
		return 1;
	}
	int n = nst - 1;
	double *iv = malloc(sizeof(double) * (size_t)n), sum = 0;
	for (int i = 0; i < n; i++) {
		iv[i] = (double)(stamps[i + 1] - stamps[i]) / 1000.0;
		sum += iv[i];
	}
	qsort(iv, (size_t)n, sizeof(double), cmp);
	double med = iv[n / 2];
	int late = 0;
	for (int i = 0; i < n; i++) {
		if (iv[i] > 1.5 * med) late++;
	}
	double spanS = (double)(stamps[nst - 1] - stamps[0]) / 1e6;
	printf("%s frames=%d fps=%.1f mean=%.2fms median=%.2fms p95=%.2fms p99=%.2fms max=%.2fms late(>1.5x median)=%d\n",
		label, nst, n / spanS, sum / n, med, iv[(int)(0.95 * n)], iv[(int)(0.99 * n)], iv[n - 1], late);
	/* 2026-09-27: every run had one ~470 ms interval. Say WHERE hitches (> 50 ms) happen and give steady-state
	 * numbers without the first 0.5 s (window map / first commit). */
	int nh = 0;
	printf("%s hitches>50ms:", label);
	for (int i = 0; i + 1 < nst; i++) {
		double d = (double)(stamps[i + 1] - stamps[i]) / 1000.0;
		if (d > 50.0) {
			if (nh < 8) printf(" %.0fms@%.2fs(frame %d)", d, (double)(stamps[i] - stamps[0]) / 1e6, i);
			nh++;
		}
	}
	printf(" total=%d\n", nh);
	int s0 = 0;
	while (s0 < nst && stamps[s0] - stamps[0] < 500000) s0++;
	int m = nst - s0 - 1;
	if (m >= 5) {
		double *jv = malloc(sizeof(double) * (size_t)m);
		for (int i = 0; i < m; i++) jv[i] = (double)(stamps[s0 + i + 1] - stamps[s0 + i]) / 1000.0;
		qsort(jv, (size_t)m, sizeof(double), cmp);
		double med2 = jv[m / 2];
		int late2 = 0;
		for (int i = 0; i < m; i++) if (jv[i] > 1.5 * med2) late2++;
		double span2 = (double)(stamps[nst - 1] - stamps[s0]) / 1e6;
		printf("%s steady(after 0.5s) frames=%d fps=%.1f median=%.2fms p95=%.2fms p99=%.2fms max=%.2fms late=%d\n",
			label, m + 1, m / span2, med2, jv[(int)(0.95 * m)], jv[(int)(0.99 * m)], jv[m - 1], late2);
	}
	return 0;
}
