// sfsentinel.c — placeholder "SurfaceFlinger"/"SurfaceFlingerAIDL" binder service
// for the gts10u Fedora-native project.
//
// WHY THIS EXISTS (root-loss fix, 2026-09-20):
// Every session script stops surfaceflinger so mutter can take DRM master.
// With the real SF process gone, its servicemanager names disappear, and
// system_server's PowerManagerService.userActivity -> nativeSetPowerBoost ->
// SurfaceComposerClient::notifyPowerBoost -> ComposerServiceAIDL::
// getComposerService() blocks FOREVER in libbinder's waitForService() while
// holding the PowerManager NamedLock. Every other system_server service thread
// piles up behind that lock, the system_server Watchdog fires at 60s and kills
// system_server (framework restart = root lapse), and repeated restarts feed
// Android RescueParty escalation -> full kernel reboot (root access lost)
// and eventually factory-reset level. Both 2026-09-20 root-loss
// incidents went through exactly this chain (watchdog dumps in dropbox prove
// it). Registering harmless placeholder binders under the same names makes
// every such call FAIL FAST instead of hanging — no wedge, no watchdog, no
// framework restart, no root loss.
//
// The stub answers PING and INTERFACE (descriptor) transactions and returns an
// error for everything else, so clients unblock immediately with a clean
// "transaction failed" instead of real data. It MUST be killed before the real
// surfaceflinger init service is started (fedora-restore.sh does this — the
// real SF's addService would otherwise fail with ALREADY_EXISTS and Android's
// display stack would stay broken until reboot).
//
// Usage: sfsentinel [testname]  — no args registers BOTH real names (the
// session-script mode); one arg registers only that name (non-destructive
// mechanism test on a throwaway name, safe to run while real SF is alive).

#include <android/binder_ibinder.h> // NDK r29: also declares AServiceManager_* APIs
#include <sys/types.h>
#include <sys/socket.h>
#include <android/log.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define LOG_TAG "SfSentinel"
#define LOGI(...) __android_log_print(ANDROID_LOG_INFO, LOG_TAG, __VA_ARGS__)
#define LOGE(...) __android_log_print(ANDROID_LOG_ERROR, LOG_TAG, __VA_ARGS__)

// Android 16 platform ABI divergence (found by tombstone + disassembly,
// 2026-09-20): the device's libbinder_ndk.so AServiceManager_addService takes
// (AIBinder*, const char*) — the REVERSE of the NDK header's documented
// (name, binder) order. NDK r29's stub lib has no such symbol, so we link
// against the device's own libbinder_ndk.so and must match ITS order.
binder_status_t AServiceManager_addService(AIBinder *binder, const char *name);

// API 31, undeclared by the r29 sysroot headers. NDK AIBinders default to
// VINTF stability, which makes servicemanager silently require the name to be
// declared in the device VINTF manifest (PERMISSION_DENIED, no avc log —
// root-caused via the device servicemanager's own requiresVintfDeclaration
// linkage). Downgrading to SYSTEM stability removes that requirement, same as
// real platform services.
void AIBinder_forceDowngradeToSystemStability(AIBinder *binder);

// Thread-pool controls (r29 headers predate <android/binder_process.h>, but
// the device's libbinder_ndk.so exports both symbols — nm-verified 2026-09-21;
// the 19:06 watchdog that day was exactly their absence: registered names
// with no thread to answer transactions = synchronous callers wedge anyway).
binder_status_t ABinderProcess_setThreadPoolMaxThreadCount(size_t max_threads);
void ABinderProcess_startThreadPool(void);

// --- v2 (2026-09-24): ANSWER the verified crash-class calls instead of failing ---
// Private-ABI helpers, both verified on THIS device's libs by disassembly:
//  * AParcel (libbinder_ndk) keeps its android::Parcel* at offset 8 (AParcel_
//    getDataSize does `ldr x0,[x0,#8]; bl Parcel::dataSize`).
//  * android::Parcel::writeDupFileDescriptor(int) is exported by libbinder.so.
// NDK has no raw-FD-object writer (AParcel_writeParcelFileDescriptor wraps the
// fd in extra int32s), and BitTube's wire format needs bare FD objects.
extern int _ZN7android6Parcel22writeDupFileDescriptorEi(void *parcel, int fd);
static int aparcel_write_dup_fd(AParcel *p, int fd) {
	void *inner = *(void **)((char *)p + 8);
	return _ZN7android6Parcel22writeDupFileDescriptorEi(inner, fd);
}

