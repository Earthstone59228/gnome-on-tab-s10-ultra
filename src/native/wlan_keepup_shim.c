/* wlan_keepup_shim.c - LD_PRELOAD for NetworkManager + wpa_supplicant ONLY (2026-09-25).
 *
 * Why: on this MediaTek gen4m driver ANY admin-down of wlan0 deinitialises the radio
 * (aisFsmUninit, PCIe link off) and every later "ip link set wlan0 up" fails with ENODEV
 * until Android's Wi-Fi HAL re-arms it (svc wifi enable). NM and wpa_supplicant both set the
 * link down when Wi-Fi is toggled off in GNOME, so the toggle permanently broke Wi-Fi for the
 * rest of the session. This shim turns "wlan0 down" into a no-op in both places they do it:
 *   - rtnetlink RTM_NEWLINK/SETLINK with IFF_UP in ifi_change and cleared in ifi_flags
 *     (NM): IFF_UP is set in ifi_flags, so the kernel ACKs and the link stays up.
 *   - ioctl(SIOCSIFFLAGS) with IFF_UP cleared (wpa_supplicant linux_set_iface_flags):
 *     IFF_UP is forced back on.
 *   - rfkill writes (added later the same day): toggling Wi-Fi off in GNOME makes NM write a
 *     soft-block to rfkill; the kernel then closes wlan0 (same unrecoverable deinit). Any fd
 *     opened on /dev/rfkill or an rfkill "soft" sysfs file has its write()s swallowed
 *     (reported as successful); reads are untouched so wpa_supplicant/NM still see real state.
 * Only the interface named wlan0 is affected. Rollback: drop the LD_PRELOAD. */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <linux/netlink.h>
#include <linux/rtnetlink.h>
#include <net/if.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <fcntl.h>
#include <sys/uio.h>
#include <sys/stat.h>
#include <unistd.h>

#define LOG(...) do { fprintf(stderr, "[wlan-keepup] " __VA_ARGS__); fflush(stderr); } while (0)
/* A mode argument exists only with O_CREAT or the full O_TMPFILE bit pattern. O_TMPFILE
 * includes O_DIRECTORY, so the old `flags & (O_CREAT|O_TMPFILE)` read a nonexistent vararg
 * on every plain O_DIRECTORY open (audit #3; harmless on aarch64, but undefined). */
#define NEEDS_MODE(f) (((f) & O_CREAT) || ((f) & O_TMPFILE) == O_TMPFILE)

static int hits;

/* 2026-09-26 audit #3: resolved once in a constructor (see resolve_all) rather than lazily in
 * whatever process state the first call happens in (e.g. a child NetworkManager forked for a
 * helper). Robustness only — glibc 2.43 resets the loader locks in a fork child. Lazy lookup
 * kept as fallback for calls made before the constructor ran. */
static ssize_t (*real_sendmsg)(int, const struct msghdr *, int);
static ssize_t (*real_sendto)(int, const void *, size_t, int, const struct sockaddr *, socklen_t);
static ssize_t (*real_send)(int, const void *, size_t, int);
static int (*real_ioctl)(int, unsigned long, ...);
static int (*real_open)(const char *, int, ...);
static int (*real_open64)(const char *, int, ...);
static int (*real_openat)(int, const char *, int, ...);
static int (*real_openat64)(int, const char *, int, ...);
static int (*real_dup)(int);
static int (*real_dup2)(int, int);
static int (*real_dup3)(int, int, int);
static int (*real_close)(int);
static ssize_t (*real_write)(int, const void *, size_t);
static int (*real_open_2)(const char *, int);
static int (*real_open64_2)(const char *, int);
static int (*real_openat_2)(int, const char *, int);
static int (*real_openat64_2)(int, const char *, int);
/* ---- END REAL DECLS ---- */

