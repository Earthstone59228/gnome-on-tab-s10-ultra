#!/system/bin/sh
# Session IME safety gate. No environment-variable path overrides on the tablet.
T=/data/local/tmp
D=$T/.fedora-session-ime
LOCK=$T/.fedora-session-ime-lock
IME=org.fedora.sessionime/.InertIme
KEYS='default_input_method enabled_input_methods selected_input_method_subtype input_methods_subtype_history'
log() { echo "$(date) session-ime: $*" >> "$T/fedora-session-gnome-shell.log"; }
fail() { log "$*"; exit 1; }
s() { timeout -k 2 15 runcon u:r:shell:s0 /system/bin/settings --user 0 "$@" </dev/null; }
c() { timeout -k 2 15 runcon u:r:shell:s0 /system/bin/cmd "$@" </dev/null; }
display_ready() {
    [ "$(getprop init.svc.surfaceflinger)" = running ] &&
    [ "$(getprop init.svc.vendor.hwcomposer-3-2)" = running ] &&
    ! pidof sfsentinel >/dev/null 2>&1 &&
    timeout -k 2 5 runcon u:r:shell:s0 /system/bin/dumpsys SurfaceFlinger --list >/dev/null 2>&1
}
user_zero() { current_user=$(c activity get-current-user) || return 1; [ "$current_user" = 0 ]; }
# Check the selected AND connected user-0 binding, never an installed-method list.
verify_binding() {
    wanted=$1
    dump=$(timeout -k 2 8 runcon u:r:shell:s0 /system/bin/dumpsys input_method 2>/dev/null) || return 1
    bound=$(printf '%s\n' "$dump" | awk '/^  UserId=0$/ {inuser=1; next} inuser && /^    Input Methods:/ {exit} inuser {print}')
    # Literal field comparisons avoid component dots becoming regex wildcards.
    printf '%s\n' "$bound" | awk -v id="$wanted" '
        { sub(/^[ \t]+/, "") }
        $0 == "mSelectedMethodId=" id {selected=1}
        $0 == "mCurId=" id {current=1}
        $0 == "mHasMainConnection=true" {connected=1}
        /^mCurMethod=com.android.server.inputmethod.IInputMethodInvoker@/ {method=1}
        END {exit !(selected && current && connected && method)}' || return 1
}
verify() {
    user_zero || return 1
    selected=$(s get secure default_input_method) || return 1
    [ "$selected" = "$IME" ] || return 1
    verify_binding "$IME" || return 1
    printf '%s\n' "$bound" | grep -q 'mSupportsStylusHw=false$' || return 1
    printf '%s\n' "$bound" | grep -q 'mSupportsConnectionlessStylusHw=false$' || return 1
    printf '%s\n' "$bound" | grep -q 'mImeWindowVis=0$' || return 1
}
umask 077
# Android mksh marks `exec 9>file` close-on-exec, so toybox `flock -n 9` (fd-only) sees EBADF.
# A compound-command redirection keeps fd 9 inheritable; the lock lives until this script exits.
{
flock -n 9 || fail 'IME operation already running (or lock unusable): refusing to race'
case "$1" in
launch-check)
    [ -f "$T/.fedora-ime-runtime-admitted" ] || fail 'Android-only runtime admission missing; launch refused'
    sha256sum -c "$T/.fedora-ime-runtime-admitted" >/dev/null 2>&1 || fail 'runtime admission hashes no longer match; launch refused'
    ;;
