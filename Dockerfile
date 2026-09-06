FROM rockylinux:9
RUN dnf install -y epel-release && dnf install -y gcc python3.11 python3.11-devel python3.11-tkinter patchelf git && dnf clean all

COPY nuitka_build.py /opt/nuitka/nuitka_build.py

ENV NUITKA_CACHE_DIR=/tmp/nuitka-cache