__attribute__((constructor)) static void resolve_all(void) {
	real_sendmsg = dlsym(RTLD_NEXT, "sendmsg");
	real_sendto = dlsym(RTLD_NEXT, "sendto");
	real_send = dlsym(RTLD_NEXT, "send");
	real_ioctl = dlsym(RTLD_NEXT, "ioctl");
	real_open = dlsym(RTLD_NEXT, "open");
	real_open64 = dlsym(RTLD_NEXT, "open64");
	real_openat = dlsym(RTLD_NEXT, "openat");
	real_openat64 = dlsym(RTLD_NEXT, "openat64");
	real_dup = dlsym(RTLD_NEXT, "dup");
	real_dup2 = dlsym(RTLD_NEXT, "dup2");
	real_dup3 = dlsym(RTLD_NEXT, "dup3");
	real_close = dlsym(RTLD_NEXT, "close");
	real_write = dlsym(RTLD_NEXT, "write");
	real_open_2 = dlsym(RTLD_NEXT, "__open_2");
	real_open64_2 = dlsym(RTLD_NEXT, "__open64_2");
	real_openat_2 = dlsym(RTLD_NEXT, "__openat_2");
	real_openat64_2 = dlsym(RTLD_NEXT, "__openat64_2");
}

static int fix_buf(void *buf, size_t len) {
	int changed = 0;
	struct nlmsghdr *nh = buf;
	while (len >= sizeof(*nh) && NLMSG_OK(nh, len)) {
		if ((nh->nlmsg_type == RTM_NEWLINK || nh->nlmsg_type == RTM_SETLINK) &&
		    nh->nlmsg_len >= NLMSG_LENGTH(sizeof(struct ifinfomsg))) {
			struct ifinfomsg *ifi = NLMSG_DATA(nh);
			if ((ifi->ifi_change & IFF_UP) && !(ifi->ifi_flags & IFF_UP) && ifi->ifi_index > 0 &&
			    (int)if_nametoindex("wlan0") == ifi->ifi_index) {
				/* Force IFF_UP on rather than clearing it from ifi_change: the kernel
				 * treats ifi_change == 0 as ~0 (rtnl_dev_combine_flags, v6.1), which
				 * would apply ifi_flags wholesale and take wlan0 down anyway. */
				ifi->ifi_flags |= IFF_UP;
				changed = 1;
				if (hits++ < 20) LOG("blocked netlink link-down of wlan0 (ifindex %d)\n", ifi->ifi_index);
			}
		}
		nh = NLMSG_NEXT(nh, len);
	}
	return changed;
}

/* Cheap pre-filter on the buffer (no syscall) so ordinary traffic — NM's D-Bus
 * sendmsg() calls, wpa's control socket — never pays for the getsockopt below. */
static int maybe_link_msg(const void *buf, size_t len) {
	if (!buf) return 0;
	for (const struct nlmsghdr *nh = buf; len >= sizeof(*nh) && NLMSG_OK(nh, len);
	     nh = NLMSG_NEXT(nh, len)) {
		if ((nh->nlmsg_type == RTM_NEWLINK || nh->nlmsg_type == RTM_SETLINK) &&
		    nh->nlmsg_len >= NLMSG_LENGTH(sizeof(struct ifinfomsg)))
			return 1;
	}
	return 0;
}

static int is_route_nl(int fd) {
	int proto = -1;
	socklen_t l = sizeof(proto);
	return getsockopt(fd, SOL_SOCKET, SO_PROTOCOL, &proto, &l) == 0 && proto == NETLINK_ROUTE;
}

ssize_t sendmsg(int fd, const struct msghdr *msg, int flags) {
	ssize_t (*real)(int, const struct msghdr *, int) = real_sendmsg;
	if (!real) real = real_sendmsg = dlsym(RTLD_NEXT, "sendmsg");
	if (msg && msg->msg_iov && msg->msg_iovlen > 0 &&
	    maybe_link_msg(msg->msg_iov[0].iov_base, msg->msg_iov[0].iov_len) && is_route_nl(fd)) {
		for (size_t i = 0; i < msg->msg_iovlen; i++)
			fix_buf(msg->msg_iov[i].iov_base, msg->msg_iov[i].iov_len);
	}
	return real(fd, msg, flags);
}

