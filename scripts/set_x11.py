import os
import sys
import glob
import subprocess
import tempfile

if sys.platform.startswith('linux'):
    os.environ['GDK_BACKEND'] = 'x11'

    if getattr(sys, 'frozen', False):
        meipass = sys._MEIPASS

        # The binary is built on Ubuntu 20.04, so it bundles that era's GLib. On
        # newer systems, libraries pulled in by GTK (e.g. libsecret) need GLib
        # symbols (g_task_set_static_name, GLib >= 2.76) the bundled old GLib
        # lacks, so `import wx` dies with
        # "ImportError: ... undefined symbol: g_task_set_static_name".
        # Remove the bundled GLib family so the loader falls back to the TARGET
        # system's GLib -- the 20.04-built wx/GTK libs are backward-compatible
        # with newer GLib, and the system libsecret then finds the symbol. This
        # runs in the runtime hook, before `import wx` loads any of these.
        # Also drop GLib's low-level dependency closure (util-linux libmount /
        # libblkid, libselinux, pcre2): the system GLib needs their NEWER symbol
        # versions (e.g. libmount MOUNT_2_40 on Ubuntu 26) which the bundled
        # 20.04 copies lack. Their sonames are stable across releases, so the
        # system copies satisfy every reference. libffi is deliberately NOT
        # dropped -- its soname bumped (.so.7 -> .so.8), so the system copy
        # would not satisfy a bundled reference to the old soname.
        for _pat in ('libglib-2.0.so*', 'libgobject-2.0.so*', 'libgio-2.0.so*',
                     'libgmodule-2.0.so*', 'libgthread-2.0.so*',
                     'libmount.so*', 'libblkid.so*', 'libselinux.so*',
                     'libpcre2-8.so*'):
            for _so in glob.glob(os.path.join(meipass, _pat)):
                try:
                    os.remove(_so)
                except OSError:
                    pass

        # Strategy 1: use bundled loaders from the build system. The cache
        # uses MEIPASS_PLACEHOLDER so it is valid regardless of extraction path.
        loaders_dir = os.path.join(meipass, 'gdk-pixbuf-loaders')
        cache_template = os.path.join(meipass, 'pixbuf-loaders.cache')
        if os.path.isdir(loaders_dir) and os.path.exists(cache_template):
            try:
                content = open(cache_template).read().replace(
                    'MEIPASS_PLACEHOLDER', meipass
                )
                cache_file = os.path.join(
                    tempfile.gettempdir(), f'puck_pixbuf_{os.getpid()}.cache'
                )
                with open(cache_file, 'w') as f:
                    f.write(content)
                os.environ['GDK_PIXBUF_MODULE_FILE'] = cache_file
                os.environ['GDK_PIXBUF_MODULEDIR'] = loaders_dir
            except Exception:
                pass

        # Strategy 2: generate a fresh cache from the target system's own
        # loaders. Always ABI-compatible regardless of Ubuntu version.
        if 'GDK_PIXBUF_MODULE_FILE' not in os.environ:
            try:
                result = subprocess.run(
                    ['gdk-pixbuf-query-loaders'],
                    capture_output=True, text=True, timeout=5
                )
                if result.returncode == 0 and result.stdout.strip():
                    cache_file = os.path.join(
                        tempfile.gettempdir(), f'puck_pixbuf_{os.getpid()}.cache'
                    )
                    with open(cache_file, 'w') as f:
                        f.write(result.stdout)
                    os.environ['GDK_PIXBUF_MODULE_FILE'] = cache_file
            except Exception:
                pass

        # Strategy 3: fall back to the system's pre-generated cache file.
        if 'GDK_PIXBUF_MODULE_FILE' not in os.environ:
            for path in [
                '/usr/lib/x86_64-linux-gnu/gdk-pixbuf-2.0/2.10.0/loaders.cache',
                '/usr/lib/aarch64-linux-gnu/gdk-pixbuf-2.0/2.10.0/loaders.cache',
            ]:
                if os.path.exists(path):
                    os.environ['GDK_PIXBUF_MODULE_FILE'] = path
                    break
