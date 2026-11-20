# Notes for coding agents

See CONTRIBUTING.md for the build, test and release procedures.

## Running the GUI headless (Xvfb)

On a Wayland desktop with an NVIDIA driver, Xvfb segfaults (or the app
reports "Unable to access the X Display") because glvnd loads NVIDIA's EGL
library into it. Force Mesa's EGL, keep the server alive between wx.App
instances, and keep GTK off Wayland:

```sh
__EGL_VENDOR_LIBRARY_FILENAMES=/usr/share/glvnd/egl_vendor.d/50_mesa.json \
xvfb-run -a -s "-screen 0 1280x1024x24 -noreset" \
    env -u WAYLAND_DISPLAY GDK_BACKEND=x11 python ...
```

`make test` already does this (the `XVFB` variable in the Makefile); use the
same line for anything else that opens a window, such as
`docs/quickstart/make_screenshots.py`. Never run GUI tests on the user's
screen.

For screenshots, grab the X server's pixels with `PIL.ImageGrab.grab(xdisplay=None)`
and crop to the frame. A `wx.ScreenDC` blit can come back faded or miss a tab
change, and `wx.SafeYield()` just before a grab disables every window, so the
shot shows greyed-out controls.