// Device (Samsung) libgui transaction codes — NOT the upstream AOSP AIDL order
// (diverges from code 30 up). Read from BpSurfaceComposer::* /
// BpDisplayEventConnection::* disassembly 2026-09-24.
#define SC_CREATE_DISPLAY_EVENT_CONNECTION 2u
#define SC_GET_OVERLAY_SUPPORT             73u
#define DEC_STEAL_RECEIVE_CHANNEL          1u
#define DEC_SET_VSYNC_RATE                 2u
#define DEC_REQUEST_NEXT_VSYNC             3u // oneway

// Standard binder control transaction codes (not exported by NDK headers).
#define SENTINEL_INTERFACE_TRANSACTION 1598968902u // '_ADR'/getInterfaceDescriptor
#define SENTINEL_PING_TRANSACTION      1598311764u // 'PING'
#define SENTINEL_DUMP_TRANSACTION      1598830983u // 'DUMP'

typedef struct {
	const char *iface_descriptor;
} SentinelClass;

static binder_status_t reply_display_event_connection(AParcel *out);
static binder_status_t reply_overlay_props(AParcel *out);

// 2026-09-24 (v1.1, logging only): record which SF transactions real framework
// callers make while the stub is standing in, so sfsentinel v2's graceful
// replies can be built against MEASURED traffic (codes are Samsung-libgui
// specific — they diverge from AOSP from code 30 up — see 02 notes). Behaviour
// is unchanged: still fail-fast. Logs the first 3 hits per (iface,code), then
// every 50th, with calling pid/uid and request parcel size.
#define SEEN_MAX 512
static struct { const char *iface; uint32_t code; unsigned n; } seen[SEEN_MAX];
static int seen_cnt;
static pthread_mutex_t seen_mu = PTHREAD_MUTEX_INITIALIZER;
static void note_call(const char *iface, uint32_t code, const AParcel *in) {
	unsigned n = 0;
	pthread_mutex_lock(&seen_mu);
	int i;
	for (i = 0; i < seen_cnt; i++)
		if (seen[i].code == code && seen[i].iface == iface) break;
	if (i == seen_cnt && seen_cnt < SEEN_MAX) {
		seen[i].iface = iface;
		seen[i].code = code;
		seen[i].n = 0;
		seen_cnt++;
	}
	if (i < SEEN_MAX) n = ++seen[i].n;
	pthread_mutex_unlock(&seen_mu);
	if (n <= 3 || n % 50 == 0)
		LOGI("CALL iface=%s code=%u n=%u pid=%d uid=%d insize=%d", iface, code, n,
			 (int)AIBinder_getCallingPid(), (int)AIBinder_getCallingUid(),
			 (int)AParcel_getDataSize(in));
}

static void *on_create(void *args) { return args; }
static void on_destroy(void *data) { (void)data; }

