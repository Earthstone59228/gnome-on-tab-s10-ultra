/* mastercheck — F11 master-free helper (02-display-session-stack.md).
 * Briefly acquires then immediately releases DRM master on card0 to verify
 * nobody else (Android's hwcomposer HAL) currently holds it.
 * Exit 0 = master was free (acquired + released again here).
 * Exit 1 = master held by someone else (SET_MASTER failed, e.g. EBUSY).
 * Exit 2 = other error (open/DROP_MASTER failed).
 */
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/ioctl.h>
#include <unistd.h>

#define DRM_IOCTL_BASE 'd'
#define DRM_IO(nr) _IO(DRM_IOCTL_BASE, nr)
#define DRM_IOCTL_SET_MASTER DRM_IO(0x1e)
#define DRM_IOCTL_DROP_MASTER DRM_IO(0x1f)

int main(void) {
	int fd = open("/dev/dri/card0", O_RDWR | O_CLOEXEC);
	if (fd < 0) {
		fprintf(stderr, "mastercheck: open card0: %s\n", strerror(errno));
		return 2;
	}
	if (ioctl(fd, DRM_IOCTL_SET_MASTER, 0) == -1) {
		fprintf(stderr, "mastercheck: SET_MASTER: %s\n", strerror(errno));
		close(fd);
		return 1;
	}
	if (ioctl(fd, DRM_IOCTL_DROP_MASTER, 0) == -1) {
		fprintf(stderr, "mastercheck: DROP_MASTER: %s\n", strerror(errno));
		close(fd);
		return 2;
	}
	close(fd);
	return 0;
}
