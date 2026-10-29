FROM ubuntu:20.04

ENV DEBIAN_FRONTEND=noninteractive
# Land the uv-managed interpreter in a world-readable global path: the build is
# run as the non-root host user (see build-linux-installer.sh `docker run
# --user`), which must be able to read+exec the venv's interpreter at runtime.
ENV UV_PYTHON_INSTALL_DIR=/opt/uv-python
ENV PATH=/usr/local/bin:$PATH

# The Ubuntu 20.04 base is kept on purpose: it provides the oldest glibc/GTK, so
# the resulting binary runs on Ubuntu >= 20.04. The Python interpreter is NOT
# the distro's — uv pulls a standalone CPython 3.12 below, so the build is on
# 3.12 regardless of what apt ships. wxPython is installed from the matching
# prebuilt ubuntu-20.04 cp312 wheel, so NOTHING is compiled here.

# Layer 1: system packages — rebuilds only when this RUN command changes.
# --no-install-recommends is essential: without it libgtk-3-0's recommends chain
# drags in a desktop (~1000 packages, emacs, evolution-data-server...).
# Build tools: curl/ca-certificates for the uv installer; binutils for the
# objdump/strip PyInstaller runs; zip for the release archive.
# Runtime libs: exactly the DT_NEEDED set of the wx wheel's core/base/adv/aui/
# html/stc/xml/net libraries (readelf on wxPython-4.2.2 ubuntu-20.04 cp312).
# PyInstaller copies these into the bundle, so they must exist here. Not
# installed on purpose: webkit2gtk (wx.html2), gstreamer (wx.media) and GL
# (wx.glcanvas) -- the apps import none of those modules.
# librsvg2-common: the SVG pixbuf loader, bundled with the others so GTK's
# symbolic icons render (see the loader step in build-linux-installer.sh).
RUN apt-get update -qq && \
    apt-get install -y -qq --no-install-recommends \
        curl ca-certificates binutils zip \
        libgtk-3-0 libgdk-pixbuf2.0-bin librsvg2-common \
        libsecret-1-0 libnotify4 libsdl2-2.0-0 \
        libxxf86vm1 libxtst6 libsm6 libxkbcommon0 \
        libjpeg8 libtiff5 libpng16-16 libpcre2-32-0 libexpat1 libcurl4 \
        libusb-1.0-0 && \
    rm -rf /var/lib/apt/lists/* && \
    GDK_QUERY=$(find /usr/lib -name 'gdk-pixbuf-query-loaders' 2>/dev/null | head -1) && \
    if [ -n "$GDK_QUERY" ] && [ ! -f /usr/bin/gdk-pixbuf-query-loaders ]; then \
        ln -s "$GDK_QUERY" /usr/bin/gdk-pixbuf-query-loaders; \
    fi

# Layer 2: uv + standalone CPython 3.12 + base venv — rebuilds only when layer 1
# changes. No dependency on the system Python.
RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh && \
    uv python install 3.12 && \
    uv venv --python 3.12 /opt/puckbuild

# Layer 3: the shared pinned requirements (identical in p4gui, puckutility and
# pucktuner), wxPython from its prebuilt ubuntu-20.04 cp312 wheel. Rebuild the
# image after a requirements.txt change; the build also re-syncs it at run time.
COPY requirements.txt /tmp/requirements.txt
RUN uv pip install --python /opt/puckbuild \
        --find-links https://extras.wxpython.org/wxPython4/extras/linux/gtk3/ubuntu-20.04/ \
        -r /tmp/requirements.txt && \
    chmod -R a+rX /opt/uv-python && chmod -R a+rwX /opt/puckbuild

ENV UV_CACHE_DIR=/tmp/uv-cache
ENV VENV_ROOT=/opt/puckbuild