static binder_status_t on_transact(AIBinder *binder, uint32_t code,
								   const AParcel *in, AParcel *out) {
	SentinelClass *cls = (SentinelClass *)AIBinder_getUserData(binder);
	switch (code) {
	case SENTINEL_INTERFACE_TRANSACTION:
		if (AParcel_writeString(out, cls->iface_descriptor,
								strlen(cls->iface_descriptor)) != STATUS_OK)
			return STATUS_UNKNOWN_ERROR;
		return STATUS_OK;
	case SENTINEL_PING_TRANSACTION:
	case SENTINEL_DUMP_TRANSACTION:
		return STATUS_OK;
	case SC_CREATE_DISPLAY_EVENT_CONNECTION:
		if (strcmp(cls->iface_descriptor, "android.gui.ISurfaceComposer") == 0) {
			binder_status_t r = reply_display_event_connection(out);
			LOGI("SF: createDisplayEventConnection answered rc=%d pid=%d", r,
				 (int)AIBinder_getCallingPid());
			return r;
		}
		note_call(cls->iface_descriptor, code, in);
		return STATUS_UNKNOWN_ERROR;
	case SC_GET_OVERLAY_SUPPORT:
		if (strcmp(cls->iface_descriptor, "android.gui.ISurfaceComposer") == 0)
			return reply_overlay_props(out);
		note_call(cls->iface_descriptor, code, in);
		return STATUS_UNKNOWN_ERROR;
	default:
		note_call(cls->iface_descriptor, code, in);
		// The whole point: fail FAST so callers never wedge holding locks.
		return STATUS_UNKNOWN_ERROR;
	}
}

static AIBinder_Class *g_conn_class;

// IDisplayEventConnection stub: hands the client a working BitTube channel (a
// SOCK_SEQPACKET socketpair — same shape as the real one) that never carries a
// vsync. Choreographer/DisplayEventReceiver init then SUCCEEDS instead of
// throwing "Failed to initialize display event receiver" (the verified
// system_server android.display crash-loop trigger), and simply never gets
// frames while SF is down. Both socket ends go to the client as dups, so we
// close ours — the client's pair can never see EOF.
static binder_status_t conn_transact(AIBinder *binder, uint32_t code,
									 const AParcel *in, AParcel *out) {
	(void)binder;
	switch (code) {
	case SENTINEL_INTERFACE_TRANSACTION:
		return AParcel_writeString(out, "android.gui.IDisplayEventConnection",
								   strlen("android.gui.IDisplayEventConnection"));
	case SENTINEL_PING_TRANSACTION:
	case SENTINEL_DUMP_TRANSACTION:
		return STATUS_OK;
	case DEC_STEAL_RECEIVE_CHANNEL: {
		int sv[2];
		if (socketpair(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC, 0, sv) != 0)
			return STATUS_UNKNOWN_ERROR;
		int rc = AParcel_writeInt32(out, 0);        // Status: OK
		if (rc == STATUS_OK) rc = AParcel_writeInt32(out, 1); // non-null parcelable
		if (rc == STATUS_OK) rc = aparcel_write_dup_fd(out, sv[0]); // receive fd
		if (rc == STATUS_OK) rc = aparcel_write_dup_fd(out, sv[1]); // send fd
		close(sv[0]);
		close(sv[1]);
		LOGI("conn: stealReceiveChannel answered rc=%d pid=%d", rc,
			 (int)AIBinder_getCallingPid());
		return rc == STATUS_OK ? STATUS_OK : STATUS_UNKNOWN_ERROR;
	}
	case DEC_SET_VSYNC_RATE:
		return AParcel_writeInt32(out, 0); // Status: OK, void
	case DEC_REQUEST_NEXT_VSYNC:
		return STATUS_OK; // oneway
	default:
		note_call("android.gui.IDisplayEventConnection", code, in);
		return STATUS_UNKNOWN_ERROR;
	}
}

static AIBinder *new_conn_binder(void) {
	if (!g_conn_class)
		g_conn_class = AIBinder_Class_define("android.gui.IDisplayEventConnection",
											 on_create, on_destroy, conn_transact);
	if (!g_conn_class) return NULL;
	AIBinder *b = AIBinder_new(g_conn_class, NULL);
	if (b) AIBinder_forceDowngradeToSystemStability(b);
	return b;
}

