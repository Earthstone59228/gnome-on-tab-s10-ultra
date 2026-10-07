#!/usr/bin/python3
import time
import gi
gi.require_version('Gtk','4.0')
from gi.repository import Gtk, Gio, GLib
started=time.monotonic()
app=Gtk.Application(application_id='org.fedora.RecordingMotionTest',flags=Gio.ApplicationFlags.NON_UNIQUE)
def activate(app):
 window=Gtk.ApplicationWindow(application=app,title='Temporary recording test')
 window.set_default_size(600,360)
 area=Gtk.DrawingArea()
 def draw(area,cr,w,h):
  cr.set_source_rgb(0.08,0.12,0.2); cr.paint()
  x=(time.monotonic()-started)*160%max(1,w-100)
  cr.set_source_rgb(0.2,0.75,0.9); cr.rectangle(x,60,100,160); cr.fill()
 area.set_draw_func(draw)
 window.set_child(area); window.present()
 def tick():
  if time.monotonic()-started>70:
   app.quit(); return GLib.SOURCE_REMOVE
  area.queue_draw(); return GLib.SOURCE_CONTINUE
 GLib.timeout_add(33,tick)
app.connect('activate',activate)
app.run([])