ssize_t sendto(int fd, const void *buf, size_t len, int flags, const struct sockaddr *a, socklen_t al) {
	ssize_t (*real)(int, const void *, size_t, int, const struct sockaddr *, socklen_t) = real_sendto;
	if (!real) real = real_sendto = dlsym(RTLD_NEXT, "sendto");
	if (maybe_link_msg(buf, len) && is_route_nl(fd)) fix_buf((void *)buf, len);
	return real(fd, buf, len, flags, a, al);
}

ssize_t send(int fd, const void *buf, size_t len, int flags) {
	ssize_t (*real)(int, const void *, size_t, int) = real_send;
	if (!real) real = real_send = dlsym(RTLD_NEXT, "send");
	if (maybe_link_msg(buf, len) && is_route_nl(fd)) fix_buf((void *)buf, len);
	return real(fd, buf, len, flags);
}

int ioctl(int fd, unsigned long req, ...) {
	int (*real)(int, unsigned long, ...) = real_ioctl;
	if (!real) real = real_ioctl = dlsym(RTLD_NEXT, "ioctl");
	va_list ap;
	va_start(ap, req);
	void *arg = va_arg(ap, void *);
	va_end(ap);
	if (req == SIOCSIFFLAGS && arg) {
		struct ifreq *r = arg;
		if (strncmp(r->ifr_name, "wlan0", IFNAMSIZ) == 0 && !(r->ifr_flags & IFF_UP)) {
			r->ifr_flags |= IFF_UP;
			if (hits++ < 20) LOG("blocked SIOCSIFFLAGS link-down of wlan0\n");
		}
	}
	return real(fd, req, arg);
}

/* ---- rfkill write swallowing ---- */
#define MAXFD 1024
static unsigned char rk_fd[MAXFD];
/* 2026-09-27 audit (pass 1, B3): identity of the rfkill file each marked fd was opened on. write() only
 * swallows when fstat(fd) still matches, so a stale mark (fd closed through a path this shim does not see,
 * e.g. fclose or a glibc-internal close, then reused) can never swallow a real write. */
static dev_t rk_dev[MAXFD];
static ino_t rk_ino[MAXFD];

static int is_rfkill_path(const char *p) {
	if (!p) return 0;
	if (strcmp(p, "/dev/rfkill") == 0) return 1;
	size_t n = strlen(p);
	return strstr(p, "rfkill") != NULL && n > 5 && strcmp(p + n - 5, "/soft") == 0;
}

static int note_open(int fd, const char *path) {
	if (fd >= 0 && fd < MAXFD) {
		struct stat st;
		rk_fd[fd] = is_rfkill_path(path) && fstat(fd, &st) == 0;
		if (rk_fd[fd]) { rk_dev[fd] = st.st_dev; rk_ino[fd] = st.st_ino; }
	}
	return fd;
}
/* Fortified variants (_FORTIFY_SOURCE builds call these; NetworkManager imports __open64_2/__openat64_2). */
int __open_2(const char *path, int flags) {
	if (!real_open_2) real_open_2 = dlsym(RTLD_NEXT, "__open_2");
	return note_open(real_open_2(path, flags), path);
}
int __open64_2(const char *path, int flags) {
	if (!real_open64_2) real_open64_2 = dlsym(RTLD_NEXT, "__open64_2");
	return note_open(real_open64_2(path, flags), path);
}
int __openat_2(int dfd, const char *path, int flags) {
	if (!real_openat_2) real_openat_2 = dlsym(RTLD_NEXT, "__openat_2");
	return note_open(real_openat_2(dfd, path, flags), path);
}
int __openat64_2(int dfd, const char *path, int flags) {
	if (!real_openat64_2) real_openat64_2 = dlsym(RTLD_NEXT, "__openat64_2");
	return note_open(real_openat64_2(dfd, path, flags), path);
}