// Empty-but-valid OverlayProperties reply (getOverlaySupport): AIDL structured
// parcelable = [int32 size][vector<combinations>: int32 count=0][bool
// supportMixedColorSpaces][optional array: int32 -1 = null]; wrapped by the
// non-null flag readParcelable expects. Layout read off
// OverlayProperties::writeToParcel disassembly (libgui @0xed8d0). Java gets a
// non-null object whose isCombinationSupported() is simply false instead of
// null (the SystemUI/Settings HardwareRenderer NPE crash-loop).
static binder_status_t reply_overlay_props(AParcel *out) {
	binder_status_t rc = AParcel_writeInt32(out, 0);         // Status: OK
	if (rc == STATUS_OK) rc = AParcel_writeInt32(out, 1);    // non-null
	if (rc == STATUS_OK) rc = AParcel_writeInt32(out, 16);   // parcelable size
	if (rc == STATUS_OK) rc = AParcel_writeInt32(out, 0);    // 0 combinations
	if (rc == STATUS_OK) rc = AParcel_writeInt32(out, 0);    // mixed color spaces=false
	if (rc == STATUS_OK) rc = AParcel_writeInt32(out, -1);   // null optional array
	return rc;
}

static binder_status_t reply_display_event_connection(AParcel *out) {
	AIBinder *conn = new_conn_binder();
	if (!conn) return STATUS_UNKNOWN_ERROR;
	binder_status_t rc = AParcel_writeInt32(out, 0);         // Status: OK
	if (rc == STATUS_OK) rc = AParcel_writeStrongBinder(out, conn);
	AIBinder_decStrong(conn); // the reply parcel now holds the reference
	return rc;
}

static int register_name(const char *name, const char *descriptor) {
	SentinelClass *cls = malloc(sizeof(SentinelClass));
	if (!cls) return -1;
	cls->iface_descriptor = descriptor;

	// 2026-09-24 FIX: the NDK class descriptor MUST equal the interface token
	// clients write (android.gui.ISurfaceComposer / android.ui.ISurfaceComposer),
	// NOT the servicemanager name. With the service name here, libbinder's
	// enforceInterface() rejected EVERY real transaction before on_transact ran
	// ("expected 'SurfaceFlingerAIDL' but read 'android.gui.ISurfaceComposer'"),
	// so the stub never logged/answered anything and system_server's Choreographer
	// init failed with status=-2147483647 on every framework start.
	AIBinder_Class *bclass = AIBinder_Class_define(descriptor, on_create, on_destroy,
												   on_transact);
	if (!bclass) {
		LOGE("AIBinder_Class_define(%s) failed", name);
		return -1;
	}
	AIBinder *binder = AIBinder_new(bclass, cls);
	if (!binder) {
		LOGE("AIBinder_new(%s) failed", name);
		return -1;
	}
	AIBinder_forceDowngradeToSystemStability(binder);
	binder_status_t rc = AServiceManager_addService(binder, name);
	if (rc != STATUS_OK) {
		LOGE("addService(%s) failed rc=%d (SELinux/ALREADY_EXISTS?)", name, rc);
		return -1;
	}
	LOGI("registered placeholder service '%s' (iface %s)", name, descriptor);
	return 0;
}

int main(int argc, char **argv) {
	LOGI("sfsentinel starting (pid %d, argv0=%s)", getpid(), argv[0]);

	if (argc == 2) {
		// Mechanism-test mode: register a single throwaway name. Safe to run
		// while the REAL surfaceflinger is alive; touches nothing else.
		if (register_name(argv[1], "rmg.sentinel.test.Interface") != 0)
			return 1;
	} else {
		// Session mode: hold both real SF names (old BBinder + AIDL).
		if (register_name("SurfaceFlingerAIDL",
						  "android.gui.ISurfaceComposer") != 0)
			return 1;
		if (register_name("SurfaceFlinger",
						  "android.ui.ISurfaceComposer") != 0)
			return 1;
	}

	// Serve transactions (2026-09-21 watchdog lesson: registration alone is
	// not enough — without a thread pool every synchronous call into the
	// stub blocks in ioctl forever, the exact wedge this stub exists to
	// prevent). One pool thread suffices for PING/INTERFACE/error replies.
	ABinderProcess_setThreadPoolMaxThreadCount(4);
	ABinderProcess_startThreadPool();

	LOGI("sfsentinel live — SF-name binder calls will fail fast, not hang");
	while (1) pause(); // servicemanager holds our refs; just stay alive
	return 0;
}
