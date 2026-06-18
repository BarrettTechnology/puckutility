FROM ubuntu:20.04

ENV DEBIAN_FRONTEND=noninteractive
# Land the uv-managed interpreter in a world-readable global path: the build is
# run as the non-root host user (see build-linux-installer.sh `docker run
# --user`), which must be able to read+exec the venv's interpreter at runtime.
ENV UV_PYTHON_INSTALL_DIR=/opt/uv-python
ENV PATH=/usr/local/bin:$PATH

# The Ubuntu 20.04 base is kept on purpose: it provides the oldest glibc/GTK, so
# the resulting binary runs on Ubuntu >= 20.04. The Python interpreter is NOT
# the distro's — uv pulls a standalone CPython 3.13 below, so the build is on
# 3.13 regardless of what apt ships. wxPython is installed from the matching
# prebuilt ubuntu-20.04 cp313 wheel, so NOTHING is compiled here.

# Layer 1: system packages — rebuilds only when this RUN command changes.
# curl/ca-certificates are for the uv installer; binutils provides `objdump`,
# which PyInstaller needs to scan the binary's shared-lib dependencies (no
# compiler is needed since everything installs from wheels); libgtk-3-0 + libsdl2
# are the runtime libs the prebuilt wxPython wheel links against.
RUN apt-get update -qq && \
    apt-get install -y -qq \
        curl ca-certificates binutils \
        libgtk-3-0 libsdl2-2.0-0 libgdk-pixbuf2.0-bin libusb-1.0-0 zip dpkg && \
    rm -rf /var/lib/apt/lists/* && \
    GDK_QUERY=$(find /usr/lib -name 'gdk-pixbuf-query-loaders' 2>/dev/null | head -1) && \
    if [ -n "$GDK_QUERY" ] && [ ! -f /usr/bin/gdk-pixbuf-query-loaders ]; then \
        ln -s "$GDK_QUERY" /usr/bin/gdk-pixbuf-query-loaders; \
    fi

# Layer 2: uv + standalone CPython 3.13 + base venv — rebuilds only when layer 1
# changes. No dependency on the system Python.
RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh && \
    uv python install 3.13 && \
    uv venv --python 3.13 /opt/puckbuild

# Layer 3: wxPython alone — prebuilt cp313 wheel from the wxPython extras index
# (matches this image's ubuntu-20.04 base). Cached independently so changes to
# other requirements don't re-fetch it. Keep the version in sync with
# requirements.txt.
RUN uv pip install --python /opt/puckbuild \
        --find-links https://extras.wxpython.org/wxPython4/extras/linux/gtk3/ubuntu-20.04/ \
        "wxPython==4.2.2"

# Layer 4: everything else — fast, rebuilds when requirements.txt changes.
COPY requirements.txt /tmp/requirements.txt
RUN grep -v '^wxPython' /tmp/requirements.txt > /tmp/requirements-no-wx.txt && \
    uv pip install --python /opt/puckbuild -r /tmp/requirements-no-wx.txt && \
    chmod -R a+rX /opt/uv-python /opt/puckbuild

ENV VENV_ROOT=/opt/puckbuild