prepare)
    display_ready || fail 'prepare requires real SurfaceFlinger/HWC and no placeholder'
    user_zero || fail 'only foreground Android user 0 supported'
    [ ! -e "$D" ] || fail 'old snapshot exists; restore it before starting a new session'
    list=$(s list secure) || fail 'cannot snapshot secure settings'
    printf '%s\n' "$list" | grep -q '^default_input_method=.' || fail 'original IME is unset; refusing unsafe fallback'
    original=$(printf '%s\n' "$list" | sed -n 's/^default_input_method=//p')
    [ "$original" != "$IME" ] || fail 'inert IME already default without a snapshot; recover manually'
    case "$original" in *[!a-zA-Z0-9_./]*|''|*/*/*) fail 'unrecognized original IME component';; esac
    [ ! -e "$D.tmp" ] || fail 'incomplete old snapshot present; recover before retry'
    mkdir "$D.tmp" || fail 'cannot create snapshot'
    for key in $KEYS; do
        if printf '%s\n' "$list" | grep -q "^$key="; then
            printf '%s\n' "$list" | sed -n "s/^$key=//p" > "$D.tmp/$key.value" || fail 'snapshot write failed'
            echo 1 > "$D.tmp/$key.present" || fail 'snapshot write failed'
        else
            : > "$D.tmp/$key.value"
            echo 0 > "$D.tmp/$key.present"
        fi
    done
    echo 1 > "$D.tmp/version" && mv "$D.tmp" "$D" || fail 'cannot finalize snapshot'
    # Snapshot is durable BEFORE either mutation; never disable Samsung Keyboard.
    c input_method ime enable --user 0 "$IME" >/dev/null || fail 'cannot enable inert IME (snapshot retained)'
    c input_method ime set --user 0 "$IME" >/dev/null || fail 'cannot select inert IME (snapshot retained)'
    n=0; good=0
    while [ "$n" -lt 12 ]; do
        sleep 1
        if verify; then good=$((good + 1)); else good=0; fi
        [ "$good" -ge 3 ] && { log 'bound inert IME verified for three samples'; exit 0; }
        n=$((n + 1))
    done
    fail 'bound IME safety verification failed; SF must stay up'
    ;;
verify) verify || fail 'inert IME safety state changed; SF must stay up';;
restored-binding-check)
    user_zero || fail 'original foreground user unavailable'
    selected=$(s get secure default_input_method) || fail 'original default unreadable'
    [ "$selected" != "$IME" ] && [ -n "$selected" ] && [ "$selected" != null ] || fail 'original default not restored'
    verify_binding "$selected" || fail 'original default not connected'
    ;;
service-identity)
    verify || fail 'dummy binding unavailable for process identity proof'
    identity=$(printf '%s\n' "$dump" | sed -n 's/^[ \t]*FedoraSessionInertIme pid=\([0-9][0-9]*\) uid=\([0-9][0-9]*\)$/\1 \2/p')
    [ -n "$identity" ] || fail 'bound dummy service process proof missing'
    printf '%s\n' "$identity"
    ;;
binding-evidence)
    verify || fail 'cannot read connected dummy binding evidence'
    token=$(printf '%s\n' "$bound" | sed -n 's/^[ \t]*mCurToken=\(android.os.Binder@[0-9a-f]*\)$/\1/p')
    display=$(printf '%s\n' "$bound" | sed -n 's/^[ \t]*mCurTokenDisplayId=\([0-9][0-9]*\)$/\1/p')
    [ -n "$token" ] && [ "$display" = 0 ] || fail 'binding token/display evidence invalid'
    printf '%s %s\n' "$token" "$display"
    ;;
restore)
    [ -d "$D" ] || exit 0
    display_ready || fail 'restore deferred until real SF/HWC available; snapshot retained'
    user_zero || fail 'restore deferred until user 0 foreground; snapshot retained'
    [ "$(cat "$D/version" 2>/dev/null)" = 1 ] || fail 'unrecognized snapshot'
    for key in $KEYS; do
        [ -f "$D/$key.value" ] && [ -f "$D/$key.present" ] || fail 'incomplete snapshot'
        case "$(cat "$D/$key.present")" in 0|1) :;; *) fail 'invalid snapshot presence';; esac
    done
    original=$(cat "$D/default_input_method.value")
    case "$original" in *[!a-zA-Z0-9_./]*|''|*/*/*) fail 'invalid original IME component';; esac
    # Temporarily re-enable original if an Android observer rewrote enabled methods.
    c input_method ime enable --user 0 "$original" >/dev/null || fail 'original IME unavailable'
    c input_method ime set --user 0 "$original" >/dev/null || fail 'original IME selection failed'
    # Observer/subtype side effects must settle before restoring exact setting bytes.
    sleep 2
    for key in $KEYS; do
        if [ "$(cat "$D/$key.present")" = 1 ]; then
            s put secure "$key" "$(cat "$D/$key.value")" >/dev/null || fail 'restore write failed'
        else
            s delete secure "$key" >/dev/null || fail 'restore delete failed'
        fi
    done
    sleep 2
    list=$(s list secure) || fail 'restore verification unreadable'
    for key in $KEYS; do
        if [ "$(cat "$D/$key.present")" = 1 ]; then
            printf '%s\n' "$list" | grep -q "^$key=" || fail 'restore lost a present setting'
            now=$(printf '%s\n' "$list" | sed -n "s/^$key=//p")
            [ "$now" = "$(cat "$D/$key.value")" ] || fail 'restore value mismatch; snapshot retained'
        else
            printf '%s\n' "$list" | grep -q "^$key=" && fail 'restore recreated an absent setting'
        fi
    done
    # Settings bytes alone cannot prove the original IME successfully rebound.
    n=0; good=0
    while [ "$n" -lt 8 ]; do
        if verify_binding "$original"; then good=$((good + 1)); else good=0; fi
        [ "$good" -ge 2 ] && break
        sleep 1; n=$((n + 1))
    done
    [ "$good" -ge 2 ] || fail 'original IME has not rebound; snapshot retained for recovery'
    # Re-read after binding settles: subtype observers may have changed values meanwhile.
    list=$(s list secure) || fail 'restore verification unreadable'
    for key in $KEYS; do
        if [ "$(cat "$D/$key.present")" = 1 ]; then
            printf '%s\n' "$list" | grep -q "^$key=" || fail 'restore lost a present setting'
            now=$(printf '%s\n' "$list" | sed -n "s/^$key=//p")
            [ "$now" = "$(cat "$D/$key.value")" ] || fail 'restore value mismatch; snapshot retained'
        else
            printf '%s\n' "$list" | grep -q "^$key=" && fail 'restore recreated an absent setting'
        fi
    done
    if [ "$2" = final ]; then rm -rf "$D"; log 'IME restored and verified; snapshot removed';
    else log 'IME restored and verified; snapshot retained across zygote'; fi
    ;;
*) fail 'usage: fedora-session-ime.sh prepare|verify|restore [final]';;
esac
} 9>"$LOCK" || fail 'cannot open IME operation lock'
