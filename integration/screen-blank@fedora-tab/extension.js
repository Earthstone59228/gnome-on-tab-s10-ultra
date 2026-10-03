// screen-blank@fedora-tab (2026-09-27) — "screen off" for the Tab S10 Ultra session without touching panel power.
//
// WHY NOT bl_power / KMS DPMS: powering the panel down makes mcd_panel post LCD_OFF on sec_input_notifier; the
// pogo driver (stm32_pogo_v3, registered on the same notifier) then detaches the Book Cover Keyboard. Android's
// InputManager sees the keyboard vanish -> CONFIG_KEYBOARD/KEYBOARD_HIDDEN config change -> WindowManager
// transition -> BLASTSync timeout against the stopped SurfaceFlinger -> DEAD_OBJECT -> system_server crash loop
// (logcat 2026-09-27 19:04:57 "handleNotifyPogoKeyboardStatus status=false", crash 19:04:59). Panel power-up also
// produced a spurious extra power-key Shutdown, so the old toggle desynced.
//
// WHAT: a pure-black fullscreen actor on top of the whole shell UI. On this OLED panel black pixels are off, so it
// looks like a real screen-off, and the panel/touch/keyboard never change power state. While blanked, pointer/touch
// and key focus are captured (modal grab) and the grab runs in ActionMode.LOGIN_SCREEN: shell keybindings (Super,
// Alt+Tab, ...) are filtered, but LOGIN_SCREEN is in gsd 50.1 POWER_KEYS_MODE and not in its no-dialog set, so the
// power key still reaches SessionManager.Shutdown -> un-blank (audit 2026-09-27). Fallback to NORMAL mode:
// /usr/local/etc/screen-blank-normal-mode. Other ways back: Escape on the black screen, and any modal dialog opening
// (it would sit invisible underneath and block the power key) un-blanks automatically.
//
// D-Bus (session bus, on gnome-shell's own connection, dest org.gnome.Shell):
//   /org/fedoratab/ScreenBlank  org.fedoratab.ScreenBlank.{Blank,Unblank,Toggle -> b}, property Blanked
// Callers: fake_sessionmanager.py (power button), fake_logind.py (book cover).

import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Shell from 'gi://Shell';
import St from 'gi://St';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const OBJECT_PATH = '/org/fedoratab/ScreenBlank';
const IFACE_XML = `
<node>
  <interface name="org.fedoratab.ScreenBlank">
    <method name="Blank"/>
    <method name="Unblank"/>
    <method name="Toggle">
      <arg type="b" name="blanked" direction="out"/>
    </method>
    <property name="Blanked" type="b" access="read"/>
  </interface>
</node>`;

class ScreenBlanker {
    constructor() {
        this._actor = null;
        this._grab = null;
        this._cursorInhibited = false;
        this._dbus = Gio.DBusExportedObject.wrapJSObject(IFACE_XML, this);
        this._dbus.export(Gio.DBus.session, OBJECT_PATH);
        // On the lock screen gsd ignores the power key (INTERACTIVE + in_lock_screen = no-op, gsd 50.1
        // do_config_power_action), and mutter 50.4 passes a shell-filtered keybinding on to Clutter
        // (keybindings.c process_event -> not_found) -> catch it here so power still blanks while locked.
        global.stage.connectObject('captured-event', (_stage, event) => {
            if (event.type() !== Clutter.EventType.KEY_PRESS ||
                event.get_key_symbol() !== Clutter.KEY_PowerOff ||
                this._actor || !Main.sessionMode.isLocked)
                return Clutter.EVENT_PROPAGATE;
            this.Blank();
            return Clutter.EVENT_STOP;
        }, this);
    }

    // 2026-09-27 (user request): blank = lock first (only when the runner armed a real lock: fake_gdm up + root
    // password set -> /run/fedora-lock-ready), so un-blanking (power again / cover open) shows the lock screen.
    _lockIfArmed() {
        if (Main.sessionMode.isLocked || !Main.screenShield ||
            !GLib.file_test('/run/fedora-lock-ready', GLib.FileTest.EXISTS))
            return;
        try {
            Main.screenShield.lock(false);   // synchronous pushModal(LOCK_SCREEN); our grab goes on top of it
        } catch (e) {
            console.warn(`screen-blank: lock: ${e.message}`);
        }
    }

    get Blanked() {
        return this._actor !== null;
    }

    Blank() {
        if (this._actor)
            return;

        this._lockIfArmed();

        this._actor = new St.Widget({
            name: 'fedoraTabScreenBlank',
            style: 'background-color: black;',
            reactive: true,
            can_focus: true,
        });
        this._actor.add_constraint(new Clutter.BindConstraint({
            source: global.stage,
            coordinate: Clutter.BindCoordinate.ALL,
        }));
        const uiGroup = Main.layoutManager.uiGroup;
        uiGroup.add_child(this._actor);
        uiGroup.set_child_above_sibling(this._actor, null);
        // anything the shell adds later (OSD, dialogs) must stay underneath
        const raise = () => {
            if (this._actor)
                uiGroup.set_child_above_sibling(this._actor, null);
        };
        uiGroup.connectObject('child-added', raise, this);
        // lock -> session mode 'unlock-dialog' restacks the shell UI; stay on top of the lock screen too
        Main.sessionMode.connectObject('updated', raise, this);

        // a dialog (Wi-Fi secret, polkit, ...) would be invisible under us and takes a SYSTEM_MODAL grab in which
        // the power key is filtered -> never leave the user stuck: un-blank when one opens
        Main.layoutManager.modalDialogGroup.connectObject('child-added', () => this.Unblank(), this);
        this._actor.connect('key-press-event', (_actor, event) => {
            if (event.get_key_symbol() === Clutter.KEY_Escape)
                this.Unblank();
            return Clutter.EVENT_STOP;
        });

        const actionMode = GLib.file_test('/usr/local/etc/screen-blank-normal-mode', GLib.FileTest.EXISTS)
            ? Shell.ActionMode.NORMAL : Shell.ActionMode.LOGIN_SCREEN;
        this._grab = Main.pushModal(this._actor, {actionMode});

        const tracker = global.backend.get_cursor_tracker();
        tracker.inhibit_cursor_visibility();
        this._cursorInhibited = true;

        this._notify();
        console.log('screen-blank: blanked');
    }

    Unblank() {
        if (!this._actor)
            return;

        Main.layoutManager.uiGroup.disconnectObject(this);
        Main.layoutManager.modalDialogGroup.disconnectObject(this);
        Main.sessionMode.disconnectObject(this);
        if (this._cursorInhibited) {
            global.backend.get_cursor_tracker().uninhibit_cursor_visibility();
            this._cursorInhibited = false;
        }
        const grab = this._grab;
        this._grab = null;
        try {
            if (grab)
                Main.popModal(grab);
        } catch (e) {
            console.warn(`screen-blank: popModal: ${e.message}`);
        }
        this._actor.destroy();
        this._actor = null;

        this._notify();
        console.log('screen-blank: unblanked');
    }

    Toggle() {
        if (this._actor)
            this.Unblank();
        else
            this.Blank();
        return this.Blanked;
    }

    _notify() {
        this._dbus.emit_property_changed('Blanked', GLib.Variant.new_boolean(this.Blanked));
    }

    destroy() {
        global.stage.disconnectObject(this);
        this.Unblank();
        this._dbus.unexport();
    }
}

export default class ScreenBlankExtension extends Extension {
    enable() {
        this._blanker = new ScreenBlanker();
    }

    disable() {
        this._blanker?.destroy();
        this._blanker = null;
    }
}