int open(const char *path, int flags, ...) {
	int (*real)(const char *, int, ...) = real_open;
	if (!real) real = real_open = dlsym(RTLD_NEXT, "open");
	mode_t m = 0;
	if (NEEDS_MODE(flags)) { va_list ap; va_start(ap, flags); m = va_arg(ap, mode_t); va_end(ap); }
	return note_open(real(path, flags, m), path);
}
int open64(const char *path, int flags, ...) {
	int (*real)(const char *, int, ...) = real_open64;
	if (!real) real = real_open64 = dlsym(RTLD_NEXT, "open64");
	mode_t m = 0;
	if (NEEDS_MODE(flags)) { va_list ap; va_start(ap, flags); m = va_arg(ap, mode_t); va_end(ap); }
	return note_open(real(path, flags, m), path);
}
int openat(int dfd, const char *path, int flags, ...) {
	int (*real)(int, const char *, int, ...) = real_openat;
	if (!real) real = real_openat = dlsym(RTLD_NEXT, "openat");
	mode_t m = 0;
	if (NEEDS_MODE(flags)) { va_list ap; va_start(ap, flags); m = va_arg(ap, mode_t); va_end(ap); }
	return note_open(real(dfd, path, flags, m), path);
}
int openat64(int dfd, const char *path, int flags, ...) {
	int (*real)(int, const char *, int, ...) = real_openat64;
	if (!real) real = real_openat64 = dlsym(RTLD_NEXT, "openat64");
	mode_t m = 0;
	if (NEEDS_MODE(flags)) { va_list ap; va_start(ap, flags); m = va_arg(ap, mode_t); va_end(ap); }
	return note_open(real(dfd, path, flags, m), path);
}
static int note_dup(int oldfd, int newfd) {
	if (newfd >= 0 && newfd < MAXFD) {
		int src = oldfd >= 0 && oldfd < MAXFD;
		rk_fd[newfd] = src ? rk_fd[oldfd] : 0;
		if (rk_fd[newfd]) { rk_dev[newfd] = rk_dev[oldfd]; rk_ino[newfd] = rk_ino[oldfd]; }
	}
	return newfd;
}
int dup(int fd) {
	int (*real)(int) = real_dup;
	if (!real) real = real_dup = dlsym(RTLD_NEXT, "dup");
	return note_dup(fd, real(fd));
}
int dup2(int fd, int fd2) {
	int (*real)(int, int) = real_dup2;
	if (!real) real = real_dup2 = dlsym(RTLD_NEXT, "dup2");
	return note_dup(fd, real(fd, fd2));
}
int dup3(int fd, int fd2, int fl) {
	int (*real)(int, int, int) = real_dup3;
	if (!real) real = real_dup3 = dlsym(RTLD_NEXT, "dup3");
	return note_dup(fd, real(fd, fd2, fl));
}
int close(int fd) {
	int (*real)(int) = real_close;
	if (!real) real = real_close = dlsym(RTLD_NEXT, "close");
	if (fd >= 0 && fd < MAXFD) rk_fd[fd] = 0;
	return real(fd);
}
ssize_t write(int fd, const void *buf, size_t n) {
	ssize_t (*real)(int, const void *, size_t) = real_write;
	if (!real) real = real_write = dlsym(RTLD_NEXT, "write");
	struct stat st;
	if (fd >= 0 && fd < MAXFD && rk_fd[fd] &&
	    fstat(fd, &st) == 0 && st.st_dev == rk_dev[fd] && st.st_ino == rk_ino[fd]) {
		if (hits++ < 20) LOG("swallowed %zu-byte write to rfkill fd %d\n", n, fd);
		return (ssize_t)n;
	}
	return real(fd, buf, n);
}
