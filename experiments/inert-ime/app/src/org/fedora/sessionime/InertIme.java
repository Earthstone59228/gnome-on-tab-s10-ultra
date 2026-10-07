package org.fedora.sessionime;

import android.inputmethodservice.AbstractInputMethodService;
import android.os.Bundle;
import android.os.IBinder;
import android.os.ResultReceiver;
import android.view.KeyEvent;
import android.view.inputmethod.*;
import android.graphics.Rect;

/** Session-only IME. Never retains InputConnection, builds a View, or shows a window. */
public final class InertIme extends AbstractInputMethodService {
    @Override protected void dump(java.io.FileDescriptor fd, java.io.PrintWriter out, String[] args) {
        out.println("FedoraSessionInertIme pid=" + android.os.Process.myPid()
                + " uid=" + android.os.Process.myUid());
    }
    @Override public AbstractInputMethodImpl onCreateInputMethodInterface() {
        return new AbstractInputMethodImpl() {
            @Override public void attachToken(IBinder token) { }
            @Override public void bindInput(InputBinding binding) { }
            @Override public void unbindInput() { }
            @Override public void startInput(InputConnection connection, EditorInfo info) { }
            @Override public void restartInput(InputConnection connection, EditorInfo info) { }
            @Override public void changeInputMethodSubtype(InputMethodSubtype subtype) { }
            @Override public void showSoftInput(int flags, ResultReceiver result) {
                if (result != null) result.send(1 /* RESULT_UNCHANGED_HIDDEN */, null);
            }
            @Override public void hideSoftInput(int flags, ResultReceiver result) {
                if (result != null) result.send(1 /* RESULT_UNCHANGED_HIDDEN */, null);
            }
        };
    }
    @Override public AbstractInputMethodSessionImpl onCreateInputMethodSessionInterface() {
        return new AbstractInputMethodSessionImpl() {
            @Override public void finishInput() { }
            @Override public void updateSelection(int a, int b, int c, int d, int e, int f) { }
            @Override public void viewClicked(boolean changed) { }
            @Override public void updateCursor(Rect rect) { }
            @Override public void displayCompletions(CompletionInfo[] completions) { }
            @Override public void updateExtractedText(int token, ExtractedText text) { }
            @Override public void appPrivateCommand(String action, Bundle data) { }
            @Override public void toggleSoftInput(int showFlags, int hideFlags) { }
            @Override public void updateCursorAnchorInfo(CursorAnchorInfo info) { }
        };
    }
    @Override public boolean onKeyDown(int code, KeyEvent event) { return false; }
    @Override public boolean onKeyUp(int code, KeyEvent event) { return false; }
    @Override public boolean onKeyLongPress(int code, KeyEvent event) { return false; }
    @Override public boolean onKeyMultiple(int code, int count, KeyEvent event) { return false; }
}
